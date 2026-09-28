from metagpt.actions import Action


class CodeReviewRequest(Action):
    """Engineer2 publishes this to hand off to Reviewer after editing."""


class CodeReviewFeedback(Action):
    """Reviewer publishes this to send review feedback to Engineer2."""


class ReviewApproved(Action):
    """Reviewer publishes this to TeamLeader to signal the patch is approved."""
