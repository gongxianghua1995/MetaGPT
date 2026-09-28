"""Small execution reminders around the upstream mini loop; no model code."""
from dataclasses import dataclass


@dataclass
class EditingProgress:
    seconds: float
    repair_only: bool = False
    edited: bool = False
    verification_due: bool = False
    check_failed: bool = False

    def observe(self, *, changed, evidence):
        if changed:
            self.edited = True
            self.verification_due = True
        # A check that also rewrites the tree cannot verify the final tree.
        elif evidence:
            self.check_failed = evidence.get('status') != 'tests_passed'
            self.verification_due = self.check_failed

    def guidance(self, elapsed):
        remaining = max(0, int(self.seconds - elapsed))
        message = f'Editing checkpoint in {remaining}s; this includes testing and submission.'
        if self.repair_only:
            message += ' Final repair: fix only the concrete review blockers and rerun their checks; do not restart broad exploration or unrelated refactoring.'
        elif not self.edited and elapsed >= min(180, self.seconds * .35):
            message += (' Implementation milestone reached. Use the source already read to implement one public requirement now. '
                        'Do not keep searching for a new interface the task asks you to create. '
                        'If an exact contract remains unknown, identify that specific uncertainty instead of inventing a value.')
        if self.verification_due:
            message += (' The changed tree lacks successful focused verification. Next run a small check via swe-check: '
                        'import/load the changed module AND invoke the changed entry point with a minimal representative input. '
                        'Compilation alone misses missing imports and NameError. Use the existing focused test when available. '
                        'Fix observed errors before widening scope; print PASS only after assertions succeed.')
        if self.check_failed:
            message += ' The last recorded check failed or was inconclusive; inspect its actual exit status and resolve or report the blocker.'
        if remaining < 90:
            message += ' Stop expanding scope. Verify the current patch and submit for review with any unmet public requirements explicitly noted.'
        return message
