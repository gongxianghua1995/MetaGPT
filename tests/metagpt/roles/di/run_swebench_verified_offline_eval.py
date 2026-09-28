#!/usr/bin/env python3
"""Official Verified harness with local frozen data and fail-closed offline containers."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'metagpt/roles/di'))
from swe_container import verify_network_isolation


def create_offline_container(test_spec, client, run_id, logger):
    # Images are checked before the batch starts; do not silently build or pull.
    client.images.get(test_spec.image)
    container = client.containers.create(
        image=test_spec.image,
        name=f'sweb.eval.{test_spec.instance_id.lower()}.{run_id}',
        user='root', detach=True, network_mode='none',
        command='tail -f /dev/null', cap_add=['SYS_ADMIN'],
    )
    try:
        container.start()
        container.reload()
        isolation = verify_network_isolation(container.attrs)
        logger.info(f'Evaluation container isolation verified: {isolation}')
    except Exception:
        container.remove(force=True)
        raise
    return container


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instances-file', type=Path, required=True)
    parser.add_argument('--preds', type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--report-dir', type=Path, required=True)
    parser.add_argument('--max-workers', type=int, default=1)
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.instances_file.read_text().splitlines() if line.strip()]
    preds = [json.loads(line) for line in args.preds.read_text().splitlines() if line.strip()]
    ids = [row['instance_id'] for row in rows]
    if not ids or len(set(ids)) != len(ids) or sorted(ids) != sorted(p['instance_id'] for p in preds):
        parser.error('Expected exactly one prediction per frozen dataset instance')
    if len({p['model_name_or_path'] for p in preds}) != 1:
        parser.error('Predictions must use one model identifier')

    import swebench.harness.run_evaluation as harness
    original = harness.create_container
    harness.create_container = create_offline_container
    try:
        harness.main(
            dataset_name=str(args.instances_file.resolve()), split='test', instance_ids=ids,
            predictions_path=str(args.preds.resolve()), max_workers=args.max_workers,
            open_file_limit=4096, run_id=args.run_id, timeout=args.timeout,
            rewrite_reports=False, modal=False, report_dir=str(args.report_dir), task_repo=None,
        )
    finally:
        harness.create_container = original
    model_slug = preds[0]['model_name_or_path'].replace('/', '__')
    report = args.report_dir / f'{model_slug}.{args.run_id}.json'
    if not report.exists():
        raise RuntimeError('Official harness did not produce the current-run summary')
    (args.preds.parent / 'eval_results.json').write_text(report.read_text())


if __name__ == '__main__':
    main()
