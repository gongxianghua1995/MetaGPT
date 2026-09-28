#!/usr/bin/env python3
"""SWE-bench docker evaluation for MetaGPT SWE-bench Verified runs.

Reads the all_preds.jsonl produced by run_swe_agent_for_benchmark.py and
scores it via the official swebench docker harness (swebench.run_evaluation),
producing resolved/unresolved status and per-instance FAIL_TO_PASS /
PASS_TO_PASS detail. Results are written to <save_folder>/eval_results.json.

The script does NOT import metagpt; it only needs the `swebench` package.
Run it with a python that has swebench installed, e.g. the evomas env:

    /home/xhgong/miniconda/envs/evomas/bin/python \\
        tests/metagpt/roles/di/run_swebench_eval.py \\
        --save-folder workspace/metagpt_verified_smoke_<ts>

Resume (skip instances already scored):
    ... --save-folder ... --skip-existing

Re-read existing reports without re-running the harness:
    ... --save-folder ... --merge-only
"""
import argparse
import json
import os
import sys
import traceback
from pathlib import Path

DATASET_NAME = "SWE-bench/SWE-bench_Verified"
SPLIT = "test"


def load_predictions(preds_path):
    """Read JSONL predictions (one instance per line, all_preds.jsonl format).

    Returns (preds_list, instance_ids, model_name). Each pred dict already
    carries instance_id / model_name_or_path / model_patch, which is exactly
    what swebench.run_evaluation expects.
    """
    preds = []
    with open(preds_path, "r", encoding="utf-8") as fp:
        for ln, line in enumerate(fp, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"  [warn] line {ln} not JSON: {e}")
                continue
            if not obj.get("instance_id") or "model_patch" not in obj:
                print(f"  [warn] line {ln} missing instance_id/model_patch, skip")
                continue
            preds.append(obj)
    instance_ids = [p["instance_id"] for p in preds]
    model_names = {p.get("model_name_or_path") for p in preds if p.get("model_name_or_path")}
    model_name = sorted(model_names)[0] if model_names else "metagpt"
    return preds, instance_ids, model_name


def _summary_candidate_paths(report_dir, run_id, model_name):
    """swebench 5.0.2 writes the run-level summary as <report_dir>/<model>.<run_id>.json."""
    slug = model_name.replace("/", "__")
    return [
        Path(report_dir) / f"{slug}.{run_id}.json",
        Path(report_dir) / f"{model_name}.{run_id}.json",
    ]


def _report_roots(report_dir, save_folder):
    """Candidate roots to scan for per-instance report.json files.

    swebench 5.0.2 typically writes per-instance logs under
    <cwd>/logs/run_evaluation/<run_id>/<model>/<iid>/report.json; when a
    report_dir is given the summary lives there but per-instance logs may
    still land under logs/run_evaluation. We scan both.
    """
    roots = [Path(report_dir)]
    for r in ("logs/run_evaluation", str(Path(save_folder) / "logs" / "run_evaluation")):
        p = Path(r)
        if p.exists():
            roots.append(p)
    # dedup
    seen, out = set(), []
    for r in roots:
        rp = str(r.resolve())
        if rp not in seen:
            seen.add(rp)
            out.append(r)
    return out


def collect_results(report_dir, run_id, model_name, instance_ids, preds):
    """Locate the swebench run-level summary + per-instance reports and build
    a structured result list."""
    report_dir = Path(report_dir)
    model_slug = model_name.replace("/", "__")

    summary = None
    summary_path = None
    for cand in _summary_candidate_paths(report_dir, run_id, model_name):
        if cand.exists():
            summary_path = cand
            break
    if summary_path is None:
        for root in _report_roots(report_dir, "."):
            for cand in root.rglob(f"*{run_id}*.json"):
                # avoid picking a per-instance report.json as the summary
                if cand.name == "report.json":
                    continue
                summary_path = cand
                break
            if summary_path:
                break
    if summary_path and summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text())
        except Exception as e:
            print(f"  [warn] failed to read summary {summary_path}: {e}")

    resolved_ids = set()
    unresolved_ids = set()
    empty_patch_ids = set()
    error_ids = set()
    infra_failure_ids = set()
    ambiguous_failure_ids = set()
    if summary:
        resolved_ids = set(summary.get("resolved_ids", []))
        unresolved_ids = set(summary.get("unresolved_ids", []))
        empty_patch_ids = set(summary.get("empty_patch_ids", []))
        error_ids = set(summary.get("error_ids", []))
        infra_failure_ids = set(summary.get("infra_failure_ids", []))
        ambiguous_failure_ids = set(summary.get("ambiguous_failure_ids", []))

    # per-instance report.json: nested as {instance_id: {...}}
    # Prefer the exact current-run path (logs/run_evaluation/<run_id>/<model>/<iid>/);
    # stale reports from earlier runs (possibly under a different model-name case)
    # must not shadow the current run's results.
    per_instance = {}
    roots = _report_roots(report_dir, ".")
    exact: dict = {}
    fallback: dict = {}
    for root in roots:
        for iid in instance_ids:
            exact_path = root / run_id / model_slug / iid / "report.json"
            if exact_path.exists() and iid not in exact:
                try:
                    r = json.loads(exact_path.read_text())
                except Exception:
                    continue
                if isinstance(r, dict) and iid in r:
                    exact[iid] = r[iid]
        for rj in root.rglob("report.json"):
            try:
                r = json.loads(rj.read_text())
            except Exception:
                continue
            if not isinstance(r, dict) or not r:
                continue
            iid = next(iter(r.keys()))
            if iid and iid not in fallback:
                fallback[iid] = r[iid]
    per_instance = {**fallback, **exact}
    per_instance = {k: v for k, v in per_instance.items() if v is not None}

    results = []
    for p in preds:
        iid = p["instance_id"]
        if iid in resolved_ids:
            status = "RESOLVED"
        elif iid in unresolved_ids:
            status = "UNRESOLVED"
        elif iid in empty_patch_ids:
            status = "EMPTY_PATCH"
        elif iid in error_ids:
            status = "ERROR"
        elif iid in infra_failure_ids:
            status = "INFRA_FAILURE"
        elif iid in ambiguous_failure_ids:
            status = "AMBIGUOUS_FAILURE"
        else:
            status = "UNKNOWN"
        ri = per_instance.get(iid, {})
        ts = ri.get("tests_status", {}) or {}
        ftp = ts.get("FAIL_TO_PASS", {}) or {}
        ptp = ts.get("PASS_TO_PASS", {}) or {}
        ftp_succ = ftp.get("success", []) if isinstance(ftp, dict) else []
        ftp_fail = ftp.get("failure", []) if isinstance(ftp, dict) else []
        ptp_succ = ptp.get("success", []) if isinstance(ptp, dict) else []
        ptp_fail = ptp.get("failure", []) if isinstance(ptp, dict) else []
        results.append({
            "instance_id": iid,
            "model_name_or_path": p.get("model_name_or_path"),
            "resolved": iid in resolved_ids,
            "status": status,
            "patch_successfully_applied": ri.get("patch_successfully_applied"),
            "infra_failure": ri.get("infra_failure"),
            "FAIL_TO_PASS": {
                "passed": len(ftp_succ),
                "failed": len(ftp_fail),
                "total": len(ftp_succ) + len(ftp_fail),
            },
            "PASS_TO_PASS": {
                "passed": len(ptp_succ),
                "failed": len(ptp_fail),
                "total": len(ptp_succ) + len(ptp_fail),
            },
        })

    n_resolved = sum(1 for r in results if r["resolved"])
    return {
        "model_name_or_path": model_name,
        "run_id": run_id,
        "report_dir": str(report_dir),
        "summary_file": str(summary_path) if summary_path else None,
        "total": len(results),
        "resolved": n_resolved,
        "unresolved": len([r for r in results if r["status"] == "UNRESOLVED"]),
        "empty_patch": len([r for r in results if r["status"] == "EMPTY_PATCH"]),
        "error": len([r for r in results if r["status"] == "ERROR"]),
        "infra_failure": len([r for r in results if r["status"] == "INFRA_FAILURE"]),
        "ambiguous_failure": len([r for r in results if r["status"] == "AMBIGUOUS_FAILURE"]),
        "results": results,
    }


def print_summary(payload):
    print("\n=== EVAL SUMMARY ===")
    print(f"model: {payload['model_name_or_path']}  run_id: {payload['run_id']}")
    print(f"total={payload['total']}  resolved={payload['resolved']}  "
          f"unresolved={payload['unresolved']}  empty_patch={payload['empty_patch']}  "
          f"error={payload['error']}  infra_failure={payload['infra_failure']}  "
          f"ambiguous={payload['ambiguous_failure']}")
    if payload["total"]:
        rate = payload["resolved"] / payload["total"] * 100
        print(f"resolved rate: {rate:.1f}%")
    if payload.get("summary_file"):
        print(f"summary: {payload['summary_file']}")
    print(f"\n{'instance_id':50s}  {'status':18s}  FAIL_TO_PASS  PASS_TO_PASS")
    for r in payload["results"]:
        ftp = r.get("FAIL_TO_PASS", {})
        ptp = r.get("PASS_TO_PASS", {})
        ftp_s = f"{ftp.get('passed',0)}/{ftp.get('total',0)}"
        ptp_s = f"{ptp.get('passed',0)}/{ptp.get('total',0)}"
        print(f"{r['instance_id']:50s}  {r['status']:18s}  {ftp_s:>11s}  {ptp_s:>11s}")


def main():
    ap = argparse.ArgumentParser(description="SWE-bench docker eval for MetaGPT runs.")
    ap.add_argument("--save-folder", default=None,
                    help="Folder containing all_preds.jsonl (output dir of run_swe_agent_for_benchmark.py).")
    ap.add_argument("--preds", default=None,
                    help="Direct path to an all_preds.jsonl file (overrides --save-folder).")
    ap.add_argument("--instance-ids", default=None,
                    help="Comma-separated instance_ids to evaluate (default: all in preds).")
    ap.add_argument("--run-id", default="metagpt_docker",
                    help="swebench run_id (default: metagpt_docker).")
    ap.add_argument("--max-workers", type=int, default=4,
                    help="Parallel docker containers.")
    ap.add_argument("--timeout", type=int, default=900,
                    help="Per-instance timeout in seconds.")
    ap.add_argument("--report-dir", default=None,
                    help="Root for swebench reports (default: <save_folder>/swebench_reports).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Validate predictions, skip run_evaluation.")
    ap.add_argument("--skip-existing", action="store_true",
                    help="Skip instances that already have a report.json (resume mode).")
    ap.add_argument("--merge-only", action="store_true",
                    help="Skip run_evaluation; just read existing reports and write eval_results.json.")
    args = ap.parse_args()

    if args.preds:
        preds_path = Path(args.preds)
        save_folder = Path(args.preds).parent
    else:
        if not args.save_folder:
            sys.exit("error: provide --save-folder or --preds")
        save_folder = Path(args.save_folder)
        preds_path = save_folder / "all_preds.jsonl"
    if not preds_path.exists():
        sys.exit(f"error: predictions file not found: {preds_path}")

    report_dir = Path(args.report_dir) if args.report_dir else save_folder / "swebench_reports"
    report_dir.mkdir(parents=True, exist_ok=True)

    preds, instance_ids, model_name = load_predictions(preds_path)
    print(f"loaded {len(preds)} predictions from {preds_path}")
    print(f"model_name_or_path: {model_name}")

    if args.instance_ids:
        keep = set(s.strip() for s in args.instance_ids.split(",") if s.strip())
        instance_ids = [i for i in instance_ids if i in keep]
        preds = [p for p in preds if p["instance_id"] in keep]
        print(f"filtered to {len(instance_ids)} instances")

    run_id = args.run_id
    model_slug = model_name.replace("/", "__")

    if args.skip_existing:
        kept, skipped = [], 0
        for iid in instance_ids:
            done = False
            for root in _report_roots(report_dir, save_folder):
                if (root / run_id / model_slug / iid / "report.json").exists():
                    done = True
                    break
            if done:
                skipped += 1
            else:
                kept.append(iid)
        print(f"skip_existing: {skipped} done, {len(kept)} todo")
        instance_ids = kept
        if not instance_ids:
            print("nothing to do; writing results from existing reports")
            payload = collect_results(report_dir, run_id, model_name, instance_ids, preds)
            out = save_folder / "eval_results.json"
            out.write_text(json.dumps(payload, indent=2))
            print_summary(payload)
            return

    if args.dry_run:
        print("dry-run: predictions validated, not calling run_evaluation")
        return

    if args.merge_only:
        payload = collect_results(report_dir, run_id, model_name, instance_ids, preds)
        out = save_folder / "eval_results.json"
        out.write_text(json.dumps(payload, indent=2))
        print_summary(payload)
        return

    try:
        import swebench
    except ImportError:
        sys.exit(
            "error: swebench not installed in this python.\n"
            f"  try: /home/xhgong/miniconda/envs/evomas/bin/python {' '.join(sys.argv)}"
        )

    print(f"run_id={run_id}  instances={len(instance_ids)}  "
          f"workers={args.max_workers}  timeout={args.timeout}")
    print(f"calling swebench.run_evaluation (dataset={DATASET_NAME}) ...")
    try:
        summary = swebench.run_evaluation(
            dataset_name=DATASET_NAME,
            split=SPLIT,
            instance_ids=instance_ids,
            predictions_path=str(preds_path),
            max_workers=args.max_workers,
            open_file_limit=4096,
            run_id=run_id,
            timeout=args.timeout,
            rewrite_reports=False,
            modal=False,
            report_dir=str(report_dir),
            task_repo=None,
        )
        if isinstance(summary, dict):
            for k, v in summary.items():
                if k not in ("resolved_instances",):
                    print(f"  {k}: {v}")
    except Exception as e:
        print(f"run_evaluation EXCEPTION: {e}")
        traceback.print_exc()
        return

    payload = collect_results(report_dir, run_id, model_name, instance_ids, preds)
    out = save_folder / "eval_results.json"
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nresults written to {out}")
    print_summary(payload)


if __name__ == "__main__":
    main()
