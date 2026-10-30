"""What is left of a timeout, on the `time.monotonic()` clock.

A deadline set as `time.monotonic() + timeout` and read back as `deadline - time.monotonic()`
can come out a little above `timeout`: the sum is rounded to a float, and a coarse clock (as
Windows has) may not have moved in between. `remaining` keeps the result within the timeout,
and never below 0, so a wait computed from it is never longer than the caller allowed.
"""

import time


def remaining(deadline: float, timeout: float) -> float:
    """Seconds left before `deadline`, at most `timeout` and at least 0. A caller that stops
    at 0 or less still stops: the result is 0 once the deadline has passed."""
    return min(timeout, max(0.0, deadline - time.monotonic()))
