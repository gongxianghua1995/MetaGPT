"""SWE-bench Verified generation runner for MetaGPT.

Produces per-instance patches (all_preds.jsonl) by running a SWEBenchEngineer
(single-agent) or a SWEBenchTeamLeader + SWEBenchEngineer + Reviewer team (MAS)
inside per-instance docker containers. Core MetaGPT classes are NOT modified:
all SWE-bench behavior is injected via subclasses (see metagpt/roles/di/swe_engineer.py,
swe_team_leader.py) and docker tool classes (metagpt/tools/libs/docker_terminal.py,
docker_editor.py).

Eval scoring is done separately by tests/metagpt/roles/di/run_swebench_eval.py.

Usage (single-agent smoke):
    /home/xhgong/miniconda/envs/metagpt/bin/python \\
        tests/metagpt/roles/di/run_swe_agent_for_benchmark.py \\
        --instance_ids django__django-11138 --use_docker

MAS smoke:
    ... --instance_ids django__django-11138 --use_docker --mas --n_round 30
"""
import argparse
from dataclasses import asdict
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from metagpt.actions import UserRequirement
from metagpt.config2 import config
from metagpt.const import DEFAULT_WORKSPACE_ROOT, METAGPT_ROOT
from metagpt.logs import logger
from metagpt.roles.di.swe_engineer import SWEBenchEngineer
from metagpt.team import Team
from metagpt.roles.di.swe_protocol import SWECaseState, run_swe_team
from metagpt.roles.di.swe_budget import DEFAULT_CASE_MINUTES, add_team_budget_arguments, team_budget_from_args
from metagpt.tools.libs.docker_bash import DockerBash
from metagpt.tools.libs.docker_terminal import DockerTerminal
from metagpt.tools.swe_agent_commands.swe_agent_utils import load_hf_dataset
from metagpt.utils.swe_requirements import requirement_text, interface_paths

# Specify by yourself
TEST_REPO_DIR = METAGPT_ROOT / "data" / "test_repo"
DATA_DIR = METAGPT_ROOT / "data/hugging_face"

# Noise paths excluded when collecting model_patch. Background yarn/webpack
# processes inside Pro containers touch lockfiles and build artifacts
# (31/39 webclients patches in pro_full2 were yarn.lock-only), and the eval
# harness strips these hunks anyway (_strip_lockfile_hunks) — excluding at
# collection keeps the patch honest from the start.
COLLECT_DIFF_CMD = "git diff --cached"

INSTANCE_TEMPLATE = """## Task
Fix the issue in the checked-out repository at `{repo_path}` (base {base_commit}).

## ISSUE
{issue}

## HINTS
{hints_text}
{requirements_section}{interface_section}
## Workflow
Read focused source context, form a concrete hypothesis, edit the implementation,
and run focused existing tests or a small reproduction. Use failures to revise.
Inspect the final diff and submit the patch. Do not weaken tests to get a pass.
Use the tools actually provided by your role. Commands run inside the repository.
Do not change the base commit, push commits, or access infrastructure outside the repository.
"""


def check_instance_status(instance, swe_result_dir):
    output_file = swe_result_dir / "all_preds.jsonl"
    res = True
    if not output_file.exists():
        return res
    with open(output_file, "r") as fp:
        for line in fp:
            existing_instance = json.loads(line.strip())
            if existing_instance["instance_id"] == instance["instance_id"]:
                return False
    return res


async def terminal_run_command(cmd, terminal):
    cmd_output = await terminal.run_command(cmd)
    logger.info(f"command:{cmd} output:\n {cmd_output}")
    return cmd_output


async def refresh_repo(instance, test_repo_dir, reclone_existing_repo=False):
    """Clone/reset the target repo to the host (non-docker mode only)."""
    terminal = Terminal()
    try:
        repo_path = Path(test_repo_dir) / (
            instance["repo"].replace("-", "_").replace("/", "__") + "_" + instance["version"]
        )
        repo_identifier = instance["repo"]
        base_commit = instance["base_commit"]
        if os.path.exists(repo_path) and reclone_existing_repo is True:
            logger.info(f"remove exist repo path:{repo_path.absolute()}")
            shutil.rmtree(repo_path)
        if os.path.exists(repo_path):
            logger.info(f"reset exist repo path:{repo_path.absolute()}")
            for cmd in [
                f"cd {repo_path.absolute()}",
                "git reset --hard && git clean -n -d && git clean -f -d",
                "BRANCH=$(git remote show origin | awk '/HEAD branch/ {print $NF}')",
                'git checkout "$BRANCH"',
                "git branch",
                "pwd",
            ]:
                await terminal_run_command(cmd, terminal)
        else:
            logger.info(f"clone repo to path:{repo_path}")
            for cmd in [
                f"git clone 'https://github.com/{repo_identifier}.git' {repo_path.absolute()}",
                f"cd {repo_path.absolute()}" + f" && git checkout -f {base_commit}" if base_commit else "",
                "git branch",
                "pwd",
            ]:
                await terminal_run_command(cmd, terminal)
    except Exception as e:
        logger.warning(e)
    finally:
        await terminal.close()
    return repo_path


async def get_git_diff(instance, test_repo_dir):
    """Get patch from host repo (non-docker mode only)."""
    git_diff = ""
    terminal = Terminal()
    try:
        repo_path = Path(test_repo_dir) / (
            instance["repo"].replace("-", "_").replace("/", "__") + "_" + instance["version"]
        )
        for cmd in [f"cd {repo_path.absolute()} ", "echo '.backup.*' >> .gitignore", "git add -A"]:
            await terminal_run_command(cmd, terminal)
        git_diff = await terminal_run_command(COLLECT_DIFF_CMD, terminal)
    except Exception as e:
        logger.error(f"Error during submission: {e}")
    finally:
        await terminal.close()
    return git_diff


def resolve_docker_image(instance) -> str:
    """Per-instance SWE-bench eval image. Prefer the dataset's `image` field (authoritative);
    then try `dockerhub_tag` (SWE-bench Pro); fall back to the swebench naming convention:
    instance_id `django__django-11138` -> `swebench/sweb.eval.x86_64.django_1776_django-11138:latest`.
    """
    if instance.get("image"):
        return instance["image"]
    if instance.get("dockerhub_tag"):
        # Docker tags are capped at 128 chars; the Pro dockerhub_tag can be longer
        # and Docker truncates it on pull, so reference the truncated tag.
        return f"jefzda/sweap-images:{instance['dockerhub_tag'][:128]}"
    iid_docker = instance["instance_id"].replace("__", "_1776_").lower()
    return f"swebench/sweb.eval.x86_64.{iid_docker}:latest"


async def ensure_container(image: str, cname: str, workdir: str = "/testbed") -> str:
    """Start (or reuse) a detached container with cwd=workdir. Returns cname."""
    # Reuse if it already exists (running or stopped).
    check = await asyncio.create_subprocess_exec(
        "docker", "inspect", "-f", "{{.State.Running}}", cname,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await check.communicate()
    if b"true" in out:
        logger.info(f"Container {cname} already running, reusing.")
        return cname
    # Exists but stopped -> start it.
    if await _container_exists(cname):
        logger.info(f"Container {cname} exists but stopped, starting.")
        (await asyncio.create_subprocess_exec("docker", "start", cname)).wait()
        return cname
    logger.info(f"Creating container {cname} from image {image} (network=none)")
    # --network none: isolate the container (no outbound internet) so the agent
    # cannot search for answers online. LLM API calls run on the host process,
    # not inside the container, so orchestration is unaffected.
    # --entrypoint /bin/sleep: Pro images set ENTRYPOINT=/bin/bash, under which
    # `sleep 2h` is interpreted as `bash sleep 2h` and exits immediately; force
    # the sleep binary directly so the container stays alive.
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "-d", "--network", "none",
        "--entrypoint", "/bin/sleep",
        "--name", cname, "-w", workdir, image, "2h",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"docker run failed for {image} (container {cname}):\n"
            f"stdout={out.decode(errors='ignore')}\nstderr={err.decode(errors='ignore')}"
        )
    # Wait until the container reports running.
    for _ in range(30):
        await asyncio.sleep(1)
        c = await asyncio.create_subprocess_exec(
            "docker", "inspect", "-f", "{{.State.Running}}", cname,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        o, _ = await c.communicate()
        if b"true" in o:
            # Pro images mount the repo at /app, but the agent prompts and
            # command guards hardcode /testbed. Add a /testbed -> /app symlink
            # when /testbed is absent so all hardcoded paths resolve.
            if workdir != "/testbed":
                link = await asyncio.create_subprocess_exec(
                    "docker", "exec", cname, "ln", "-s", workdir, "/testbed",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await link.wait()
            return cname
    raise RuntimeError(f"Container {cname} did not reach running state.")


async def _container_exists(cname: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{cname}$",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return cname in out.decode().split()


async def cleanup_container(cname: str):
    """Stop and remove the container (always called in finally)."""
    if not cname:
        return
    for args in (["docker", "stop", cname], ["docker", "rm", "-f", cname]):
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
        except Exception as e:
            logger.warning(f"cleanup {' '.join(args)} failed: {e}")


async def get_git_diff_docker(docker_term) -> str:
    """Collect the patch from inside the container (staged + untracked, excluding backups)."""
    return await docker_term.collect_patch()


async def save_predictions(engineer, instance, test_repo_dir, swe_result_dir):
    output_file = swe_result_dir / "all_preds.jsonl"
    # engineer may be None if instantiation failed; fall back to the global config.
    model = engineer.config.llm.model if engineer is not None else config.llm.model
    instance["model_name_or_path"] = model
    logger.info(f"'model_patch':\n{instance.get('model_patch', '')}")
    logger.info(f"Preparing to save predictions to {output_file}")
    with open(output_file, "a+") as fp:
        print(json.dumps(instance), file=fp, flush=True)
    logger.info(f"Saved prediction of {instance['instance_id']} to {output_file}")


def observe_model_usage(role, state):
    """Capture usage at the provider boundary, including TL and Reviewer calls."""
    llm = role.llm
    original = llm._update_costs
    def update(usage, *args, **kwargs):
        value = usage.model_dump() if hasattr(usage, "model_dump") else usage
        state.record("model_usage", role=role.name, backend="metagpt", usage=value)
        return original(usage, *args, **kwargs)
    llm._update_costs = update


async def run(instance, swe_result_dir, args):
    if not check_instance_status(instance, swe_result_dir):
        logger.info(f"Instance {instance['instance_id']} already exists, skipping execution.")
        return

    logger.info(f"**** Preparing to run {instance['instance_id']} ****")

    # SWE-bench Pro images (dockerhub_tag present) mount the repo at /app;
    # the classic Verified images use /testbed.
    workdir = "/app" if (args.use_docker and instance.get("dockerhub_tag")) else "/testbed"
    repo_path = workdir if args.use_docker else str(await refresh_repo(instance, args.test_repo_dir, args.reclone_existing_repo))

    # SWE-bench Pro carries `requirements` (the exact behaviors the hidden
    # tests assert) and `interface` (exact function/method signatures and
    # file paths). The pro_full2 run dropped both — patches were then
    # "logically plausible but assertion-mismatched" and badly under-scoped
    # (median +1 line vs multi-file gold patches). Verified instances don't
    # have these fields; the sections collapse to "" there.
    _requirements = requirement_text(instance.get("requirements"))
    _interface = requirement_text(instance.get("interface"))
    requirements_section = (
        "\n## REQUIREMENTS (the hidden tests assert EXACTLY these behaviors — "
        "your patch MUST satisfy every item)\n" + _requirements + "\n"
    ) if _requirements else ""
    interface_section = (
        "\n## INTERFACE (implement these EXACT names/signatures in the listed "
        "files — tests import and call them directly)\n" + _interface + "\n"
    ) if _interface else ""

    user_requirement_and_issue = INSTANCE_TEMPLATE.format(
        issue=instance["problem_statement"],
        hints_text=instance.get("hints_text", ""),
        requirements_section=requirements_section,
        interface_section=interface_section,
        repo_path=repo_path,
        version=instance.get("version", ""),
        base_commit=instance["base_commit"],
    )

    logger.info(f"**** Starting to run {instance['instance_id']} ****")
    logger.info("User Requirement:\n" + user_requirement_and_issue)

    cname = f"sweb-{instance['instance_id']}-{uuid.uuid4().hex[:10]}"
    backend = getattr(args, "engineer_backend", "native")
    engineer_class = SWEBenchEngineer
    backend_kwargs = {}
    trace_dir = swe_result_dir / "traces" / instance["instance_id"]
    if backend == "mini":
        if not args.use_docker:
            raise ValueError("mini backend requires --use_docker")
        from metagpt.roles.di.swe_mini_engineer import SWEMiniEngineer
        engineer_class = SWEMiniEngineer
        backend_kwargs = dict(mini_python=getattr(args, "mini_python", sys.executable),
                              mini_output_dir=str(trace_dir / "mini"))
    budget_seconds = args.max_wait_time_per_case * 60
    team_budget = team_budget_from_args(args).validate(budget_seconds, mas=args.mas)
    case_state = SWECaseState(trace_path=str(trace_dir / "events.jsonl"), **asdict(team_budget))
    common_kwargs = dict(max_native_steps=getattr(args, "max_native_steps", 80),
                         review_reserve_seconds=team_budget.review_seconds, **backend_kwargs)
    docker_bash = None
    engineer = None
    generation_error = None
    try:
        if args.use_docker:
            image = resolve_docker_image(instance)
            await ensure_container(image, cname, workdir)
            from metagpt.roles.di.swe_container import verify_network_isolation
            inspect = await asyncio.create_subprocess_exec(
                'docker', 'inspect', cname, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            inspected, _ = await inspect.communicate()
            if inspect.returncode:
                raise RuntimeError('Unable to verify generation container network isolation')
            case_state.record('network_isolated', container=cname, **verify_network_isolation(json.loads(inspected)[0]))
            from metagpt.roles.di.swe_repository import isolate_history_command
            from metagpt.roles.di.swe_shell import docker_shell_argv
            history_proc = await asyncio.create_subprocess_exec(
                *docker_shell_argv(cname, workdir, isolate_history_command(
                    instance['base_commit'], instance.get('image_snapshot_commit')), 120),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            history_out, _ = await history_proc.communicate()
            if history_proc.returncode:
                raise RuntimeError('Failed to isolate benchmark Git history: '+history_out.decode(errors='replace')[-1500:])
            case_state.record('history_isolated', base_commit=instance['base_commit'], output=history_out.decode(errors='replace'))
            docker_bash = DockerBash(container_name=cname, cwd=workdir)
            # Upstream mini submits via patch.txt. Exclude the transport artifact
            # for every arm, including final-tree salvage and native submission.
            await docker_bash.run_with_status("printf '\n/patch.txt\n' >> .git/info/exclude")
            docker_bash.initial_patch = await docker_bash.collect_patch()
            case_state.record("image_baseline", initial_patch=docker_bash.initial_patch)
            case_state.started_at = time.monotonic()
            case_state.deadline = case_state.started_at + budget_seconds
            case_state.first_edit_deadline = case_state.started_at + budget_seconds * team_budget.first_edit_fraction
            case_state.record("case_started", backend=backend, mas=args.mas, seconds=budget_seconds,
                              image=image, container=cname, task=user_requirement_and_issue,
                              model=config.llm.model, max_tokens=config.llm.max_token,
                              temperature=config.llm.temperature, team_budget=asdict(team_budget))
            if args.mas:
                from metagpt.roles.di.reviewer import Reviewer
                from metagpt.roles.di.swe_team_leader import SWEBenchTeamLeader
                from metagpt.tools.libs.docker_edit import DockerEdit

                docker_term = DockerTerminal(container_name=cname, cwd=workdir)
                docker_term.trace_state = case_state
                docker_edit = DockerEdit(container_name=cname, cwd=workdir)
                engineer = engineer_class(
                    **common_kwargs,
                    case_state=case_state, native_deadline=time.monotonic() + args.max_wait_time_per_case * 60,
                    run_eval=True, terminal=docker_bash, docker_edit=docker_edit, mas_mode=True,
                    task_requirements=user_requirement_and_issue,
                    interface_files=interface_paths(_interface),
                )
                team = Team(use_mgx=True)
                members = [
                    SWEBenchTeamLeader(terminal=docker_term, case_state=case_state),
                    engineer,
                    Reviewer(terminal=docker_term, task_context=user_requirement_and_issue, case_state=case_state),
                ]
                team.hire(members)
                for member in members:
                    observe_model_usage(member, case_state)
                await asyncio.wait_for(
                    run_swe_team(team, case_state, user_requirement_and_issue, args.n_round),
                    timeout=args.max_wait_time_per_case * 60,
                )
            else:
                from metagpt.tools.libs.docker_edit import DockerEdit

                engineer = engineer_class(
                    **common_kwargs,
                    run_eval=True, terminal=docker_bash,
                    task_requirements=user_requirement_and_issue, case_state=case_state,
                    native_deadline=time.monotonic() + args.max_wait_time_per_case * 60,
                    docker_edit=DockerEdit(container_name=cname, cwd=workdir),
                )
                observe_model_usage(engineer, case_state)
                engineer._watch([UserRequirement])
                await asyncio.wait_for(engineer.run(user_requirement_and_issue), timeout=args.max_wait_time_per_case * 60)
        else:
            from metagpt.roles.di.swe_agent import SWEAgent

            engineer = SWEAgent(run_eval=True)
            engineer._watch([UserRequirement])
            await asyncio.wait_for(engineer.run(user_requirement_and_issue), timeout=args.max_wait_time_per_case * 60)
    except Exception as e:
        generation_error = {"type": type(e).__name__, "message": str(e)[-1200:]}
        state = getattr(engineer, "case_state", None)
        if state is not None:
            state.finish("wall time budget exhausted" if isinstance(e, asyncio.TimeoutError) else type(e).__name__)
        logger.warning(f"**** exception lead to end: {instance['instance_id']} ****\n\nerror:{e}")
    finally:
        # Collect the patch WHILE the container is still alive (before cleanup),
        # so we salvage partial edits even if the agent crashed.
        try:
            patch = getattr(engineer, "output_diff", "") if engineer else ""
            # Always collect the final tree: output_diff may predate review revisions.
            if args.use_docker and docker_bash is not None:
                patch = await get_git_diff_docker(docker_bash)
            elif not patch and not args.use_docker:
                patch = await get_git_diff(instance, args.test_repo_dir)
            instance["model_patch"] = patch
        except Exception as diff_err:
            logger.warning(f"diff collection failed for {instance['instance_id']}: {diff_err}")
            # Preserve a confirmed explicit submission if final-tree collection
            # fails, and expose the failure instead of silently grading it empty.
            instance["model_patch"] = getattr(engineer, "output_diff", "") if engineer else ""
            instance["patch_collection_error"] = type(diff_err).__name__
            if engineer is not None and engineer.case_state:
                engineer.case_state.record("patch_collection_failed", error=type(diff_err).__name__,
                                           fallback_submission_chars=len(instance["model_patch"]))
        if args.use_docker:
            await cleanup_container(cname)

    state = getattr(engineer, "case_state", None)
    if generation_error:
        # Preserve any salvageable tree, but distinguish framework failure
        # from a model-generated empty patch for the batch controller.
        instance["generation_error"] = generation_error
    if state is not None:
        instance["swe_run_state"] = state.model_dump(exclude={"events"})
        instance["swe_run_state"]["trace_file"] = str(trace_dir / "events.jsonl")
        instance["swe_run_state"]["backend"] = backend
        instance["swe_run_state"]["elapsed_seconds"] = round(time.monotonic() - state.started_at, 3)
        instance["swe_run_state"]["event_counts"] = {name: sum(e["event"] == name for e in state.events)
                                                      for name in {e["event"] for e in state.events}}
        instance["swe_run_state"]["model_action_rounds"] = engineer._act_count
        instance["swe_run_state"]["repository_changes"] = engineer._edits_count
    await save_predictions(engineer, instance, args.test_repo_dir, swe_result_dir)
    logger.info(f"**** Finished running {instance['instance_id']} ****")


async def async_main(args):
    dataset_path = args.dataset
    if getattr(args, "instances_file", ""):
        dataset = [json.loads(line) for line in Path(args.instances_file).read_text().splitlines() if line.strip()]
    else:
        dataset = load_hf_dataset(dataset_name_or_path=dataset_path, cache_dir=DATA_DIR, split="test")

    # Optional instance_id filter (comma-separated).
    wanted = None
    if args.instance_ids:
        wanted = {x.strip() for x in args.instance_ids.split(",") if x.strip()}
        dataset = [inst for inst in dataset if inst["instance_id"] in wanted]
        logger.info(f"Filtered to {len(dataset)} instance(s) by --instance_ids.")

    swe_result_dir = Path(args.save_folder)
    if swe_result_dir.exists():
        logger.info(f"{swe_result_dir} exists; resuming test from last checkpoint.")
    swe_result_dir.mkdir(parents=True, exist_ok=True)
    (swe_result_dir / "logs").mkdir(parents=True, exist_ok=True)

    for index, instance in enumerate(dataset):
        # switch to a new logger file
        logger.remove()
        logger.add(sys.stderr, level="INFO")
        logger.add(swe_result_dir / "logs" / f"{index + 1}_{instance['instance_id']}.log", level="DEBUG")
        await run(instance, swe_result_dir, args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MetaGPT SWE-bench Verified generation runner")
    default_save_folder = (
        DEFAULT_WORKSPACE_ROOT / f"metagpt_verified_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S')}"
    )
    parser.add_argument(
        "-rw", "--test_repo_dir", default=str(TEST_REPO_DIR.absolute()),
        help="Directory to save temporary repositories (non-docker mode only)", type=str,
    )
    parser.add_argument("-s", "--save_folder", default=str(default_save_folder.absolute()), help="Folder to save results and logs", type=str)
    parser.add_argument(
        "-mwtc", "--max_wait_time_per_case", default=DEFAULT_CASE_MINUTES,
        help="Maximum wait time allowed per instance (in minutes)", type=int,
    )
    parser.add_argument(
        "-o", "--reclone_existing_repo", action="store_true",
        help="If set, the existing repository will be removed and recloned (non-docker mode only).",
    )
    parser.add_argument(
        "--dataset", default="SWE-bench/SWE-bench_Verified",
        help="HuggingFace dataset name (default: SWE-bench/SWE-bench_Verified)",
    )
    parser.add_argument(
        "--use_docker", action=argparse.BooleanOptionalAction, default=True,
        help="Run the agent inside a per-instance SWE-bench docker container (default: True).",
    )
    parser.add_argument("--model-max-tokens", type=int, default=None)
    add_team_budget_arguments(parser)
    parser.add_argument("--engineer-backend", choices=["native", "mini"], default="native")
    parser.add_argument("--mini-python", default=sys.executable, help="Python containing mini-swe-agent 2.4.6")
    parser.add_argument("--max-native-steps", type=int, default=80, help="Coding model-call budget, either backend")
    parser.add_argument("--instances-file", default="", help="Local JSONL task metadata; bypass dataset fetching")
    parser.add_argument("--mas", action="store_true", help="Use the MAS topology (TeamLeader + Engineer + Reviewer).")
    parser.add_argument("--n_round", default=30, help="Max team rounds for MAS mode", type=int)
    parser.add_argument(
        "--instance_ids", default="",
        help="Comma-separated instance_ids to run (e.g. django__django-11138). Empty = run all in dataset.",
    )
    args = parser.parse_args()
    try:
        team_budget_from_args(args).validate(args.max_wait_time_per_case * 60, mas=args.mas)
        if args.model_max_tokens is not None and args.model_max_tokens <= 0:
            raise ValueError('--model-max-tokens must be positive')
    except ValueError as exc:
        parser.error(str(exc))
    if args.model_max_tokens is not None:
        config.llm.max_token = args.model_max_tokens
    asyncio.run(async_main(args))
