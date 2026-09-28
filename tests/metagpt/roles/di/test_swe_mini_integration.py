"""Offline regressions for real handoff routing and the mini role lifecycle."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from metagpt.roles.di import swe_protocol
from metagpt.roles.di.swe_mini_engineer import SWEMiniEngineer
from metagpt.roles.di.swe_team_leader import SWEBenchTeamLeader
from metagpt.roles.di.reviewer import Reviewer
from metagpt.roles.di.swe_engineer import SWEBenchEngineer
from metagpt.roles.di.swe_protocol import SWECaseState
from metagpt.roles.di.swe_execution import EditingProgress
from metagpt.team import Team
from metagpt.tools.libs.docker_bash import DockerBash
from metagpt.tools.libs.docker_terminal import DockerTerminal


class MiniIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_each_edit_invalidates_verification_and_failed_checks_keep_reminder(self):
        progress = EditingProgress(600)
        self.assertNotIn('milestone reached', progress.guidance(30))
        self.assertIn('milestone reached', progress.guidance(200))
        progress.observe(changed=True, evidence=None)
        self.assertTrue(progress.verification_due)
        self.assertIn('invoke the changed entry point', progress.guidance(220))
        progress.observe(changed=False, evidence={'status': 'failure_observed'})
        self.assertTrue(progress.verification_due)
        progress.observe(changed=False, evidence={'status': 'tests_passed'})
        self.assertFalse(progress.verification_due)
        progress.observe(changed=True, evidence={'status': 'tests_passed'})
        self.assertTrue(progress.verification_due)
        final = EditingProgress(120, repair_only=True)
        self.assertIn('fix only the concrete review blockers', final.guidance(10))

    def test_routed_handoff_reaches_request_after_memory_trim(self):
        state = SWECaseState()
        leader = SWEBenchTeamLeader(terminal=DockerTerminal('offline'), case_state=state)
        engineer = SWEBenchEngineer(terminal=DockerBash('offline'), case_state=state, task_requirements='ORIGINAL_CONTRACT')
        reviewer = Reviewer(terminal=DockerTerminal('offline'), case_state=state)
        Team(use_mgx=True).hire([leader, engineer, reviewer])
        self.assertTrue(leader._publish_to_member('PLAN_SENTINEL: inspect foo before changing bar', 'Alex'))
        # No role-memory fallback: the durable successful handoff must survive.
        engineer.rc.memory.clear()
        engineer._native_turns = [{'tool': 'bash', 'arguments': {'command': 'ls'}, 'result': 'x'*5000}]*20
        request = json.dumps(swe_protocol.request_messages(engineer))
        self.assertIn('PLAN_SENTINEL', request)
        self.assertIn('ORIGINAL_CONTRACT', request)
        self.assertEqual(sum(e['event']=='handoff' for e in state.events), 1)
        # A handoff to a different role must not override Engineer instructions.
        leader._publish_to_member('REVIEWER_ONLY_SENTINEL', 'Reviewer')
        self.assertNotIn('REVIEWER_ONLY_SENTINEL', json.dumps(swe_protocol.request_messages(engineer)))

    async def test_expired_editing_window_submits_without_model_call(self):
        state = SWECaseState()
        engineer = SWEBenchEngineer(terminal=DockerBash('offline'), case_state=state, mas_mode=True,
                                    native_deadline=time.monotonic()+40, review_reserve_seconds=60)
        with patch.object(swe_protocol, 'submit', new=AsyncMock()) as submit:
            self.assertFalse(await swe_protocol.think(engineer))
            submit.assert_awaited_once()

    async def test_mini_uses_handoff_feedback_and_shared_submission(self):
        state = SWECaseState(review_feedback='REVIEW_SENTINEL')
        state.delegate('Mike', 'Alex', 'PLAN_SENTINEL')
        with tempfile.TemporaryDirectory() as folder:
            engineer = SWEMiniEngineer(terminal=DockerBash('offline'), case_state=state, mas_mode=True,
                                      mini_output_dir=folder, task_requirements='ORIGINAL_CONTRACT')
            async def worker(request, path, seconds):
                self.assertIn('PLAN_SENTINEL', request['task'])
                self.assertIn('REVIEW_SENTINEL', request['task'])
                self.assertIn('ORIGINAL_CONTRACT', request['task'])
                self.assertNotIn('api_key', request)
                (path/'events.jsonl').write_text(json.dumps({'event':'model_request','worker_elapsed_seconds':1})+'\n'+
                                                  json.dumps({'event':'repository_changed','worker_elapsed_seconds':2})+'\n')
                (path/'result.json').write_text(json.dumps({'exit_status':'Submitted'}))
            with patch.object(SWEMiniEngineer, '_run_worker', new=AsyncMock(side_effect=worker)):
                with patch.object(swe_protocol, 'submit', new=AsyncMock()) as submit:
                    await engineer._react()
                    submit.assert_awaited_once_with(engineer, 'mini: Submitted')
            self.assertEqual(engineer._act_count, 1)
            self.assertEqual(engineer._edits_count, 1)
            self.assertEqual(state.events[-1]['event'], 'repository_changed')

    async def test_worker_timeout_salvages_and_outer_cancellation_propagates(self):
        with tempfile.TemporaryDirectory() as folder:
            engineer = SWEMiniEngineer(terminal=DockerBash('offline'), case_state=SWECaseState(), mini_output_dir=folder)
            with patch.object(SWEMiniEngineer, '_run_worker', new=AsyncMock(side_effect=asyncio.TimeoutError)):
                with patch.object(swe_protocol, 'submit', new=AsyncMock()) as submit:
                    await engineer._react()
                    self.assertIn('time exhausted', submit.call_args.args[1])
            with patch.object(SWEMiniEngineer, '_run_worker', new=AsyncMock(side_effect=asyncio.CancelledError)):
                with patch.object(swe_protocol, 'submit', new=AsyncMock()) as submit:
                    with self.assertRaises(asyncio.CancelledError):
                        await engineer._react()
                    submit.assert_not_awaited()

    async def test_third_submission_uses_reserved_repair_in_upstream_worker(self):
        now = time.monotonic()
        state = SWECaseState(submission_id=2, deadline=now+300, first_edit_deadline=now-300, repair_seconds=120,
                             review_feedback='Fix the missing import; rerun the failing entry point.')
        with tempfile.TemporaryDirectory() as folder:
            engineer = SWEMiniEngineer(terminal=DockerBash('offline'), case_state=state, mas_mode=True,
                                      native_deadline=state.deadline, mini_output_dir=folder)
            async def worker(request, path, seconds):
                self.assertTrue(request['repair_only'])
                self.assertEqual(path.name, 'submission_3')
                self.assertGreater(seconds, 110)
                self.assertLessEqual(seconds, 120)
                self.assertIn('missing import', request['task'])
            with patch.object(SWEMiniEngineer, '_run_worker', new=AsyncMock(side_effect=worker)):
                with patch.object(swe_protocol, 'submit', new=AsyncMock()) as submit:
                    await engineer._react()
                    submit.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
