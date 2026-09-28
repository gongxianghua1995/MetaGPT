"""Budget propagation and phase-boundary regressions, without model calls."""
import argparse
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from metagpt.roles.di.swe_budget import (
    SWETeamBudget, add_team_budget_arguments, team_budget_from_args, team_budget_cli_args,
)
from metagpt.roles.di.swe_protocol import SWECaseState, editing_seconds
from metagpt.roles.di.swe_rate_limit import wait_for_model_slot
from metagpt.roles.di.swe_role_tools import request_tools
from metagpt.roles.di.reviewer import Reviewer
from metagpt.tools.libs.docker_terminal import DockerTerminal


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    def test_shared_pacer_reserves_the_next_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'pacer'
            with patch('metagpt.roles.di.swe_rate_limit.time.time', side_effect=[100.0, 100.0, 100.0, 105.0]), \
                 patch('metagpt.roles.di.swe_rate_limit.time.sleep') as sleep:
                self.assertEqual(wait_for_model_slot(interval=5, state_path=path), 0)
                self.assertEqual(wait_for_model_slot(interval=5, state_path=path), 5)
            sleep.assert_called_once_with(5)

    def test_controller_to_runner_roundtrip_keeps_independent_overrides(self):
        parser = argparse.ArgumentParser()
        add_team_budget_arguments(parser)
        supplied = parser.parse_args(['--leader-max-tokens', '8192', '--reviewer-max-tokens', '32768',
                                      '--leader-seconds', '240', '--reviewer-seconds', '300',
                                      '--first-edit-fraction', '.4', '--repair-seconds', '90'])
        budget = team_budget_from_args(supplied).validate(1800)
        forwarded = team_budget_from_args(parser.parse_args(team_budget_cli_args(budget)))
        self.assertEqual(forwarded, budget)
        self.assertNotEqual(forwarded.leader_max_tokens, forwarded.reviewer_max_tokens)

    def test_opt_in_schedule_keeps_three_reviews_and_final_repair(self):
        budget = SWETeamBudget(first_edit_fraction=.4, repair_seconds=120).validate(1200)
        state = SWECaseState(started_at=1000, deadline=2200,
                             first_edit_deadline=1000+1200*budget.first_edit_fraction,
                             repair_seconds=budget.repair_seconds)
        engineer = NS(case_state=state, mas_mode=True, native_deadline=2200)
        with patch('metagpt.roles.di.swe_protocol.time.monotonic', return_value=1180):
            self.assertEqual(editing_seconds(engineer), 300)
        state.submission_id = 1
        with patch('metagpt.roles.di.swe_protocol.time.monotonic', return_value=1660):
            self.assertEqual(editing_seconds(engineer), 60)
        state.submission_id = 2
        with patch('metagpt.roles.di.swe_protocol.time.monotonic', return_value=1900):
            self.assertEqual(editing_seconds(engineer), 120)

    async def test_second_review_leaves_real_repair_then_final_review(self):
        state = SWECaseState(started_at=1000, deadline=2500, first_edit_deadline=1600,
                             phase='reviewing', submission_id=2, submitted_patch='patch')
        reviewer = Reviewer(terminal=DockerTerminal('offline'), case_state=state)
        reviewer._active_submission_id = 2
        with patch('metagpt.roles.di.reviewer.time.monotonic', return_value=2200):
            await reviewer.publish_review('Fix missing import in src/x.py; rerun the entry-point check.', status='changes_requested')
            self.assertEqual(state.phase, 'editing')
            engineer = NS(case_state=state, mas_mode=True, native_deadline=2500)
            self.assertEqual(editing_seconds(engineer), 120)
        state.submission_id = 3
        state.phase = 'reviewing'
        reviewer._active_submission_id = 3
        with patch('metagpt.roles.di.reviewer.time.monotonic', return_value=2450):
            await reviewer.publish_review('Remaining defect.', status='changes_requested')
        self.assertEqual(state.phase, 'done')

    def test_legacy_schedule_can_disable_final_repair(self):
        budget = SWETeamBudget().validate(1500)
        self.assertEqual(budget.first_edit_fraction, .6)
        self.assertEqual(budget.repair_seconds, 0)
        state = SWECaseState(started_at=1000, deadline=2500, first_edit_deadline=1900,
                             submission_id=1, repair_seconds=budget.repair_seconds)
        engineer = NS(case_state=state, mas_mode=True, native_deadline=2500)
        with patch('metagpt.roles.di.swe_protocol.time.monotonic', return_value=2080):
            self.assertEqual(editing_seconds(engineer), 240)

    def test_invalid_or_overcommitted_budget_rejected(self):
        for budget, seconds in ((SWETeamBudget(leader_max_tokens=0), 1200),
                                (SWETeamBudget(review_seconds=float('nan')), 1200),
                                (SWETeamBudget(first_edit_fraction=1), 1200),
                                (SWETeamBudget(planning_seconds=700), 1200),
                                (SWETeamBudget(), 600)):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                budget.validate(seconds)
        # Team reserves do not apply to single-agent runs.
        SWETeamBudget().validate(600, mas=False)

    async def test_role_output_and_http_timeout_reach_provider_without_engineer_cap(self):
        state = SWECaseState()
        response = NS(choices=[NS(finish_reason='tool_calls', message=NS(model_dump=lambda: {}))])
        model = AsyncMock(return_value=response)
        role = NS(name='Reviewer', case_state=state, config=NS(llm=NS(max_token=128)),
                  llm=NS(_achat_completion_function=model))
        await request_tools(role, [], [], 180, max_tokens=32768, final_tool='publish_review')
        self.assertEqual(model.call_args.kwargs['max_tokens'], 32768)
        self.assertGreater(model.call_args.kwargs['timeout'], 160)
        self.assertLessEqual(model.call_args.kwargs['timeout'], 180)
        event = next(e for e in state.events if e['event']=='role_model_request')
        self.assertEqual(event['max_tokens'], 32768)
        self.assertEqual(event['seconds'], 180)

    async def test_transient_role_error_retries_but_quota_does_not(self):
        class APIError(Exception):
            status_code=429
        response=NS(choices=[NS(finish_reason='tool_calls',message=NS(model_dump=lambda:{}))])
        for error,retries in [(APIError('retry later'),2),(APIError('insufficient_quota'),1)]:
            state=SWECaseState()
            model=AsyncMock(side_effect=[error,response])
            role=NS(name='Reviewer',case_state=state,llm=NS(_achat_completion_function=model))
            with patch('metagpt.roles.di.swe_role_tools.wait_for_model_slot',return_value=0), patch('metagpt.roles.di.swe_role_tools.asyncio.sleep',new=AsyncMock()):
                if retries==1:
                    with self.assertRaises(APIError):await request_tools(role,[],[],30,max_tokens=16384)
                else:
                    await request_tools(role,[],[],30,max_tokens=16384)
                self.assertEqual(model.await_count,retries)

    async def test_exploration_timeout_still_uses_reserved_verdict_window(self):
        state = SWECaseState(phase='reviewing', submission_id=1, submitted_patch='patch',
                             review_packet={'patch':'patch'}, review_seconds=240, reviewer_max_tokens=32768)
        reviewer = Reviewer(terminal=DockerTerminal('offline'), case_state=state)
        reviewer._active_submission_id=1
        response = NS(finish_reason='tool_calls', message=NS(tool_calls=[NS(function=NS(
            name='publish_review', arguments='{"status":"inconclusive","content":"No completed verification."}'))]))
        with patch('metagpt.roles.di.reviewer.request_tools', new=AsyncMock(side_effect=[asyncio.TimeoutError(), response])) as model:
            await reviewer._react()
        self.assertEqual(model.await_count, 2)
        first, final = model.call_args_list
        self.assertGreater(first.args[3], 110)
        self.assertEqual(final.kwargs['max_tokens'], 32768)
        self.assertEqual(final.kwargs['final_tool'], 'publish_review')
        self.assertEqual(state.phase, 'done')
        self.assertEqual(state.review_cycles, 0)


if __name__ == '__main__':
    unittest.main()
