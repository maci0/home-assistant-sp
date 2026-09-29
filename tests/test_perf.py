"""Deterministic perf gates for the poll and the sensor-spec cache.

Every entity reads the spec list on each coordinator update, and the list walks
the whole AMI window. The gate asserts work (how often the specs are built) and
CPU time, never wall clock, so a loaded runner cannot make it flap. The poll
gate asserts overlap, not duration, for the same reason.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pytest

from custom_components.sp_group import mapper
from custom_components.sp_group.client import HttpResponse
from custom_components.sp_group.const import (
    B2C_HOST,
    JARVIS_AMI_PATH,
    JARVIS_SMRD_PATH,
)
from custom_components.sp_group.mapper import SensorSpec, SensorSpecCache
from custom_components.sp_group.models import (
    SG_TZ,
    PeriodReading,
    PremiseInfo,
    UsageReadings,
    UtilitySeries,
)

from .conftest import FixtureTransport, fixture_client

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


def test_specs_built_once_per_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """One build covers every entity read until a new poll replaces the data."""
    builds: list[UsageReadings | None] = []
    real = mapper.sensors_from_usage

    def counting_sensors_from_usage(
        usage: UsageReadings | None, now: datetime
    ) -> list[SensorSpec]:
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


# The three reads that follow the charts call. None of them needs another's
# answer, so the poll issues them together. The barrier is the assertion: both
# must be in flight at once, and a barrier that times out raises rather than
# hanging, so a regression to serial reads fails the test instead of stalling
# the suite. The metered read stands in for the whole first fan-out; the AMI
# pair is two requests of one unit, so only the first of them is gated.
CONCURRENT_PATHS = (JARVIS_SMRD_PATH, JARVIS_AMI_PATH)
BARRIER_TIMEOUT_SECONDS = 10


class _OverlappingTransport(FixtureTransport):
    barrier: threading.Barrier
    gated: int

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout: int | None = None,
    ) -> HttpResponse:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if (
            origin == B2C_HOST
            and parsed.path.startswith(CONCURRENT_PATHS)
            and self.gated < len(CONCURRENT_PATHS)
        ):
            self.gated += 1
            self.barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
        return super().request(method, url, headers, body, timeout=timeout)


def test_poll_reads_independent_hosts_at_the_same_time() -> None:
    transport = _OverlappingTransport()
    transport.barrier = threading.Barrier(len(CONCURRENT_PATHS))
    transport.gated = 0

    usage = fixture_client(transport).fetch_usage()

    assert usage.premise_id
    assert transport.gated == len(CONCURRENT_PATHS)
