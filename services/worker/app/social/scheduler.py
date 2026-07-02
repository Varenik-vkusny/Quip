"""PURE drip planner: spread N clips across fixed daily slots ("post 3/day at 9/15/21").

No I/O, timezone-explicit. The runner/repo turn these due-times into stored post_jobs; this
module owns ONLY the arithmetic of "when does each clip go out", so it is fully unit-tested.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, time, timedelta, tzinfo


def compute_due_times(
    count: int,
    *,
    start: datetime,
    slot_times: Sequence[time],
    tz: tzinfo,
    daily_cap: int | None = None,
) -> list[datetime]:
    """Return ``count`` ascending UTC datetimes, one per clip, filling daily slots in order.

    ``start`` is an aware "now"; ``slot_times`` are wall-clock times-of-day in ``tz`` (the
    user's schedule timezone). Day 0 only uses slots strictly AFTER ``start`` (we never
    schedule a slot already in the past); subsequent days use every slot. ``daily_cap`` (if
    given) limits how many of the earliest slots are used per day — e.g. cap=2 with three
    slots posts only at the first two.

    Rule #8 (no silent fallbacks): empty ``slot_times`` or ``daily_cap < 1`` raises ValueError
    rather than quietly producing nothing.
    """
    if count <= 0:
        return []
    if not slot_times:
        raise ValueError("slot_times must not be empty")
    ordered = sorted(slot_times)
    per_day = len(ordered) if daily_cap is None else min(daily_cap, len(ordered))
    if per_day < 1:
        raise ValueError("daily_cap must be >= 1")
    slots = ordered[:per_day]

    start_local = start.astimezone(tz)
    day0 = start_local.date()
    out: list[datetime] = []
    day = 0
    while len(out) < count:
        base_date = day0 + timedelta(days=day)
        for slot in slots:
            candidate = datetime.combine(base_date, slot, tzinfo=tz)
            if day == 0 and candidate <= start_local:
                continue  # slot already passed today — skip to the next available one
            out.append(candidate.astimezone(UTC))
            if len(out) == count:
                break
        day += 1
    return out
