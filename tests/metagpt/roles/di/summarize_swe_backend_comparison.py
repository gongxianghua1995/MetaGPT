"""Read-only progress/outcome summary for the three-arm SWE diagnostic."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path


def read_jsonl(path):
    rows=[]
    if path.exists():
        for line in path.read_text().splitlines():
            try: rows.append(json.loads(line))
            except ValueError: pass  # a worker may be writing the final line
    return rows


def summarize(root):
    rows=[]
    for arm in ('native_single','mini_single','mini_mas'):
        for folder in sorted((root/arm).glob('case_*')):
            predictions=read_jsonl(folder/'all_preds.jsonl')
            events=[]
            for p in folder.glob('traces/*/events.jsonl'):events.extend(read_jsonl(p))
            # Before a mini phase returns its events live in the worker trace.
            # Never double-count an imported completed phase.
            worker_events=[]
            for p in folder.glob('traces/*/mini/submission_*/events.jsonl'):worker_events.extend(read_jsonl(p))
            imported=sum(e.get('backend')=='mini' and 'worker_elapsed_seconds' in e for e in events)
            if len(worker_events)>imported:
                events.extend(worker_events[imported:])
            counts=Counter(e['event'] for e in events)
            usages=defaultdict(lambda: {'prompt_tokens':0,'completion_tokens':0,'calls':0})
            for event in events:
                if event['event']=='model_usage':
                    group=usages[event.get('role','Alex')]
                    group['calls']+=1
                    for k in ('prompt_tokens','completion_tokens'):group[k]+=(event.get('usage') or {}).get(k,0) or 0
            reports=[json.loads(p.read_text()) for p in (folder/'reports').glob('*.json')]
            status='running'
            if reports:
                report=reports[0]
                status=next((name for key,name in [('resolved_instances','resolved'),('unresolved_instances','unresolved'),
                          ('empty_patch_instances','empty_patch'),('error_instances','error'),('incomplete_instances','incomplete')]
                             if report.get(key,0)), 'unknown')
            elif predictions:status='evaluating'
            elif (folder/'job_result.json').exists():status='generation_failed'
            first={}
            for name in ('repository_changed','verification_command','submitted','review_started','review_verdict'):
                times=[e.get('elapsed_seconds') for e in events if e['event']==name and 'elapsed_seconds' in e]
                first[name]=round(min(times),1) if times else None
            state=predictions[0].get('swe_run_state',{}) if predictions else {}
            rows.append(dict(arm=arm,case=folder.name,status=status,event_counts=dict(counts),first_seconds=first,
                             usage_by_role=dict(usages),patch_chars=len(predictions[0].get('model_patch','')) if predictions else None,
                             terminal_reason=state.get('terminal_reason'),elapsed_seconds=state.get('elapsed_seconds')))
    return rows


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('output')
    parser.add_argument('--json',action='store_true')
    args=parser.parse_args()
    rows=summarize(Path(args.output))
    if args.json:print(json.dumps(rows,indent=2))
    else:
        for row in rows:
            print(row['arm'],row['case'],row['status'], 'changes',row['event_counts'].get('repository_changed',0),
                  'submits',row['event_counts'].get('submitted',0),'reviews',row['event_counts'].get('review_verdict',0),
                  'tokens',sum(r['prompt_tokens']+r['completion_tokens'] for r in row['usage_by_role'].values()),
                  'reason',row['terminal_reason'])
