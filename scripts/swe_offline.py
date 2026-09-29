#!/usr/bin/env python3
"""Portable offline SWE launcher. `check` never starts models or containers."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

PROJECT = 'MetaGPT'
ROOT = Path(__file__).resolve().parents[1]
REPOS = {'django/django': 'django', 'matplotlib/matplotlib': 'matplotlib',
         'sphinx-doc/sphinx': 'sphinx', 'sympy/sympy': 'sympy',
         'ansible/ansible': 'ansible', 'flipt-io/flipt': 'flipt',
         'internetarchive/openlibrary': 'openlibrary', 'protonmail/webclients': 'webclients'}
# Imports and real argparse validation are allowed; sockets and runtime are not.
GUARD = """
import socket
def blocked(*a, **kw):
    raise RuntimeError('Network access is forbidden during CLI smoke checks')
socket.socket.connect = blocked
socket.create_connection = blocked
"""
PARSE_PROBE = GUARD + """
import argparse, runpy, sys
original = argparse.ArgumentParser.parse_args
def checked(self, *args, **kwargs):
    original(self, *args, **kwargs)
    print('SWE_CLI_PARSE_OK')
    raise SystemExit(0)
argparse.ArgumentParser.parse_args = checked
script = sys.argv[1]
sys.argv = sys.argv[1:]
runpy.run_path(script, run_name='__main__')
"""


def read_rows(path):
    text = path.read_text()
    if text.lstrip().startswith('['):
        rows = json.loads(text)
    else:
        rows = [json.loads(line) for line in text.split('\n') if line.strip()]
    if not rows or not all(isinstance(r, dict) for r in rows):
        raise ValueError('Expected a nonempty JSON array or JSONL file')
    return rows


def interpreter(value):
    p = Path(shutil.which(value) or value).absolute()
    if not p.is_file() or not os.access(p, os.X_OK):
        raise ValueError('Python interpreter not executable: ' + str(p))
    return str(p)


def checked(command, env, marker=None):
    result = subprocess.run(command, cwd=ROOT, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if result.returncode or (marker and marker not in result.stdout):
        # Provider configuration must never be printed by a failed import.
        raise RuntimeError('Check failed for ' + shlex.join(command[:2]) +
                           '; exit=' + str(result.returncode) +
                           '. Verify dependencies/configuration using the guide.')
    return result.stdout


def cli_check(command, env):
    output = checked([command[0], '-c', PARSE_PROBE, *command[1:]], env,
                     marker='SWE_CLI_PARSE_OK')
    return True


def select_rows(args):
    source = read_rows(args.data)
    mapped = {}
    for r in source:
        iid = r.get('instance_id') or r.get('id')
        if not iid or iid in mapped:
            raise ValueError('Input IDs must be present and unique')
        mapped[iid] = r
    split = ROOT / 'scripts/swe_offline_ids' / (args.benchmark + '.txt')
    ids = [s for s in split.read_text().splitlines() if s]
    missing = set(ids) - set(mapped)
    if missing:
        raise ValueError(f'Input lacks {len(missing)} IDs from the committed {args.benchmark} split')
    selected = []
    counts = Counter()
    for iid in ids:
        row = dict(mapped[iid])
        meta = row.get('metadata', row)
        domain = REPOS.get(meta.get('repo', '').lower())
        if not domain:
            raise ValueError('Unknown repository for ' + iid)
        if args.domain and domain != args.domain:
            continue
        if args.limit_per_domain and counts[domain] >= args.limit_per_domain:
            continue
        required = (['query', 'metadata'] if PROJECT == 'EvoMAS' else
                    ['instance_id', 'repo', 'base_commit', 'problem_statement', 'test_patch'])
        if PROJECT != 'EvoMAS':
            required += (['dockerhub_tag', 'fail_to_pass', 'pass_to_pass', 'before_repo_set_cmd']
                         if args.benchmark == 'pro' else ['FAIL_TO_PASS', 'PASS_TO_PASS', 'version'])
            if PROJECT == 'MetaGPT' and args.benchmark == 'verified':
                required += ['image', 'eval_script', 'eval_type', 'log_parser', 'image_snapshot_commit']
        if any(k not in row for k in required):
            raise ValueError(f'{iid}: missing prepared input fields: ' + ','.join(k for k in required if k not in row))
        row['experiment_domain'] = domain
        selected.append(row)
        counts[domain] += 1
    if not selected:
        raise ValueError('No tasks selected; check --domain')
    return selected, dict(counts)


def images_for(rows, benchmark):
    images = set()
    for row in rows:
        meta = row.get('metadata', row)
        iid = meta['instance_id']
        if benchmark == 'pro':
            tag = meta['dockerhub_tag']
            images.add(tag if tag.startswith('jefzda/') else 'jefzda/sweap-images:' + tag[:128])
        else:
            images.add(meta.get('image') or 'swebench/sweb.eval.x86_64.' + iid.replace('__', '_1776_').lower() + ':latest')
            if PROJECT == 'GPTSwarm':
                images.add('sweb.eval.x86_64.' + iid + ':latest')
    return sorted(images)


def commands(args, rows, input_path):
    out = args.output
    if PROJECT == 'GPTSwarm':
        batch = str(ROOT / 'experiments/run_swebench_mini_batch.py')
        config = args.config or ROOT / 'config/swebench' / ('mini_pro_fixed.json' if args.benchmark == 'pro' else 'mini_fixed.json')
        return [
            [args.python, batch, '--prepare-only', '--batch-dir', str(out), '--data-path', str(input_path),
             '--config', str(config), '--mini-python', args.mini_python],
            [args.python, str(out/'source/experiments/run_swebench_mini_batch.py'), '--batch-dir', str(out)],
        ]
    if PROJECT == 'MetaGPT':
        cmd = [args.python, str(ROOT/'tests/metagpt/roles/di/run_swe_full_experiment.py'),
               '--benchmark', args.benchmark, '--instances-file', str(input_path), '--output', str(out),
               '--gen-python', args.python, '--mini-python', args.mini_python, '--eval-python', args.eval_python,
               '--minutes', '25', '--max-tokens', '16384', '--leader-max-tokens', '16384',
               '--reviewer-max-tokens', '16384', '--leader-seconds', '180', '--reviewer-seconds', '180',
               '--first-edit-fraction', '0.6', '--repair-seconds', '0', '--eval-workers', str(args.workers),
               '--eval-timeout', '1800']
        if args.mode == 'resume':cmd.append('--resume')
        return [cmd]
    result = []
    for domain in sorted({r['experiment_domain'] for r in rows}):
        if args.stage != 'evaluate':
            result.append([args.python, str(ROOT/'main.py'), '--dataset', 'swe_bench_'+args.benchmark,
                           '--baseline', 'chatdev', '--task-ids-file', str(out/'task_ids'/f'{domain}.txt'),
                           '--llm-as-judge', 'none', '--task-timeout', '7200', '--generate-only',
                           '--output-dir', str(out/f'chatdev_{domain}')])
        if args.stage != 'generate':
            entry = 'run_swebench_pro_eval.py' if args.benchmark == 'pro' else 'run_swebench_docker_eval.py'
            command = [args.eval_python, str(ROOT/'scripts'/entry), '--domain', domain,
                       '--output-root', str(out), '--report-dir', str(out/'reports'),
                       '--model-name', args.model_name, '--max-workers', '1', '--timeout', '1800',
                       '--instance-ids', ','.join(r['id'] for r in rows if r['experiment_domain']==domain)]
            if args.benchmark == 'verified':
                command += ['--run-id-prefix', out.name, '--dataset-path', str(args.eval_data)]
            result.append(command)
    return result


def check(args, rows, plan, env):
    probe = GUARD + """
import json, platform
from importlib.metadata import version
from minisweagent.agents.default import DefaultAgent
from minisweagent.models.litellm_model import LitellmModel
import swebench.harness.run_evaluation
print(json.dumps({'python': platform.python_version(), 'mini-swe-agent': version('mini-swe-agent'), 'swebench': version('swebench')}))
"""
    versions = json.loads(checked([args.mini_python, '-c', probe], env).strip().split('\n')[-1])
    checked([args.eval_python, '-c', GUARD+'\nimport swebench.harness.run_evaluation'], env)
    if PROJECT == 'EvoMAS':
        if args.data != ROOT/'dataset'/('swe_bench_'+args.benchmark)/'test.json':
            raise ValueError('EvoMAS loader uses repository-relative dataset paths; place inputs under dataset/swe_bench_<benchmark>/')
        config_probe = "import yaml; c=yaml.safe_load(open('mas_pools/swebench/chatdev.yaml')); assert c['execution']['use_docker'] and c['execution']['network_isolated']; assert c['backend']=='minisweagent'"
        checked([args.python, '-c', config_probe], env)
        if not (ROOT/'dataset'/('swe_bench_'+args.benchmark)/'tasks_map.json').is_file():
            raise ValueError('Missing tasks_map.json for evaluation')
    if PROJECT == 'EvoMAS' and args.benchmark == 'verified' and args.stage != 'generate':
        evaluation_rows = read_rows(args.eval_data)
        evaluation_ids = {r['instance_id'] for r in evaluation_rows}
        if not {r['id'] for r in rows} <= evaluation_ids:
            raise ValueError('Verified evaluation input does not cover selected generation IDs')
    if PROJECT == 'MetaGPT':
        sys.path.insert(0, str(ROOT/'metagpt/roles/di'))
        from swe_budget import SWETeamBudget
        SWETeamBudget().validate(25*60)
    for command in plan:
        command = list(command)
        if PROJECT == 'GPTSwarm' and '/source/experiments/' in command[1]:
            command[1] = str(ROOT/'experiments/run_swebench_mini_batch.py')
        cli_check(command, env)
    # Check the nested generation entry, not only the batch controller.
    if PROJECT == 'MetaGPT':
        cli_check([args.python, str(ROOT/'tests/metagpt/roles/di/run_swe_agent_for_benchmark.py'),
                   '--instances-file', str(args.data), '--save_folder', str(args.output/'probe'),
                   '--engineer-backend', 'mini', '--mini-python', args.mini_python, '--mas', '--use_docker',
                   '--max_wait_time_per_case', '25'], env)
        evaluator = 'run_swebench_pro_eval.py' if args.benchmark=='pro' else 'run_swebench_verified_offline_eval.py'
        command = [args.eval_python, str(ROOT/'tests/metagpt/roles/di'/evaluator), '--preds', str(args.output/'all_preds.jsonl'),
                   '--run-id', 'cli_check', '--report-dir', str(args.output/'reports')]
        if args.benchmark=='verified':command += ['--instances-file', str(args.data)]
        cli_check(command, env)
    elif PROJECT == 'GPTSwarm':
        cli_check([args.python, str(ROOT/'experiments/run_swebench_mini_swarm.py'),
                   '--data-path', str(args.data), '--instance-id', rows[0]['instance_id'],
                   '--mini-python', args.mini_python, '--output-dir', str(args.output/'probe')], env)
        config = args.config or ROOT/'config/swebench'/('mini_pro_fixed.json' if args.benchmark=='pro' else 'mini_fixed.json')
        data = json.loads(Path(config).read_text())
        if data.get('task_seconds', 0) <= 0 or not data.get('phases'):
            raise ValueError('Invalid fixed-team budget configuration')
    required = images_for(rows, args.benchmark)
    listing = checked(['docker', 'image', 'ls', '--format', '{{.Repository}}:{{.Tag}}'], env)
    missing = set(required) - set(listing.splitlines())
    if missing:
        raise ValueError(f'{len(missing)} local Docker images missing; first: {sorted(missing)[0]}. Prepare images before offline execution.')
    return {'dependencies': versions, 'images_checked': len(required), 'cli_commands_checked': len(plan) + (2 if PROJECT == 'MetaGPT' else 1 if PROJECT == 'GPTSwarm' else 0),
            'model_calls': 0, 'containers_started': 0,
            'scope': 'Imports, real CLI parsing, input selection, budget/config and read-only image availability; API quota and container execution not tested.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['check', 'run', 'resume'])
    p.add_argument('--benchmark', choices=['verified', 'pro'], required=True)
    p.add_argument('--data', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--python', default=os.environ.get('SWE_PYTHON', sys.executable))
    p.add_argument('--mini-python', default=os.environ.get('MINISWE_PYTHON'))
    p.add_argument('--eval-python', default=os.environ.get('SWE_EVAL_PYTHON'))
    p.add_argument('--domain', choices=sorted(REPOS.values()))
    p.add_argument('--limit-per-domain', type=int, default=0, help='0 selects the full committed test split')
    p.add_argument('--workers', type=int, default=2, help='EvoMAS parallel domains / MetaGPT eval slots; GPTSwarm is fixed one queue per repository')
    p.add_argument('--report', type=Path, help='Write a credential-free check report')
    if PROJECT == 'GPTSwarm':p.add_argument('--config', type=Path)
    if PROJECT == 'EvoMAS':
        p.add_argument('--stage', choices=['all', 'generate', 'evaluate'], default='all')
        p.add_argument('--model-name', default='chatdev_DeepSeek-V4-Flash-0731')
        p.add_argument('--eval-data', type=Path, default=ROOT/'dataset/swe_bench_verified/eval.jsonl', help='Prepared official Verified evaluation JSONL')
    args = p.parse_args()
    args.output = args.output.absolute()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.output.name):
        p.error('Output directory basename must be safe for Docker run IDs (letters, digits, dot, dash, underscore)')
    if args.workers < 1 or args.limit_per_domain < 0:p.error('Workers must be positive and limit nonnegative')
    args.python = interpreter(args.python)
    args.mini_python = interpreter(args.mini_python or args.python)
    args.eval_python = interpreter(args.eval_python or args.mini_python)
    if PROJECT == 'EvoMAS':default_data=ROOT/'dataset'/('swe_bench_'+args.benchmark)/'test.json'
    elif PROJECT == 'GPTSwarm':default_data=ROOT/'outputs/swebench'/('pro_test_216.json' if args.benchmark=='pro' else 'swebench_verified_test_154.json')
    else:default_data=ROOT/'workspace/inputs'/(args.benchmark+'.jsonl')
    args.data = (args.data or default_data).absolute()
    rows, domains = select_rows(args)
    env = dict(os.environ, HF_HUB_OFFLINE='1', HF_DATASETS_OFFLINE='1', PYTHONUNBUFFERED='1',
               PYTHONPATH=str(ROOT)+os.pathsep+os.environ.get('PYTHONPATH', ''))
    if PROJECT == 'MetaGPT':
        env.setdefault('METAGPT_SWE_MODEL_MIN_INTERVAL_SECONDS', '4.2')
        env.setdefault('METAGPT_SWE_MODEL_PACER_PATH', str(Path(tempfile.gettempdir())/'metagpt_swe_model_request_pacer'))
    with tempfile.TemporaryDirectory(prefix='swe-launch-check-') as tmp:
        input_path = Path(tmp)/'dataset.json'
        input_path.write_text(json.dumps(rows))
        if PROJECT == 'MetaGPT':input_path=args.output/'instances.jsonl'
        plan = commands(args, rows, input_path)
        if args.mode == 'resume':
            if PROJECT == 'EvoMAS':p.error('EvoMAS has no durable generation resume; use a new output and --domain or --stage evaluate')
            if not (args.output/'manifest.json').is_file():p.error('Resume needs an existing manifest')
            if PROJECT == 'GPTSwarm':
                if json.loads((args.output/'dataset.json').read_text()) != rows:
                    raise ValueError('Resume selection differs from the frozen dataset')
                plan=plan[1:]
        elif args.output.exists() and not (PROJECT=='EvoMAS' and args.stage=='evaluate'):
            p.error('Output already exists; choose a new directory (or resume an existing batch)')
        checks = check(args, rows, plan, env)
        report = {'project': PROJECT, 'benchmark': args.benchmark, 'tasks': len(rows), 'domains': domains,
                  'input_sha256': hashlib.sha256(args.data.read_bytes()).hexdigest(), **checks}
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        if args.mode == 'check':return
        if PROJECT == 'MetaGPT':
            args.output.mkdir(parents=True, exist_ok=True)
            content=''.join(json.dumps(r, ensure_ascii=True)+'\n' for r in rows)
            if args.mode=='resume' and input_path.read_text()!=content:
                raise ValueError('Resume input differs from the original frozen selection')
            if args.mode!='resume':input_path.write_text(content)
        elif PROJECT == 'EvoMAS':
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output/'task_ids').mkdir(exist_ok=True)
            for domain in domains:
                (args.output/'task_ids'/f'{domain}.txt').write_text(''.join(r['id']+'\n' for r in rows if r['experiment_domain']==domain))
        if PROJECT == 'EvoMAS':
            def run_domain(domain):
                commands_for_domain=[c for c in plan if ('--domain' in c and c[c.index('--domain')+1]==domain) or str(args.output/f'chatdev_{domain}') in c]
                with (args.output/(domain+'.log')).open('a') as log:
                    for c in commands_for_domain:
                        if '--domain' in c:
                            patch_dir = args.output/f'chatdev_{domain}'/'output_selected'/('swe_bench_'+args.benchmark)/args.model_name
                            missing = [r['id'] for r in rows if r['experiment_domain']==domain and not (patch_dir/(r['id']+'.txt')).is_file()]
                            if missing:
                                raise RuntimeError(f'Missing {len(missing)} generated patch files under {patch_dir}; generation is incomplete or --model-name is wrong')
                        subprocess.run(c, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                list(pool.map(run_domain, sorted(domains)))
        else:
            for command in plan:
                subprocess.run(command, cwd=ROOT, env=env, check=True)
                if PROJECT == 'GPTSwarm' and '--prepare-only' in command:
                    manifest_path = args.output/'manifest.json'
                    manifest = json.loads(manifest_path.read_text())
                    manifest['pause_on_api_budget_exhaustion'] = True
                    manifest_path.write_text(json.dumps(manifest, indent=2)+'\n')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, FileNotFoundError, subprocess.SubprocessError) as error:
        print('ERROR: '+str(error), file=sys.stderr)
        raise SystemExit(1)
