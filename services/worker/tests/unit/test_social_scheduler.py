"""Tests for app.social.scheduler — the PURE 'drip' planner.

The heart of the user-facing feature: "I have N clips, post K/day at fixed times." Pure logic,
no I/O, timezone-explicit. Covers count, ascending order, day-0 past-slot skipping, multi-day
rollover, the per-day cap clamp, and the rule-#8 error on an empty slot list.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone

import pytest

from app.social import scheduler

# A real user timezone (Asia/Almaty, UTC+5) so the local-vs-UTC conversion is exercised.
ALMATY = timezone(timedelta(hours=5))
# Post three times a day at 09:00 / 15:00 / 21:00 local.
SLOTS = [time(9, 0), time(15, 0), time(21, 0)]


def _local(dts: list[datetime]) -> list[datetime]:
    return [d.astimezone(ALMATY) for d in dts]


def test_returns_one_due_time_per_clip() -> None:
    start = datetime(2026, 7, 1, 6, 0, tzinfo=ALMATY)  # before the first slot
    out = scheduler.compute_due_times(25, start=start, slot_times=SLOTS, tz=ALMATY)
    assert len(out) == 25


def test_due_times_are_ascending_utc_and_after_start() -> None:
    start = datetime(2026, 7, 1, 6, 0, tzinfo=ALMATY)
    out = scheduler.compute_due_times(7, start=start, slot_times=SLOTS, tz=ALMATY)
    assert all(d.tzinfo == UTC for d in out)  # normalized to UTC for storage
    assert out == sorted(out)
    assert all(d > start.astimezone(UTC) for d in out)


def test_fills_three_per_local_day_then_rolls_over() -> None:
    start = datetime(2026, 7, 1, 6, 0, tzinfo=ALMATY)
    out = _local(scheduler.compute_due_times(4, start=start, slot_times=SLOTS, tz=ALMATY))
    # day 0: 09:00, 15:00, 21:00 — then day 1: 09:00
    assert [(d.date().day, d.hour) for d in out] == [(1, 9), (1, 15), (1, 21), (2, 9)]


def test_day0_skips_slots_already_in_the_past() -> None:
    start = datetime(2026, 7, 1, 16, 0, tzinfo=ALMATY)  # 16:00 → 09:00 & 15:00 already gone
    out = _local(scheduler.compute_due_times(2, start=start, slot_times=SLOTS, tz=ALMATY))
    # only 21:00 remains on day 0, then 09:00 next day
    assert [(d.date().day, d.hour) for d in out] == [(1, 21), (2, 9)]


def test_daily_cap_clamps_to_fewer_slots_per_day() -> None:
    start = datetime(2026, 7, 1, 6, 0, tzinfo=ALMATY)
    out = _local(
        scheduler.compute_due_times(4, start=start, slot_times=SLOTS, tz=ALMATY, daily_cap=2)
    )
    # cap=2 → only the first two slots (09:00, 15:00) used each day
    assert [(d.date().day, d.hour) for d in out] == [(1, 9), (1, 15), (2, 9), (2, 15)]


def test_zero_clips_returns_empty() -> None:
    start = datetime(2026, 7, 1, 6, 0, tzinfo=ALMATY)
    assert scheduler.compute_due_times(0, start=start, slot_times=SLOTS, tz=ALMATY) == []


def test_empty_slot_times_raises_not_silent() -> None:
    start = datetime(2026, 7, 1, 6, 0, tzinfo=ALMATY)
    with pytest.raises(ValueError):
        scheduler.compute_due_times(3, start=start, slot_times=[], tz=ALMATY)


def test_daily_cap_below_one_raises_not_silent() -> None:
    start = datetime(2026, 7, 1, 6, 0, tzinfo=ALMATY)
    with pytest.raises(ValueError):
        scheduler.compute_due_times(3, start=start, slot_times=SLOTS, tz=ALMATY, daily_cap=0)
