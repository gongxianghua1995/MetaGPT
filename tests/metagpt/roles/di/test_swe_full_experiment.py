import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT/'metagpt/roles/di'))
from swe_container import verify_network_isolation

spec=importlib.util.spec_from_file_location('full_experiment',Path(__file__).with_name('run_swe_full_experiment.py'))
full=importlib.util.module_from_spec(spec)
spec.loader.exec_module(full)
eval_spec=importlib.util.spec_from_file_location('verified_offline',Path(__file__).with_name('run_swebench_verified_offline_eval.py'))
verified=importlib.util.module_from_spec(eval_spec)
eval_spec.loader.exec_module(verified)


class IsolationTests(unittest.TestCase):
    def test_verified_eval_inspects_running_container_and_fails_closed(self):
        for mode in ('none', 'bridge'):
            with self.subTest(mode=mode):
                client=Mock();container=client.containers.create.return_value
                container.attrs={'HostConfig':{'NetworkMode':mode},'NetworkSettings':{'Networks':{mode:{}}}}
                ts=Mock(instance_id='repo__task-1',image='local-image')
                if mode=='none':
                    self.assertIs(verified.create_offline_container(ts,client,'run',Mock()),container)
                    container.remove.assert_not_called()
                else:
                    with self.assertRaises(RuntimeError):verified.create_offline_container(ts,client,'run',Mock())
                    container.remove.assert_called_once_with(force=True)
                self.assertEqual(client.containers.create.call_args.kwargs['network_mode'],'none')
                container.start.assert_called_once();container.reload.assert_called_once()

    def test_only_offline_container_is_accepted(self):
        verify_network_isolation({'HostConfig':{'NetworkMode':'none'},'NetworkSettings':{'Networks':{'none':{}}}})
        for attrs in ({}, {'HostConfig':{'NetworkMode':'bridge'}},
                      {'HostConfig':{'NetworkMode':'none'},'NetworkSettings':{'Networks':{'bridge':{}}}}):
            with self.subTest(attrs=attrs), self.assertRaises(RuntimeError):
                verify_network_isolation(attrs)


class FullExperimentTests(unittest.TestCase):
    def test_damaged_trace_is_recoverable_but_predictions_remain_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'events.jsonl'
            original='{"event":"network_isolated"}\n{"event":"broken\n[]\n{"event":"case_started"}\n'
            path.write_text(original)
            with patch('builtins.print') as warning:
                self.assertEqual([e['event'] for e in full.read_trace_jsonl(path)],['network_isolated','case_started'])
                self.assertEqual(warning.call_count,2)
            self.assertEqual(path.read_text(),original)
            with self.assertRaises(json.JSONDecodeError):full.read_jsonl(path)

    def test_repository_authentication_symbol_cannot_pause_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'worker.log'
            log.write_text('diff --git a/plugin.py b/plugin.py\n from pypsrp.exceptions import AuthenticationError, WinRMError\n')
            self.assertFalse(full.api_error_status([log])['terminal_api_error'])
            log.write_text('pypsrp.exceptions.AuthenticationError: repository test failure')
            self.assertFalse(full.api_error_status([log])['terminal_api_error'])
            for message in ['openai.AuthenticationError: incorrect key',
                            'litellm.exceptions.AuthenticationError: rejected']:
                log.write_text(message)
                self.assertTrue(full.api_error_status([log])['terminal_api_error'])
            log.write_text('provider details omitted')
            events = log.with_name('events.jsonl')
            events.write_text(json.dumps({'event': 'model_error', 'error': 'AuthenticationError'})+'\n')
            self.assertTrue(full.api_error_status([log])['terminal_api_error'])

    def test_verified_parallel_domains_resume_without_regenerating_and_count_failed_eval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tasks=root/'tasks.jsonl';out=root/'run'
            rows=[{'instance_id':f'task_{d}', 'experiment_domain':d} for d in ('a','b','c','d')]
            tasks.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            barrier=threading.Barrier(4, timeout=10)
            generations=[]
            def fake(command, log, seconds):
                log.write_text('completed\n')
                if '--save_folder' in command:
                    folder=Path(command[command.index('--save_folder')+1]);row=json.loads((folder/'instance.jsonl').read_text())
                    self.assertIn('--mas',command);self.assertEqual(command[command.index('--reviewer-max-tokens')+1],'32768')
                    barrier.wait();generations.append(row['instance_id'])
                    (folder/'all_preds.jsonl').write_text(json.dumps({**row,'model_patch':'diff'})+'\n')
                    trace=folder/'traces'/row['instance_id'];trace.mkdir(parents=True)
                    (trace/'events.jsonl').write_text(''.join(json.dumps({'event':e})+'\n' for e in ('network_isolated','history_isolated','case_started'))+'{"event":"interrupted\n')
                else:
                    self.assertEqual(Path(command[1]).name,'run_swebench_verified_offline_eval.py')
                    frozen=Path(command[command.index('--instances-file')+1])
                    pred=json.loads(Path(command[command.index('--preds')+1]).read_text())
                    self.assertEqual(json.loads(frozen.read_text())['instance_id'],pred['instance_id'])
                    folder=Path(command[command.index('--report-dir')+1]);folder.mkdir()
                    status='unresolved' if pred['instance_id']=='task_d' else 'resolved'
                    (folder/'report.json').write_text(json.dumps({status+'_ids':[pred['instance_id']]}))
                return 0
            argv=['full','--benchmark','verified','--instances-file',str(tasks),'--output',str(out),'--reviewer-max-tokens','32768']
            with patch.object(sys,'argv',argv), patch.object(full,'run_logged',side_effect=fake):
                full.main()
            summary=json.loads((out/'summary.json').read_text())
            self.assertEqual(summary['completed'],4);self.assertEqual(summary['resolved'],3)
            self.assertEqual(len(generations),4)
            with patch.object(sys,'argv',argv+['--resume']), patch.object(full,'run_logged') as launch:
                full.main();launch.assert_not_called()
            manifest=json.loads((out/'manifest.json').read_text())
            self.assertEqual(manifest['benchmark'],'SWE-bench Verified')
            self.assertEqual(manifest['generation_network'],'none');self.assertEqual(manifest['evaluation_network'],'none')
            self.assertEqual(len(full.read_jsonl(out/'all_preds.jsonl')),4)

    def test_api_errors_never_discard_completed_prediction(self):
        for marker,terminal in [('RateLimitError: retry later',False),('Budget has been exceeded',True)]:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); tasks=root/'tasks.jsonl'; out=root/'run'
                rows=[{'instance_id':f'task_{i}','experiment_domain':'a'} for i in range(2)]
                tasks.write_text(''.join(json.dumps(r)+'\n' for r in rows))
                generations=[];evaluations=[]
                def fake(command,log,seconds):
                    if '--save_folder' in command:
                        folder=log.parent;row=json.loads((folder/'instance.jsonl').read_text())
                        generations.append(row['instance_id']);log.write_text(marker)
                        (folder/'all_preds.jsonl').write_text(json.dumps({**row,'model_patch':'diff'})+'\n')
                        trace=folder/'traces'/row['instance_id'];trace.mkdir(parents=True)
                        (trace/'events.jsonl').write_text(''.join(json.dumps({'event':e})+'\n' for e in ('network_isolated','history_isolated','case_started')))
                        return 1 if terminal else 0  # A recovered patch survives nonzero exit too.
                    pred=json.loads(Path(command[command.index('--preds')+1]).read_text())
                    evaluations.append(pred['instance_id'])
                    reports=Path(command[command.index('--report-dir')+1]);reports.mkdir()
                    (reports/'report.json').write_text(json.dumps({'resolved_ids':[pred['instance_id']]}))
                    return 0
                argv=['full','--instances-file',str(tasks),'--output',str(out)]
                with patch.object(sys,'argv',argv),patch.object(full,'run_logged',side_effect=fake):full.main()
                expected=1 if terminal else 2
                self.assertEqual(len(generations),expected);self.assertEqual(len(evaluations),expected)
                state=json.loads((out/'progress.json').read_text())
                self.assertTrue(state['task_0']['prediction_ready'])
                self.assertEqual(state['task_0']['terminal_api_error'],terminal)
                self.assertEqual(state['task_0']['outcome'],'resolved')
                self.assertEqual(json.loads((out/'summary.json').read_text())['stopped'],terminal)

    def test_terminal_error_in_one_domain_still_drains_other_domain(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tasks=root/'tasks.jsonl';out=root/'run'
            rows=[{'instance_id':'task_'+d,'experiment_domain':d} for d in ('a','b')]
            tasks.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            barrier=threading.Barrier(2,timeout=10);a_evaluated=threading.Event();evaluations=[]
            def fake(command,log,seconds):
                if '--save_folder' in command:
                    folder=log.parent;row=json.loads((folder/'instance.jsonl').read_text())
                    log.write_text('Budget has been exceeded' if row['experiment_domain']=='a' else 'Completed')
                    (folder/'all_preds.jsonl').write_text(json.dumps({**row,'model_patch':'diff'})+'\n')
                    trace=folder/'traces'/row['instance_id'];trace.mkdir(parents=True)
                    (trace/'events.jsonl').write_text(''.join(json.dumps({'event':e})+'\n' for e in ('network_isolated','history_isolated','case_started')))
                    barrier.wait()
                    if row['experiment_domain']=='b':self.assertTrue(a_evaluated.wait(timeout=10))
                else:
                    pred=json.loads(Path(command[command.index('--preds')+1]).read_text())
                    evaluations.append(pred['instance_id'])
                    reports=Path(command[command.index('--report-dir')+1]);reports.mkdir()
                    (reports/'report.json').write_text(json.dumps({'resolved_ids':[pred['instance_id']]}))
                    if pred['instance_id']=='task_a':a_evaluated.set()
                return 0
            argv=['full','--instances-file',str(tasks),'--output',str(out)]
            with patch.object(sys,'argv',argv),patch.object(full,'run_logged',side_effect=fake):full.main()
            self.assertCountEqual(evaluations,['task_a','task_b'])
            summary=json.loads((out/'summary.json').read_text())
            self.assertTrue(summary['stopped']);self.assertEqual(summary['resolved'],2)

    def test_fatal_api_detection_distinguishes_rate_and_budget_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'log';p.write_text('RateLimitError: retry later')
            self.assertFalse(full.fatal_api_error(p))
            p.write_text('Budget has been exceeded')
            self.assertTrue(full.fatal_api_error(p))
            p.write_text('AuthenticationError')
            self.assertFalse(full.fatal_api_error(p))
            p.write_text('openai.AuthenticationError: provider rejected credentials')
            self.assertTrue(full.fatal_api_error(p))


if __name__=='__main__':unittest.main()
