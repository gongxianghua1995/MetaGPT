"""SWE-specific actions and explicit submission lifecycle.

Action and observation are replayed together as ordinary chat content, which
is compatible with MetaGPT's role/content-only message serializer.
"""
import asyncio
from pathlib import Path

import hashlib
import json
import re
import shlex
import time

from pydantic import BaseModel, Field

from metagpt.roles.di.swe_checks import check_evidence, clip
from metagpt.roles.di.swe_budget import DEFAULT_TEAM_BUDGET
from metagpt.roles.di.swe_rate_limit import wait_for_model_slot
from metagpt.actions.di.run_command import RunCommand
from metagpt.actions.di.swe_review import CodeReviewRequest
from metagpt.logs import logger
from metagpt.schema import AIMessage, Message, UserMessage


class SWECaseState(BaseModel):
    phase: str = "editing"
    submission_id: int = 0
    patch_digest: str = ""
    review_cycles: int = 0
    verification_cycles: int = 0
    max_revisions: int = 2
    terminal_reason: str = ""

    deadline: float = Field(default=0, exclude=True)
    first_edit_deadline: float = Field(default=0, exclude=True)
    planning_seconds: float = DEFAULT_TEAM_BUDGET.planning_seconds
    review_seconds: float = DEFAULT_TEAM_BUDGET.review_seconds
    leader_max_tokens: int = DEFAULT_TEAM_BUDGET.leader_max_tokens
    reviewer_max_tokens: int = DEFAULT_TEAM_BUDGET.reviewer_max_tokens
    first_edit_fraction: float = DEFAULT_TEAM_BUDGET.first_edit_fraction
    repair_seconds: float = DEFAULT_TEAM_BUDGET.repair_seconds
    submitted_patch: str = Field(default="", exclude=True)
    submission_signature: str = Field(default="", exclude=True)
    review_packet: dict = Field(default_factory=dict, exclude=True)
    handoffs: dict = Field(default_factory=dict)
    review_feedback: str = ""
    events: list = Field(default_factory=list)
    started_at: float = Field(default_factory=time.monotonic, exclude=True)
    trace_path: str = Field(default="", exclude=True)

    def record(self, event, **details):
        item = {"event": event, "elapsed_seconds": round(time.monotonic() - self.started_at, 3),
                "phase": self.phase, "submission_id": self.submission_id, **details}
        self.events.append(item)
        if self.trace_path:
            path = Path(self.trace_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as stream:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    def delegate(self, sender, recipient, content):
        self.handoffs[recipient] = {"sender": sender, "content": content}
        self.record("handoff", sender=sender, recipient=recipient, content=content)

    def finish(self, reason):
        self.record("case_finished", reason=reason)
        self.phase = "done"
        self.terminal_reason = reason


async def run_swe_team(team, state, idea, n_round):
    team.run_project(idea=idea)
    for _ in range(n_round):
        if state.phase == "done" or team.env.is_idle:
            break
        team._check_balance()
        await team.env.run()
    if state.phase != "done":
        state.finish("team idle" if team.env.is_idle else "team round budget exhausted")
    return team.env.history


def tool_schema(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


FIRST_EDIT_MODEL_ROUNDS = 6
# Tool-call responses are normally tiny. Letting one reasoning-heavy request
# occupy the whole editing window left a partial patch with no time to test.
NATIVE_MODEL_REQUEST_SECONDS = 75
NATIVE_MODEL_MAX_TOKENS = 8192
MODEL_OBSERVATION_CHARS = 6000
CONTRACT_RESEARCH_GRACE_ROUNDS = 2
_CRITICAL_CONTRACT_UNKNOWN = re.compile(
    r"\b(?:predefined|default|fallback|exact (?:constant|value|version|name|signature)|interface contract)\b", re.I)


def unresolved_contracts(state):
    """Return explicitly unknown public-contract details from planning.

    A missing test path is normal discovery. A missing predefined default,
    exact public name, or contract value is not: substituting a convenient
    zero/empty value can make a patch look plausible while changing behavior.
    """
    if state is None:
        return []
    for event in reversed(state.events):
        if event.get("event") != "planning_completed":
            continue
        unknowns = event.get("plan", {}).get("unknowns", [])
        if isinstance(unknowns, str):
            unknowns = [unknowns]
        return [str(item) for item in unknowns
                if _CRITICAL_CONTRACT_UNKNOWN.search(str(item))]
    return []


def require_first_edit(engineer):
    """Whether the next native turn must turn existing evidence into a patch.

    The benchmark run showed that prompt-only guidance allowed twenty model
    turns of broad search without a single edit.  The production runner always
    provides ``DockerEdit``; after six model/action rounds its exact-replace
    tool is sufficient for a focused implementation and prevents further
    unbounded discovery.  Unit and non-container callers without that tool
    retain the ordinary bash schema.
    """
    return bool(
        engineer.docker_edit is not None
        and engineer._edits_count == 0
        and engineer._act_count >= FIRST_EDIT_MODEL_ROUNDS
        # Permit two bounded local reads to resolve a required contract before
        # an edit is forced; never reopen unbounded exploration.
        and (not unresolved_contracts(engineer.case_state)
             or engineer._act_count >= FIRST_EDIT_MODEL_ROUNDS + CONTRACT_RESEARCH_GRACE_ROUNDS)
    )


def native_tools(engineer, *, first_edit_required=None):
    if first_edit_required is None:
        first_edit_required = require_first_edit(engineer)
    tools = []
    if not first_edit_required:
        tools.append(tool_schema("bash", "Run a shell command in the repository; observe output and exit code.",
                                 {"command": {"type": "string"}}, ["command"]))
    if engineer.docker_edit is not None:
        tools.append(tool_schema("edit_file", "Replace one unique exact text occurrence in a source file.",
                     {k: {"type": "string"} for k in ("file", "old", "new")}, ["file", "old", "new"]))
    tools.append(tool_schema("submit", "Submit the current patch and yield to review. Ends this editing phase.", {}, []))
    return tools


# Read content, including staged changes and new files. Edits are identified
# by changed repository state, irrespective of the shell command's spelling.
SNAPSHOT_COMMAND = (
    '(cd "$(git rev-parse --show-toplevel)" && '
    '(git diff HEAD --binary; git ls-files --others --exclude-standard -z | '
    'xargs -0 -r git hash-object --) | sha256sum)'
)


def remember(engineer, name, args, result, call_id=""):
    record = {"tool": name, "arguments": args, "result": result, "call_id": call_id}
    # Keep full records on disk via the instance logger, bounded observations
    # in the model context. Never discard the command associated with output.
    logger.info("SWE action: " + json.dumps(record, ensure_ascii=False))
    model_record = dict(record)
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) > MODEL_OBSERVATION_CHARS:
        head = MODEL_OBSERVATION_CHARS * 3 // 4
        tail = MODEL_OBSERVATION_CHARS - head
        model_record["result"] = encoded[:head] + "\n[output truncated]\n" + encoded[-tail:]
    engineer._native_turns.append(model_record)
    if engineer.case_state:
        engineer.case_state.record("action", role=engineer.name, backend="native", **model_record)
        command = args.get("command", "")
        if re.search(r"\b(pytest|unittest|npm test|yarn test|go test|make test|ansible-test)\b", command):
            engineer.case_state.record("verification_command", command=command, result=model_record["result"],
                                       classification="command heuristic; does not establish correctness")


def collaboration_messages(engineer):
    """Durable, explicit handoffs survive role memory trimming and action replay."""
    state = engineer.case_state
    messages = []
    if state:
        handoff = state.handoffs.get(engineer.name)
        if handoff:
            messages.append({"role": "user", "content": "Team Leader handoff (analysis to verify):\n" + handoff["content"]})
    feedback = [m.content for m in engineer.get_memories() if "CodeReviewFeedback" in str(m.cause_by)]
    latest = state.review_feedback if state and state.review_feedback else (feedback[-1] if feedback else "")
    if state and state.submission_id and state.review_packet:
        packet = {**state.review_packet, 'repository_facts': repository_facts(state)}
        messages.append({"role": "user", "content": "Previous submission and execution evidence (continue from this shared tree):\n" + json.dumps(packet, ensure_ascii=False)})
    if latest:
        messages.append({"role": "user", "content": "Latest review feedback:\n" + latest})
    return messages


def revision_deadline(state):
    """End the main revision early enough for review and a focused final repair."""
    stop = state.deadline - state.review_seconds
    if state.first_edit_deadline and state.submission_id == 1 and state.repair_seconds:
        stop -= state.review_seconds + state.repair_seconds
    return stop


def editing_seconds(engineer):
    if engineer.native_deadline is None:
        return None
    state = engineer.case_state
    if engineer.mas_mode and state and state.first_edit_deadline:
        stop = state.first_edit_deadline if not state.submission_id else revision_deadline(state)
        return max(0, stop - time.monotonic())
    # Reserve a real review window on the first submission; revisions still
    # leave time for a final verdict. Both coding backends use this budget.
    reserve = engineer.review_reserve_seconds if engineer.mas_mode and (not engineer.case_state or not engineer.case_state.submission_id) else 15
    return max(0, engineer.native_deadline - time.monotonic() - reserve)


def request_messages(engineer):
    first_edit_required = require_first_edit(engineer)
    tools = native_tools(engineer, first_edit_required=first_edit_required)
    names = ", ".join(t["function"]["name"] for t in tools)
    system = (
        "You are an SWE engineer working in an existing checked-out repository. "
        "Available tools: " + names + ". Use the actual function tools. "
        "Locate the cause, read focused context, edit the implementation, run "
        "focused existing tests or a small reproduction, and use failure evidence "
        "to revise. Do not weaken tests or use future git history as an answer. "
        "The container has no external network. Use local source and fixtures; do not retry external downloads. "
        "Once focused context supports a concrete implementation, implement it and test that hypothesis. "
        "Repeat a search only to resolve a specific remaining uncertainty. "
        "Use submit when the patch is ready; a diff alone does not prove correctness. "
        "If verification is unavailable, state the specific limitation. "
        "If the Leader marked a required contract (a predefined default, exact literal, or public name) unknown, "
        "resolve it from local source, existing tests, or package conventions before editing. Do not invent a zero/empty fallback. "
        "Every previous action below contains its exact arguments and observation.\n"
        "Repository: " + engineer.terminal.cwd + "\nOriginal task:\n" + engineer.task_requirements
    )
    if first_edit_required:
        system += (
            "\nExploration is complete: use the focused source evidence already "
            "collected and make the smallest concrete implementation change now "
            "with edit_file. Do not request more repository searches."
        )
    # Keep complete action-observation records together under a character
    # budget, rather than orphaning outputs by slicing individual messages.
    selected, size = [], 0
    for record in reversed(engineer._native_turns):
        text = json.dumps(record, ensure_ascii=False)
        if selected and size + len(text) > engineer.native_context_chars:
            break
        # One oversized action must not bypass the context budget merely
        # because it is the most recent record.
        if not selected and len(text) > engineer.native_context_chars:
            text = text[:engineer.native_context_chars] + "\n[record truncated]"
        selected.append({"role": "user", "content": "Completed action:\n" + text})
        size += len(text)
    req = [{"role": "system", "content": system}] + collaboration_messages(engineer) + list(reversed(selected))
    remaining = max(0, engineer.max_native_steps - engineer._act_count)
    progress = f"Remaining model-action rounds: {remaining}. Observed repository changes: {engineer._edits_count}."
    if engineer.native_deadline is not None:
        progress += f" Remaining case wall time: {max(0, int(engineer.native_deadline - time.monotonic()))} seconds. Reserve time for implementation, verification and submission."
    if engineer._native_turns:
        last = engineer._native_turns[-1]
        repeats = sum(r['tool'] == last['tool'] and r['arguments'] == last['arguments']
                      for r in engineer._native_turns)
        if repeats > 1:
            progress += f" The most recent action has been requested {repeats} times. Check its existing observation before repeating it."
    if first_edit_required:
        progress += " First-edit budget reached: edit_file is required before any further exploration."
    req.append({"role": "user", "content": progress + " Choose the next action using the available tools."})
    return req


async def think(engineer):
    if engineer._force_done or (engineer.case_state and engineer.case_state.phase == "done"):
        engineer.rc.todo = None
        return False
    remaining_time = editing_seconds(engineer)
    if remaining_time is not None and remaining_time <= 1:
        await submit(engineer, "editing time exhausted; reserved submission/review window")
        return False
    if engineer._act_count >= engineer.max_native_steps:
        await submit(engineer, "action budget exhausted")
        return False
    if not engineer.task_requirements:
        memories = engineer.get_memories()
        if memories:
            engineer.task_requirements = memories[0].content
    try:
        messages = request_messages(engineer)
        first_edit_required = require_first_edit(engineer)
        tools = native_tools(engineer, first_edit_required=first_edit_required)
        if first_edit_required and engineer.case_state:
            already_recorded = any(e['event'] == 'first_edit_required' for e in engineer.case_state.events)
            if not already_recorded:
                engineer.case_state.record('first_edit_required', role=engineer.name,
                                           after_model_rounds=engineer._act_count)
        if engineer.case_state:
            engineer.case_state.record("model_request", role=engineer.name, backend="native", messages=messages)
        slot_wait = wait_for_model_slot()
        if slot_wait and engineer.case_state:
            engineer.case_state.record("model_rate_wait", role=engineer.name,
                                       seconds=round(slot_wait, 3))
        request_seconds = min(remaining_time, NATIVE_MODEL_REQUEST_SECONDS)
        rsp = await asyncio.wait_for(engineer.llm._achat_completion_function(
            messages=messages, tools=tools,
            tool_choice=({'type': 'function', 'function': {'name': 'edit_file'}}
                         if first_edit_required else 'auto'),
            max_tokens=NATIVE_MODEL_MAX_TOKENS, timeout=request_seconds), timeout=request_seconds)
        engineer._native_rsp = rsp
        choice = rsp.choices[0]
        usage = getattr(rsp, "usage", None)
        logger.info("SWE model response: finish_reason=%s usage=%s" % (choice.finish_reason, usage))
        if choice.finish_reason == "length":
            # Never execute an incomplete write, even if its arguments happen
            # to parse. Tell the model to split the edit into smaller actions.
            remember(engineer, "model_response", {}, "Output budget exhausted; use a smaller action.")
            engineer._native_rsp = None
    except asyncio.TimeoutError:
        if engineer.case_state:
            engineer.case_state.record("model_request_timeout", role=engineer.name, seconds=request_seconds)
        remember(engineer, "model_response", {}, "Model request timed out; issue one short tool call.")
        engineer._native_rsp = None
    except Exception as exc:
        logger.warning("SWE model request failed: %s" % type(exc).__name__)
        remember(engineer, "model_response", {}, "Request failed: " + type(exc).__name__)
        engineer._native_rsp = None
    engineer.rc.todo = RunCommand
    return True


def repository_facts(state):
    """Retain observed file locations across submissions without inventing facts.

    Only successful, simple file reads qualify. Pipes, redirection, scripts,
    search patterns and failed reads cannot establish that a path exists.
    Observations retain provenance; they do not establish current test success.
    """
    files = {}
    for event in state.events:
        if event.get('event') not in ('action', 'review_read'):
            continue
        result = event.get('result', {})
        if not isinstance(result, dict) or result.get('returncode', result.get('exit_code')) != 0:
            continue
        if event.get('tree_before') and event.get('tree_before') != event.get('tree_after'):
            continue
        command = event.get('command', event.get('arguments', {}).get('command', ''))
        if any(marker in command for marker in ('\n', '`', '$(')):
            continue
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            continue
        segments = [[]]
        for token in tokens:
            if token == '&&':
                segments.append([])
            else:
                segments[-1].append(token)
        if any(not part or part[0] not in ('cd', 'ls', 'cat', 'sed', 'head', 'tail', 'wc')
               or any(t in ('|', '||', ';', '>', '>>', '<', '<<', '&', '(', ')') for t in part)
               for part in segments):
            continue
        for part in segments:
            if part[0] not in ('cat', 'sed', 'head', 'tail', 'wc'):
                continue
            # sed -i is a mutation, and shell expansions are not literal paths.
            if part[0] == 'sed' and not (len(part) == 4 and part[1] == '-n'
                                       and re.fullmatch(r'[0-9,$;p ]+', part[2])):
                continue
            path = part[-1]
            if not re.fullmatch(r'[\w./-]+\.[\w-]+', path) or path.startswith('-'):
                continue
            files[path] = dict(path=path, command=clip(command, 1200),
                               elapsed_seconds=event.get('elapsed_seconds'), tree=event.get('tree_after'))
    # Test locations must survive a long tail of source exploration. Bound the
    # handoff; raw observations remain available in the on-disk trajectory.
    ordered = sorted(files.values(), key=lambda item: bool(re.search(
        r'(?:^|/)(?:tests?|test_[^/]+)(?:/|\.)|(?:_test|\.test)\.', item['path'])), reverse=True)
    return dict(observed_files=ordered[:32],
                note='These exact paths were read successfully at the recorded time. Recheck if removed or renamed; file existence is not test coverage or current correctness.')


async def submit(engineer, reason="submitted by engineer"):
    if engineer._force_done:
        return {"submitted": True, "duplicate": True}
    try:
        patch = await engineer.terminal.collect_patch()
    except Exception as exc:
        result = {"error": type(exc).__name__, "message": str(exc)}
        if engineer.case_state:
            engineer.case_state.record("submission_failed", execution=result)
        return {"submitted": False, "execution": result}
    engineer.output_diff = patch
    engineer._force_done = True
    engineer.rc.todo = None
    state = engineer.case_state
    if state is not None:
        file_changes = []
        for section in re.split(r'(?=^diff --git )', patch, flags=re.M):
            match = re.match(r'^diff --git a/(.*?) b/(.*?)$', section, re.M)
            if not match:
                continue
            _left, right = match.groups()
            if right == '/dev/null':
                continue
            file_changes.append(dict(path=right, status='added' if re.search(
                r'^new file mode ', section, re.M) else 'modified'))
        changed_files = sorted({item['path'] for item in file_changes})
        is_test = lambda path: bool(re.search(
            r"(?:^|/)(?:test|tests)(?:/|_).*|(?:^|/)[^/]*(?:_test|\.test)\.[^/]+$", path))
        # New focused coverage is legitimate. Only an edit to a test already
        # present in the baseline can conceal a failing implementation.
        modified_test_files = [item['path'] for item in file_changes
                               if item['status'] == 'modified' and is_test(item['path'])]
        actions = [e for e in state.events if
                   (e['event'] == 'action' and e.get('role') == engineer.name) or e['event'] == 'review_check']
        current_tree = next((e.get('tree_after') for e in reversed(actions)
                             if e.get('role') == engineer.name and e.get('tree_after')), None)
        checks = {}
        recent = []
        for e in actions:
            command = e.get('command', e.get('arguments', {}).get('command', ''))
            result = e.get('result', {})
            if not isinstance(result, dict):
                continue
            evidence = check_evidence(command, result)
            item = dict(command=clip(command,1200), elapsed_seconds=e.get('elapsed_seconds'),
                        tree=e.get('tree_after'), result={**result, 'output':clip(result.get('output',''),1800)})
            if evidence:
                item['check'] = evidence
                checks[command] = item
            elif 'git diff' not in command and 'COMPLETE_TASK_AND_SUBMIT' not in command:
                recent.append(item)
        # Keep failure/no-test evidence even if later read commands fill the tail.
        check_list = list(checks.values())
        failed = [c for c in check_list if c['check']['status'] != 'tests_passed']
        passed = [c for c in check_list if c['check']['status'] == 'tests_passed']
        selected = failed[-6:]+passed[-3:]
        signature = hashlib.sha256(json.dumps({'patch':patch,'checks':[
            {k:v for k,v in c.items() if k!='elapsed_seconds'} for c in selected
        ]},sort_keys=True).encode()).hexdigest()
        if engineer.mas_mode and state.submission_id and signature == state.submission_signature:
            state.record('duplicate_submission_avoided', patch_digest=state.patch_digest)
            state.finish('unchanged patch and verification evidence; no duplicate review')
            return {'submitted': False, 'duplicate': True, 'patch_chars':len(patch)}
        state.submission_signature = signature
        state.submission_id += 1
        state.submitted_patch = patch
        state.patch_digest = hashlib.sha256(patch.encode()).hexdigest()
        state.review_packet = dict(submission_id=state.submission_id, reason=reason, patch_digest=state.patch_digest,
            patch=clip(patch,24000), changed_files=changed_files, file_changes=file_changes,
            modified_test_files=modified_test_files,
            unresolved_contracts=unresolved_contracts(state),
            repository_facts=repository_facts(state),
            checks=selected, current_tree=current_tree, recent_actions=recent[-2:],
            note='Current patch is authoritative. Historical checks carry tree hashes and can be stale; explain unresolved failures against the public task. Zero tests is unverified.')
        state.record('review_packet', packet=state.review_packet)
        state.record("submitted", patch_chars=len(engineer.output_diff), patch_digest=state.patch_digest, reason=reason)
        state.phase = "reviewing" if engineer.mas_mode else "done"
        if not engineer.mas_mode:
            state.terminal_reason = reason
    if engineer.mas_mode and engineer.rc.env:
        engineer.rc.env.publish_message(Message(
            content="Review the submitted patch against the original task.\n" + (json.dumps(state.review_packet, ensure_ascii=False) if state else reason),
            sent_from=engineer.name, send_to={"Reviewer"}, cause_by=CodeReviewRequest,
            metadata={"submission_id": state.submission_id if state else 0,
                      "patch_digest": state.patch_digest if state else ""},
        ), publicer=engineer.profile)
    return {"submitted": True, "patch_chars": len(engineer.output_diff), "reason": reason}


async def act(engineer):
    if engineer.case_state and engineer.case_state.phase == "done":
        engineer.rc.todo = None
        return AIMessage(content="", cause_by=RunCommand)
    engineer._act_count += 1
    rsp = engineer._native_rsp
    calls = rsp.choices[0].message.tool_calls if rsp else None
    if not calls:
        engineer._consecutive_empty += 1
        remember(engineer, "model_response", {}, "No executable tool call; issue a tool call.")
        if engineer._consecutive_empty >= 3:
            await submit(engineer, "three responses without an executable tool call")
        return AIMessage(content="", cause_by=RunCommand)
    engineer._consecutive_empty = 0
    for call in calls:
        name = call.function.name
        try:
            args = json.loads(call.function.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("Tool arguments must be an object")
            if engineer.case_state:
                engineer.case_state.record("action_started", role=engineer.name, backend="native", tool=name, arguments=args)
            remaining = editing_seconds(engineer)
            if remaining is not None and remaining <= 0:
                await submit(engineer, "editing time exhausted before action")
                break
            if name == "submit" or (name == "bash" and re.fullmatch(r"\s*(?:cd\s+/(?:testbed|app)\s*&&\s*)?submit\s*;?\s*", args.get("command", ""))):
                result = await submit(engineer)
            elif name == "bash":
                cmd = args.get("command")
                if not isinstance(cmd, str) or not cmd.strip():
                    raise ValueError("command must be a nonempty string")
                if require_first_edit(engineer):
                    # A few compatible providers can emit an undeclared bash
                    # call despite a function-specific tool_choice.  Do not
                    # let that hallucination reopen unlimited exploration.
                    result = ("First-edit budget reached: bash was not executed. "
                              "Use edit_file with exact source text, or submit.")
                    if engineer.case_state:
                        engineer.case_state.record('first_edit_bash_blocked', role=engineer.name)
                else:
                    before = await engineer.terminal.run_with_status(SNAPSHOT_COMMAND)
                    result = await engineer.terminal.run_with_status(cmd)
                    after = await engineer.terminal.run_with_status(SNAPSHOT_COMMAND)
                    if before["exit_code"] == after["exit_code"] == 0 and before["output"] != after["output"]:
                        engineer._edits_count += 1
                        if engineer.case_state:
                            engineer.case_state.record("repository_changed", role=engineer.name)
                        engineer._round_of_last_edit = engineer._act_count
                # Accept legacy shell submission only when a standalone
                # submit was requested through the explicit tool above.
            elif name == "edit_file" and engineer.docker_edit is not None:
                result = await engineer._docker_edit_replace(**args)
                if engineer.case_state and "REPLACE_OK" in str(result):
                    engineer.case_state.record("repository_changed", role=engineer.name)
            else:
                raise ValueError("Unavailable tool: " + name)
        except Exception as exc:
            result = {"error": type(exc).__name__, "message": str(exc)}
            args = {"raw_arguments": call.function.arguments}
        remember(engineer, name, args, result, call.id)
        if engineer._force_done:
            break
    return AIMessage(content="", cause_by=RunCommand)
