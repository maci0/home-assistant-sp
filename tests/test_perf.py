"""Deterministic perf gate for the sensor-spec cache.

Every entity reads the spec list on each coordinator update, and the list walks
the whole AMI window. The gate asserts work (how often the specs are built) and
CPU time, never wall clock, so a loaded runner cannot make it flap.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from custom_components.sp_group import mapper
from custom_components.sp_group.mapper import SensorSpecCache
from custom_components.sp_group.models import (
    SG_TZ,
    PeriodReading,
    PremiseInfo,
    UsageReadings,
    UtilitySeries,
)

ENTITIES = 26
ROUNDS = 10
# The instant the synthetic usage is anchored to, and the clock reading the
# cache is asked to map it with.
NOW = datetime(2026, 9, 21, 21, 0, tzinfo=SG_TZ)


def _usage() -> UsageReadings:
    hourly = tuple(
        PeriodReading(start=NOW - timedelta(minutes=30 * index), amount=0.42)
        for index in range(8 * 48)
    )
    daily = tuple(
        PeriodReading(start=NOW - timedelta(days=index), amount=9.7)
        for index in range(60)
    )
    return UsageReadings(
        premise=PremiseInfo(
            id="2100110882",
            address="1 Example Road",
            account_number="2100110882",
            account_status="Active",
            account_type="Residential",
            premise_type="HDB 4-Room Flats",
            utilities=("electricity",),
            ami_elec=True,
            retailer_name="SP Group",
            ppms_exists=False,
        ),
        electricity=UtilitySeries(
            total=310.0, unit="kWh", periods=(), average=None, comparison=None
        ),
        water=None,
        gas=None,
        ami_hourly=hourly,
        ami_daily=daily,
    )


def test_specs_built_once_per_usage(monkeypatch) -> None:
    """One build covers every entity read until a new poll replaces the data."""
    builds: list[UsageReadings | None] = []
    real = mapper.sensors_from_usage

    def counting_sensors_from_usage(
        usage: UsageReadings | None, now: datetime
    ) -> list[mapper.SensorSpec]:
        builds.append(usage)
        return real(usage, now)

    monkeypatch.setattr(mapper, "sensors_from_usage", counting_sensors_from_usage)
    cache = SensorSpecCache()
    usage = _usage()

    for _ in range(ENTITIES):
        specs = cache.specs(usage, NOW)
    assert len(builds) == 1
    assert {spec.key for spec in specs} == {spec.key for spec in real(usage, NOW)}

    cache.specs(_usage(), NOW)
    assert len(builds) == 2, "a new poll must invalidate the cache"
    assert cache.specs(None, NOW) == ()
    assert len(builds) == 3


def test_cached_reads_cost_far_less_cpu_than_rebuilds() -> None:
    """CPU-time ratio, not wall clock: measured ~140x, asserted at 5x."""
    usage = _usage()
    cache = SensorSpecCache()
    cache.specs(usage, NOW)

    start = time.process_time()
    for _ in range(ROUNDS):
        for _ in range(ENTITIES):
            cache.specs(usage, NOW)
    cached = time.process_time() - start

    start = time.process_time()
    for _ in range(ROUNDS):
        for _ in range(ENTITIES):
            mapper.sensors_from_usage(usage, NOW)
    rebuilt = time.process_time() - start

    assert rebuilt > 0
    assert cached * 5 < rebuilt
