"""Isolated mini-swe-agent worker; deliberately imports no MetaGPT dependencies.

Run with a Python environment containing mini-swe-agent 2.4.6. Credentials
arrive via OPENAI_API_KEY/OPENAI_API_BASE, never through the request file.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

import minisweagent
import yaml
from swe_shell import docker_shell_argv
from swe_checks import CHECK_SCRIPT, check_evidence
from swe_execution import EditingProgress
from swe_rate_limit import wait_for_model_slot
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.litellm_model import LitellmModel


def main(request_path):
    request = json.loads(Path(request_path).read_text())
    output_dir = Path(request['output_dir'])
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + request['seconds']
    progress_state = EditingProgress(request['seconds'], repair_only=request.get('repair_only', False))

    def record(event, **details):
        with (output_dir / 'events.jsonl').open('a') as stream:
            stream.write(json.dumps({'event': event, 'worker_elapsed_seconds': round(time.monotonic()-started, 3),
                                     **details}, ensure_ascii=False) + '\n')

    class AttachedEnvironment(LocalEnvironment):
        """Fresh shell per upstream action, attached to the runner's existing container."""
        def shell(self, command, timeout=60):
            seconds = max(1, min(timeout, int(deadline-time.monotonic())))
            try:
                proc = subprocess.run(
                    docker_shell_argv(request['container'], request['cwd'], command, seconds),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace', timeout=seconds+5)
                return {'output': proc.stdout, 'returncode': proc.returncode, 'exception_info': ''}
            except subprocess.TimeoutExpired as exc:
                raw = exc.output or b''
                return {'output': raw.decode(errors='replace') if isinstance(raw, bytes) else raw,
                        'returncode': 124, 'exception_info': 'Command timed out'}

        def execute(self, action, **kwargs):
            command = action.get('command', '')
            snapshot = '(git diff HEAD --binary; git ls-files --others --exclude-standard -z | xargs -0 -r git hash-object --) | sha256sum'
            before = self.shell(snapshot)
            record('action_started', command=command)
            output = self.shell(command)
            after = self.shell(snapshot)
            evidence = check_evidence(command, output)
            record('action', command=command, result=output, tree_before=before.get('output','').strip(), tree_after=after.get('output','').strip(), check=evidence)
            changed = before['returncode'] == after['returncode'] == 0 and before['output'] != after['output']
            if changed:
                record('repository_changed')
            progress_state.observe(changed=changed, evidence=evidence)
            if re.search(r'\b(pytest|unittest|npm test|yarn test|go test|make test|ansible-test)\b', command):
                record('verification_command', command=command, result=output,
                       classification='command heuristic; does not establish correctness')
            self._check_finished(output)
            progress = ('\n[Verification evidence: '+json.dumps(evidence)+']') if evidence else ''
            progress += '\n[' + progress_state.guidance(time.monotonic()-started) + ']'
            return {**output, 'output': output['output'] + progress}

    class ObservedModel(LitellmModel):
        def _query(self, messages, **kwargs):
            record('model_request', messages=messages)
            try:
                slot_wait = wait_for_model_slot()
                if slot_wait:
                    record('model_rate_wait', seconds=round(slot_wait, 3))
                response = super()._query(messages, **kwargs)
            except Exception as exc:
                record('model_error', error=type(exc).__name__)
                raise
            record('model_usage', usage=response.usage.model_dump() if response.usage else {},
                   finish_reason=response.choices[0].finish_reason)
            return response

    cfg_path = Path(minisweagent.__file__).parent / 'config/benchmarks/swebench.yaml'
    cfg = yaml.safe_load(cfg_path.read_text())
    model_cfg = cfg['model']
    model_cfg.update(model_name='openai/' + request['model'], cost_tracking='ignore_errors',
                     model_kwargs={'temperature': request['temperature'], 'max_tokens': request['max_tokens'],
                                   'timeout': max(1, request['seconds']), 'num_retries': 0})
    agent_cfg = cfg['agent']
    agent_cfg.update(step_limit=request['steps'], cost_limit=0,
                     wall_time_limit_seconds=max(1, int(request['seconds'])), output_path=output_dir/'trajectory.json')
    # Preserve upstream workflow and tool protocol, adapting only the workdir
    # and the network policy of the shared benchmark container.
    agent_cfg['instance_template'] = agent_cfg['instance_template'].replace('/testbed', request['cwd'])
    agent_cfg['instance_template'] += '\nThe container has no external network. Use local dependencies.\n'
    agent_cfg['instance_template'] += 'Include new source files with git add -N before generating a diff. Keep reproduction files in /tmp.\n'
    agent_cfg['instance_template'] += 'For tests, use swe-check with the actual test command and no output-filter pipelines, e.g. swe-check python -m pytest path/to/test.py -q. It retains the real status and bounds displayed output. Zero tests means unverified. After a focused fix and verification, submit promptly; do not escalate to whole-repository builds.\n'
    agent_cfg['instance_template'] += 'You have {{wall_time_limit_seconds}} seconds for this editing phase, including verification and submission.\n'
    agent_cfg['instance_template'] += ('This is a team checkpoint, not a request for exhaustive research. Map each public requirement to the source change and a focused check; distinguish input types, operations, and error paths. '
        'Use the Leader and repository_facts to avoid rediscovery. Implement a small runnable change within the first three minutes, then iterate. '
        'After each edit, check module loading AND invoke the changed entry point before unrelated searches. '
        'Before submission, list any public requirements still unimplemented or unverified. Do not claim coverage based only on old passing tests.\n')
    if request.get('repair_only'):
        agent_cfg['instance_template'] += 'FINAL REPAIR WINDOW: prioritize the specific failing commands and files in the latest review. Make the minimal correction, rerun focused checks, and submit.\n'
    env = AttachedEnvironment(cwd=request['cwd'])
    installed = env.shell('printf %s '+shlex.quote(CHECK_SCRIPT)+' > /usr/local/bin/swe-check && chmod +x /usr/local/bin/swe-check')
    record('check_helper_installed', returncode=installed['returncode'])
    agent = DefaultAgent(ObservedModel(**model_cfg), env, **agent_cfg)
    record('worker_started', mini_version=minisweagent.__version__, agent_class='minisweagent.agents.default.DefaultAgent',
           seconds=request['seconds'], steps=request['steps'])
    try:
        result = agent.run(request['task'])
    except Exception as exc:
        # Detailed traceback stays in the upstream trajectory. Avoid echoing
        # API exception representations (which can contain credentials).
        result = {'exit_status': type(exc).__name__}
    result.update(n_calls=agent.n_calls, elapsed_seconds=time.monotonic()-started)
    (output_dir/'result.json').write_text(json.dumps(result))
    record('worker_finished', exit_status=result.get('exit_status'), n_calls=agent.n_calls)


if __name__ == '__main__':
    main(sys.argv[1])
