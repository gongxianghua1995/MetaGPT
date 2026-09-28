"""Offline regressions for patch recovery and terminal cancellation boundaries."""
import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from metagpt.tools.libs.docker_bash import DockerBash
from metagpt.tools.libs.docker_terminal import DockerTerminal
from metagpt.roles.di.swe_team_leader import SWEBenchTeamLeader
from metagpt.roles.di.swe_protocol import SWECaseState


class PatchRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_patch_survives_unusable_persistent_shell(self):
        class LocalControl(DockerBash):
            async def run_independent(self, cmd, timeout=30):
                proc = await asyncio.create_subprocess_exec('bash', '-c', cmd, cwd=self.cwd,
                                                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await proc.communicate()
                return {'output': stdout.decode(), 'stderr': stderr.decode(), 'exit_code': proc.returncode}
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git','init','-q',tmp],check=True)
            subprocess.run(['git','-C',tmp,'-c','user.name=Test','-c','user.email=test@example.invalid',
                            'commit','--allow-empty','-qm','base'],check=True)
            (Path(tmp)/'image_compat.py').write_text('baseline = 1\n')
            terminal = LocalControl('offline', cwd=tmp)
            terminal.initial_patch = await terminal.collect_patch()
            self.assertEqual(await terminal.collect_patch(), '')
            (Path(tmp)/'fixed.py').write_text('answer = 42\n')
            (Path(tmp)/'patch.txt').write_text('TRANSPORT ARTIFACT\n')
            # This simulates a cancelled/busy shell with unread output. Control
            # collection must never depend on that stream, even as fallback.
            with patch.object(LocalControl,'run_command',new=AsyncMock(side_effect=AssertionError('stale shell'))):
                diff=await terminal.collect_patch()
                self.assertIn('diff --git a/fixed.py b/fixed.py',diff)
                self.assertIn('+answer = 42',diff)
                self.assertNotIn('patch.txt',diff)
                self.assertNotIn('image_compat.py', diff)
                self.assertEqual(await terminal.collect_patch(),diff)
                (Path(tmp)/'image_compat.py').write_text('baseline = 2\n')
                changed = await terminal.collect_patch()
                self.assertIn('image_compat.py', changed)
                self.assertIn('+baseline = 2', changed)
                (Path(tmp)/'.git/index.lock').write_text('')
                with self.assertRaises(RuntimeError):
                    await terminal.collect_patch()

    async def test_docker_terminal_returns_unterminated_tail_at_eof(self):
        reader=asyncio.StreamReader()
        reader.feed_data(b'output without newline')
        reader.feed_eof()
        terminal=DockerTerminal('offline')
        output=await terminal._read_docker_output(SimpleNamespace(stdout=reader),'printf test')
        self.assertEqual(output,'output without newline')

    async def test_leader_yields_during_review(self):
        leader=SWEBenchTeamLeader(terminal=DockerTerminal('offline'),case_state=SWECaseState(phase='reviewing'))
        with patch.object(SWEBenchTeamLeader,'_think_native_toolcall',new=AsyncMock()) as think:
            self.assertFalse(await leader._think())
            self.assertEqual((await leader._react()).content,'')
            think.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
