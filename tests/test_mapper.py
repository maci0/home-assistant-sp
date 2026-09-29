"""HA sensor specs from parsed usage; failed auth yields none."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from custom_components.sp_group.client import AuthError, SpGroupClient
from custom_components.sp_group.const import (
    DEVICE_CLASS_ENERGY,
    DEVICE_CLASS_MONETARY,
    DEVICE_CLASS_WATER,
    ENTITY_CATEGORY_DIAGNOSTIC,
    SENSOR_KEY_ACCOUNT,
    SENSOR_KEY_AMOUNT_DUE,
    SENSOR_KEY_BILL_DELIVERY,
    SENSOR_KEY_ELECTRICITY,
    SENSOR_KEY_ELECTRICITY_GOAL,
    SENSOR_KEY_ELECTRICITY_LAST,
    SENSOR_KEY_ELECTRICITY_METER,
    SENSOR_KEY_EV_UNPAID,
    SENSOR_KEY_GAS,
    SENSOR_KEY_LAST_BILL,
    SENSOR_KEY_PPMS,
    SENSOR_KEY_WATER,
    SENSOR_KEY_WATER_GOAL,
    SENSOR_KEY_WATER_LAST,
    SENSOR_KEY_WATER_METER,
    SENSOR_STATE_EBILL,
    SENSOR_STATE_OFF,
    SENSOR_STATE_ON,
    SENSOR_STATE_PAPER,
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_TOTAL,
    STATE_CLASS_TOTAL_INCREASING,
    UNIT_KWH,
    UNIT_M3,
    UNIT_SGD,
)
from custom_components.sp_group.mapper import (
    _currency,
    _fcu_from_key,
    _fcu_sensor_key,
    extra_attributes,
    sensors_from_usage,
)
from custom_components.sp_group.models import (
    BillDeliveryInfo,
    EvUnpaidInfo,
    FcuInfo,
    UsageReadings,
)

from .conftest import (
    FIXED_NOW,
    FixedClock,
    FixtureTransport,
    billed_totals_from_charts_payload,
    fixture_client,
    load_fixture,
)


def _usage_with(**overrides: object) -> UsageReadings:
    """A real fixture poll with the named fields swapped, for optional branches."""
    return replace(fixture_client().fetch_usage(), **overrides)


def test_sensors_match_energy_dashboard_contract() -> None:
    charts = json.loads(load_fixture("jarvis_charts.json"))
    expected_kwh, expected_m3 = billed_totals_from_charts_payload(charts)
    client = fixture_client()
    usage = client.fetch_usage()
    specs = sensors_from_usage(usage, FIXED_NOW)
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

    elec_attrs = extra_attributes(usage, SENSOR_KEY_ELECTRICITY, FIXED_NOW)
    water_attrs = extra_attributes(usage, SENSOR_KEY_WATER, FIXED_NOW)
    # Premise identifiers belong to the account sensor only.
    assert "account_number" not in elec_attrs
    assert "address" not in water_attrs
    assert elec_attrs["period_count"] == 4
    assert elec_attrs["ami_half_hour_count"] == 4
    assert elec_attrs["last_period_amount"] == pytest.approx(1.0)
    assert water_attrs["period_count"] == len(usage.water_periods)
    account_attrs = extra_attributes(usage, SENSOR_KEY_ACCOUNT, FIXED_NOW)
    assert account_attrs["premise_id"] == usage.premise_id
    assert account_attrs["account_number"] == "1234567890"
    assert account_attrs["address"] == "1 Example Road, Singapore"
    assert account_attrs["meter_reading_title"] == "Sep 2026"

    last_bill = by_key[SENSOR_KEY_LAST_BILL]
    assert last_bill.native_value == pytest.approx(203.69)
    assert last_bill.device_class == DEVICE_CLASS_MONETARY
    assert last_bill.state_class == STATE_CLASS_TOTAL
    assert last_bill.unit_of_measurement == UNIT_SGD
    bill_attrs = extra_attributes(usage, SENSOR_KEY_LAST_BILL, FIXED_NOW)
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
    goal_attrs = extra_attributes(usage, SENSOR_KEY_ELECTRICITY_GOAL, FIXED_NOW)
    assert goal_attrs["goal_target"] == pytest.approx(672.47)
    assert goal_attrs["cost_difference_sgd"] == pytest.approx(103.10)
    assert SENSOR_KEY_WATER_GOAL not in by_key


@pytest.mark.parametrize(
    ("soft_copy", "hard_copy", "expected"),
    [
        pytest.param(True, False, SENSOR_STATE_EBILL, id="soft-only"),
        pytest.param(True, True, SENSOR_STATE_EBILL, id="soft-and-hard"),
        pytest.param(False, True, SENSOR_STATE_PAPER, id="hard-only"),
        pytest.param(None, True, SENSOR_STATE_PAPER, id="unknown-soft-hard-copies"),
        pytest.param(None, None, SENSOR_STATE_PAPER, id="both-unknown"),
    ],
)
def test_bill_delivery_state_follows_the_copy_preference(
    soft_copy: bool | None, hard_copy: bool | None, expected: str
) -> None:
    """The state is one of the shipped translated values, never a raw bool."""
    usage = _usage_with(
        bill_delivery=BillDeliveryInfo(soft_copy=soft_copy, hard_copy=hard_copy)
    )

    spec = {item.key: item for item in sensors_from_usage(usage)}[
        SENSOR_KEY_BILL_DELIVERY
    ]

    assert spec.native_value == expected
    assert spec.native_value in {SENSOR_STATE_EBILL, SENSOR_STATE_PAPER}


def test_amount_due_carries_the_payable_currency() -> None:
    """A USD payable is labelled USD on the entity, not hard-coded SGD."""
    usage = _usage_with()
    due = replace(usage.amount_due, currency="usd")
    usage = replace(usage, amount_due=due)

    spec = {item.key: item for item in sensors_from_usage(usage)}[SENSOR_KEY_AMOUNT_DUE]

    assert spec.native_value == pytest.approx(203.69)
    assert spec.unit_of_measurement == "USD"
    assert extra_attributes(usage, SENSOR_KEY_AMOUNT_DUE)["currency"] == "usd"


@pytest.mark.parametrize(
    ("unpaid", "expected_value", "expected_unit"),
    [
        pytest.param(
            EvUnpaidInfo(count=2, amount=18.5), 18.5, UNIT_SGD, id="with-amount"
        ),
        pytest.param(EvUnpaidInfo(count=2, amount=None), 2, None, id="count-only"),
    ],
)
def test_ev_unpaid_falls_back_to_the_order_count(
    unpaid: EvUnpaidInfo, expected_value: float, expected_unit: str | None
) -> None:
    usage = _usage_with(ev_unpaid=unpaid)

    spec = {item.key: item for item in sensors_from_usage(usage)}[SENSOR_KEY_EV_UNPAID]

    assert spec.native_value == expected_value
    assert spec.unit_of_measurement == expected_unit
    assert (spec.device_class == DEVICE_CLASS_MONETARY) is (expected_unit == UNIT_SGD)
    assert extra_attributes(usage, SENSOR_KEY_EV_UNPAID)["order_count"] == 2


@pytest.mark.parametrize(
    ("is_on", "expected_state"),
    [(True, SENSOR_STATE_ON), (False, SENSOR_STATE_OFF)],
)
def test_fcu_without_a_temperature_reports_on_and_off(
    is_on: bool, expected_state: str
) -> None:
    usage = _usage_with(
        fcus=(
            FcuInfo(
                thing_name="Tengah Living 1!",
                display_name="Living",
                is_on=is_on,
                is_online=True,
                room_temperature=None,
                setpoint=25,
                mode="cool",
            ),
        )
    )

    specs = {item.key: item for item in sensors_from_usage(usage)}

    assert "fcu_tengah_living_1" in specs
    spec = specs["fcu_tengah_living_1"]
    assert spec.native_value == expected_state
    assert spec.device_class is None
    assert spec.state_class is None
    assert spec.unit_of_measurement is None
    attrs = extra_attributes(usage, "fcu_tengah_living_1")
    assert attrs["thing_name"] == "Tengah Living 1!"
    assert attrs["is_on"] is is_on
    assert attrs["setpoint"] == 25


def test_last_period_sensor_is_empty_when_the_utility_has_no_periods() -> None:
    usage = _usage_with()
    assert usage.electricity is not None
    usage = replace(
        usage,
        electricity=replace(usage.electricity, periods=()),
    )

    specs = {item.key: item for item in sensors_from_usage(usage)}

    assert specs[SENSOR_KEY_ELECTRICITY_LAST].native_value is None
    assert specs[SENSOR_KEY_ELECTRICITY_LAST].state_class == STATE_CLASS_MEASUREMENT
    assert SENSOR_KEY_ELECTRICITY in specs


def test_ppms_credit_sensor_appears_only_when_enrolled() -> None:
    usage = _usage_with(ppms_credit=42.5, ppms_updated_at="2026-09-01T00:00:00Z")
    assert SENSOR_KEY_PPMS not in {
        item.key for item in sensors_from_usage(_usage_with())
    }

    spec = {item.key: item for item in sensors_from_usage(usage)}[SENSOR_KEY_PPMS]

    assert spec.native_value == pytest.approx(42.5)
    assert spec.device_class == DEVICE_CLASS_MONETARY
    assert spec.unit_of_measurement == UNIT_SGD
    assert spec.entity_category == ENTITY_CATEGORY_DIAGNOSTIC
    assert spec.state_class is None


def test_gas_only_charts_yield_gas_sensors() -> None:
    client = fixture_client(FixtureTransport(charts_fixture="jarvis_charts_gas.json"))
    usage = client.fetch_usage()
    by_key = {spec.key: spec for spec in sensors_from_usage(usage, FIXED_NOW)}
    assert SENSOR_KEY_ELECTRICITY not in by_key
    assert SENSOR_KEY_WATER not in by_key
    gas = by_key[SENSOR_KEY_GAS]
    assert gas.native_value == pytest.approx(17.7)
    assert gas.unit_of_measurement == UNIT_KWH
    assert gas.device_class == DEVICE_CLASS_ENERGY


def test_failed_auth_does_not_yield_sensor_values() -> None:
    client = SpGroupClient(
        transport=FixtureTransport(fail_login=True), clock=FixedClock()
    )
    with pytest.raises(AuthError):
        client.login("user@example.com", "wrong")
    assert sensors_from_usage(None, FIXED_NOW) == []


def test_amount_due_unit_follows_the_payable_currency() -> None:
    """Monetary sensors carry the ISO code the API reported, not a fixed SGD."""
    assert _currency("usd") == "USD"
    assert _currency(None) == UNIT_SGD
    assert _currency("S$") == UNIT_SGD
    # A full-width code is the same code, and a trailing newline is not part
    # of one: neither may reach Home Assistant as a unit of measurement.
    assert _currency("ＵＳＤ") == "USD"
    assert _currency("USD\n") == UNIT_SGD


def test_fcu_key_folds_the_two_spellings_of_one_name() -> None:
    """NFC and NFD spellings of a coil name are one coil, not two entities."""
    assert _fcu_sensor_key("Café Coil") == _fcu_sensor_key("Café Coil")
    # A name the safe form can spell keeps the key it always had.
    assert _fcu_sensor_key("Tengah-001") == "fcu_tengah_001"


def test_fcu_keys_stay_distinct_when_characters_are_dropped() -> None:
    """Two coils whose names differ only in a dropped character stay apart."""
    assert _fcu_sensor_key("Tengah-001") != _fcu_sensor_key("Tengah–001")
    assert _fcu_sensor_key("客厅") != _fcu_sensor_key("Kamar")


def test_fcu_lookup_returns_the_own_readings_of_each_coil() -> None:
    usage = fixture_client().fetch_usage()

    def coil(thing_name: str, temperature: float) -> FcuInfo:
        return FcuInfo(
            thing_name=thing_name,
            display_name=None,
            is_on=True,
            is_online=True,
            room_temperature=temperature,
            setpoint=24.0,
            mode="cool",
        )

    cool = coil("Cool–01", 21.5)
    other = coil("Cool-01", 29.0)
    two = replace(usage, fcus=(cool, other))
    by_key = {spec.key: spec for spec in sensors_from_usage(two)}
    assert by_key[_fcu_sensor_key("Cool–01")].native_value == pytest.approx(21.5)
    assert by_key[_fcu_sensor_key("Cool-01")].native_value == pytest.approx(29.0)
    assert _fcu_from_key(two, _fcu_sensor_key("Cool–01")) is cool
