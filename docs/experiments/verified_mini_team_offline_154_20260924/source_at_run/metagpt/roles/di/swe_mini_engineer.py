"""MetaGPT role adapter around the installed open-source mini-swe-agent loop."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from metagpt.actions.di.run_command import RunCommand
from metagpt.roles.di.swe_engineer import SWEBenchEngineer
from metagpt.roles.di import swe_protocol
from metagpt.schema import AIMessage


class SWEMiniEngineer(SWEBenchEngineer):
    mini_python: str = sys.executable
    mini_output_dir: str = ''

    async def _run_worker(self, request, folder, seconds):
        request_path = folder / 'request.json'
        request_path.write_text(json.dumps(request, ensure_ascii=False))
        env = os.environ.copy()
        env['OPENAI_API_KEY'] = self.config.llm.api_key
        env['OPENAI_API_BASE'] = self.config.llm.base_url
        env['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
        worker = Path(__file__).with_name('swe_mini_worker.py')
        with (folder / 'worker.log').open('w') as log:
            proc = await asyncio.create_subprocess_exec(self.mini_python, str(worker), str(request_path),
                                                        env=env, stdout=log, stderr=log)
            try:
                await asyncio.wait_for(proc.wait(), timeout=seconds)
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()

    async def _react(self):
        self._maybe_rearm_on_feedback()
        state = self.case_state
        if self._force_done or (state and state.phase == 'done'):
            return AIMessage(content='', cause_by=RunCommand)
        seconds = swe_protocol.editing_seconds(self)
        seconds = seconds if seconds is not None else 600
        steps = self.max_native_steps - self._act_count
        if seconds <= 1 or steps <= 0:
            if state and state.submission_id:
                state.finish('editing budget exhausted before revision; current patch preserved')
                return AIMessage(content='', cause_by=RunCommand)
            await swe_protocol.submit(self, 'editing budget exhausted')
            return AIMessage(content='', cause_by=RunCommand)
        folder = Path(self.mini_output_dir) / ('submission_%d' % (state.submission_id + 1 if state else 1))
        folder.mkdir(parents=True, exist_ok=True)
        task = self.task_requirements + '\n\n' + '\n\n'.join(m['content'] for m in swe_protocol.collaboration_messages(self))
        request = dict(task=task, container=self.terminal.container_name, cwd=self.terminal.cwd,
                       output_dir=str(folder.resolve()), seconds=seconds, steps=steps,
                       model=self.config.llm.model, temperature=self.config.llm.temperature,
                       max_tokens=self.config.llm.max_token,
                       repair_only=bool(state and state.submission_id >= 2 and state.repair_seconds))
        if state:
            state.record('editing_started', role=self.name, backend='mini', seconds=seconds)
        phase_offset = time.monotonic() - state.started_at if state else 0
        reason = 'mini worker exited without result'
        try:
            await self._run_worker(request, folder, seconds)
        except asyncio.TimeoutError:
            reason = 'mini editing time exhausted'
        finally:
            events = folder / 'events.jsonl'
            calls = 0
            if events.exists():
                for line in events.read_text().splitlines():
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # interrupted final write only
                    name = event.pop('event')
                    calls += name == 'model_request'
                    self._edits_count += name == 'repository_changed'
                    if state:
                        state.record(name, role=self.name, backend='mini',
                                     elapsed_seconds=round(phase_offset + event['worker_elapsed_seconds'], 3), **event)
            self._act_count += calls
        result_path = folder/'result.json'
        if result_path.exists():
            result = json.loads(result_path.read_text())
            reason = 'mini: ' + result.get('exit_status', 'unknown')
        # Submit the actual shared tree through MetaGPT's explicit lifecycle.
        # A worker timeout/limit is recorded as such, never as successful repair.
        await swe_protocol.submit(self, reason)
        return AIMessage(content='', cause_by=RunCommand)
