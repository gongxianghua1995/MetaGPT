"""SWEBenchTeamLeader: a TeamLeader subclass for SWE-bench Verified MAS runs.

TeamLeader._think overwrites self.instruction with TL_INSTRUCTION.format(team_info=...) at
runtime, so simply setting the `instruction` field on a subclass is NOT enough — the field
gets clobbered on every _think call. This subclass overrides _think to install the
SWE-specific instruction instead, then calls RoleZero._think directly (bypassing
TeamLeader._think's clobber) so the SWE constraints reach the LLM.
"""
from __future__ import annotations

import asyncio
import time
import json
import re
from typing import Any, ClassVar, Optional
from metagpt.roles.di.swe_protocol import SWECaseState, tool_schema
from metagpt.roles.di.swe_role_tools import request_tools
from metagpt.roles.di.swe_checks import clip

from pydantic import Field

from metagpt.actions.di.run_command import RunCommand
from metagpt.logs import logger
from metagpt.roles.di.role_zero import RoleZero
from metagpt.roles.di.team_leader import TeamLeader
from metagpt.schema import AIMessage, Message, UserMessage
from metagpt.tools.libs.docker_terminal import DockerTerminal

# Empty-response circuit breaker: after this many CONSECUTIVE skipped TL
# rounds the model is permanently stuck — verify_skipe rerun showed TLs
# burning 20-60 min alternating bash/empty or pure-empty without ever
# delegating. Breaker = one slim-prompt retry, then hand the issue to Alex.
TL_EMPTY_BREAKER_LIMIT = 5

# Native function-calling tool schemas for the TL (mirrors EvoMAS's approach):
# the weak model fills {"command": ...} / {"send_to": ..., "content": ...} via
# the API's tool_calls field. smoke25 showed the TL's OWN RoleZero JSON output
# collapses to empty during CTO analysis — the format tax hits coordinators
# too, so delegation never happened and Alex idled with no task.
NATIVE_TL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command to explore the repo at /testbed "
            "(read-only: grep/cat/sed -n/git log) to build the technical plan.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The bash command to execute"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_team_message",
            "description": "Publish a message to a team member. Copy ALL relevant context "
            "(issue text, technical plan, reviewer feedback) — you are their sole info source.",
            "parameters": {
                "type": "object",
                "properties": {
                    "send_to": {"type": "string", "description": "Team member name: 'Alex' (Engineer) or 'Reviewer'"},
                    "content": {"type": "string", "description": "Full message content to send"},
                },
                "required": ["send_to", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end",
            "description": "End the task. ONLY call after the Reviewer approved the fix.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

SWE_TL_INSTRUCTION = """You are the Team Leader of a SWE-bench repair team. Members: {team_info}
Your task is a brief technical investigation and one handoff to Alex, the Engineer.
Use at most 3 focused read-only bash commands in the repository. Do not edit files.
Then call publish_team_message with send_to="Alex" and a concise technical plan:
- Verified files and symbols, with the observation supporting each.
- Likely cause and minimal implementation steps; label hypotheses and unknowns honestly.
- A runnable focused test or reproduction, using the actual repository layout.
Alex already receives the full original task; do not repeat it. File mentions alone do not prove relevance.
New public interfaces requested by the issue may need implementing; do not repeatedly search for their definitions.
After delegation yield. The explicit protocol routes Engineer submissions to Reviewer and feedback back to Alex.
Never call end before approval or reply_to_human. Preserve time for coding and verification."""


class SWEBenchTeamLeader(TeamLeader):
    """TeamLeader for SWE-bench MAS: analyzes issue, delegates technical plan
    to Engineer → Reviewer → end/iterate. Acts as CTO: explores /testbed,
    produces root-cause analysis and implementation plan for Alex."""

    instruction: str = SWE_TL_INSTRUCTION
    case_state: Optional[SWECaseState] = Field(default=None, exclude=True)
    # CTO role: TeamLeader needs read-only container access to explore /testbed
    # and produce a technical plan. Uses DockerTerminal (stateless).
    terminal: DockerTerminal = Field(default=None, exclude=True)
    # Native function-calling path (same rationale as SWEBenchEngineer).
    use_native_toolcall: bool = True
    # smoke27: base TeamLeader caps max_react_loop=3. TL exhausted its loop
    # after 3 exploration rounds and exited without ever delegating - the
    # whole team idled with an empty patch. 20 gives room for
    # explore -> delegate -> forward to Reviewer -> end across phases.
    max_react_loop: int = 20
    # NOTE: pydantic v2 forbids Field() on underscore-prefixed names — plain
    # defaults become private attributes instead of fields.
    _native_rsp: Any = None
    _no_toolcall_streak: int = 0
    _delegated: bool = False
    # Set by the empty-response breaker in _think; consumed in _act which
    # force-delegates the raw issue to Alex and yields control.
    _empty_breaker: bool = False
    # Counts CodeReviewFeedback (rejection) verdicts seen so far. Used by
    # _reviewer_verdict_action to allow exactly one revise-and-resubmit
    # cycle before giving up (see that method's docstring for the bug this
    # fixes).
    _revise_rounds: int = 0
    # Pro-run fix: the TL routinely burned its ENTIRE 20-round loop on bash
    # exploration and never delegated ("reached max_react_loop: 20" →
    # force-delegating the RAW issue, i.e. all exploration wasted). Cap
    # pre-delegation bash rounds; at the cap, delegate WITH the partial
    # findings collected so far instead of continuing to explore.
    _pre_delegate_bash: int = 0

    # Max bash exploration rounds before the TL must delegate. Leaves the
    # remaining max_react_loop budget for Phases 2-3 (forwarding, verdict
    # handling, revise cycles).
    TL_EXPLORE_CAP: ClassVar[int] = 3
    _handled_review_ids: set = set()

    def _update_tool_execution(self):
        """Route Bash.run to the container so TL can grep/cat /testbed."""
        self.tool_execution_map.update(
            {
                "Bash.run": self._docker_bash_run,
            }
        )

    async def _docker_bash_run(self, cmd: str) -> str:
        """Execute a read-only bash command in the container."""
        if self.terminal is None:
            return "Error: no DockerTerminal configured for TeamLeader"
        result = await self.terminal.run_command(cmd)
        return result if isinstance(result, str) else str(result)

    def _exploration_notes(self, max_notes: int = 3, max_chars: int = 12000) -> str:
        """Collect the TL's own bash observations from memory so a forced
        delegation carries the partial findings instead of the raw issue
        (pro-run fix: the raw-issue fallback threw away all TL exploration)."""
        notes = []
        for m in self.rc.memory.get(40):
            c = m.content or ""
            if c.startswith("[bash] "):
                notes.append(c if len(c)<=4000 else c[:1800]+'\n[truncated]\n'+c[-2200:])
        return "\n---\n".join(notes[-max_notes:])[:max_chars]

    def _delegation_content(self) -> str:
        notes = self._exploration_notes()
        return ("Planning time exhausted; these are partial observations, not a verified diagnosis. "
                "Use the original task contract to implement and verify a minimal fix.\n" + notes)

    def _tool_arguments(self, call):
        """Decode a native tool payload without letting one malformed model
        response discard the whole planning phase.

        Some compatible providers occasionally wrap valid JSON in prose or
        append a partial suffix.  Tool arguments are model output, so this is
        a recoverable protocol error: return ``None`` and hand the collected
        evidence to Alex instead of raising JSONDecodeError from ``_react``.
        """
        raw = getattr(getattr(call, "function", None), "arguments", "{}")
        if isinstance(raw, dict):
            return raw
        if not isinstance(raw, str):
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            if start < 0:
                return None
            try:
                value, _ = json.JSONDecoder().raw_decode(raw[start:])
            except json.JSONDecodeError:
                return None
        return value if isinstance(value, dict) else None

    async def _quick_think(self):
        return None, "TASK"

    async def _think(self) -> bool:
        if self.case_state and (self._delegated or self.case_state.phase in {"reviewing", "done"}):
            self.rc.todo = None
            return False
        self.instruction = SWE_TL_INSTRUCTION.format(team_info=self._get_team_info())
        if self.use_native_toolcall:
            # After publish_team_message/end set _set_state(-1) -> todo=None
            # -> yield control so the env can route messages to Alex/Reviewer.
            # (Never-delegated protection lives in _react's exit hook: within
            # a live loop rc.todo is always truthy, so a guard here would be
            # unreachable - exactly the smoke27 failure.)
            if not self.rc.todo:
                return False
            return await self._think_native_toolcall()
        return await RoleZero._think(self)

    async def _react(self) -> Message:
        if self._delegated or (self.case_state and self.case_state.phase in {'reviewing','done'}):
            return AIMessage(content='', cause_by=RunCommand)
        from metagpt.roles.di.swe_budget import DEFAULT_TEAM_BUDGET
        budget = self.case_state.planning_seconds if self.case_state else DEFAULT_TEAM_BUDGET.planning_seconds
        max_tokens = self.case_state.leader_max_tokens if self.case_state else DEFAULT_TEAM_BUDGET.leader_max_tokens
        if self.case_state and self.case_state.deadline:
            budget = max(.01, min(budget, self.case_state.deadline-time.monotonic()))
        deadline = time.monotonic()+budget
        synthesis_reserve = budget / 2
        observations = []
        plan_tool = tool_schema('publish_plan', 'Publish a compact technical plan to Alex. Label unknowns, invent no paths.', {
            'locations': {'type':'array','items':{'type':'string'}},
            'hypothesis': {'type':'string'},
            'steps': {'type':'array','items':{'type':'string'}},
            'verification': {'type':'string'},
            'unknowns': {'type':'array','items':{'type':'string'}},
        }, ['locations','hypothesis','steps','verification','unknowns'])
        messages = [{'role':'system','content':
            'You are the MetaGPT Team Leader. Investigate the public task using at most three focused read-only bash commands, then publish_plan. '
            'Do not edit, inspect future git history, or search repeatedly for a new interface that must be implemented. '
            'Give verified locations, a hypothesis, concrete steps, one focused check, and honest unknowns. '
            'The Engineer already has the original task. Keep the plan under 250 words. Do not just announce future exploration.'},
            {'role':'user','content':self._latest_issue_content()}]
        count = 0
        # Every exploration exit, including a batch exceeding the cap, goes
        # through the same structured plan request below.
        try:
            for _ in range(4):
                remaining = deadline-time.monotonic()
                if count >= self.TL_EXPLORE_CAP or remaining <= synthesis_reserve + 2:
                    break
                choice = await request_tools(self, messages, [NATIVE_TL_TOOLS[0],plan_tool], remaining-synthesis_reserve, max_tokens=max_tokens)
                if choice.finish_reason == 'length':
                    messages.append({'role':'user','content':'Truncated response. Use one small focused command or publish a short plan.'})
                    continue
                for call in choice.message.tool_calls or []:
                    args = self._tool_arguments(call)
                    if args is None:
                        if self.case_state:
                            self.case_state.record('planning_tool_arguments_invalid',
                                                   tool=call.function.name)
                        continue
                    if call.function.name == 'publish_plan':
                        self._publish_structured_plan(args)
                        if self._delegated:
                            return AIMessage(content='Plan delivered.', cause_by=RunCommand)
                    if call.function.name != 'bash' or count >= self.TL_EXPLORE_CAP:
                        continue
                    if deadline-time.monotonic() <= synthesis_reserve + 2:
                        break
                    command = args.get('command','')
                    self.terminal.execution_deadline = min(deadline-synthesis_reserve, time.monotonic()+budget/3)
                    self.terminal.trace_role = self.name
                    output = await self.terminal.run_command(command)
                    count += 1
                    observation = {'command':command, 'output':clip(output,3000)}
                    observations.append(observation)
                    messages.append({'role':'user','content':json.dumps(observation,ensure_ascii=False)})
        except Exception as exc:
            if self.case_state:
                self.case_state.record('planning_exploration_stopped', error=type(exc).__name__)
        if not self._delegated and deadline-time.monotonic() > .1:
            try:
                choice = await request_tools(self, messages+[{'role':'user','content':'Exploration finished. Publish the plan now; label missing information as unknown.'}],
                                             [plan_tool], deadline-time.monotonic(), max_tokens=max_tokens, final_tool='publish_plan')
                if choice.finish_reason != 'length':
                    for call in choice.message.tool_calls or []:
                        if call.function.name == 'publish_plan':
                            plan = self._tool_arguments(call)
                            if plan is None:
                                if self.case_state:
                                    self.case_state.record('planning_tool_arguments_invalid',
                                                           tool='publish_plan')
                                continue
                            self._publish_structured_plan(plan)
                            if self._delegated:
                                break
            except Exception as exc:
                if self.case_state:
                    self.case_state.record('planning_synthesis_failed', error=type(exc).__name__)
        if not self._delegated:
            content = 'Planning incomplete: no verified plan was produced. Follow the original task, using these partial observations only as evidence.\n'+json.dumps(observations,ensure_ascii=False)
            self._delegated = self._publish_to_member(content, 'Alex')
            if self.case_state:
                self.case_state.record('planning_incomplete')
        return AIMessage(content='Planning finished.', cause_by=RunCommand)

    def _publish_structured_plan(self, plan):
        required = ('locations','hypothesis','steps','verification','unknowns')
        if not isinstance(plan, dict) or any(k not in plan for k in required):
            return
        if not plan['steps'] or not plan['hypothesis']:
            return
        self._delegated = self._publish_to_member('Technical plan (verify hypotheses):\n'+json.dumps(plan,ensure_ascii=False), 'Alex')
        if self._delegated and self.case_state:
            self.case_state.record('planning_completed', plan=plan)

    async def _think_native_toolcall(self) -> bool:
        """Native function-calling think for the TL: the system prompt carries
        the three-phase instruction; the API tool schemas carry bash /
        publish_team_message / end. No JSON-in-content format tax."""
        system_prompt = self.system_prompt.format(
            role_info=self._get_prefix(),
            task_type_desc=self.task_type_desc,
            available_commands=json.dumps({
                "bash": {"description": "Explore /testbed (grep/cat/sed -n/git log)"},
                "publish_team_message": {"description": "Delegate to Alex or Reviewer with full context"},
                "end": {"description": "End the task after Reviewer approval"},
            }),
            example="",
            instruction=self.instruction.strip(),
        )

        # Build decision prompt, with optional Tier-3 empty-response warning.
        empty_warning = ""
        if getattr(self, "_consecutive_empty", 0) >= 1:
            empty_warning = (
                f"\n⚠ WARNING: You returned {self._consecutive_empty} empty "
                f"response(s). Call a REAL tool now — bash (explore), "
                f"publish_team_message (delegate), or end."
            )
        prompt = (
            "Produce a concise technical plan from focused exploration, then delegate to Alex with publish_team_message."
            f"{empty_warning}"
        )
        memory = self.rc.memory.get(self.memory_k)
        req = [{"role": "system", "content": system_prompt}]
        req += self.llm.format_msg(memory + [UserMessage(content=prompt)])

        try:
            # Empty-response handling v2 (same three-tier as engineer):
            # Tier 1 retry 3× transient empties; Tier 2 skip + consecutive
            # counter (no memory pollution); Tier 3 prompt warning above.
            retries_left = 3
            empty_this_round = False
            while True:
                rsp = await self.llm._achat_completion_function(
                    messages=req,
                    tools=NATIVE_TL_TOOLS if self._pre_delegate_bash < self.TL_EXPLORE_CAP else [NATIVE_TL_TOOLS[1]],
                    tool_choice="auto" if self._pre_delegate_bash < self.TL_EXPLORE_CAP else {"type":"function", "function":{"name":"publish_team_message"}},
                )
                message = rsp.choices[0].message
                if message.tool_calls or (message.content or "").strip():
                    break
                if retries_left <= 0:
                    empty_this_round = True
                    break
                retries_left -= 1
                logger.warning(
                    f"TL LLM EMPTY response — retrying ({retries_left} left)"
                )
            self._native_rsp = rsp
            message = rsp.choices[0].message

            if empty_this_round:
                self._consecutive_empty = getattr(self, "_consecutive_empty", 0) + 1
                logger.warning(
                    f"TL still empty — skipping round "
                    f"(consecutive_empty={self._consecutive_empty})"
                )
                # Circuit breaker: a long CONSECUTIVE empty streak means the
                # TL model is permanently stuck. Try ONE slim-prompt retry
                # (system + oldest memory + current prompt); if that also
                # comes back empty, hand the raw issue to Alex instead of
                # burning the wall clock (verify_skipe: 10/26 empty patches
                # ended with the team idling before Alex was ever activated).
                if self._consecutive_empty >= TL_EMPTY_BREAKER_LIMIT:
                    slim_ok = False
                    try:
                        slim_req = [req[0], req[1], req[-1]] if len(req) > 3 else req
                        slim_rsp = await self.llm._achat_completion_function(
                            messages=slim_req,
                            tools=NATIVE_TL_TOOLS,
                            tool_choice="auto",
                        )
                        slim_msg = slim_rsp.choices[0].message
                        if slim_msg.tool_calls or (slim_msg.content or "").strip():
                            logger.warning(
                                "Slim-prompt retry recovered TL from empty streak — resuming."
                            )
                            slim_ok = True
                            self._consecutive_empty = 0
                            self._native_rsp = slim_rsp
                            self.rc.todo = RunCommand
                            return True
                    except Exception as e:
                        logger.warning(f"TL slim retry failed: {e}")
                    if not slim_ok:
                        logger.warning(
                            f"TL EMPTY-RESPONSE BREAKER tripped after "
                            f"{self._consecutive_empty} consecutive empty rounds — "
                            f"force-delegating issue to Alex."
                        )
                        self._empty_breaker = True
                        self.rc.todo = RunCommand
                        return True
                self._skip_round = True
                self.rc.todo = RunCommand
                return True

            self._consecutive_empty = 0
            self.rc.todo = RunCommand
            if not message.tool_calls:
                content = message.content or ""
                logger.warning(f"TL native toolcall: no tool_calls, content: {content[:100]}")
                self.rc.memory.add(AIMessage(content=content[:200], sent_from=self.name, cause_by=RunCommand))
                self.rc.memory.add(UserMessage(
                    content="Please call one of the tools: bash, publish_team_message, or end.",
                    cause_by=RunCommand,
                ))
            return True
        except Exception as e:
            logger.error(f"TL native toolcall _think failed: {e}")
            self.use_native_toolcall = False
            return await RoleZero._think(self)

    async def ask_human(self, question: str) -> str:
        """Unmanned eval guard: smoke18/19 crashed with EOFError when the TL
        called ask_human in a no-stdin (nohup) process. Auto-degrade instead
        of crashing the whole team."""
        logger.warning(f"ask_human auto-degraded in unmanned mode: {question[:80]}")
        return (
            "[auto-degraded] No human available. If the Engineer has not "
            "produced changes after multiple attempts, end the task and "
            "collect whatever patch is on disk."
        )

    async def _act(self) -> Message:
        """Absorb weak-model JSON failures (JSON fallback) and dispatch native
        tool calls (native path). smoke16 died 75s in when the TL's very first
        LLM call returned empty and the raw JSONDecodeError from parse_commands
        bubbled up and killed the whole team.
        """
        # Empty-response breaker: _think tripped after a long consecutive
        # empty streak. Force-delegate the raw issue to Alex (with anchors)
        # and yield — same publish path as the normal toolcall, so _set_state
        # (-1) happens inside publish_team_message and the next _think ends.
        if getattr(self, "_empty_breaker", False):
            self._empty_breaker = False
            issue = self._delegation_content()
            if self._publish_to_member(content=issue, send_to="Alex"):
                self._delegated = True
                logger.warning("TL breaker: issue force-delegated to Alex, yielding.")
            else:
                logger.warning("TL breaker: publish failed; ending activation without delegation.")
            return AIMessage(
                content="Delegated the issue to Alex (TL empty-response breaker).",
                sent_from=self.name,
                cause_by=RunCommand,
            )

        # Skip empty-response rounds — same as engineer.
        if getattr(self, "_skip_round", False):
            self._skip_round = False
            return AIMessage(
                content="(LLM returned empty; retrying without counting this round)",
                sent_from=self.name,
                cause_by=RunCommand,
            )
        if self.use_native_toolcall and self._native_rsp is not None:
            return await self._act_native_toolcall()
        try:
            return await super()._act()
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"TeamLeader absorbed {type(e).__name__}: {e}")
            self.rc.memory.add(UserMessage(
                content="Invalid JSON output. Follow the three-phase instruction and output a valid JSON command, e.g. TeamLeader.publish_team_message.",
                cause_by=RunCommand))
            return AIMessage(
                content="Invalid JSON output; retry with a valid command.",
                sent_from=self.name,
                cause_by=RunCommand)

    def _reviewer_verdict_action(self, recent_all: list) -> AIMessage | None:
        """Inspect recent memory for a Reviewer verdict and decide whether to
        end the task or give Alex a chance to revise.

        BUG FIXED (improvement report #1 "TL auto-end过早终止"): the previous
        implementation matched fuzzy content substrings (e.g. "Review
        completed") to decide the task was "done" — but that phrase appears
        in BOTH the Reviewer's approval AND rejection messages
        (metagpt/roles/di/reviewer.py), so a single rejection (empty diff /
        "bug is not fixed" / etc.) triggered the exact same auto-end as a
        real approval. This cut Alex off the instant the Reviewer sent back
        ANY feedback, before Alex had a chance to act on it — a major
        contributor to empty/incomplete patches (confirmed in the webclients
        infra-failure trajectory analysis).

        Fix: use the reliable `cause_by` tag (ReviewApproved vs
        CodeReviewFeedback) instead of content text, only auto-end on a real
        approval, and give Alex multiple revise-and-resubmit cycles on
        rejection before giving up (up to 5 rounds).
        """
        for m in reversed(recent_all):
            cause = str(m.cause_by or "")
            if "ReviewApproved" not in cause and "CodeReviewFeedback" not in cause:
                continue
            if m.id in self._handled_review_ids:
                return None
            self._handled_review_ids.add(m.id)
            if "ReviewApproved" in cause:
                logger.warning("TL auto-end: Reviewer approved the fix, ending task.")
                self._set_state(-1)
                return AIMessage(
                    content="Task ended (Reviewer approved the fix).",
                    sent_from=self.name,
                    cause_by=RunCommand,
                )
            if "CodeReviewFeedback" in cause:
                self._revise_rounds += 1
                if self._revise_rounds > 5:
                    logger.warning("TL auto-end: Reviewer rejected the fix again, giving up.")
                    self._set_state(-1)
                    return AIMessage(
                        content="Task ended (Reviewer feedback unresolved after revision).",
                        sent_from=self.name,
                        cause_by=RunCommand,
                    )
                logger.warning("TL: Reviewer requested changes; giving Alex one revise cycle.")
                self._publish_to_member(
                    content=(
                        f"Reviewer feedback (address this, then resubmit): {m.content}"
                    ),
                    send_to="Alex",
                )
                self._delegated = True
                return AIMessage(
                    content="Forwarded Reviewer feedback to Alex for one revise cycle.",
                    sent_from=self.name,
                    cause_by=RunCommand,
                )
        return None

    async def _act_native_toolcall(self) -> Message:
        """Dispatch tool_calls from the native response:
        - bash → read-only container exploration, observation into memory
        - publish_team_message → base implementation (sets _set_state(-1), so
          the next _think returns False and TL yields control to the team)
        - end → _set_state(-1), task over
        """
        message = self._native_rsp.choices[0].message
        tool_calls = message.tool_calls or []

        # smoke28: Alex force-submitted with no patch, Mike received the
        # message but re-entered exploration instead of Phase 3.
        # Detect "Forced submit" in recent memory and shortcut to Reviewer.
        # Deduplication: if a Reviewer verdict (CodeReviewFeedback or
        # ReviewApproved) already exists in the same window, Alex already
        # handed off to Reviewer via MAS handshake and this 2nd path is
        # redundant — skip the re-forward so we don't re-spawn a Reviewer
        # read-only loop when the task is already in Phase 3.
        #
        # smoke40: TL's _think LLM call takes ~40s. During that time
        # Reviewer may publish a verdict to env, but TL won't see it
        # until the NEXT _observe (which happens AFTER _act). Fix:
        # pull fresh messages from env before checking dedup.
        try:
            await self._observe()
        except Exception:
            pass
        recent = self.rc.memory.get(20)
        saw_forced_submit = any("Forced submit" in (m.content or "") for m in reversed(recent))
        saw_verdict = any(
            any(kw in (m.content or "") for kw in (
                "Review completed",
                "[Fast-path empty-diff review]",
                "[Forced review",
            ))
            or "CodeReviewFeedback" in str(m.cause_by or "")
            or "ReviewApproved" in str(m.cause_by or "")
            for m in reversed(recent)
        )
        # smoke41: Alex force-submit sends BOTH CodeReviewRequest (MAS
        # handoff to Reviewer) AND "Forced submit" AIMessage (to TL) in
        # the same batch. TL and Reviewer process concurrently — TL can't
        # see Reviewer's verdict because Reviewer hasn't produced it yet.
        # Fix: if Alex already sent CodeReviewRequest, Reviewer is already
        # notified — TL doesn't need to forward again.
        saw_code_review_request = any(
            "CodeReviewRequest" in str(m.cause_by or "")
            for m in reversed(recent)
        )
        if saw_forced_submit and not saw_verdict and not saw_code_review_request:
            logger.warning("TL detected Alex force-submit -> Phase 3 (forward to Reviewer).")
            self._publish_to_member(
                content=(
                    "Alex has submitted the current changes (editing stalled without producing a patch). "
                    "Please review: run `git diff HEAD` in /testbed and decide if the changes "
                    "(if any) address the issue. If not, provide actionable feedback to Alex."
                ),
                send_to="Reviewer",
            )
            self._delegated = True
            return AIMessage(
                content="Forwarded to Reviewer (Alex force-submit detected).",
                sent_from=self.name,
                cause_by=RunCommand,
            )
        # smoke30/32: Reviewer already published a verdict (approved or
        # feedback). See _reviewer_verdict_action docstring for the
        # approve-vs-reject bug this fixes.
        recent_all = self.rc.memory.get(20)
        verdict_action = self._reviewer_verdict_action(recent_all)
        if verdict_action:
            return verdict_action
        if not tool_calls:
            self._no_toolcall_streak += 1
            if self._no_toolcall_streak >= 3:
                # smoke30: if Reviewer already published a verdict, handle
                # via _reviewer_verdict_action instead of re-delegating to a
                # silenced Alex (which just loops).
                recent_all = self.rc.memory.get(20)
                verdict_action = self._reviewer_verdict_action(recent_all)
                if verdict_action:
                    return verdict_action
                # Force-delegate fallback (smoke25 failure mode): the weak TL
                # failed 3x to emit any tool call — guarantee Alex receives the
                # task, else the whole team idles. Reverts to simple forwarding.
                issue = self._delegation_content()
                if issue:
                    self._publish_to_member(content=issue, send_to="Alex")
                    self._delegated = True
                    logger.warning("Force-delegated raw issue to Alex after 3 empty TL responses.")
                    self._no_toolcall_streak = 0
                    return AIMessage(
                        content="Delegated the issue to Alex (fallback).",
                    sent_from=self.name,
                    cause_by=RunCommand,
                    )
            self.rc.memory.add(UserMessage(
                content="No tool call detected. Call bash to explore, or "
                "publish_team_message to delegate, or end.",
                cause_by=RunCommand))
            return AIMessage(content="Nudged to call a tool.", sent_from=self.name, cause_by=RunCommand)

        self._no_toolcall_streak = 0
        if message.content:
            self.rc.memory.add(AIMessage(content=message.content, sent_from=self.name, cause_by=RunCommand))

        logger.info(f"TL tool batch: {[tc.function.name for tc in tool_calls]}")
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "bash":
                # Pro-run fix: cap pre-delegation exploration. Beyond the cap,
                # stop exploring and delegate with the partial findings.
                if not self._delegated:
                    self._pre_delegate_bash += 1
                    if self._pre_delegate_bash > self.TL_EXPLORE_CAP:
                        content = self._delegation_content()
                        if content:
                            logger.warning(
                                f"TL explore cap ({self.TL_EXPLORE_CAP}) reached — "
                                "delegating to Alex with partial findings."
                            )
                            if self._publish_to_member(content=content, send_to="Alex"):
                                self._delegated = True
                                return AIMessage(
                                    content="Delegated to Alex (explore cap reached).",
                                    sent_from=self.name,
                                    cause_by=RunCommand,
                                )
                cmd = args.get("command", "")
                obs = await self._docker_bash_run(cmd) if cmd else "Error: empty command"
                self.rc.memory.add(UserMessage(content=f"[bash] {cmd}\n\n{obs}", cause_by=RunCommand))
                logger.info(f"TL bash: {cmd[:80]} -> {obs[:80]}")
            elif name == "publish_team_message":
                send_to = args.get("send_to", "")
                content = args.get("content", "")
                published = self._publish_to_member(content=content, send_to=send_to)
                self._delegated = self._delegated or published
                logger.info(f"TL delegated to {send_to or 'Alex'}: {content[:120]}")
                return AIMessage(
                    content=f"Delegated to {send_to}.",
                    sent_from=self.name,
                    cause_by=RunCommand,
                )
            elif name == "end":
                if not self._delegated:
                    # smoke27: weak TL explored then called end without ever
                    # delegating - intercept and hand the issue to Alex.
                    content = self._delegation_content()
                    if content:
                        logger.warning("TL called end WITHOUT delegating — force-delegating issue (+notes) to Alex instead.")
                        if self._publish_to_member(content=content, send_to="Alex"):
                            self._delegated = True
                            return AIMessage(
                                content="Delegated the issue to Alex (end intercepted).",
                                sent_from=self.name,
                                cause_by=RunCommand,
                            )
                logger.info("TL ended the task.")
                self._set_state(-1)
                return AIMessage(content="Task ended.", sent_from=self.name, cause_by=RunCommand)
            else:
                logger.warning(f"TL unknown tool: {name}")
        return AIMessage(content="Tool calls executed.", sent_from=self.name, cause_by=RunCommand)

    def _collect_edit_anchors(self) -> str:
        # A filename mentioned by a search is not proof that it needs editing.
        return ""

    def _publish_to_member(self, content: str, send_to: str) -> bool:
        """Safe delegation: MGXEnv.get_role raises KeyError on an unknown
        recipient (e.g. weak model emits "alex"/"Alex (Engineer)"), which
        aborted _act AFTER _set_state(-1) with zero logging — the team then
        idled with Alex never activated. Normalize + absorb routing errors.

        verify_skipe rerun: the weak TL called publish_team_message with
        EMPTY send_to AND empty content ("TL published message to  (0 chars)")
        in 10/26 empty-patch cases — the call "succeeded", `_delegated` was
        set True, the never-delegated guard never fired, and the team idled.
        So: empty recipient -> Alex, empty content -> raw issue, and report
        success honestly so guards stay armed.

        The original task is provided independently to the Engineer; this
        message carries the Leader's plan and observations."""
        aliases = {"alex": "Alex", "engineer": "Alex", "reviewer": "Reviewer"}
        raw = (send_to or "").split("(")[0].strip().lower()
        target = aliases.get(raw, raw)
        if not target:
            target = "Alex"
        content = content or ""
        if not content.strip():
            content = self._latest_issue_content()
        if not content.strip():
            logger.warning("TL publish skipped: empty content and no issue in memory")
            return False
        if target == "Alex":
            anchors = self._collect_edit_anchors()
            content = anchors + "\n" + content
        try:
            self.publish_team_message(content=content, send_to=target)
            if self.case_state:
                self.case_state.delegate(self.name, target, content)
            logger.info(f"TL published message to {target} ({len(content)} chars)")
            return True
        except Exception:
            logger.exception(f"TL publish to {send_to!r} failed — falling back to Alex")
            try:
                self.publish_team_message(content=content, send_to="Alex")
                if self.case_state:
                    self.case_state.delegate(self.name, "Alex", content)
                return True
            except Exception:
                return False

    def _latest_issue_content(self) -> str:
        """First memory message = the issue the runner injected (the TL is the
        sole info source for Alex, same contract as the old forward-only TL)."""
        msgs = self.rc.memory.get()
        return msgs[0].content if msgs else ""
