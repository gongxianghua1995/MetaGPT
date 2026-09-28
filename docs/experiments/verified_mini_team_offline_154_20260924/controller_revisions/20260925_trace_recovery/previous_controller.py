#!/usr/bin/env python3
"""Durable multi-domain MetaGPT+mini full experiment, one attempt per task."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'metagpt/roles/di'))
from swe_budget import DEFAULT_CASE_MINUTES, add_team_budget_arguments, team_budget_from_args, team_budget_cli_args

GEN = ROOT / 'tests/metagpt/roles/di/run_swe_agent_for_benchmark.py'
EVAL = ROOT / 'tests/metagpt/roles/di/run_swebench_pro_eval.py'


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


def run_logged(command, log_path, seconds):
    with log_path.open('a') as log:
        proc = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            return proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            return 124


def api_error_status(log_paths):
    # Never persist provider text: it can contain credentials. Historical
    # transient errors are telemetry, not a reason to discard a prediction.
    terminal = transient = False
    for path in log_paths:
        text = path.read_text(errors='replace') if path.exists() else ''
        terminal |= any(marker in text for marker in (
            'insufficient_quota', 'TokenStatusExhausted', 'Error code: 401',
            'Error code: 402', 'Budget has been exceeded'))
        # Generated patches can import a repository's AuthenticationError.
        # Only provider exception records or model telemetry establish an API
        # authentication failure; a bare source-code symbol must not stop a run.
        terminal |= bool(re.search(
            r'\b(?:openai|litellm)(?:\.\w+)*\.AuthenticationError\s*[:(]', text))
        event_paths = list(path.parent.glob('traces/*/events.jsonl')) if path.name == 'generation.log' else [path.with_name('events.jsonl')]
        for events in event_paths:
            if not events.exists():
                continue
            for line in events.read_text().splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get('event') in ('model_error', 'role_model_interrupted', 'role_model_retry'):
                    terminal |= event.get('error') == 'AuthenticationError'
        transient |= any(marker in text for marker in (
            'RateLimitError', 'rate limit exceeded', 'BadGatewayError',
            'APITimeoutError', 'APIConnectionError', 'Error code: 429', 'Error code: 502'))
    return dict(terminal_api_error=terminal, had_transient_api_error=transient)


def fatal_api_error(log_path):
    return api_error_status([log_path])['terminal_api_error']


def outcome_from_report(report, iid):
    for status in ('resolved', 'unresolved', 'empty_patch', 'error', 'incomplete'):
        if iid in report.get(status + '_ids', []):
            return status
    return 'unknown'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instances-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--benchmark', choices=('pro', 'verified'), default='pro')
    parser.add_argument('--gen-python', default='/home/xhgong/miniconda/envs/metagpt/bin/python')
    parser.add_argument('--mini-python', default='/home/xhgong/miniconda/envs/evomas/bin/python')
    parser.add_argument('--eval-python', default='/home/xhgong/miniconda/envs/evomas/bin/python')
    parser.add_argument('--minutes', type=int, default=DEFAULT_CASE_MINUTES)
    parser.add_argument('--max-tokens', type=int, default=16384)
    parser.add_argument('--eval-workers', type=int, default=2)
    parser.add_argument('--eval-timeout', type=int, default=1800)
    parser.add_argument('--resume', action='store_true')
    add_team_budget_arguments(parser)
    args = parser.parse_args()
    eval_script = EVAL if args.benchmark == 'pro' else ROOT / 'tests/metagpt/roles/di/run_swebench_verified_offline_eval.py'
    budget = team_budget_from_args(args).validate(args.minutes * 60)
    if min(args.max_tokens, args.eval_workers, args.eval_timeout) <= 0:
        parser.error('Token and evaluation limits must be positive')
    rows = read_jsonl(args.instances_file)
    if not rows or len({r['instance_id'] for r in rows}) != len(rows):
        parser.error('Input must contain distinct nonempty task IDs')
    domains = {}
    for row in rows:
        domain = row['experiment_domain']
        if not domain.replace('_', '').isalnum():
            parser.error('Invalid domain name')
        domains.setdefault(domain, []).append(row)
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    controller_lock = (out / 'controller.lock').open('a')
    fcntl.flock(controller_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    (out / 'controller.pid').write_text(str(os.getpid()) + '\n')
    sources = sorted(set(list((ROOT / 'metagpt/roles/di').glob('swe_*.py')) + [
        ROOT / 'metagpt/roles/di/reviewer.py', ROOT / 'metagpt/utils/swe_requirements.py',
        ROOT / 'metagpt/tools/libs/docker_terminal.py', ROOT / 'metagpt/tools/libs/docker_bash.py',
        GEN, eval_script, Path(__file__).resolve()]))
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    manifest = dict(instance_ids=[r['instance_id'] for r in rows],
                    domains={d: len(tasks) for d, tasks in domains.items()},
                    input_sha256=hashlib.sha256(args.instances_file.read_bytes()).hexdigest(),
                    architecture='MetaGPT Leader + mini-swe-agent Engineer + MetaGPT Reviewer',
                    minutes=args.minutes, engineer_max_tokens=args.max_tokens, team_budget=asdict(budget),
                    generation_workers=len(domains), eval_workers=args.eval_workers,
                    eval_timeout=args.eval_timeout, coding_model_calls=80,
                    generation_network='none', evaluation_network='none', history='base-only Git objects',
                    gen_python=args.gen_python, mini_python=args.mini_python, eval_python=args.eval_python,
                    source_sha256=hashes)
    if args.benchmark == 'verified':
        manifest['benchmark'] = 'SWE-bench Verified'
    manifest_path = out / 'manifest.json'
    if manifest_path.exists():
        if not args.resume or json.loads(manifest_path.read_text()) != manifest:
            parser.error('Existing experiment requires --resume with identical inputs, budgets and source hashes')
    else:
        atomic_json(manifest_path, manifest)
        for p in sources:
            dest = out / 'source_at_run' / p.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())
    state_path = out / 'progress.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    lock = threading.Lock()
    stop = threading.Event()
    eval_slots = threading.Semaphore(args.eval_workers)

    def summarize():
        return {'planned': len(rows), 'completed': sum(v.get('phase')=='done' for v in state.values()),
                'resolved': sum(v.get('outcome')=='resolved' for v in state.values()),
                'phases': dict(Counter(v.get('phase') for v in state.values())),
                'outcomes': dict(Counter(v.get('outcome') for v in state.values() if v.get('outcome'))),
                'domains': {d: {'planned': len(tasks),
                    'completed': sum(v.get('domain')==d and v.get('phase')=='done' for v in state.values()),
                    'resolved': sum(v.get('domain')==d and v.get('outcome')=='resolved' for v in state.values())}
                    for d,tasks in domains.items()}, 'stopped': stop.is_set()}

    def save(iid, **values):
        with lock:
            state.setdefault(iid, {}).update(values, updated=datetime.now(timezone.utc).isoformat())
            atomic_json(state_path, state)
            atomic_json(out / 'summary.json', summarize())
            print(iid, json.dumps(values), flush=True)

    def sources_unchanged():
        return all((ROOT/p).exists() and hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in hashes.items())

    def clean_generation_containers(folder):
        for trace in folder.glob('traces/*/events.jsonl'):
            for event in read_jsonl(trace):
                name = event.get('container', '')
                if event.get('event')=='network_isolated' and name.startswith('sweb-'):
                    subprocess.run(['docker','rm','-f',name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)

    def run_domain(domain, tasks):
        (out/domain).mkdir(exist_ok=True)
        for index, row in enumerate(tasks, 1):
            iid = row['instance_id']
            if state.get(iid, {}).get('phase')=='done':
                continue
            if not sources_unchanged():
                stop.set(); save(iid, domain=domain, phase='blocked_source_changed'); break
            folder = out / domain / f'case_{index:03d}'
            folder.mkdir(parents=True, exist_ok=True)
            task = folder / 'instance.jsonl'
            task.write_text(json.dumps(row, ensure_ascii=False)+'\n')
            preds = folder / 'all_preds.jsonl'
            generated = read_jsonl(preds)
            if not generated:
                if stop.is_set():
                    save(iid, domain=domain, phase='blocked_api', prediction_ready=False)
                    continue
                save(iid, domain=domain, phase='generating', folder=str(folder.relative_to(out)))
                command = [args.gen_python, str(GEN), '--instances-file', str(task), '--save_folder', str(folder),
                           '--engineer-backend', 'mini', '--mini-python', args.mini_python, '--mas', '--use_docker',
                           '--max_wait_time_per_case', str(args.minutes), '--model-max-tokens', str(args.max_tokens),
                           '--max-native-steps', '80', *team_budget_cli_args(budget)]
                rc = run_logged(command, folder/'generation.log', args.minutes*60+300)
                if rc==124:
                    clean_generation_containers(folder)
                api_logs=[folder/'generation.log', *folder.glob('traces/*/mini/submission_*/worker.log')]
                api_status = api_error_status(api_logs)
                generated = read_jsonl(preds)
                ready = len(generated)==1 and generated[0].get('instance_id')==iid and isinstance(generated[0].get('model_patch'), str)
                if api_status['terminal_api_error']:
                    stop.set()  # Pause future generation; drain completed predictions.
                save(iid, generation_returncode=rc, prediction_ready=ready, **api_status)
                if not ready:
                    if api_status['terminal_api_error']:
                        save(iid, phase='blocked_api')
                    else:
                        save(iid, phase='done', outcome='generation_error')
                    continue
            elif len(generated)!=1 or generated[0].get('instance_id')!=iid:
                raise RuntimeError('Prediction/instance mismatch: '+iid)
            events = [e for trace in folder.glob('traces/*/events.jsonl') for e in read_jsonl(trace)]
            if not all(any(e['event']==name for e in events) for name in ('network_isolated','history_isolated','case_started')):
                save(iid, phase='done', outcome='setup_error'); continue
            save(iid, network_isolated=True, history_isolated=True, prediction_ready=True)
            with eval_slots:
                if not sources_unchanged():
                    stop.set(); save(iid, phase='blocked_source_changed'); break
                save(iid, phase='evaluating')
                run_id=f'{out.name}_{hashlib.sha256(str(out).encode()).hexdigest()[:8]}_{domain}_{index:03d}'
                reports=folder/'reports'
                eval_args = ['--instances-file', str(task)] if args.benchmark == 'verified' else []
                rc=run_logged([args.eval_python,str(eval_script),'--preds',str(preds),'--run-id',run_id,
                               '--max-workers','1','--timeout',str(args.eval_timeout),'--report-dir',str(reports), *eval_args],
                              folder/'evaluation.log', args.eval_timeout+300)
                if rc==124:
                    subprocess.run(['docker','rm','-f',f'sweb.eval.{iid.lower()}.{run_id}'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
                paths=list(reports.glob('*.json'))
                if rc or len(paths)!=1:
                    save(iid, phase='evaluation_error', evaluation_returncode=rc); continue
                report=json.loads(paths[0].read_text())
                outcome=outcome_from_report(report,iid)
                save(iid, phase='done', outcome=outcome, evaluation_returncode=rc, report=str(paths[0].relative_to(out)))
        # Per-domain predictions remain separate per task during concurrent work.
        merged=[r for p in sorted((out/domain).glob('case_*/all_preds.jsonl')) for r in read_jsonl(p)]
        (out/domain/'all_preds.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in merged))

    with lock:
        atomic_json(out/'summary.json',summarize())
    print('START', json.dumps(manifest['domains']), 'network=none generation+evaluation', flush=True)
    with ThreadPoolExecutor(max_workers=len(domains)) as pool:
        futures=[pool.submit(run_domain,d,tasks) for d,tasks in domains.items()]
        for future in futures:
            future.result()
    merged=[r for p in out.glob('*/case_*/all_preds.jsonl') for r in read_jsonl(p)]
    (out/'all_preds.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in merged))
    with lock:
        atomic_json(out/'summary.json',summarize())
    print('FINISHED',json.dumps(summarize()),flush=True)
    controller_lock.close()


if __name__=='__main__':
    main()
