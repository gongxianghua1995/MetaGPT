"""Small controlled native / mini / MetaGPT+mini experiment, with common evaluation.

Inputs are trusted local benchmark metadata. Only the normal public task
fields enter model requests; test/gold patches are reserved for the evaluator.
"""
import argparse
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
from metagpt.roles.di.swe_budget import (
    DEFAULT_CASE_MINUTES, add_team_budget_arguments, team_budget_from_args, team_budget_cli_args,
)
ARMS = {'native_single': ('native', False), 'native_mas': ('native', True), 'mini_single': ('mini', False), 'mini_mas': ('mini', True)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--instances-file', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--mini-python', required=True)
    parser.add_argument('--eval-python', required=True)
    parser.add_argument('--minutes', type=int, default=DEFAULT_CASE_MINUTES)
    parser.add_argument('--max-tokens', type=int, default=16384)
    parser.add_argument('--arms', nargs='+', choices=list(ARMS), default=list(ARMS))
    parser.add_argument('--workers', type=int, default=3)
    add_team_budget_arguments(parser)
    args = parser.parse_args()
    try:
        budget = team_budget_from_args(args).validate(args.minutes * 60, mas='mini_mas' in args.arms)
        if args.max_tokens <= 0:
            raise ValueError('--max-tokens must be positive')
    except ValueError as exc:
        parser.error(str(exc))
    selected_arms = {arm: ARMS[arm] for arm in args.arms}
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    instances = [json.loads(line) for line in Path(args.instances_file).read_text().splitlines() if line.strip()]
    # Refuse accidental expensive dataset-scale execution through this diagnostic runner.
    if not 1 <= len(instances) <= 10:
        raise ValueError('Provide 1 to 10 diagnostic instances')
    files = list((ROOT/'metagpt/roles/di').glob('swe_*.py')) + [ROOT/'metagpt/roles/di/reviewer.py',
             ROOT/'tests/metagpt/roles/di/run_swe_agent_for_benchmark.py',
             ROOT/'tests/metagpt/roles/di/run_swebench_pro_eval.py']
    files += [ROOT/'metagpt/tools/libs/docker_terminal.py', ROOT/'metagpt/tools/libs/docker_bash.py', Path(__file__)]
    manifest = dict(instance_ids=[r['instance_id'] for r in instances], arms=selected_arms,
                    minutes=args.minutes, max_tokens=args.max_tokens, coding_model_calls=80, workers=args.workers,
                    team_budget=asdict(budget),
                    generation_network='none', budget_scope='whole case including Leader and Reviewer; setup/evaluation excluded',
                    selection='diagnostic cases; not a random sample or benchmark score',
                    python=sys.executable, mini_python=args.mini_python, eval_python=args.eval_python,
                    source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files})
    (output/'manifest.json').write_text(json.dumps(manifest, indent=2))
    jobs=[]
    for index, instance in enumerate(instances, 1):
        task=output/('case_%d.jsonl'%index)
        instance={k:v for k,v in instance.items() if k not in ('model_patch','model_name_or_path','swe_run_state')}
        task.write_text(json.dumps(instance)+'\n')
        for arm in selected_arms:
            jobs.append((index, task, arm))

    def run_job(job):
        index, task, arm = job
        backend, mas=ARMS[arm]
        folder=output/arm/('case_%d'%index)
        folder.mkdir(parents=True,exist_ok=True)
        command=[sys.executable, str(ROOT/'tests/metagpt/roles/di/run_swe_agent_for_benchmark.py'),
                 '--instances-file',str(task),'--save_folder',str(folder),'--engineer-backend',backend,
                 '--mini-python',args.mini_python,'--max_wait_time_per_case',str(args.minutes),
                 '--model-max-tokens',str(args.max_tokens),'--max-native-steps','80']
        command.extend(team_budget_cli_args(budget))
        if mas:command.append('--mas')
        print('START',arm,index,flush=True)
        with (folder/'generation.log').open('w') as log:
            generation=subprocess.run(command,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
        row=dict(arm=arm,case=index,generation_exit_code=generation.returncode)
        preds=folder/'all_preds.jsonl'
        if preds.exists():
            run_id='backend_%s_%s_%d'%(output.name,arm,index)
            with (folder/'evaluation.log').open('w') as log:
                evaluated=subprocess.run([args.eval_python,str(ROOT/'tests/metagpt/roles/di/run_swebench_pro_eval.py'),
                    '--preds',str(preds),'--run-id',run_id,'--max-workers','1','--timeout','600',
                    '--report-dir',str(folder/'reports')],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
            row['evaluation_exit_code']=evaluated.returncode
            row['run_id']=run_id
            row['reports']=[json.loads(p.read_text()) for p in (folder/'reports').glob('*.json')]
        (folder/'job_result.json').write_text(json.dumps(row,indent=2))
        print('DONE',arm,index,'reports',[(r.get('resolved_instances'),r.get('total_instances')) for r in row.get('reports',[])],flush=True)
        return row
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results=list(pool.map(run_job,jobs))
    (output/'results.json').write_text(json.dumps(results,indent=2))
    print('COMPLETED',output,flush=True)


if __name__=='__main__':
    main()
