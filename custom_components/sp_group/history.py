"""Turn billed Jarvis periods into cumulative hourly points for HA statistics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .const import DOMAIN
from .models import SG_TZ, BillInfo, PeriodReading


def cost_points(
    points: list[CumulativePoint] | tuple[CumulativePoint, ...], price: float
) -> list[CumulativePoint]:
    """Cumulative cost for a fixed unit price, e.g. SGD per kWh."""
    return [
        CumulativePoint(
            start=point.start, cumulative=round(point.cumulative * price, 4)
        )
        for point in points
    ]


def external_statistic_id(premise_id: str, key: str) -> str:
    """Recorder id for imported history, e.g. ``sp_group:2001590888_electricity``.

    Imported history must not share an id with a sensor: the recorder seeds
    its own running sum from short-term rows only, so a sensor with a
    ``total_increasing`` state class would get a second, conflicting sum.
    """
    return f"{DOMAIN}:{premise_id}_{key}"


@dataclass(frozen=True)
class CumulativePoint:
    start: datetime
    cumulative: float


def cumulative_points(
    periods: tuple[PeriodReading, ...] | list[PeriodReading],
) -> list[CumulativePoint]:
    ordered = sorted(periods, key=lambda item: item.start)
    total = 0.0
    points: list[CumulativePoint] = []
    for item in ordered:
        total += item.amount
        hour = item.start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        if points and points[-1].start == hour:
            points[-1] = CumulativePoint(start=hour, cumulative=total)
        else:
            points.append(CumulativePoint(start=hour, cumulative=total))
    return points


def trim_unreported(
    periods: tuple[PeriodReading, ...] | list[PeriodReading],
) -> tuple[PeriodReading, ...]:
    """Drop trailing zero slots the AMI feed has not filled yet."""
    ordered = sorted(periods, key=lambda item: item.start)
    last_idx = -1
    for index, item in enumerate(ordered):
        if item.amount:
            last_idx = index
    if last_idx < 0:
        return ()
    return tuple(ordered[: last_idx + 1])


def fold_half_hours(
    periods: tuple[PeriodReading, ...] | list[PeriodReading],
) -> tuple[PeriodReading, ...]:
    """Sum 30-minute AMI slots into SGT clock hours for Energy statistics."""
    buckets: dict[datetime, float] = {}
    for item in periods:
        hour = item.start.astimezone(SG_TZ).replace(minute=0, second=0, microsecond=0)
        buckets[hour] = buckets.get(hour, 0.0) + item.amount
    return tuple(
        PeriodReading(start=start, amount=amount)
        for start, amount in sorted(buckets.items())
    )


def monthly_bill_points(
    bills: tuple[BillInfo, ...] | list[BillInfo],
) -> tuple[PeriodReading, ...]:
    """One point per calendar month in SGT, using the issued bill amount."""
    by_month: dict[datetime, float] = {}
    for bill in bills:
        if bill.issued_at is None:
            continue
        month = bill.issued_at.astimezone(SG_TZ).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        by_month[month.astimezone(UTC)] = bill.amount_sgd
    return tuple(
        PeriodReading(start=start, amount=amount)
        for start, amount in sorted(by_month.items())
    )


def merge_ami_periods(
    daily: tuple[PeriodReading, ...] | list[PeriodReading],
    hourly: tuple[PeriodReading, ...] | list[PeriodReading],
) -> tuple[PeriodReading, ...]:
    """Prefer half-hourly AMI on days that have it; keep daily points before that."""
    hourly_days = {item.start.astimezone(SG_TZ).date() for item in hourly}
    merged = [
        item for item in daily if item.start.astimezone(SG_TZ).date() not in hourly_days
    ]
    merged.extend(hourly)
    return tuple(sorted(merged, key=lambda item: item.start))
