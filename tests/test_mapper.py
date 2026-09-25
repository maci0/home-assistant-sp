"""HA sensor specs from parsed usage; failed auth yields none."""

from __future__ import annotations

import json

import pytest

from custom_components.sp_group.client import AuthError, SpGroupClient
from custom_components.sp_group.const import (
    DEVICE_CLASS_ENERGY,
    DEVICE_CLASS_MONETARY,
    DEVICE_CLASS_WATER,
    ENTITY_CATEGORY_DIAGNOSTIC,
    SENSOR_KEY_ACCOUNT,
    SENSOR_KEY_AMOUNT_DUE,
    SENSOR_KEY_ELECTRICITY,
    SENSOR_KEY_ELECTRICITY_GOAL,
    SENSOR_KEY_ELECTRICITY_LAST,
    SENSOR_KEY_ELECTRICITY_METER,
    SENSOR_KEY_GAS,
    SENSOR_KEY_LAST_BILL,
    SENSOR_KEY_WATER,
    SENSOR_KEY_WATER_GOAL,
    SENSOR_KEY_WATER_LAST,
    SENSOR_KEY_WATER_METER,
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_TOTAL,
    STATE_CLASS_TOTAL_INCREASING,
    UNIT_KWH,
    UNIT_M3,
    UNIT_SGD,
)
from custom_components.sp_group.mapper import (
    _currency,
    extra_attributes,
    sensors_from_usage,
)

from .conftest import (
    FixtureTransport,
    billed_totals_from_charts_payload,
    fixture_client,
    load_fixture,
)


def test_sensors_match_energy_dashboard_contract() -> None:
    charts = json.loads(load_fixture("jarvis_charts.json"))
    expected_kwh, expected_m3 = billed_totals_from_charts_payload(charts)
    client = fixture_client()
    usage = client.fetch_usage()
    specs = sensors_from_usage(usage)
    by_key = {spec.key: spec for spec in specs}

    electricity = by_key["electricity"]
    # AMI daily 10+12 plus two folded hours of 1.0 kWh each.
    assert electricity.native_value == pytest.approx(24.0)
    assert electricity.device_class == DEVICE_CLASS_ENERGY
    assert electricity.state_class == STATE_CLASS_TOTAL_INCREASING
    assert electricity.unit_of_measurement == UNIT_KWH
    assert expected_kwh > 0

    water = by_key["water"]
    assert water.native_value == expected_m3
    assert water.device_class == DEVICE_CLASS_WATER
    assert water.state_class is None
    assert water.unit_of_measurement == UNIT_M3

    last_elec = by_key[SENSOR_KEY_ELECTRICITY_LAST]
    assert last_elec.state_class == STATE_CLASS_MEASUREMENT
    last_water = by_key[SENSOR_KEY_WATER_LAST]
    assert last_water.state_class == STATE_CLASS_MEASUREMENT

    account = by_key[SENSOR_KEY_ACCOUNT]
    assert account.native_value == "Active"
    assert account.entity_category == ENTITY_CATEGORY_DIAGNOSTIC
    assert SENSOR_KEY_GAS not in by_key

    elec_attrs = extra_attributes(usage, SENSOR_KEY_ELECTRICITY)
    water_attrs = extra_attributes(usage, SENSOR_KEY_WATER)
    assert elec_attrs["premise_id"] == usage.premise_id
    assert elec_attrs["account_number"] == "1234567890"
    assert elec_attrs["address"] == "1 Example Road, Singapore"
    assert elec_attrs["period_count"] == 4
    assert elec_attrs["ami_half_hour_count"] == 4
    assert elec_attrs["last_period_amount"] == pytest.approx(1.0)
    assert water_attrs["period_count"] == len(usage.water_periods)
    account_attrs = extra_attributes(usage, SENSOR_KEY_ACCOUNT)
    assert account_attrs["meter_reading_title"] == "Sep 2026"

    last_bill = by_key[SENSOR_KEY_LAST_BILL]
    assert last_bill.native_value == pytest.approx(203.69)
    assert last_bill.device_class == DEVICE_CLASS_MONETARY
    assert last_bill.state_class == STATE_CLASS_TOTAL
    assert last_bill.unit_of_measurement == UNIT_SGD
    bill_attrs = extra_attributes(usage, SENSOR_KEY_LAST_BILL)
    assert bill_attrs["bill_count"] == 2
    assert bill_attrs["bill_date"] == "2026-08-03T16:00:00Z"
    assert bill_attrs["due_date"] == "2026-08-17T16:00:00Z"
    assert "pdf_url" not in bill_attrs
    amount_due = by_key[SENSOR_KEY_AMOUNT_DUE]
    assert amount_due.native_value == pytest.approx(203.69)
    assert amount_due.device_class == DEVICE_CLASS_MONETARY
    elec_meter = by_key[SENSOR_KEY_ELECTRICITY_METER]
    assert elec_meter.native_value == pytest.approx(14256)
    # A meter register is a running total: HA rejects measurement for energy/water.
    assert elec_meter.state_class == STATE_CLASS_TOTAL
    water_meter = by_key[SENSOR_KEY_WATER_METER]
    assert water_meter.native_value == pytest.approx(931.4)
    assert water_meter.state_class == STATE_CLASS_TOTAL
    goal = by_key[SENSOR_KEY_ELECTRICITY_GOAL]
    assert goal.native_value == pytest.approx(1160.77)
    goal_attrs = extra_attributes(usage, SENSOR_KEY_ELECTRICITY_GOAL)
    assert goal_attrs["goal_target"] == pytest.approx(672.47)
    assert goal_attrs["cost_difference_sgd"] == pytest.approx(103.10)
    assert SENSOR_KEY_WATER_GOAL not in by_key


def test_gas_only_charts_yield_gas_sensors() -> None:
    client = fixture_client(FixtureTransport(charts_fixture="jarvis_charts_gas.json"))
    usage = client.fetch_usage()
    by_key = {spec.key: spec for spec in sensors_from_usage(usage)}
    assert SENSOR_KEY_ELECTRICITY not in by_key
    assert SENSOR_KEY_WATER not in by_key
    gas = by_key[SENSOR_KEY_GAS]
    assert gas.native_value == pytest.approx(17.7)
    assert gas.unit_of_measurement == UNIT_KWH
    assert gas.device_class == DEVICE_CLASS_ENERGY


def test_failed_auth_does_not_yield_sensor_values() -> None:
    client = SpGroupClient(transport=FixtureTransport(fail_login=True))
    with pytest.raises(AuthError):
        client.login("user@example.com", "wrong")
    assert sensors_from_usage(None) == []


def test_amount_due_unit_follows_the_payable_currency() -> None:
    """Monetary sensors carry the ISO code the API reported, not a fixed SGD."""
    assert _currency("usd") == "USD"
    assert _currency(None) == UNIT_SGD
    assert _currency("S$") == UNIT_SGD
