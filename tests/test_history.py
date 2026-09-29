"""Billed periods become cumulative hourly points for Energy statistics."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.sp_group.history import (
    CumulativePoint,
    cost_points,
    cumulative_points,
    external_statistic_id,
    fold_half_hours,
    merge_ami_periods,
    monthly_bill_points,
    trim_unreported,
    unimported,
)
from custom_components.sp_group.mapper import electricity_view
from custom_components.sp_group.models import SG_TZ, BillInfo, PeriodReading

from .conftest import billed_totals_from_charts_payload, fixture_client, load_fixture


def test_cumulative_points_sum_fixture_currents() -> None:
    charts = json.loads(load_fixture("jarvis_charts.json"))
    expected_kwh, expected_m3 = billed_totals_from_charts_payload(charts)
    client = fixture_client()
    usage = client.fetch_usage()
    elec = cumulative_points(usage.electricity_periods)
    water = cumulative_points(usage.water_periods)
    assert elec[-1].cumulative == expected_kwh
    assert water[-1].cumulative == expected_m3
    assert elec[0].start.tzinfo is not None
    assert elec[0].start == elec[0].start.replace(minute=0, second=0, microsecond=0)


def test_same_hour_periods_collapse() -> None:
    start = datetime(2026, 6, 1, 0, 15, tzinfo=UTC)
    points = cumulative_points(
        (
            PeriodReading(start=start, amount=10.0),
            PeriodReading(start=start.replace(minute=45), amount=2.5),
        )
    )
    assert len(points) == 1
    assert points[0].cumulative == 12.5
    assert points[0].start.minute == 0


def test_cumulative_points_sort_unordered_periods() -> None:
    """The AMI feed is not ordered; a late row must not become a lower sum."""
    later = datetime(2026, 6, 1, 2, 0, tzinfo=UTC)
    earlier = later - timedelta(hours=1)

    points = cumulative_points(
        (
            PeriodReading(start=later, amount=2.0),
            PeriodReading(start=earlier, amount=1.0),
        )
    )

    assert [(point.start, point.cumulative) for point in points] == [
        (earlier, 1.0),
        (later, 3.0),
    ]


def test_trim_unreported_drops_trailing_zeros() -> None:
    start = datetime(2026, 8, 28, 15, 0, tzinfo=SG_TZ)
    trimmed = trim_unreported(
        (
            PeriodReading(start=start, amount=0.4),
            PeriodReading(start=start.replace(minute=30), amount=0.7),
            PeriodReading(start=start.replace(hour=16), amount=0.0),
            PeriodReading(start=start.replace(hour=16, minute=30), amount=0.0),
        )
    )
    assert len(trimmed) == 2
    assert trimmed[-1].amount == pytest.approx(0.7)


def test_trim_unreported_drops_everything_when_nothing_is_reported() -> None:
    """A brand new account has an all-zero AMI window, not a zero-length one."""
    start = datetime(2026, 8, 28, 15, 0, tzinfo=SG_TZ)

    assert (
        trim_unreported(
            (
                PeriodReading(start=start, amount=0.0),
                PeriodReading(start=start.replace(minute=30), amount=0.0),
            )
        )
        == ()
    )


def test_monthly_bill_points_one_per_month() -> None:
    client = fixture_client()
    usage = client.fetch_usage()
    points = monthly_bill_points(usage.bills)
    assert len(points) == 2
    assert points[0].amount == pytest.approx(323.26)
    assert points[1].amount == pytest.approx(203.69)
    assert points[0].start.tzinfo is not None
    assert points[0].start.hour == 16
    assert points[0].start.minute == 0
    assert points[0].start.month == 6
    assert points[1].start.month == 7


def test_monthly_bill_points_skip_undated_and_keep_the_latest_of_a_month() -> None:
    """A bill with no parseable date has no month, and a re-issued bill replaces."""
    points = monthly_bill_points(
        (
            BillInfo(
                amount_sgd=10.0,
                date=None,
                period=None,
                due_date=None,
                issued_at=None,
            ),
            BillInfo(
                amount_sgd=1.0,
                date=None,
                period=None,
                due_date=None,
                issued_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
            ),
            BillInfo(
                amount_sgd=2.0,
                date=None,
                period=None,
                due_date=None,
                issued_at=datetime(2026, 6, 20, 0, 0, tzinfo=UTC),
            ),
        )
    )

    # The month is anchored in SGT, so it lands at 16:00Z on the previous day.
    assert [(point.start, point.amount) for point in points] == [
        (datetime(2026, 5, 31, 16, 0, tzinfo=UTC), 2.0)
    ]


def test_fold_half_hours_sums_clock_hour() -> None:
    start = datetime(2026, 8, 2, 0, 0, tzinfo=SG_TZ)
    folded = fold_half_hours(
        (
            PeriodReading(start=start, amount=0.4),
            PeriodReading(start=start.replace(minute=30), amount=0.6),
        )
    )
    assert len(folded) == 1
    assert folded[0].amount == pytest.approx(1.0)
    assert folded[0].start.minute == 0


def test_merge_prefers_hourly_on_same_day() -> None:
    day = datetime(2026, 8, 2, 0, 0, tzinfo=SG_TZ)
    day_before = day - timedelta(days=1)
    daily = (
        PeriodReading(start=day_before, amount=99.0),
        PeriodReading(start=day, amount=88.0),
    )
    hourly = (
        PeriodReading(start=day, amount=1.0),
        PeriodReading(start=day.replace(hour=1), amount=2.0),
    )

    merged = merge_ami_periods(daily, hourly)

    # The hourly day wins; the day with no hourly data keeps its daily total.
    assert [(item.start, item.amount) for item in merged] == [
        (day_before, 99.0),
        (day, 1.0),
        (day.replace(hour=1), 2.0),
    ]


def test_electricity_graph_uses_ami_not_billed() -> None:
    client = fixture_client()
    usage = client.fetch_usage()
    graph = electricity_view(usage, client.clock.now()).graph
    assert sum(item.amount for item in graph) == pytest.approx(24.0)
    points = cumulative_points(graph)
    assert points[-1].cumulative == pytest.approx(24.0)


def test_external_statistic_id_is_recorder_safe() -> None:
    assert external_statistic_id("2001590888", "electricity") == (
        "sp_group:2001590888_electricity"
    )


def test_cost_points_scale_cumulative_kwh() -> None:
    points = [
        CumulativePoint(
            start=datetime(2026, 9, 21, 0, tzinfo=SG_TZ), cumulative=8.4612
        ),
        CumulativePoint(
            start=datetime(2026, 9, 21, 1, tzinfo=SG_TZ), cumulative=8.5793
        ),
    ]

    cost = cost_points(points, 0.3478)

    assert [c.start for c in cost] == [p.start for p in points]
    assert cost[0].cumulative == pytest.approx(2.9428, abs=1e-4)
    assert cost[1].cumulative == pytest.approx(2.9839, abs=1e-4)


def test_unimported_sends_everything_on_the_first_import() -> None:
    points = [
        CumulativePoint(start=datetime(2026, 9, 21, 0, tzinfo=UTC), cumulative=1.0),
        CumulativePoint(start=datetime(2026, 9, 21, 1, tzinfo=UTC), cumulative=2.0),
    ]

    assert unimported(points, None) == points


def test_unimported_drops_only_the_points_already_written() -> None:
    written = datetime(2026, 9, 21, 0, tzinfo=UTC)
    points = [
        CumulativePoint(start=written, cumulative=1.0),
        CumulativePoint(start=datetime(2026, 9, 21, 1, tzinfo=UTC), cumulative=2.0),
        CumulativePoint(start=datetime(2026, 9, 21, 2, tzinfo=UTC), cumulative=3.0),
    ]

    fresh = unimported(points, written)

    assert [point.start for point in fresh] == [point.start for point in points[1:]]


def test_unimported_is_empty_when_the_series_did_not_move() -> None:
    points = [
        CumulativePoint(start=datetime(2026, 9, 21, 0, tzinfo=UTC), cumulative=1.0),
        CumulativePoint(start=datetime(2026, 9, 21, 1, tzinfo=UTC), cumulative=2.0),
    ]

    assert unimported(points, points[-1].start) == []


def test_unimported_works_for_bill_periods() -> None:
    month = datetime(2026, 9, 1, tzinfo=UTC)
    bills = [PeriodReading(start=month, amount=42.0)]

    assert unimported(bills, month) == []
    assert unimported(bills, None) == bills
