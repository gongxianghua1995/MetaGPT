import asyncio
import time
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from metagpt.roles.di.swe_checks import CHECK_SCRIPT, capture_check, check_evidence
from metagpt.roles.di.swe_protocol import SWECaseState, submit, repository_facts, collaboration_messages
from metagpt.roles.di.swe_engineer import SWEBenchEngineer
from metagpt.roles.di.swe_mini_engineer import SWEMiniEngineer
from metagpt.roles.di.swe_team_leader import SWEBenchTeamLeader
from metagpt.roles.di.reviewer import Reviewer
from metagpt.tools.libs.docker_bash import DockerBash
from metagpt.tools.libs.docker_terminal import DockerTerminal
from metagpt.schema import UserMessage


def choice(*calls):
    return NS(finish_reason='tool_calls',message=NS(tool_calls=[NS(function=NS(name=n,arguments=json.dumps(a))) for n,a in calls]))

class NativeTeamTests(unittest.IsolatedAsyncioTestCase):
    async def test_test_path_survives_submission_and_fresh_engineer_context(self):
        state = SWECaseState()
        engineer = SWEMiniEngineer(terminal=DockerBash('offline'), case_state=state, mas_mode=True)
        def read(command, rc=0):
            state.record('action', role=engineer.name, command=command,
                         result={'returncode': rc, 'output': 'source'}, tree_before='TREE', tree_after='TREE')
        read("cd /app && sed -n '1,260p' package/tests/test_feature.py")
        for i in range(40):
            read('cat package/source%d.py' % i)
        read('cat package/wrong/test_feature.py', 1)
        read('cat invented.py | true')
        read("echo 'package/guessed.py'")
        read('cat missing.py; true')
        with patch.object(DockerBash, 'collect_patch', new=AsyncMock(return_value='diff --git a/x.py b/x.py\n+x')):
            await submit(engineer)
        paths = [f['path'] for f in state.review_packet['repository_facts']['observed_files']]
        self.assertIn('package/tests/test_feature.py', paths)
        self.assertEqual(len(paths), 32)
        self.assertFalse(set(paths) & {'package/wrong/test_feature.py', 'invented.py', 'package/guessed.py', 'missing.py'})
        self.assertNotIn('test_feature.py', json.dumps(state.review_packet['recent_actions']))
        state.record('review_read', role='Reviewer', command='cat package/tests/test_extra.py',
                     result={'returncode': 0, 'output': 'test source'})
        fresh = SWEMiniEngineer(terminal=DockerBash('offline'), case_state=state, mas_mode=True)
        self.assertIn('package/tests/test_feature.py', json.dumps(collaboration_messages(fresh)))
        self.assertIn('package/tests/test_extra.py', json.dumps(collaboration_messages(fresh)))
        self.assertIn('package/tests/test_feature.py', json.dumps(Reviewer(
            terminal=DockerTerminal('offline'), case_state=state).review_messages()))

    async def test_reviewer_can_correct_path_then_run_check_in_later_turn(self):
        state = SWECaseState(phase='reviewing', submission_id=1, submitted_patch='patch',
                             review_packet={'current_tree': 'TREE'})
        reviewer = Reviewer(terminal=DockerTerminal('offline'), case_state=state)
        reviewer._active_submission_id = 1
        responses = [choice(('read', {'command': 'cat wrong.py'})),
                     choice(('read', {'command': 'cat tests/test_x.py'})),
                     choice(('check', {'command': 'pytest tests/test_x.py'})),
                     choice(('publish_review', {'status': 'approved', 'content': 'Focused behavior verified.'}))]
        with patch('metagpt.roles.di.reviewer.request_tools', new=AsyncMock(side_effect=responses)) as model:
            with patch.object(DockerTerminal, 'run_with_status', new=AsyncMock(side_effect=[
                {'returncode': 1, 'output': 'No such file'}, {'returncode': 0, 'output': 'test source'},
                {'returncode': 0, 'output': '2 passed'}])) as shell:
                await reviewer._react()
        self.assertEqual(shell.await_count, 3)
        for call in model.call_args_list[:3]:
            self.assertIn('check', [t['function']['name'] for t in call.args[2]])
        self.assertIn('approved', state.terminal_reason)

    async def test_reviewer_action_cap_and_timeout_preserve_verdict_turn(self):
        for timed_out in (False, True):
            reviewer = Reviewer(terminal=DockerTerminal('offline'))
            reads = [asyncio.TimeoutError()] if timed_out else [choice(('read', {'command': 'cat x.py'}))] * 4
            responses = reads + [choice(('publish_review', {'status': 'inconclusive', 'content': 'Insufficient verification.'}))]
            with patch('metagpt.roles.di.reviewer.request_tools', new=AsyncMock(side_effect=responses)) as model:
                with patch.object(DockerTerminal, 'run_with_status', new=AsyncMock(return_value={'returncode': 0, 'output': 'source'})) as shell:
                    await reviewer._run_review(180)
            self.assertEqual(shell.await_count, 0 if timed_out else 4)
            self.assertEqual([t['function']['name'] for t in model.call_args.args[2]], ['publish_review'])

    def test_check_wrapper_keeps_real_failure_after_large_output(self):
        command="python3 -c 'print(\"x\"*20000); print(\"1 failed\"); raise SystemExit(7)'"
        p=subprocess.run(['bash','-c',capture_check(command)],capture_output=True,text=True)
        self.assertEqual(p.returncode,7)
        self.assertIn('1 failed',p.stdout)
        self.assertLess(len(p.stdout),13000)
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'swe-check';script.write_text(CHECK_SCRIPT)
            p=subprocess.run(['bash',str(script),'bash','-c','printf "FAIL\\n"; exit 7'],capture_output=True,text=True)
            self.assertEqual(p.returncode,7)

    def test_no_tests_and_masked_failures_are_not_passed(self):
        self.assertEqual(check_evidence('go test ./pkg',{'returncode':0,'output':'PASS\nok pkg [no tests to run]'})['status'],'no_tests')
        self.assertEqual(check_evidence('pytest x | tail',{'returncode':0,'output':'1 failed, 20 passed'})['status'],'failure_observed')
        self.assertEqual(check_evidence('pytest x',{'returncode':0,'output':'20 passed'})['status'],'tests_passed')

    def test_reading_exception_names_is_not_test_evidence(self):
        for command,output,rc in [
            ("sed -n '1,120p' metadata.py", "except AssertionError as exc:\n  raise AnsibleError(exc)",0),
            ("grep 'pytest' source.py", "AssertionError: 2 failed",0),
            ("cat tests/test_pytest.py", "assert False, 'AssertionError'",0),
            ("grep -rn SolrUpdateState /", "",124)]:
            with self.subTest(command=command):
                self.assertIsNone(check_evidence(command,{'returncode':rc,'output':output}))

    def test_real_tests_and_reproduction_tracebacks_remain_evidence(self):
        for command in ['cd /app && PYTHONPATH=/app python -m pytest test_x.py -q | tail -20',
                        'swe-check python /tmp/repro.py', 'yarn run test file.test.ts']:
            with self.subTest(command=command):
                self.assertEqual(check_evidence(command,{'returncode':1,'output':'2 failed, 4 passed'})['status'],'failure_observed')
        self.assertEqual(check_evidence('python /tmp/repro.py',{
            'returncode':1,'output':'Traceback (most recent call last):\nAssertionError: wrong result'})['status'],'failure_observed')

    async def test_reviewer_single_context_native_approval(self):
        state=SWECaseState(phase='reviewing',submission_id=1,submitted_patch='PATCH_SENTINEL',review_packet={'patch':'PATCH_SENTINEL','current_tree':'CURRENT', 'checks':[{'tree':'CURRENT','check':{'status':'tests_passed'}}]},reviewer_max_tokens=32768)
        r=Reviewer(terminal=DockerTerminal('offline'),case_state=state,task_context='PUBLIC_SENTINEL')
        r._active_submission_id=1
        async def model(role,messages,tools,seconds,**kwargs):
            self.assertEqual(kwargs['max_tokens'],32768)
            text=json.dumps(messages)
            self.assertEqual(text.count('PATCH_SENTINEL'),1)
            self.assertEqual(text.count('PUBLIC_SENTINEL'),1)
            return choice(('publish_review',{'status':'approved','content':'Verified required behavior.'}))
        with patch('metagpt.roles.di.reviewer.request_tools',new=AsyncMock(side_effect=model)) as request:
            await r._react()
            request.assert_awaited_once()
        self.assertEqual(state.phase,'done')
        self.assertEqual(state.review_cycles,0)

    async def test_review_without_successful_check_cannot_approve(self):
        state=SWECaseState(phase='reviewing',submission_id=1,submitted_patch='patch',review_packet={'patch':'patch'})
        reviewer=Reviewer(terminal=DockerTerminal('offline'),case_state=state)
        reviewer._active_submission_id=1
        await reviewer.publish_review('Looks correct by inspection.', status='approved')
        self.assertEqual(state.phase,'done')
        verdict=next(e for e in state.events if e['event']=='review_verdict')
        self.assertEqual(verdict['status'],'inconclusive')
        self.assertIsNone(verdict['approved'])

    async def test_reviewer_rejects_approval_after_existing_test_change(self):
        state=SWECaseState(phase='reviewing',submission_id=1,submitted_patch='patch',
                           review_packet={'patch':'patch','modified_test_files':['src/tests/test_x.py'],
                                          'current_tree':'CURRENT', 'checks':[{'tree':'CURRENT','check':{'status':'tests_passed'}}]})
        reviewer=Reviewer(terminal=DockerTerminal('offline'),case_state=state)
        reviewer._active_submission_id=1
        await reviewer.publish_review('Tests pass.', status='approved')
        verdict=next(e for e in state.events if e['event']=='review_verdict')
        self.assertEqual(verdict['status'],'changes_requested')
        self.assertIn('Restore those tests', verdict['content'])

    async def test_reviewer_rejects_unresolved_required_contract(self):
        state=SWECaseState(phase='reviewing',submission_id=1,submitted_patch='patch',
                           review_packet={'patch':'patch', 'current_tree':'CURRENT', 'checks':[{'tree':'CURRENT','check':{'status':'tests_passed'}}],
                                          'unresolved_contracts':['Exact predefined default version is unknown']})
        reviewer=Reviewer(terminal=DockerTerminal('offline'),case_state=state)
        reviewer._active_submission_id=1
        await reviewer.publish_review('Looks correct.',status='approved')
        verdict=next(e for e in state.events if e['event']=='review_verdict')
        self.assertEqual(verdict['status'],'changes_requested')
        self.assertEqual(state.phase,'editing')
        self.assertIn('do not substitute a zero', verdict['content'])

    async def test_new_test_file_does_not_block_approval(self):
        state=SWECaseState(phase='reviewing',submission_id=1,submitted_patch='patch',
                           review_packet={'patch':'patch','file_changes':[{'path':'src/test_new.py','status':'added'}],
                                          'modified_test_files':[], 'current_tree':'CURRENT', 'checks':[{'tree':'CURRENT','check':{'status':'tests_passed'}}]})
        reviewer=Reviewer(terminal=DockerTerminal('offline'),case_state=state)
        reviewer._active_submission_id=1
        await reviewer.publish_review('Source and new coverage pass.', status='approved')
        verdict=next(e for e in state.events if e['event']=='review_verdict')
        self.assertEqual(verdict['status'],'approved')

    async def test_failed_check_is_present_in_followup_request(self):
        state=SWECaseState(phase='reviewing',submission_id=1,submitted_patch='patch',review_packet={'patch':'patch'})
        r=Reviewer(terminal=DockerTerminal('offline'),case_state=state)
        r._active_submission_id=1
        calls=[]
        async def model(role,messages,tools,seconds,**kwargs):
            calls.append(messages)
            if len(calls)==1:return choice(('check',{'command':'pytest tests/test_x.py -q'}))
            self.assertIn('failure_observed',json.dumps(messages))
            return choice(('publish_review',{'status':'changes_requested','content':'Fix src/x.py: test_x fails because empty inputs are not handled.'}))
        with patch('metagpt.roles.di.reviewer.request_tools',new=AsyncMock(side_effect=model)):
            with patch.object(DockerTerminal,'run_with_status',new=AsyncMock(return_value={'output':'1 failed','returncode':1})):
                await r._react()
        self.assertEqual(state.phase,'editing')
        self.assertEqual(state.review_cycles,1)
        self.assertIn('src/x.py',state.review_feedback)

    async def test_invalid_tool_arguments_are_observed_not_executed(self):
        state=SWECaseState(phase='reviewing',submission_id=1,submitted_patch='patch',review_packet={'patch':'patch'})
        r=Reviewer(terminal=DockerTerminal('offline'),case_state=state)
        r._active_submission_id=1
        bad=NS(finish_reason='tool_calls',message=NS(tool_calls=[NS(function=NS(name='read',arguments='{"command": "incomplete'))]))
        with patch('metagpt.roles.di.reviewer.request_tools',new=AsyncMock(side_effect=[bad,choice(('publish_review',{'status':'inconclusive','content':'Unable to verify this submission.'}))])):
            with patch.object(DockerTerminal,'run_with_status',new=AsyncMock()) as shell:
                await r._react()
                shell.assert_not_awaited()
        self.assertEqual(state.review_cycles,0)
        self.assertEqual(state.phase,'done')
        self.assertEqual(sum(e['event']=='review_tool_arguments_invalid' for e in state.events),1)

    async def test_live_empty_verdict_is_repaired_without_review_crash(self):
        # Exact malformed shape observed in pro21, then a corrected response.
        state=SWECaseState(phase='reviewing',submission_id=1,submitted_patch='patch')
        r=Reviewer(terminal=DockerTerminal('offline'),case_state=state);r._active_submission_id=1
        responses=[choice(('publish_review',{'content':'','failure_dispositions':[],
                    'next_check':'','reason':'verification_missing','status':'inconclusive'})),
                   choice(('publish_review',{'content':'Focused repository verification is still missing.',
                    'status':'inconclusive'}))]
        with patch('metagpt.roles.di.reviewer.request_tools',new=AsyncMock(side_effect=responses)) as model:
            await r._react()
        self.assertEqual(model.await_count,2)
        self.assertEqual(sum(e['event']=='review_verdict_invalid' for e in state.events),1)
        self.assertFalse(any(e['event']=='review_error' for e in state.events))
        self.assertEqual(state.phase,'done')
        self.assertIn('Empty or invalid verdict was rejected',json.dumps(model.call_args.args[1]))

    async def test_missing_verification_gets_only_one_followup_with_review_reserve(self):
        state=SWECaseState(phase='reviewing', submission_id=1, deadline=time.monotonic()+500)
        r=Reviewer(terminal=DockerTerminal('offline'),case_state=state)
        r._active_submission_id=1
        with patch.object(Reviewer, 'publish_message') as publish:
            await r.publish_review('Missing runnable evidence.',status='inconclusive',next_check='pytest tests/test_x.py')
            self.assertEqual(state.phase,'editing')
            self.assertEqual(state.verification_cycles,1)
            self.assertEqual(state.review_cycles,0)
            self.assertIn('pytest tests/test_x.py',state.review_feedback)
            publish.assert_called_once()
            self.assertIn('CodeReviewFeedback',str(publish.call_args.args[0].cause_by))
        state.phase='reviewing'
        await r.publish_review('Still unverified.',status='inconclusive')
        self.assertEqual(state.phase,'done')

    async def test_environment_block_or_short_budget_preserves_patch(self):
        for reason,remaining in [('environment_blocked',500),('verification_missing',220)]:
            state=SWECaseState(phase='reviewing',submission_id=1,deadline=time.monotonic()+remaining,submitted_patch='keep')
            r=Reviewer(terminal=DockerTerminal('offline'),case_state=state);r._active_submission_id=1
            await r.publish_review('Recorded check could not run.',status='inconclusive',reason=reason)
            self.assertEqual(state.phase,'done');self.assertEqual(state.submitted_patch,'keep')
            self.assertEqual(state.verification_cycles,0)

    async def test_custom_pass_cannot_erase_repository_failure_or_stale_success(self):
        for check in [{'command':'pytest repo_test.py','tree':'CURRENT','check':{'status':'failure_observed'}},
                      {'command':'pytest old.py','tree':'OLD','check':{'status':'tests_passed'}}]:
            state=SWECaseState(phase='reviewing',submission_id=1,review_packet={'current_tree':'CURRENT','checks':[
                check, {'command':'pytest custom.py','tree':'CURRENT','check':{'status':'tests_passed'}}]})
            r=Reviewer(terminal=DockerTerminal('offline'),case_state=state);r._active_submission_id=1
            await r.publish_review('Custom check passes.',status='approved')
            verdict=next(e for e in state.events if e['event']=='review_verdict')
            self.assertEqual(verdict['status'],'inconclusive')

    async def test_expected_behavior_needs_explicit_public_evidence(self):
        state=SWECaseState(phase='reviewing',submission_id=1,review_packet={'current_tree':'CURRENT','checks':[
            {'command':'pytest repo.py','tree':'CURRENT','check':{'status':'failure_observed'}},
            {'command':'pytest new.py','tree':'CURRENT','check':{'status':'tests_passed'}}]})
        r=Reviewer(terminal=DockerTerminal('offline'),case_state=state);r._active_submission_id=1
        await r.publish_review('Required behavior verified.',status='approved',failure_dispositions=[{
            'command':'pytest repo.py','resolution':'expected_behavior',
            'evidence':'The public task explicitly replaces return None with an empty list; this old assertion requires None.'}])
        self.assertIn('approved',state.terminal_reason)

    async def test_leader_batch_cap_always_synthesizes_plan(self):
        state=SWECaseState(leader_max_tokens=8192,planning_seconds=240)
        leader=SWEBenchTeamLeader(terminal=DockerTerminal('offline'),case_state=state)
        leader.rc.memory.add(UserMessage(content='PUBLIC TASK'))
        responses=[choice(*[('bash',{'command':'read %d'%i}) for i in range(5)]),
                   choice(('publish_plan',dict(locations=['src/x.go: verified'],hypothesis='Missing behavior',steps=['Implement public requirement'],verification='go test ./src',unknowns=[])))]
        with patch('metagpt.roles.di.swe_team_leader.request_tools',new=AsyncMock(side_effect=responses)) as model:
            with patch.object(DockerTerminal,'run_command',new=AsyncMock(return_value='source evidence')) as shell:
                with patch.object(SWEBenchTeamLeader,'_publish_to_member',return_value=True) as publish:
                    await leader._react()
        self.assertEqual(shell.await_count,3)
        self.assertEqual(model.await_count,2)
        self.assertEqual(model.call_args.kwargs['final_tool'],'publish_plan')
        self.assertTrue(all(c.kwargs['max_tokens']==8192 for c in model.call_args_list))
        self.assertGreater(model.call_args_list[0].args[3],110)
        self.assertIn('Technical plan',publish.call_args.args[0])
        self.assertTrue(any(e['event']=='planning_completed' for e in state.events))

    async def test_leader_malformed_final_plan_keeps_partial_handoff(self):
        state=SWECaseState(leader_max_tokens=8192,planning_seconds=240)
        leader=SWEBenchTeamLeader(terminal=DockerTerminal('offline'),case_state=state)
        leader.rc.memory.add(UserMessage(content='PUBLIC TASK'))
        malformed=NS(finish_reason='tool_calls', message=NS(tool_calls=[
            NS(function=NS(name='publish_plan', arguments='{"locations":'))
        ]))
        with patch('metagpt.roles.di.swe_team_leader.request_tools',new=AsyncMock(return_value=malformed)):
            with patch.object(SWEBenchTeamLeader,'_publish_to_member',return_value=True):
                await leader._react()
        self.assertTrue(any(e['event']=='planning_tool_arguments_invalid' for e in state.events))
        self.assertTrue(any(e['event']=='planning_incomplete' for e in state.events))
        self.assertFalse(any(e['event']=='planning_synthesis_failed' for e in state.events))

    async def test_duplicate_submission_does_not_restart_review(self):
        state=SWECaseState()
        eng=SWEBenchEngineer(terminal=DockerBash('offline'),case_state=state,mas_mode=True)
        with patch.object(DockerBash,'collect_patch',new=AsyncMock(return_value='patch')):
            await submit(eng)
            eng._force_done=False
            result=await submit(eng)
        self.assertTrue(result['duplicate'])
        self.assertEqual(state.submission_id,1)
        self.assertEqual(state.phase,'done')

    async def test_old_failure_survives_many_later_reads(self):
        state=SWECaseState()
        eng=SWEBenchEngineer(terminal=DockerBash('offline'),case_state=state,mas_mode=True)
        state.record('action',role=eng.name,command='python /tmp/repro.py',tree_after='TREE_A',result={'returncode':1,'output':'AssertionError: missing behavior'})
        for i in range(20):state.record('action',role=eng.name,command='cat file%d'%i,result={'returncode':0,'output':'source'})
        with patch.object(DockerBash,'collect_patch',new=AsyncMock(return_value='patch')):await submit(eng)
        self.assertIn('missing behavior',json.dumps(state.review_packet))
        self.assertEqual(state.review_packet['checks'][0]['tree'],'TREE_A')

    async def test_exhausted_revision_never_creates_submission(self):
        state=SWECaseState(submission_id=1,deadline=10,first_edit_deadline=7)
        eng=SWEMiniEngineer(terminal=DockerBash('offline'),case_state=state,mas_mode=True,native_deadline=10)
        with patch('metagpt.roles.di.swe_protocol.submit',new=AsyncMock()) as submit_mock:
            await eng._react()
            submit_mock.assert_not_awaited()
        self.assertEqual(state.submission_id,1)
        self.assertEqual(state.phase,'done')


class RepositoryBoundaryTests(unittest.TestCase):
    def test_audited_snapshot_preserves_build_tree_but_removes_snapshot_history(self):
        from metagpt.roles.di.swe_repository import isolate_history_command
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def git(*args):return subprocess.check_output(['git','-C',folder,*args],text=True).strip()
            git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.invalid')
            (root/'source.py').write_text('base\n');(root/'tox.ini').write_text('old build option\n')
            git('add','.');git('commit','-qm','base');base=git('rev-parse','HEAD')
            (root/'tox.ini').write_text('compatible build option\n');(root/'source.py').chmod(0o755)
            git('commit','-qam','SWE-bench');snapshot=git('rev-parse','HEAD')
            rejected=subprocess.run(['bash','-c',isolate_history_command(base)],cwd=folder,capture_output=True)
            self.assertNotEqual(rejected.returncode,0)
            rejected=subprocess.run(['bash','-c',isolate_history_command(base,'0'*40)],cwd=folder,capture_output=True)
            self.assertNotEqual(rejected.returncode,0)
            subprocess.run(['bash','-c',isolate_history_command(base,snapshot)],cwd=folder,check=True,capture_output=True)
            self.assertEqual(git('rev-parse','HEAD'),base)
            self.assertEqual((root/'tox.ini').read_text(),'compatible build option\n')
            self.assertEqual((root/'source.py').read_text(),'base\n')
            self.assertEqual(git('diff','--name-only','HEAD'),'tox.ini')
            self.assertEqual(git('rev-list','--all','--count'),'1')
            self.assertNotEqual(subprocess.run(['git','-C',folder,'cat-file','-e',snapshot],capture_output=True).returncode,0)

    def test_base_sha_and_dirty_tree_preserved_future_object_removed(self):
        from metagpt.roles.di.swe_repository import isolate_history_command
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def git(*args):return subprocess.check_output(['git','-C',folder,*args],text=True).strip()
            git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.invalid')
            (root/'source.py').write_text('base\n');git('add','.');git('commit','-qm','base');base=git('rev-parse','HEAD')
            (root/'source.py').write_text('future answer\n');git('commit','-qam','future');future=git('rev-parse','HEAD')
            git('checkout','-q','--detach',base)
            (root/'source.py').write_text('dirty base file\n');(root/'untracked.txt').write_text('keep me\n')
            before=git('diff','HEAD')
            subprocess.run(['bash','-c',isolate_history_command(base)],cwd=folder,check=True,capture_output=True,text=True)
            self.assertEqual(git('rev-parse','HEAD'),base)
            self.assertEqual(git('diff','HEAD'),before)
            self.assertEqual((root/'untracked.txt').read_text(),'keep me\n')
            self.assertEqual(git('rev-list','--all','--count'),'1')
            self.assertNotEqual(subprocess.run(['git','-C',folder,'cat-file','-e',future],capture_output=True).returncode,0)
            self.assertEqual(git('remote','-v'),'')

class SubmoduleBoundaryTests(unittest.TestCase):
    def test_initialized_submodule_survives_without_future_objects(self):
        from metagpt.roles.di.swe_repository import isolate_history_command
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);dep=root/'dep';repo=root/'repo'
            def git(path,*args):return subprocess.check_output(['git','-C',str(path),*args],text=True).strip()
            for path in (dep,repo):
                path.mkdir();git(path,'init','-q');git(path,'config','user.name','Test');git(path,'config','user.email','test@example.invalid')
            (dep/'lib.py').write_text('base dependency\n');git(dep,'add','.');git(dep,'commit','-qm','base');dep_base=git(dep,'rev-parse','HEAD')
            (dep/'lib.py').write_text('future dependency\n');git(dep,'commit','-qam','future');dep_future=git(dep,'rev-parse','HEAD')
            git(repo,'-c','protocol.file.allow=always','submodule','add','-q',str(dep),'vendor/lib')
            git(repo/'vendor/lib','checkout','-q','--detach',dep_base)
            git(repo,'add','.');git(repo,'commit','-qm','base');base=git(repo,'rev-parse','HEAD')
            (repo/'vendor/lib/lib.py').write_text('dirty dependency preserved\n')
            before=git(repo,'diff','--full-index','HEAD')
            subprocess.run(['bash','-c',isolate_history_command(base)],cwd=repo,check=True,capture_output=True,text=True)
            self.assertEqual(git(repo/'vendor/lib','rev-parse','HEAD'),dep_base)
            self.assertEqual(git(repo,'diff','--full-index','HEAD'),before)
            self.assertEqual((repo/'vendor/lib/lib.py').read_text(),'dirty dependency preserved\n')
            self.assertNotEqual(subprocess.run(['git','-C',str(repo/'vendor/lib'),'cat-file','-e',dep_future],capture_output=True).returncode,0)
            git(repo,'add','-A')
            self.assertEqual(git(repo,'rev-list','--all','--count'),'1')

if __name__=='__main__':unittest.main()
