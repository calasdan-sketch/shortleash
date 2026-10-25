"""Early warning: where the run is heading, before a cap stops it.

A pure helper (no clock, no I/O) so the arithmetic can be tested on its
own. The guard calls project() after each usage event and prints the result.

What it is: the pace so far (spent / elapsed) carried forward, nothing
cleverer. A run is bursty (a long read, then a flurry of subagents), so
the time it gives is a rough guess. It warns; it never stops anything.
"""

from __future__ import annotations

#: The share of a cap at which the first forecast may be given, then the
#: later ones (each is given at most once per run).
THRESHOLDS = (0.2, 0.5, 0.8)
#: Not before this many seconds into the run (the first seconds say nothing
#: about the pace).
MIN_ELAPSED = 5.0
#: Stay quiet when the cap is further away than this (a slow, steady run).
HORIZON_SECONDS = 3600.0


def project(spent: float, cap: float, elapsed: float) -> dict | None:
    """The pace so far, carried to the cap: {"share", "rate", "eta_s"}, or
    None when there is nothing to say yet (no spend, a cap that is not a
    positive number, too early in the run)."""
    try:
        if not (cap > 0 and spent > 0 and elapsed >= MIN_ELAPSED):
            return None
        rate = spent / elapsed
        if rate <= 0 or rate != rate or rate == float("inf"):
            return None
        eta = max(cap - spent, 0.0) / rate
    except (TypeError, ZeroDivisionError, OverflowError):
        return None
    return {"share": spent / cap, "rate": rate, "eta_s": eta}


def crossed(share: float, already: set) -> float | None:
    """The highest threshold ``share`` has reached that has not been given
    yet (all the lower ones are marked as given with it), or None."""
    reached = [t for t in THRESHOLDS if share >= t]
    if not reached or reached[-1] in already:
        return None
    already.update(reached)
    return reached[-1]


def should_warn(eta_s: float, time_left: float | None) -> bool:
    """Worth a line? Yes when the cap is within the horizon and would be hit
    before the wall-clock limit (if there is one) ends the run anyway."""
    if eta_s > HORIZON_SECONDS:
        return False
    return time_left is None or eta_s < time_left


def duration(seconds: float) -> str:
    """'45s', '2m 10s', '1h 05m' -- whole units, rounded down."""
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def line(kind: str, spent: float, cap: float, elapsed: float,
         eta_s: float) -> str:
    """The one-line warning."""
    if kind == "tokens":
        what = f"{int(spent):,} of the {int(cap):,} token cap"
        gone = "the token cap is reached"
    else:
        what = f"${spent:.2f} of the ${cap:.2f} budget"
        gone = "the budget is used up"
    return (f"forecast: {what} used after {duration(elapsed)} "
            f"({spent / cap:.0%}); at this pace {gone} in about "
            f"{duration(eta_s)}")
