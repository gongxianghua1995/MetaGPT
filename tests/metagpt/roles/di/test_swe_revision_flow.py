"""Offline regressions for review lifecycle and public task contracts.

Run directly with the metagpt Python environment; no model or Docker calls.
"""
import asyncio
import json
import subprocess
import tempfile
import unittest
import runpy
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from metagpt.actions.di.swe_review import CodeReviewFeedback, CodeReviewRequest
from metagpt.roles.di.role_zero import RoleZero
from metagpt.roles.di.reviewer import Reviewer
from metagpt.roles.di.swe_engineer import SWEBenchEngineer
from metagpt.roles.di.swe_team_leader import SWEBenchTeamLeader
from metagpt.schema import AIMessage, Message
from metagpt.tools.libs.docker_bash import DockerBash
from metagpt.tools.libs.docker_terminal import DockerTerminal
from metagpt.utils.swe_requirements import interface_paths, requirement_text
from metagpt.roles.di import swe_protocol
from metagpt.roles.di.swe_protocol import SWECaseState
from metagpt.tools.libs.docker_edit import DockerEdit
from metagpt.team import Team


class RevisionFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_public_field_decoding_preserves_code_escapes(self):
        raw = 'Path: `src/example.py`\nDescription: preserve \\r\\n and Unicode 中文'
        self.assertEqual(requirement_text(json.dumps(raw)), raw)
        self.assertEqual(requirement_text(raw), raw)
        self.assertEqual(interface_paths(json.dumps(raw)), ['src/example.py'])
        self.assertEqual(interface_paths('No new interfaces are introduced.'), [])
        self.assertEqual(interface_paths('Path: ../../outside.py'), [])

    def test_leader_counts_each_rejection_once(self):
        leader = SWEBenchTeamLeader(terminal=DockerTerminal('offline'))
        feedback = Message(content='Fix src/example.py', cause_by=CodeReviewFeedback)
        with patch.object(SWEBenchTeamLeader, '_publish_to_member', return_value=True) as publish:
            leader._reviewer_verdict_action([feedback])
            self.assertEqual(leader._revise_rounds, 1)
            for _ in range(8):
                self.assertIsNone(leader._reviewer_verdict_action([feedback]))
            self.assertEqual(leader._revise_rounds, 1)
            fresh = Message(content='Still missing edge case', cause_by=CodeReviewFeedback)
            leader._reviewer_verdict_action([feedback, fresh])
            self.assertEqual(leader._revise_rounds, 2)
            self.assertEqual(publish.call_count, 2)
        other = SWEBenchTeamLeader(terminal=DockerTerminal('offline'))
        self.assertFalse(other._handled_review_ids)

    async def test_reviewer_resets_only_for_new_submission(self):
        reviewer = Reviewer(terminal=DockerTerminal('offline'), task_context='Original requirements')
        reviewer._review_published = True
        reviewer._act_count = 8
        request = Message(content='Please review', cause_by=CodeReviewRequest)
        reviewer.rc.news = [request]
        with patch.object(Reviewer, '_run_review', new=AsyncMock(return_value=AIMessage(content='ok'))):
            await reviewer._react()
            self.assertFalse(reviewer._review_published)
            self.assertEqual(reviewer._act_count, 0)
            self.assertIn('Original requirements', json.dumps(reviewer.review_messages()))
            reviewer._review_published = True
            reviewer._act_count = 5
            await reviewer._react()
            self.assertTrue(reviewer._review_published)
            self.assertEqual(reviewer._act_count, 5)
            reviewer.rc.news = [Message(content='Revised patch', cause_by=CodeReviewRequest)]
            await reviewer._react()
            self.assertFalse(reviewer._review_published)
            self.assertEqual(reviewer._act_count, 0)

    async def test_nonempty_diff_never_auto_approves_at_budget_limit(self):
        reviewer = Reviewer(terminal=DockerTerminal('offline'))
        choice = NS(finish_reason='stop', message=NS(tool_calls=[]))
        with patch('metagpt.roles.di.reviewer.request_tools', new=AsyncMock(return_value=choice)):
            with patch.object(Reviewer, 'publish_review', new=AsyncMock()) as publish:
                await reviewer._run_review(45)
                self.assertEqual(publish.call_args.kwargs['status'], 'inconclusive')

    async def test_interface_mismatch_is_bounded_and_matching_paths_pass(self):
        engineer = SWEBenchEngineer(terminal=DockerBash('offline'), interface_files=['src/right.py'])
        with patch.object(DockerBash, 'run', new=AsyncMock(return_value='src/wrong.py\n')) as run:
            result = await engineer._gated_bash_run('cd /app && submit')
            self.assertIn('[SCOPE CHECK]', result)
            self.assertEqual(run.await_count, 1)
            await engineer._gated_bash_run('submit')
            self.assertEqual(run.await_count, 2)
            self.assertEqual(run.call_args.kwargs['cmd'], 'submit')
        engineer = SWEBenchEngineer(terminal=DockerBash('offline'), interface_files=['src/right.py'])
        with patch.object(DockerBash, 'run', new=AsyncMock(side_effect=['src/right.py\n', 'SUBMITTED'])):
            self.assertEqual(await engineer._gated_bash_run('submit'), 'SUBMITTED')


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    def engineer(self, **kwargs):
        return SWEBenchEngineer(terminal=DockerBash('offline'), run_eval=True, **kwargs)

    def response(self, *commands, finish_reason='tool_calls'):
        return NS(usage=None, choices=[NS(finish_reason=finish_reason, message=NS(content=None, tool_calls=[
            NS(id='call_%d' % i, function=NS(name=name, arguments=json.dumps(args)))
            for i, (name, args) in enumerate(commands)
        ]))])

    def test_context_exposes_environment_budget_and_repeat_evidence(self):
        import time
        engineer = self.engineer(native_deadline=time.monotonic() + 60)
        engineer._native_turns = [{'tool': 'bash', 'arguments': {'command': 'cat x'}, 'result': 'x'}] * 2
        messages = swe_protocol.request_messages(engineer)
        self.assertIn('no external network', messages[0]['content'])
        self.assertIn('Remaining case wall time:', messages[-1]['content'])
        self.assertIn('requested 2 times', messages[-1]['content'])

    def test_large_action_observation_cannot_overflow_model_context(self):
        engineer = self.engineer()
        swe_protocol.remember(engineer, 'bash', {'command':'find .'}, {'output':'x' * 50000, 'exit_code':0})
        self.assertLess(len(json.dumps(engineer._native_turns[-1])), 7000)
        engineer.native_context_chars = 5000
        messages = swe_protocol.request_messages(engineer)
        action_messages = [m['content'] for m in messages if m['content'].startswith('Completed action:')]
        self.assertEqual(len(action_messages), 1)
        self.assertLess(len(action_messages[0]), 5100)

    def test_first_edit_budget_removes_bash_from_native_schema(self):
        engineer = self.engineer(docker_edit=DockerEdit(container_name='offline'))
        engineer._act_count = swe_protocol.FIRST_EDIT_MODEL_ROUNDS
        messages = swe_protocol.request_messages(engineer)
        tools = swe_protocol.native_tools(engineer)
        self.assertEqual({t['function']['name'] for t in tools}, {'edit_file', 'submit'})
        self.assertIn('First-edit budget reached', messages[-1]['content'])

    async def test_docker_edit_can_create_new_file_with_empty_old(self):
        class LocalProcess:
            def __init__(self, script):
                self.script = script

            async def communicate(self):
                result = subprocess.run(['python3', '-c', self.script], text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
                return result.stdout.encode(), b''

        async def local_docker_exec(*args, **kwargs):
            return LocalProcess(args[-1])

        with tempfile.TemporaryDirectory() as folder:
            editor = DockerEdit(container_name='offline', cwd=folder)
            with patch('metagpt.tools.libs.docker_edit.asyncio.create_subprocess_exec', new=local_docker_exec):
                result = await editor.replace('new_importer.py', '', 'answer = 42\n')
                self.assertTrue(result.startswith('REPLACE_OK: created'))
                self.assertEqual((Path(folder) / 'new_importer.py').read_text(), 'answer = 42\n')
                result = await editor.replace('new_importer.py', '', 'overwritten\n')
                self.assertIn('empty old is only valid', result)
                self.assertEqual((Path(folder) / 'new_importer.py').read_text(), 'answer = 42\n')

    async def test_first_edit_budget_rejects_hallucinated_bash(self):
        engineer = self.engineer(docker_edit=DockerEdit(container_name='offline'))
        engineer._act_count = swe_protocol.FIRST_EDIT_MODEL_ROUNDS
        engineer._native_rsp = self.response(('bash', {'command': 'cat unrelated.py'}))
        with patch.object(DockerBash, 'run_with_status', new=AsyncMock()) as shell:
            await engineer._act()
        shell.assert_not_awaited()
        self.assertIn('bash was not executed', engineer._native_turns[-1]['result'])

    async def test_submit_revision_and_global_end(self):
        state = SWECaseState()
        engineer = self.engineer(case_state=state, mas_mode=True)
        reviewer = Reviewer(terminal=DockerTerminal('offline'), case_state=state)
        leader = SWEBenchTeamLeader(terminal=DockerTerminal('offline'), case_state=state)
        team = Team(use_mgx=True)
        team.hire([leader, engineer, reviewer])
        engineer._set_state(0)
        engineer._native_rsp = self.response(('submit', {}), ('bash', {'command': 'MUST_NOT_RUN'}))
        patch_text = 'diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n'
        execution = dict(exit_code=0, output='<<SUBMISSION START||\n' + patch_text + '||SUBMISSION DONE>>')
        with patch.object(DockerBash, 'collect_patch', new=AsyncMock(return_value=patch_text)) as run:
            await engineer._act()
            self.assertEqual(run.await_count, 1)
            self.assertEqual(engineer.output_diff, patch_text)
            self.assertIsNone(engineer.rc.todo)
            self.assertTrue(engineer._force_done)
            self.assertEqual(state.phase, 'reviewing')
            requests = [m for m in reviewer.rc.msg_buffer.pop_all() if 'CodeReviewRequest' in m.cause_by]
            self.assertEqual(len(requests), 1)
            reviewer.rc.news = requests
            with patch.object(Reviewer, '_run_review', new=AsyncMock(return_value=AIMessage(content=''))):
                await reviewer._react()
            await reviewer.publish_review('Fix the remaining edge case', False)
            self.assertEqual(state.phase, 'editing')
            engineer._maybe_rearm_on_feedback()
            self.assertFalse(engineer._force_done)
            engineer._native_rsp = self.response(('submit', {}))
            run.return_value = patch_text + '+revision\n'
            await engineer._act()
            self.assertEqual(state.submission_id, 2)
            self.assertIn('stale', await reviewer.publish_review('old approval', True))
            self.assertEqual(state.phase, 'reviewing')
            reviewer.rc.news = [m for m in reviewer.rc.msg_buffer.pop_all() if 'CodeReviewRequest' in m.cause_by]
            with patch.object(Reviewer, '_run_review', new=AsyncMock(return_value=AIMessage(content=''))):
                await reviewer._react()
            await reviewer.publish_review('Verified current submission', True)
            self.assertEqual(state.phase, 'done')
            self.assertFalse(await engineer._think())
            self.assertFalse(await leader._think())

    async def test_failed_submit_does_not_finish(self):
        engineer = self.engineer(case_state=SWECaseState())
        engineer._set_state(0)
        engineer._native_rsp = self.response(('submit', {}))
        with patch.object(DockerBash, 'collect_patch', new=AsyncMock(side_effect=RuntimeError('git failed'))):
            await engineer._act()
        self.assertFalse(engineer._force_done)
        self.assertEqual(engineer.case_state.submission_id, 0)
        self.assertIsNotNone(engineer.rc.todo)

    async def test_revision_budget_has_terminal_state(self):
        state = SWECaseState(phase='reviewing', submission_id=1, max_revisions=0)
        reviewer = Reviewer(terminal=DockerTerminal('offline'), case_state=state)
        reviewer._active_submission_id = 1
        await reviewer.publish_review('Not correct', False)
        self.assertEqual(state.phase, 'done')
        self.assertEqual(state.terminal_reason, 'revision budget exhausted')

    async def test_empty_output_edit_has_replayable_command(self):
        engineer = self.engineer()
        command = "cat > new_file.py <<'EOF'\nanswer = 42\nEOF"
        engineer._native_rsp = self.response(('bash', {'command': command}))
        with patch.object(DockerBash, 'run_with_status', new=AsyncMock(side_effect=[
            dict(exit_code=0, output='before'), dict(exit_code=0, output=''), dict(exit_code=0, output='after')])):
            await engineer._act()
        self.assertEqual(engineer._edits_count, 1)
        self.assertEqual(engineer._native_turns[-1]['arguments']['command'], command)
        self.assertEqual(engineer._native_turns[-1]['result']['exit_code'], 0)
        self.assertIn('new_file.py', json.dumps(swe_protocol.request_messages(engineer)))

    async def test_truncated_action_is_not_executed(self):
        engineer = self.engineer()
        response = self.response(('bash', {'command': 'dangerous_partial_write'}), finish_reason='length')
        with patch.object(type(engineer.llm), '_achat_completion_function', new=AsyncMock(return_value=response)):
            await engineer._think_native_toolcall()
        with patch.object(DockerBash, 'run_with_status', new=AsyncMock()) as run:
            await engineer._act()
            run.assert_not_awaited()

    async def test_advertised_editor_is_callable(self):
        engineer = self.engineer(docker_edit=DockerEdit(container_name='offline'))
        self.assertEqual({t['function']['name'] for t in swe_protocol.native_tools(engineer)}, {'bash', 'edit_file', 'submit'})
        engineer._native_rsp = self.response(('edit_file', dict(file='x.py', old='bad', new='good')))
        with patch.object(DockerEdit, 'replace', new=AsyncMock(return_value='REPLACE_OK: x.py')) as edit:
            await engineer._act()
            edit.assert_awaited_once_with(file='x.py', old='bad', new='good')
        self.assertEqual(engineer._edits_count, 1)

    async def test_team_stops_after_terminal_verdict(self):
        state = SWECaseState()
        async def environment_round():
            state.finish('approved')
        team = NS(run_project=lambda **kwargs: None, _check_balance=lambda: None,
                  env=NS(is_idle=False, run=AsyncMock(side_effect=environment_round), history='history'))
        self.assertEqual(await swe_protocol.run_swe_team(team, state, 'issue', 30), 'history')
        team.env.run.assert_awaited_once()

    def test_legitimate_edit_spellings_do_not_crash(self):
        from metagpt.roles.di.swe_engineer import _looks_like_edit
        for cmd in ['apply_patch < patch.txt', "cat > x.py <<'EOF'\nx\nEOF", "cd /app/src && sed -i 's/a/b/' x.py"]:
            self.assertTrue(_looks_like_edit(cmd), cmd)
        self.assertFalse(_looks_like_edit('python3 -c "print(42)"'))


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.harness = runpy.run_path(str(Path(__file__).with_name('run_swebench_pro_eval.py')))

    def test_padded_pytest_and_spaces_in_parameter_id(self):
        parser = self.harness['parse_log_pro_pytest']
        text = "tests/test_a.py::test_case[a value] PASSED     [100%]\n__PRO__PASSED\ttests/test_b.py::test_other   \n"
        self.assertEqual(parser(text, None), {'tests/test_a.py::test_case[a value]': 'PASSED', 'tests/test_b.py::test_other': 'PASSED'})

    def test_last_retry_result_wins(self):
        parser = self.harness['parse_log_pro_pytest']
        self.assertEqual(parser('t.py::test_x FAILED [100%]\nt.py::test_x PASSED [100%]', None), {'t.py::test_x': 'PASSED'})

    def test_build_and_dependency_patches_are_preserved(self):
        for path in ['setup.py', 'pyproject.toml', 'requirements.txt', 'go.mod', 'go.sum', 'yarn.lock']:
            patch_text = 'diff --git a/%s b/%s\n--- a/%s\n+++ b/%s\n@@ -1 +1 @@\n-old\n+new\n' % (path, path, path, path)
            self.assertFalse(self.harness['_is_lockfile_only_patch'](patch_text))
            self.assertEqual(self.harness['_strip_lockfile_hunks'](patch_text), patch_text)



class RealShellTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_shell_status_and_submission(self):
        import tempfile
        import subprocess
        import shlex
        class LocalBash(DockerBash):
            async def _start_process(self):
                self.process = await asyncio.create_subprocess_exec(
                    'bash', cwd=self.cwd, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            async def start(self):
                self.start_flag = True
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            subprocess.run(['git', '-C', tmp, '-c', 'user.name=Test', '-c',
                            'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'base'], check=True)
            terminal = LocalBash('local-test', cwd=tmp)
            try:
                self.assertEqual((await terminal.run_with_status('true'))['exit_code'], 0)
                self.assertEqual((await terminal.run_with_status('false'))['exit_code'], 1)
                text = 'preserve spacing: a  &&  b'
                result = await terminal.run_with_status('printf %s ' + shlex.quote(text))
                self.assertEqual(result['output'], text)
                self.assertEqual(result['exit_code'], 0)
                result = await terminal.run_with_status("printf 'hello'; false")
                self.assertEqual(result['output'].strip(), 'hello')
                self.assertEqual(result['exit_code'], 1)
                source = Path('metagpt/tools/swe_agent_commands/defaults.sh').resolve()
                await terminal.run_with_status('source ' + shlex.quote(str(source)))
                before = await terminal.run_with_status(swe_protocol.SNAPSHOT_COMMAND)
                (Path(tmp) / 'requirements.txt').write_text('example==1.0\n')
                after = await terminal.run_with_status(swe_protocol.SNAPSHOT_COMMAND)
                self.assertNotEqual(before['output'], after['output'])
                result = await terminal.run_with_status('submit')
                self.assertEqual(result['exit_code'], 0)
                self.assertIn('diff --git a/requirements.txt', result['output'])
                self.assertIn('||SUBMISSION DONE>>', result['output'])
                # A failed staging operation must not emit a successful submission.
                (Path(tmp) / '.git/index.lock').write_text('')
                result = await terminal.run_with_status('submit')
                self.assertNotEqual(result['exit_code'], 0)
                self.assertNotIn('||SUBMISSION DONE>>', result['output'])
            finally:
                if terminal.process:
                    terminal.process.terminate()
                    await terminal.process.wait()

if __name__ == '__main__':
    unittest.main()
