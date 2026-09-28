"""Regression checks for phase deadlines and evidence handed between roles."""
import asyncio
import json
import subprocess
import time
import unittest
from unittest.mock import AsyncMock, patch
from metagpt.roles.di import swe_protocol
from metagpt.roles.di.reviewer import Reviewer
from metagpt.roles.di.role_zero import RoleZero
from metagpt.roles.di.swe_engineer import SWEBenchEngineer
from metagpt.roles.di.swe_protocol import SWECaseState
from metagpt.roles.di.swe_shell import docker_shell_argv
from metagpt.tools.libs.docker_bash import DockerBash
from metagpt.tools.libs.docker_terminal import DockerTerminal

class TeamRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_heredoc_program_is_preserved_and_failure_visible(self):
        cmd="python3 - <<'PY'\nprint('probe')\nraise SystemExit(7)\nPY"
        argv=docker_shell_argv('container','/app',cmd,10)
        p=subprocess.run(argv[-5:],capture_output=True,text=True)
        self.assertEqual(p.returncode,7)
        self.assertEqual(p.stdout,'probe\n')
        self.assertIn('BASH_ENV=/root/.bashrc',argv)
        self.assertNotIn('-lc',argv)

    def test_first_review_preserves_ninety_second_revision(self):
        state=SWECaseState(first_edit_deadline=1420,deadline=1600,review_seconds=45,repair_seconds=0)
        engineer=SWEBenchEngineer(terminal=DockerBash('offline'),case_state=state,mas_mode=True,native_deadline=1600)
        with patch.object(swe_protocol.time,'monotonic',return_value=1060):
            self.assertEqual(swe_protocol.editing_seconds(engineer),360)
        state.submission_id=1
        with patch.object(swe_protocol.time,'monotonic',return_value=1465):
            self.assertEqual(swe_protocol.editing_seconds(engineer),90)

    async def test_failed_custom_reproduction_survives_submit_and_revision(self):
        state=SWECaseState()
        engineer=SWEBenchEngineer(terminal=DockerBash('offline'),case_state=state,mas_mode=True)
        state.record('action',role=engineer.name,command="python /tmp/repro.py",result={'output':'AssertionError: authors should be removed','returncode':1})
        with patch.object(DockerBash,'collect_patch',new=AsyncMock(return_value='diff --git a/x.py b/x.py\n+fix')):
            await swe_protocol.submit(engineer,'editing time exhausted')
        packet=json.dumps(state.review_packet)
        self.assertIn('authors should be removed',packet)
        self.assertIn('/tmp/repro.py',packet)
        self.assertIn('editing time exhausted',packet)
        self.assertIn('authors should be removed',json.dumps(swe_protocol.collaboration_messages(engineer)))
        self.assertEqual(state.submitted_patch,engineer.output_diff)

    async def test_empty_patch_rejected_without_model(self):
        state=SWECaseState(phase='reviewing',submission_id=1,review_packet={'patch':''})
        reviewer=Reviewer(terminal=DockerTerminal('offline'),case_state=state)
        reviewer._active_submission_id=1
        with patch.object(RoleZero,'_react',new=AsyncMock()) as react:
            await reviewer._react()
            react.assert_not_awaited()
        self.assertEqual(state.phase,'editing')
        self.assertIn('No source code changes',state.review_feedback)

    async def test_review_timeout_yields_without_false_approval(self):
        state=SWECaseState(phase='reviewing',submission_id=1,submitted_patch='diff',review_packet={'patch':'diff'},review_seconds=.01)
        reviewer=Reviewer(terminal=DockerTerminal('offline'),case_state=state)
        reviewer._active_submission_id=1
        async def slow(budget):await asyncio.sleep(1)
        with patch.object(Reviewer,'_run_review',new=AsyncMock(side_effect=slow)):
            await reviewer._react()
        self.assertEqual(state.phase,'done')
        self.assertIsNone(next(e for e in state.events if e['event']=='review_verdict')['approved'])
        self.assertEqual(state.review_cycles,0)
        self.assertEqual(state.review_feedback,'')

if __name__=='__main__':unittest.main()
