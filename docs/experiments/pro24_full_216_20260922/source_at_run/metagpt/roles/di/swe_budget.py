"""Shared, explicit budgets for SWE runners; no model/provider dependencies."""
from dataclasses import asdict, dataclass
import math


DEFAULT_CASE_MINUTES = 20


@dataclass(frozen=True)
class SWETeamBudget:
    leader_max_tokens: int = 16384
    reviewer_max_tokens: int = 16384
    planning_seconds: float = 180
    review_seconds: float = 180
    # The three-review schedule is opt-in until broader validation supports it.
    first_edit_fraction: float = .6
    repair_seconds: float = 0

    def validate(self, total_seconds, *, mas=True):
        for name, value in {**asdict(self), 'total_seconds': total_seconds}.items():
            if not math.isfinite(value) or value < 0 or (value == 0 and name != 'repair_seconds'):
                raise ValueError(f'{name} must be finite and positive')
        if self.first_edit_fraction >= 1:
            raise ValueError('first_edit_fraction must be between 0 and 1')
        if mas:
            first_edit = total_seconds * self.first_edit_fraction
            if first_edit < self.planning_seconds + 60:
                raise ValueError('First submission must leave at least 60s of coding after Leader planning; increase total time or adjust --leader-seconds/--first-edit-fraction')
            reviews = 3 if self.repair_seconds else 2
            if total_seconds - first_edit < reviews * self.review_seconds + self.repair_seconds + 60:
                raise ValueError('After first submission reserve reviews, final repair and at least 60s of revision; adjust --reviewer-seconds/--first-edit-fraction/--repair-seconds')
        return self


DEFAULT_TEAM_BUDGET = SWETeamBudget()
TEAM_BUDGET_FLAGS = {
    'leader_max_tokens': '--leader-max-tokens',
    'reviewer_max_tokens': '--reviewer-max-tokens',
    'planning_seconds': '--leader-seconds',
    'review_seconds': '--reviewer-seconds',
    'first_edit_fraction': '--first-edit-fraction',
    'repair_seconds': '--repair-seconds',
}


def add_team_budget_arguments(parser):
    for name, flag in TEAM_BUDGET_FLAGS.items():
        default = getattr(DEFAULT_TEAM_BUDGET, name)
        help_text = {
            'leader_max_tokens': 'Leader per-call output limit, including provider-reported reasoning tokens',
            'reviewer_max_tokens': 'Reviewer per-call output limit, including provider-reported reasoning tokens',
            'planning_seconds': 'Total Leader planning phase deadline in seconds',
            'review_seconds': 'Deadline for each Reviewer phase in seconds',
            'first_edit_fraction': 'First submission deadline as fraction of whole case time, including planning',
            'repair_seconds': 'Final focused repair reserve before the last review; 0 keeps two-review scheduling',
        }[name]
        parser.add_argument(flag, dest=name, type=int if name.endswith('_tokens') else float,
                            default=default, help=f'{help_text} (default: {default})')


def team_budget_from_args(args):
    return SWETeamBudget(**{name: getattr(args, name, getattr(DEFAULT_TEAM_BUDGET, name))
                           for name in TEAM_BUDGET_FLAGS})


def team_budget_cli_args(budget):
    return [arg for name, flag in TEAM_BUDGET_FLAGS.items()
            for arg in (flag, str(getattr(budget, name)))]
