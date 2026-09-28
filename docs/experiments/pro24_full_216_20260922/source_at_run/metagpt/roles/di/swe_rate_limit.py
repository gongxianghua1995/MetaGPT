"""A process-shared request pacer for the SWE model endpoint."""

import fcntl
import os
from pathlib import Path
import time


DEFAULT_MIN_INTERVAL_SECONDS = 4.2
DEFAULT_STATE_PATH = "/tmp/metagpt_swe_model_request_pacer"


def wait_for_model_slot(interval=None, state_path=None):
    """Reserve a model-request slot shared by Leader, Reviewer and mini worker."""
    interval = float(interval if interval is not None else os.getenv(
        "METAGPT_SWE_MODEL_MIN_INTERVAL_SECONDS", DEFAULT_MIN_INTERVAL_SECONDS))
    if interval <= 0:
        return 0.0
    path = Path(state_path or os.getenv("METAGPT_SWE_MODEL_PACER_PATH", DEFAULT_STATE_PATH))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        stream.seek(0)
        try:
            next_slot = float(stream.read().strip() or 0)
        except ValueError:
            next_slot = 0.0
        now = time.time()
        wait = max(0.0, next_slot - now)
        if wait:
            time.sleep(wait)
        reserved_at = time.time()
        stream.seek(0)
        stream.truncate()
        stream.write(str(reserved_at + interval))
        stream.flush()
        os.fsync(stream.fileno())
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    return wait
