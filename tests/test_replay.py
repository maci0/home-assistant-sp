"""Replaying a poll from the same clock reading must reproduce it exactly.

The client takes its time from an injected ``Clock``, so a run is a function of
the fixtures it is served and the instant it is told it is running at, not of
when it happened to start. These tests pin that: the same clock twice gives the
same result, and moving the clock moves the AMI window with it.
"""

from __future__ import annotations

import json
from datetime import timedelta

from custom_components.sp_group.client import Session
from custom_components.sp_group.const import SENSOR_KEY_ELECTRICITY_TODAY
from custom_components.sp_group.history import trim_unreported
from custom_components.sp_group.mapper import extra_attributes, sensors_from_usage
from custom_components.sp_group.models import SG_TZ

from .conftest import (
    FIXED_NOW,
    FixedClock,
    FixtureTransport,
    fixture_client,
    load_fixture,
)

TOKEN_PAYLOAD = json.loads(load_fixture("oauth_token_success.json"))
EXPIRY_BUFFER = 60


def _poll_lines(clock: FixedClock) -> tuple[str, ...]:
    """Everything a poll derives: readings, sensor values, and attributes."""
    client = fixture_client(clock=clock)
    usage = client.fetch_usage()
    now = client.clock.now()
    specs = sensors_from_usage(usage, now)
    return (
        repr(usage),
        *(f"{spec.key}={spec.native_value!r}" for spec in specs),
        *(
            f"{spec.key}:{sorted(extra_attributes(usage, spec.key, now).items())!r}"
            for spec in specs
        ),
    )


def _ami_window_starts(clock: FixedClock) -> list[str]:
    transport = FixtureTransport()
    fixture_client(transport, clock).fetch_usage()
    return [
        json.loads(request.body.decode("utf-8"))["start"]
        for request in transport.requests
        if request.body is not None and b"premise_id" in request.body
    ]


def test_the_same_clock_replays_the_poll_exactly() -> None:
    first = _poll_lines(FixedClock())
    second = _poll_lines(FixedClock())
    assert first, "the fixture poll must produce something to compare"
    assert first == second


def test_the_clock_decides_which_ami_window_is_asked_for() -> None:
    """One day apart on the clock is one day apart in the requested window."""
    today = _ami_window_starts(FixedClock())
    yesterday = _ami_window_starts(FixedClock(FIXED_NOW - timedelta(days=1)))
    assert today and yesterday
    assert today != yesterday


def test_token_expiry_is_read_from_the_clock_not_the_host() -> None:
    """The same stored session is fresh or expired purely by the clock given."""
    session = Session(
        access_token=TOKEN_PAYLOAD["access_token"],
        id_token=TOKEN_PAYLOAD["id_token"],
        refresh_token=TOKEN_PAYLOAD["refresh_token"],
        expires_at=FixedClock().timestamp() + EXPIRY_BUFFER + 60,
    )
    assert not session.is_expired(FixedClock().timestamp())
    assert session.is_expired(FixedClock().timestamp() + 2 * EXPIRY_BUFFER)


def test_today_kwh_sums_the_slots_of_the_clocks_day() -> None:
    """The reading matches the clock's date, so a later run drops those slots."""
    usage = fixture_client().fetch_usage()
    slots = trim_unreported(usage.ami_hourly)
    assert slots, "the charts fixture must carry AMI half-hours"
    on_the_clock_day = sum(
        slot.amount
        for slot in slots
        if slot.start.astimezone(SG_TZ).date() == FIXED_NOW.date()
    )

    def today_kwh(now: FixedClock) -> float | str | None:
        specs = sensors_from_usage(usage, now.now())
        return next(
            spec.native_value
            for spec in specs
            if spec.key == SENSOR_KEY_ELECTRICITY_TODAY
        )

    assert on_the_clock_day > 0
    assert today_kwh(FixedClock()) == on_the_clock_day
    # A day the fixture has no slots for reads as zero, not as the old day.
    after_the_slots = FixedClock(FIXED_NOW + timedelta(days=1))
    assert today_kwh(after_the_slots) == 0.0
