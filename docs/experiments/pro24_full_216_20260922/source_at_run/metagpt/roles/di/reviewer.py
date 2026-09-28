"""SWE-specific native-tool review while retaining MetaGPT routing and role identity."""
import asyncio
import json
import time
from typing import Optional
from pydantic import Field
from metagpt.actions.di.run_command import RunCommand
from metagpt.actions.di.swe_review import CodeReviewFeedback, ReviewApproved
from metagpt.const import TEAMLEADER_NAME
from metagpt.roles.di.role_zero import RoleZero
from metagpt.roles.di.swe_protocol import SWECaseState, tool_schema, revision_deadline
from metagpt.roles.di.swe_role_tools import request_tools
from metagpt.roles.di.swe_checks import capture_check, check_evidence, clip
from metagpt.roles.di.swe_budget import DEFAULT_TEAM_BUDGET
from metagpt.schema import AIMessage, Message
from metagpt.tools.libs.docker_terminal import DockerTerminal
from metagpt.tools.tool_registry import register_tool

REVIEW_INSTRUCTION = '''Review this submitted patch against the public task. Use actual function tools.
The packet is supplied once and describes the current submission. Previous check results may refer to older tree versions.
Check every public requirement, including distinct input types and operations; a passing check for one branch does not cover another branch.
Use repository_facts.observed_files for previously located files before guessing paths. Inspect only missing context.
Use at most four focused read/check actions (at most two checks), across sequential turns as needed to locate files, correct a failed command, and verify behavior. Then publish the verdict.
A test command must be the real test/reproduction WITHOUT head/tail/grep output pipelines; the check tool captures its exit code and limits display.
Zero tests means unverified, not passed. Test failures can reflect old expectations: explain their relation to the new requirements.
Do not install dependencies, search future history for an answer, edit the repository, or run a full repository suite.
Publish a short verdict promptly: approved, changes_requested with a specific file/behavior and evidence, or inconclusive if evidence/time is insufficient.
For EVERY failed check in the packet, supply a failure_dispositions entry with its exact command,
resolution (expected_behavior, environment_blocked, or unresolved), and concrete public-task/source evidence.
Only a successful rerun of the SAME command on the current tree establishes fixed-and-verified;
a custom passing check cannot erase a repository failure. Prefer minimal compatibility-preserving changes.
For inconclusive, set reason=verification_missing and next_check to a focused verification goal/command;
use environment_blocked only with observed dependency/runtime evidence that prevents that check.
Missing verification may trigger ONE follow-up within the remaining budget. Do not invent a code defect.
A timeout or unavailable check alone is not a code defect. Account for all changed files, including build-generated changes.
Do not approve a submission that modifies an existing test: restoring the test and demonstrating the source behavior is required first.
When source names have an existing matching test file, run that repository test before accepting a custom reproduction. A self-authored reproduction alone does not establish compatibility.
If the submission packet lists an unresolved required contract, do not approve a guessed zero/empty/default value. Require local repository evidence for the exact value or public name.
Keep each explanation under 150 words. Lead with at most three actionable blockers, each with its file, observed failure and focused verification command. Omit summaries of already-correct code. Preserve time to publish the verdict; do not narrate a long plan.'''

REVIEW_TOOLS = [
    tool_schema('read', 'Read focused repository context using a shell command. Do not edit.', {'command': {'type': 'string'}}, ['command']),
    tool_schema('check', 'Run an actual focused test or reproduction, without output filtering pipelines.', {'command': {'type': 'string'}}, ['command']),
    tool_schema('publish_review', 'Publish the review verdict and account for failed checks.',
        {'status': {'type': 'string', 'enum': ['approved', 'changes_requested', 'inconclusive']},
         'content': {'type': 'string', 'minLength': 1, 'description': 'Concise evidence and concrete required changes, if any.'},
         'reason': {'type': 'string', 'enum': ['verification_missing', 'environment_blocked']},
         'next_check': {'type': 'string', 'description': 'Focused verification command or concrete goal.'},
         'failure_dispositions': {'type': 'array', 'items': {'type': 'object', 'properties': {
             'command': {'type': 'string'},
             'resolution': {'type': 'string', 'enum': ['expected_behavior', 'environment_blocked', 'unresolved']},
             'evidence': {'type': 'string'}}, 'required': ['command', 'resolution', 'evidence']}}}, ['status', 'content']),
]

@register_tool(include_functions=['publish_review'])
class Reviewer(RoleZero):
    name: str = 'Reviewer'
    profile: str = 'Reviewer'
    goal: str = 'Review the submitted patch against the public task.'
    task_context: str = ''
    case_state: Optional[SWECaseState] = Field(default=None, exclude=True)
    terminal: DockerTerminal = Field(default=None, exclude=True)
    instruction: str = REVIEW_INSTRUCTION
    max_react_loop: int = 6
    tools: list[str] = ['DockerTerminal:run_command', 'Reviewer']
    _active_submission_id: int = 0
    _act_count: int = 0
    _review_published: bool = False
    _review_request_ids: set = set()
    _successful_check: bool = False
    _review_checks: list = []

    def _update_tool_execution(self):
        self.tool_execution_map.update({'Reviewer.publish_review': self.publish_review,
                                       'DockerTerminal.run_command': self.terminal.run_command})

    def review_messages(self):
        return [{'role': 'system', 'content': REVIEW_INSTRUCTION},
                {'role': 'user', 'content': 'Public task:\n'+self.task_context+'\nCurrent submission:\n'+
                 json.dumps(self.case_state.review_packet if self.case_state else {}, ensure_ascii=False)}]

    async def _react(self):
        state = self.case_state
        if state and state.phase != 'reviewing':
            return AIMessage(content='', cause_by=RunCommand)
        fresh = [m for m in self.rc.news if 'CodeReviewRequest' in str(m.cause_by) and m.id not in self._review_request_ids]
        if state:
            fresh = [m for m in fresh if m.metadata.get('submission_id') == state.submission_id]
        if fresh:
            self._review_request_ids.update(m.id for m in fresh)
            self._active_submission_id = state.submission_id if state else 0
            self._review_published = False
            self._act_count = 0
            self._successful_check = False
            self._review_checks = []
        elif self._review_published or (state and self._active_submission_id != state.submission_id):
            return AIMessage(content='', cause_by=RunCommand)
        if state:
            state.record('review_started', role=self.name)
            if state.review_packet and not state.submitted_patch.strip():
                await self.publish_review('No source code changes detected. Implement the public task requirements in the affected source, then verify and resubmit.', status='changes_requested')
                return AIMessage(content='Empty submission returned.', cause_by=CodeReviewFeedback)
        budget = state.review_seconds if state else DEFAULT_TEAM_BUDGET.review_seconds
        if state and state.deadline:
            budget = max(.01, min(budget, state.deadline-time.monotonic()-15))
        try:
            await asyncio.wait_for(self._run_review(budget), timeout=budget)
        except asyncio.TimeoutError:
            await self.publish_review('Review deadline reached without a completed verdict; current patch preserved. No specific code defect was established.', status='inconclusive')
        except Exception as exc:
            if state:
                state.record('review_error', role=self.name, error=type(exc).__name__)
            await self.publish_review('Review could not complete due to '+type(exc).__name__+'. Current patch preserved.', status='inconclusive', reason='environment_blocked')
        return AIMessage(content='Review phase finished.', cause_by=RunCommand)

    async def _run_review(self, budget):
        deadline = time.monotonic()+budget
        verdict_reserve = min(60, budget / 3)
        max_tokens = self.case_state.reviewer_max_tokens if self.case_state else DEFAULT_TEAM_BUDGET.reviewer_max_tokens
        messages = self.review_messages()
        reads = checks = 0
        exploration_finished = False
        for turn in range(self.max_react_loop):
            if self._review_published:
                return
            remaining = deadline-time.monotonic()
            final = exploration_finished or remaining <= verdict_reserve or reads+checks >= 4 or turn == self.max_react_loop-1
            available = [REVIEW_TOOLS[-1]] if final else REVIEW_TOOLS
            try:
                choice = await request_tools(self, messages+[{'role':'user','content':f'{remaining:.1f}s left. Publish a concise verdict now if evidence is sufficient.'}], available, remaining if final else remaining-verdict_reserve, max_tokens=max_tokens, final_tool='publish_review' if final else None)
            except asyncio.TimeoutError:
                if final:
                    raise
                # Exploration must not consume the final verdict's reserved time.
                messages.append({'role':'user','content':'Exploration timed out. Publish a verdict from existing evidence; use inconclusive if insufficient.'})
                exploration_finished = True
                continue
            self._act_count += 1
            if choice.finish_reason == 'length':
                messages.append({'role':'user','content':'Response truncated; execute no partial command. Return a much shorter tool call.'})
                continue
            calls = choice.message.tool_calls or []
            for call in calls:
                name = call.function.name
                try:
                    args = json.loads(call.function.arguments or '{}')
                    if not isinstance(args, dict):
                        raise ValueError('Tool arguments must be an object')
                except (ValueError, TypeError):
                    if self.case_state:
                        self.case_state.record('review_tool_arguments_invalid', role=self.name, tool=name)
                    messages.append({'role':'user','content':'Invalid or incomplete tool arguments were not executed. Return one short valid tool call.'})
                    continue
                if name == 'publish_review':
                    content = args.get('content')
                    if args.get('status') not in ('approved', 'changes_requested', 'inconclusive') or not isinstance(content, str) or not content.strip():
                        if self.case_state:
                            self.case_state.record('review_verdict_invalid', role=self.name, reason='empty content or invalid status')
                        messages.append({'role':'user','content':'Empty or invalid verdict was rejected. Publish one valid status and a nonempty concise explanation grounded in the current evidence. If evidence is insufficient, explain that with inconclusive.'})
                        continue
                    await self.publish_review(args.get('content',''), status=args.get('status','inconclusive'),
                                              reason=args.get('reason', 'verification_missing'),
                                              next_check=args.get('next_check', ''),
                                              failure_dispositions=args.get('failure_dispositions'))
                    return
                if name not in ('read','check') or final or reads+checks >= 4 or (name == 'check' and checks >= 2):
                    messages.append({'role':'user','content':'Tool allowance exhausted. Publish the verdict.'})
                    continue
                command = args.get('command','')
                seconds = min(budget/3, deadline-time.monotonic()-verdict_reserve)
                if seconds <= 0:
                    continue
                self.terminal.execution_deadline = time.monotonic()+seconds
                self.terminal.trace_role = self.name
                result = await self.terminal.run_with_status(capture_check(command) if name=='check' else command, timeout=seconds)
                evidence = check_evidence(command, result)
                tree = self.case_state.review_packet.get('current_tree') if self.case_state else None
                if evidence:
                    self._review_checks.append(dict(command=command, tree=tree, check=evidence))
                if evidence and evidence.get('status') == 'tests_passed':
                    self._successful_check = True
                if self.case_state:
                    self.case_state.record('review_check' if name=='check' else 'review_read', role=self.name, command=command, result=result, evidence=evidence, tree_after=tree)
                messages.append({'role':'user','content':json.dumps(dict(tool=name, command=command, result={**result,'output':clip(result['output'],3500)}, evidence=evidence),ensure_ascii=False)})
                checks += name == 'check'
                reads += name == 'read'
        if not self._review_published:
            await self.publish_review('Review action allowance exhausted without a verdict; current patch preserved.', status='inconclusive')

    async def publish_review(self, content: str, approved: Optional[bool] = None, status: Optional[str] = None,
                             reason: str = 'verification_missing', next_check: str = '', failure_dispositions=None) -> str:
        """Publish approved, concrete changes_requested, or inconclusive without requesting edits."""
        status = status or ('approved' if approved else 'changes_requested')
        if status not in ('approved','changes_requested','inconclusive') or not content.strip():
            raise ValueError('A valid status and nonempty evidence are required')
        state = self.case_state
        # Inspection alone repeatedly approved patches that violated public
        # import/return contracts.  Require a successful focused check from
        # this review, or one attached to this exact submission packet.
        packet_checks = state.review_packet.get('checks', []) if state else []
        tree = state.review_packet.get('current_tree') if state else None
        packet_has_success = any(c.get('check', {}).get('status') == 'tests_passed'
                                 and tree and c.get('tree') == tree for c in packet_checks)
        latest = {c.get('command', ''): c for c in [*packet_checks, *self._review_checks]}
        dispositions = {d.get('command'): d for d in (failure_dispositions or []) if isinstance(d, dict)}
        failures = []
        for command, check in latest.items():
            if check.get('check', {}).get('status') == 'tests_passed' and (check in self._review_checks or (tree and check.get('tree') == tree)):
                continue
            disposition = dispositions.get(command, {})
            if disposition.get('resolution') != 'expected_behavior' or not disposition.get('evidence', '').strip():
                failures.append(command)
        if status == 'approved' and failures:
            status = 'inconclusive'
            reason = 'verification_missing'
            content = 'Unresolved failed/unverified checks: ' + '; '.join(failures) + '. ' + content
        modified_tests = state.review_packet.get('modified_test_files', []) if state else []
        unresolved_contracts = state.review_packet.get('unresolved_contracts', []) if state else []
        if status == 'approved' and unresolved_contracts:
            status = 'changes_requested'
            content = ('Required public contract remains unverified: ' + '; '.join(unresolved_contracts) +
                       '. Establish the exact value/name from local source, an existing repository test, or package conventions; '
                       'do not substitute a zero or empty fallback. Attach focused evidence, then resubmit. ' + content)
        if status == 'approved' and modified_tests:
            status = 'changes_requested'
            content = ('This submission modifies existing test files: ' + ', '.join(modified_tests) +
                       '. Restore those tests, then verify the source behavior against the original test contract. ' + content)
        if status == 'approved' and not (self._successful_check or packet_has_success):
            status = 'inconclusive'
            content = ('No successful focused verification is attached to this submission. '
                       'Current patch is preserved; inspection alone is not approval. ' + content)
        if state:
            if state.phase != 'reviewing' or self._active_submission_id != state.submission_id:
                self._review_published = True
                return 'Ignored stale review: submission changed or task ended.'
            state.record('review_verdict', status=status, approved=True if status=='approved' else False if status=='changes_requested' else None, content=content, reason=reason, failure_dispositions=failure_dispositions or [])
            if status == 'approved':
                state.finish('Reviewer approved submission %d' % state.submission_id)
            elif status == 'inconclusive':
                remaining = revision_deadline(state) - time.monotonic() if state.deadline else 0
                if reason != 'environment_blocked' and remaining >= 60 and state.verification_cycles < 1:
                    state.verification_cycles += 1
                    target = next_check or ('Re-run and explain these recorded checks: ' + '; '.join(failures)
                                            if failures else 'Run the existing focused repository tests for the changed source and public requirements.')
                    content = ('Verification follow-up (one attempt; preserve time for final review). ' + target +
                               ' Record the real exit code and failure summary. Preserve existing tests. '
                               'Classify every failure using public requirements and local source evidence; '
                               'a custom passing check does not replace a failing repository test. '
                               'If infrastructure prevents verification, record the exact blocker and submit; do not install dependencies. '
                               'Change source only for an evidenced defect. Reviewer: ' + content)
                    state.review_feedback = content
                    state.phase = 'editing'
                    state.record('verification_followup', remaining_seconds=round(remaining, 3), target=target)
                else:
                    state.finish('review inconclusive; current patch preserved')
            else:
                state.review_feedback = content
                state.review_cycles += 1
                if state.review_cycles > state.max_revisions:
                    state.finish('revision budget exhausted')
                elif state.deadline and revision_deadline(state)-time.monotonic() < 20:
                    state.finish('insufficient editing time for requested changes; patch preserved')
                else:
                    state.phase = 'editing'
        self._set_state(-1)
        self._review_published = True
        if (status == 'inconclusive' and (not state or state.phase != 'editing')) or (state and state.phase=='done' and status!='approved'):
            return 'Review finished without requesting another editing phase.'
        msg = Message(content=content, sent_from=self.name,
                      send_to=TEAMLEADER_NAME if status=='approved' else 'Alex',
                      cause_by=ReviewApproved if status=='approved' else CodeReviewFeedback,
                      metadata={'submission_id':self._active_submission_id, 'review_status':status})
        self.publish_message(msg)
        return 'Review approved. Notified Team Leader.' if status=='approved' else 'Review feedback sent to Engineer.'

    def publish_message(self, msg: Message, send_to: str = 'no one'):
        if msg and self.rc.env:
            msg.send_to = send_to if send_to != 'no one' else msg.send_to
            self.rc.env.publish_message(msg, publicer=self.profile)
