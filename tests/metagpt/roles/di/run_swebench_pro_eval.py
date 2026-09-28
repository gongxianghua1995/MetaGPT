#!/usr/bin/env python3
"""SWE-bench Pro docker evaluation (custom harness for ScaleAI/SWE-bench_Pro)."""
import argparse
import ast
import json
import re
import sys
from pathlib import Path

# Evaluation runs in a separate Python environment without MetaGPT dependencies.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'metagpt/roles/di'))
from swe_container import verify_network_isolation

IMAGE_PREFIX = "jefzda/sweap-images"

# pytest verbose emits  node::id PASSED [ 42%]  (name first, and parametrized
# ids may contain spaces).  The stock parse_log_pytest expects status-first
# and splits on whitespace, so we emit our own machine-readable lines:
#   __PRO__<STATUS>\t<full node id>
_PY_LINE = re.compile(
    r"^(.*?)\s+(PASSED|FAILED|XFAIL|XPASS|SKIPPED|ERROR)\s*(?:\[\s*\d+%\])?\s*$"
)
_PY_FILTER = (
    "import sys,re\n"
    "pat=re.compile(r'" + _PY_LINE.pattern + "')\n"
    "for line in sys.stdin:\n"
    "    line=line.rstrip('\\n')\n"
    "    m=pat.match(line)\n"
    "    if m:\n"
    "        sys.stdout.write('__PRO__%s\\t%s\\n'%(m.group(2),m.group(1).rstrip()))\n"
    "    sys.stdout.write(line+'\\n')\n"
)

_JS_RUNNER = r'''
import json, os, subprocess

pairs = json.load(open("/tmp/pro_tests.json"))

def find_pkg(app_abs):
    d = os.path.dirname(os.path.join("/app", app_abs))
    while d.startswith("/app"):
        if os.path.exists(os.path.join(d, "package.json")):
            return d
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    stem = os.path.basename(app_abs).split(".")[0]
    for base in ("packages", "applications"):
        bdir = os.path.join("/app", base)
        if not os.path.isdir(bdir):
            continue
        for dirpath, dirs, files in os.walk(bdir):
            dirs[:] = [x for x in dirs if x != "node_modules"]
            for fn in files:
                if fn.endswith((".test.ts", ".test.tsx")) and fn.split(".")[0] == stem:
                    d = dirpath
                    while d.startswith("/app"):
                        if os.path.exists(os.path.join(d, "package.json")):
                            return d
                        d = os.path.dirname(d)
    return "/app"

seen = set()
for app_abs, pkg_rel in pairs:
    pkgdir = find_pkg(app_abs)
    key = (pkgdir, pkg_rel)
    if key in seen:
        continue
    seen.add(key)
    try:
        r = subprocess.run(
            ["npx", "jest", pkg_rel, "--json"],
            cwd=pkgdir, capture_output=True, text=True, timeout=600,
        )
        data = json.loads(r.stdout)
    except Exception as e:
        print("[FAILED] %s | HARNESS_ERROR %s" % (pkg_rel, e))
        continue
    for t in data.get("testResults", []):
        test_file = os.path.relpath(t.get("name", ""), pkgdir) if t.get("name") else pkg_rel
        for a in t.get("assertionResults", []):
            st = "PASSED" if a.get("status") == "passed" else "FAILED"
            fn = a.get("fullName", "")
            print("[%s] %s | %s" % (st, test_file, fn))
            title = a.get("title")
            if title and title != fn:
                print("[%s] %s | %s" % (st, test_file, title))
'''


def parse_test_list(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    for fn in (json.loads, ast.literal_eval):
        try:
            return fn(raw)
        except Exception:
            pass
    return []


def extract_test_checkout(before_repo_set_cmd):
    lines = []
    for line in (before_repo_set_cmd or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("git reset") or s.startswith("git clean"):
            continue
        if s.startswith("git checkout ") and "--" not in s:
            continue
        lines.append(s)
    return "\n".join(lines)


def _build_js_pairs(instance):
    """Flat list mixes repo-absolute paths (packages/|applications/) and
    package-relative jest patterns; pair by test-file stem (extension may
    differ, e.g. .ts vs .tsx)."""
    raw = parse_test_list(instance.get("selected_test_files_to_run"))
    abs_paths, rel_paths = [], []
    for tf in raw:
        if not isinstance(tf, str):
            continue
        (abs_paths if tf.startswith(("packages/", "applications/")) else rel_paths).append(tf)

    def stem(p):
        return p.rsplit("/", 1)[-1].split(".")[0]

    pairs, used = [], set()
    for ap in abs_paths:
        s = stem(ap)
        idx = next((i for i, rp in enumerate(rel_paths)
                    if i not in used and stem(rp) == s), None)
        if idx is not None:
            used.add(idx)
            pairs.append((ap, rel_paths[idx]))
        else:
            pairs.append((ap, ap.rsplit("/", 1)[-1]))
    for i, rp in enumerate(rel_paths):
        if i not in used:
            pairs.append((rp, rp))
    return pairs


def make_eval_script(instance):
    test_setup = extract_test_checkout(instance.get("before_repo_set_cmd", ""))
    f2p = parse_test_list(instance.get("fail_to_pass"))
    p2p = parse_test_list(instance.get("pass_to_pass"))
    lang = (instance.get("repo_language") or "").lower()
    repo = instance.get("repo", "")

    if lang == "python":
        files, seen = [], set()
        for t in f2p + p2p:
            f = t.split("::")[0] if "::" in t else t
            if f and f not in seen:
                seen.add(f)
                files.append(f)
        env = "PYTHONPATH=/app/lib " if repo == "ansible/ansible" else ""
        qf = "'" + _PY_FILTER.replace("'", "'\"'\"'") + "'"
        test_cmd = f"{env}python -m pytest {' '.join(files)} -v 2>&1 | python3 -c {qf}"
        return (
            f"{test_setup}\n"
            'echo " >>>>> Start Test Output"\n'
            f"{test_cmd}\n"
            'echo " >>>>> End Test Output"\n'
        )

    if lang == "go":
        test_names, pkgs = [], set()
        for t in f2p + p2p:
            test_names.append(t)
        for line in (instance.get("before_repo_set_cmd") or "").splitlines():
            s = line.strip()
            if s.startswith("git checkout ") and "--" in s:
                for part in s.split("--", 1)[1].split():
                    if part.endswith("_test.go"):
                        pkgs.add(str(Path(part).parent))
        for tf in parse_test_list(instance.get("selected_test_files_to_run")):
            if isinstance(tf, str) and tf.endswith("_test.go"):
                pkgs.add(str(Path(tf).parent))
        if not pkgs:
            pkgs.add("./...")
        run_pattern = "|".join(re.escape(n) for n in test_names) if test_names else "."
        pkg_args = " ".join(
            f"./{p}" if not p.startswith("./") else p for p in sorted(pkgs)
        )
        return (
            f"{test_setup}\n"
            'echo " >>>>> Start Test Output"\n'
            f"go test -v -count=1 -run '{run_pattern}' {pkg_args}\n"
            'echo " >>>>> End Test Output"\n'
        )

    if lang in ("js", "javascript", "typescript", "ts"):
        pairs = _build_js_pairs(instance)
        lines = [test_setup, 'echo " >>>>> Start Test Output"']
        lines.append("cat > /tmp/pro_tests.json << 'PRO_JSON'")
        lines.append(json.dumps(pairs))
        lines.append("PRO_JSON")
        lines.append("python3 << 'PRO_PY'")
        lines.append(_JS_RUNNER.strip("\n"))
        lines.append("PRO_PY")
        lines.append('echo " >>>>> End Test Output"')
        return "\n".join(lines) + "\n"

    return f"{test_setup}\n"


def parse_log_pro_pytest(log, test_spec):
    """Read both machine lines and historical raw pytest output.

    Whitespace alignment is presentation, not part of a pytest node ID.
    Parameter IDs may themselves contain spaces, so never split on words.
    """
    status_map = {}
    for line in log.splitlines():
        if line.startswith("__PRO__"):
            status, _, name = line[len("__PRO__"):].partition("\t")
            if name:
                status_map[name.rstrip()] = status
        else:
            match = _PY_LINE.match(line)
            if match and ".py" in match.group(1):
                status_map[match.group(1).rstrip()] = match.group(2)
    return status_map


_GO_LINE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)\s")
_GO_STATUS = {"PASS": "PASSED", "FAIL": "FAILED", "SKIP": "SKIPPED"}


def parse_log_pro_gotest(log, test_spec):
    status_map = {}
    for line in log.split("\n"):
        m = _GO_LINE.match(line)
        if m:
            status, name = m.groups()
            status_map[name] = _GO_STATUS[status]
    return status_map


_JS_LINE = re.compile(r"^\[(PASSED|FAILED)\] (.*?) \| (.*)$")


def parse_log_pro_jest(log, test_spec):
    status_map = {}
    for line in log.split("\n"):
        m = _JS_LINE.match(line)
        if m:
            status, test_file, name = m.groups()
            status_map[name] = status
            status_map[f"{test_file} | {name}"] = status
            # Jest reports nested names as ``suite test`` while several Pro
            # FAIL_TO_PASS entries omit only the outer suite. Preserve that
            # intermediate form as well (e.g. ``useCanCheckItem get-started``
            # -> ``get-started``), rather than treating a passing test as
            # absent from the status map.
            _suite, separator, nested = name.partition(" ")
            if separator:
                status_map[nested] = status
                status_map[f"{test_file} | {nested}"] = status
    return status_map


# repo -> (log parser, language)
_PRO_REPOS = {
    "ansible/ansible": (parse_log_pro_pytest, "python"),
    "internetarchive/openlibrary": (parse_log_pro_pytest, "python"),
    "flipt-io/flipt": (parse_log_pro_gotest, "go"),
    "protonmail/webclients": (parse_log_pro_jest, "js"),
}


def register_pro_parser():
    """Register custom log parsers for SWE-bench Pro repos, which are not
    present in stock swebench's PARSER_REGISTRY."""
    from swebench.harness.grading import PARSER_REGISTRY

    for repo, (parser, _lang) in _PRO_REPOS.items():
        PARSER_REGISTRY[repo] = parser


def make_test_spec(instance):
    from swebench.types import TestSpec

    repo = instance.get("repo", "")
    ts = TestSpec(
        instance_id=instance["instance_id"],
        image=f"{IMAGE_PREFIX}:{instance['dockerhub_tag']}",
        repo=repo,
        version=instance.get("base_commit", ""),
        eval_script_list=[make_eval_script(instance)],
        FAIL_TO_PASS=parse_test_list(instance.get("fail_to_pass")),
        PASS_TO_PASS=parse_test_list(instance.get("pass_to_pass")),
        log_parser=repo,
        eval_type="pass_and_fail",
    )
    return ts


def _is_lockfile_only_patch(model_patch: str) -> bool:
    """Only an actually empty patch is empty; filenames do not imply relevance."""
    return not (model_patch or "").strip()


def _is_lockfile_path(path: str) -> bool:
    return False


def _strip_lockfile_hunks(model_patch: str) -> str:
    """Preserve the exact submitted patch, including dependency and build files."""
    return model_patch


def build_run_report(instances, predictions, model_slug, run_id, report_dir):
    """Aggregate per-instance report.json files into a run-level report,
    without calling stock swebench make_run_report() (which internally
    rebuilds test_specs via the unpatched module-level make_test_spec and
    raises KeyError for SWE-bench Pro repos)."""
    from swebench.harness.constants import LOG_REPORT, RUN_EVALUATION_LOG_DIR

    completed_ids, resolved_ids, unresolved_ids = set(), set(), set()
    error_ids, empty_patch_ids, incomplete_ids = set(), set(), set()

    for inst in instances:
        iid = inst["instance_id"]
        pred = predictions.get(iid)
        model_patch = pred.get("model_patch", "") if pred else ""
        # Treat lockfile-only patches as empty (no real source-code fix)
        if not model_patch or _is_lockfile_only_patch(model_patch):
            empty_patch_ids.add(iid)
            continue
        report_path = RUN_EVALUATION_LOG_DIR / run_id / model_slug / iid / LOG_REPORT
        if not report_path.exists():
            incomplete_ids.add(iid)
            continue
        try:
            data = json.loads(report_path.read_text())
            resolved = bool(data.get(iid, {}).get("resolved", False))
        except Exception as e:
            print(f"  [warn] failed to parse report for {iid}: {e}")
            error_ids.add(iid)
            continue
        completed_ids.add(iid)
        if resolved:
            resolved_ids.add(iid)
        else:
            unresolved_ids.add(iid)

    total = len(instances)
    report = {
        "total_instances": total,
        "submitted_instances": len(predictions),
        "completed_instances": len(completed_ids),
        "resolved_instances": len(resolved_ids),
        "unresolved_instances": len(unresolved_ids),
        "empty_patch_instances": len(empty_patch_ids),
        "error_instances": len(error_ids),
        "incomplete_instances": len(incomplete_ids),
        "resolved_ids": sorted(resolved_ids),
        "unresolved_ids": sorted(unresolved_ids),
        "empty_patch_ids": sorted(empty_patch_ids),
        "error_ids": sorted(error_ids),
        "incomplete_ids": sorted(incomplete_ids),
        "schema_version": 2,
    }
    report_file = report_dir / f"{model_slug}.{run_id}.json"
    report_file.write_text(json.dumps(report, indent=4))
    print(
        f"resolved {len(resolved_ids)}/{total} "
        f"({100 * len(resolved_ids) / total if total else 0:.2f}%) - report: {report_file}"
    )
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preds", required=True)
    ap.add_argument("--run-id", default="metagpt_pro")
    ap.add_argument("--max-workers", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--report-dir", default=None)
    ap.add_argument("--instance-ids", default=None)
    ap.add_argument("--skip-existing", action="store_true")
    args = ap.parse_args()

    preds_path = Path(args.preds)
    save_folder = preds_path.parent
    report_dir = Path(args.report_dir) if args.report_dir else save_folder / "swebench_reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    preds = []
    with open(preds_path) as f:
        for line in f:
            line = line.strip()
            if line:
                preds.append(json.loads(line))
    print(f"loaded {len(preds)} predictions")

    if args.instance_ids:
        keep = {s.strip() for s in args.instance_ids.split(",") if s.strip()}
        preds = [p for p in preds if p["instance_id"] in keep]
        print(f"filtered to {len(preds)} instances")

    register_pro_parser()

    # Dataset only covers 40 instances (split='train'). Use predictions directly
    # as instances since they contain all needed fields (patch, test_patch, etc.)
    # from the original SWE-bench run. Only verify dataset coverage for non-Pro repos.
    from swebench.harness.utils import load_swebench_dataset

    instances = []
    for p in preds:
        inst = {k: v for k, v in p.items() if k in (
            "instance_id", "repo", "base_commit", "patch", "test_patch",
            "problem_statement", "requirements", "fail_to_pass", "pass_to_pass",
            "issue_specificity", "issue_categories", "before_repo_set_cmd",
            "selected_test_files_to_run", "dockerhub_tag", "model_patch",
            "model_name_or_path",
        )}
        # Map repo_language if present, else infer from repo
        if "repo_language" in p:
            inst["repo_language"] = p["repo_language"]
        else:
            repo = inst.get("repo", "")
            if "ansible" in repo or "openlibrary" in repo:
                inst["repo_language"] = "python"
            elif "flipt" in repo:
                inst["repo_language"] = "go"
            elif "webclients" in repo:
                inst["repo_language"] = "javascript"
        instances.append(inst)
    print(f"evaluating {len(instances)} instances (built from predictions)")

    import docker as _docker_mod
    from swebench.harness.run_evaluation import (
        RUN_EVALUATION_LOG_DIR,
        run_instance as _orig_run_instance,
        run_threadpool,
    )
    from swebench.harness.constants import LOG_REPORT
    import swebench.harness.run_evaluation as _re_mod

    # SWE-bench Pro images keep the repo at /app, not /testbed.
    _re_mod.CONTAINER_WORKDIR = "/app"

    # Pro images set ENTRYPOINT=[/bin/bash] and WORKDIR=/app, so the stock
    # create_container (command="tail -f /dev/null") makes bash treat the
    # string as a script filename and exit instantly. Create the container
    # with `-c` so it stays alive. Submitted code also runs offline in eval.
    def _patched_create_container(test_spec, client, run_id, logger):
        container_name = f"sweb.eval.{test_spec.instance_id.lower()}.{run_id}"
        try:
            client.containers.get(container_name).remove(force=True)
        except _docker_mod.errors.NotFound:
            pass
        except Exception:
            pass
        # Pro images set ENTRYPOINT=[/bin/bash]; a bare command string would be
        # treated as a script filename and exit instantly. Docker SDK's
        # ``command`` sets the Cmd field -- the entrypoint is prepended
        # automatically, so we must NOT duplicate it. For entrypoint-less
        # images, use the split list form.
        img_ep = client.images.get(test_spec.image).attrs.get("Config", {}).get("Entrypoint") or []
        if img_ep and img_ep[-1].endswith(("bash", "sh")):
            command = ["-c", "tail -f /dev/null"]
        else:
            command = ["tail", "-f", "/dev/null"]
        container = client.containers.create(
            image=test_spec.image,
            name=container_name,
            user="root",
            detach=True,
            network_mode="none",
            command=command,
            cap_add=["SYS_ADMIN"],
        )
        container.start()
        try:
            container.reload()
            isolation = verify_network_isolation(container.attrs)
            logger.info(f"Evaluation container isolation verified: {isolation}")
        except Exception:
            container.remove(force=True)
            raise
        return container

    _re_mod.create_container = _patched_create_container

    model_name = instances[0].get("model_name_or_path", "metagpt")
    model_slug = model_name.replace("/", "__")
    run_id = args.run_id

    if args.skip_existing:
        kept = []
        for inst in instances:
            rp = RUN_EVALUATION_LOG_DIR / run_id / model_slug / inst["instance_id"] / LOG_REPORT
            if not rp.exists():
                kept.append(inst)
        print(f"skip_existing: {len(instances) - len(kept)} done, {len(kept)} todo")
        instances = kept

    if not instances:
        print("nothing to do")
        return

    test_specs = []
    for inst in instances:
        try:
            test_specs.append(make_test_spec(inst))
        except Exception as e:
            print(f"  [warn] failed to build test spec for {inst['instance_id']}: {e}")

    predictions = {p["instance_id"]: p for p in preds}
    client = _docker_mod.from_env()

    # swebench 5.x run_instance signature:
    #   (test_spec, pred, client, run_id, timeout, rewrite_reports, skip_patch, task_repo)
    def _run_instance_wrapper(test_spec, pred, client, run_id, timeout,
                               rewrite_reports=False, skip_patch=False, task_repo=None):
        # Strip lockfile hunks from mixed patches so ``git apply`` doesn't fail
        # on lockfile context drift.  Pure lockfile patches have already been
        # filtered upstream by ``_is_lockfile_only_patch``.
        patch = pred.get("model_patch", "") if pred else ""
        if patch:
            stripped = _strip_lockfile_hunks(patch)
            if stripped and stripped != patch:
                pred = dict(pred)
                pred["model_patch"] = stripped
        return _orig_run_instance(
            test_spec, pred, client, run_id, timeout,
            rewrite_reports, skip_patch, task_repo,
        )

    payloads = []
    skipped_empty = 0
    stripped_count = 0
    for ts in test_specs:
        pred = predictions.get(ts.instance_id) or {
            "instance_id": ts.instance_id, "model_patch": "", "model_name_or_path": model_name
        }
        # Empty / lockfile-only patches are classified as EMPTY by the report
        # builder regardless of test outcome -- don't waste containers on them.
        patch = pred.get("model_patch", "")
        if not patch or _is_lockfile_only_patch(patch):
            skipped_empty += 1
            continue
        stripped = _strip_lockfile_hunks(patch)
        if stripped and stripped != patch:
            stripped_count += 1
        payloads.append((ts, pred, client, run_id, args.timeout, False, False, None))

    print(
        f"skipping {skipped_empty} empty/lockfile-only patches; "
        f"{stripped_count} mixed patches will have lockfile hunks stripped at apply time; "
        f"running {len(payloads)} instances with {args.max_workers} workers ..."
    )
    run_threadpool(_run_instance_wrapper, payloads, args.max_workers)
    print("all instances done")

    report = build_run_report(instances, predictions, model_slug, run_id, report_dir)
    out = save_folder / "eval_results.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    print(f"results written to {out}")


if __name__ == "__main__":
    main()
