"""Map Jarvis usage readings onto Energy-dashboard sensor specs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .const import (
    DEVICE_CLASS_ENERGY,
    DEVICE_CLASS_GAS,
    DEVICE_CLASS_MONETARY,
    DEVICE_CLASS_TEMPERATURE,
    DEVICE_CLASS_WATER,
    ENTITY_CATEGORY_DIAGNOSTIC,
    SENSOR_KEY_ACCOUNT,
    SENSOR_KEY_AMOUNT_DUE,
    SENSOR_KEY_BILL_DELIVERY,
    SENSOR_KEY_ELECTRICITY,
    SENSOR_KEY_ELECTRICITY_GOAL,
    SENSOR_KEY_ELECTRICITY_HOUR,
    SENSOR_KEY_ELECTRICITY_LAST,
    SENSOR_KEY_ELECTRICITY_METER,
    SENSOR_KEY_ELECTRICITY_TODAY,
    SENSOR_KEY_EV_LAST_CHARGE,
    SENSOR_KEY_EV_SESSION,
    SENSOR_KEY_EV_UNPAID,
    SENSOR_KEY_EV_WALLET,
    SENSOR_KEY_FCU,
    SENSOR_KEY_GAS,
    SENSOR_KEY_GAS_LAST,
    SENSOR_KEY_GREENUP_POINTS,
    SENSOR_KEY_LAST_BILL,
    SENSOR_KEY_PPMS,
    SENSOR_KEY_TARIFF,
    SENSOR_KEY_UNREAD_NOTIFICATIONS,
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
    UNIT_CELSIUS,
    UNIT_KWH,
    UNIT_M3,
    UNIT_SGD,
)
from .history import fold_half_hours, merge_ami_periods, trim_unreported
from .models import SG_TZ, FcuInfo, PeriodReading, UsageReadings, UtilitySeries


@dataclass(frozen=True)
class SensorSpec:
    key: str
    translation_key: str
    native_value: float | str | None
    device_class: str | None
    state_class: str | None
    unit_of_measurement: str | None
    entity_category: str | None = None
    suggested_display_precision: int | None = None
    name: str | None = None


_FCU_KEY_SAFE = re.compile(r"[^0-9A-Za-z]+")
_ISO_CURRENCY = re.compile(r"^[A-Za-z]{3}$")


def _currency(code: str | None) -> str:
    """ISO 4217 code the amount is in, so it is not labelled SGD when it is not."""
    return code.upper() if code and _ISO_CURRENCY.match(code) else UNIT_SGD


def _fcu_sensor_key(thing_name: str) -> str:
    safe = _FCU_KEY_SAFE.sub("_", thing_name).strip("_").lower()
    return f"{SENSOR_KEY_FCU}_{safe}" if safe else SENSOR_KEY_FCU


def _fcu_from_key(usage: UsageReadings, key: str) -> FcuInfo | None:
    for fcu in usage.fcus:
        if _fcu_sensor_key(fcu.thing_name) == key:
            return fcu
    if key == SENSOR_KEY_FCU and len(usage.fcus) == 1:
        return usage.fcus[0]
    return None


def _last_period(periods: tuple[PeriodReading, ...]) -> PeriodReading | None:
    if not periods:
        return None
    return max(periods, key=lambda item: item.start)


def _omit_none(attrs: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in attrs.items() if value is not None}


def reported_ami_slots(usage: UsageReadings) -> tuple[PeriodReading, ...]:
    return trim_unreported(usage.ami_hourly)


def electricity_graph_periods(usage: UsageReadings) -> tuple[PeriodReading, ...]:
    hourly = fold_half_hours(reported_ami_slots(usage))
    merged = merge_ami_periods(usage.ami_daily, hourly)
    if merged:
        return merged
    return usage.electricity.periods if usage.electricity else ()


def _today_kwh(usage: UsageReadings) -> float | None:
    slots = reported_ami_slots(usage)
    if not slots:
        return None
    today = datetime.now(SG_TZ).date()
    return sum(
        item.amount for item in slots if item.start.astimezone(SG_TZ).date() == today
    )


def _last_interval(usage: UsageReadings) -> PeriodReading | None:
    slots = reported_ami_slots(usage)
    if not slots:
        return None
    return max(slots, key=lambda item: item.start)


def extra_attributes(usage: UsageReadings, key: str) -> dict[str, object]:
    """Premise metadata plus last billed period for the matching utility."""
    premise = usage.premise
    attrs: dict[str, object] = {
        "premise_id": premise.id,
        "address": premise.address,
        "account_number": premise.account_number,
        "account_status": premise.account_status,
        "account_type": premise.account_type,
        "premise_type": premise.premise_type,
        "utilities": list(premise.utilities) if premise.utilities else None,
        "ami_elec": premise.ami_elec,
        "retailer_name": premise.retailer_name,
    }
    if key == SENSOR_KEY_ACCOUNT:
        reading = usage.meter_reading
        if reading is not None:
            attrs["meter_reading_message"] = reading.message
            attrs["meter_reading_title"] = reading.title
            attrs["meter_reading_start"] = reading.start
            attrs["meter_reading_end"] = reading.end
        return _omit_none(attrs)
    if key == SENSOR_KEY_LAST_BILL:
        bill = usage.last_bill
        if bill is not None:
            attrs["bill_date"] = bill.date
            attrs["bill_period"] = bill.period
            attrs["due_date"] = bill.due_date
            attrs["bill_account_number"] = bill.account_number
            attrs["bill_count"] = len(usage.bills)
        return _omit_none(attrs)
    if key == SENSOR_KEY_AMOUNT_DUE:
        due = usage.amount_due
        if due is not None:
            attrs["currency"] = due.currency
            attrs["giro_enabled"] = due.giro_enabled
            attrs["recurring_enabled"] = due.recurring_enabled
        return _omit_none(attrs)
    if key in {SENSOR_KEY_ELECTRICITY_METER, SENSOR_KEY_WATER_METER}:
        meter = usage.meter(
            "electric" if key == SENSOR_KEY_ELECTRICITY_METER else "water"
        )
        if meter is not None:
            attrs["meter_id"] = meter.meter_id
            attrs["last_actual_at"] = meter.last_actual_at
        return _omit_none(attrs)
    if key in {SENSOR_KEY_ELECTRICITY_GOAL, SENSOR_KEY_WATER_GOAL}:
        goal = usage.goal("elec" if key == SENSOR_KEY_ELECTRICITY_GOAL else "water")
        if goal is not None:
            attrs["goal_month"] = goal.month
            attrs["goal_target"] = goal.target
            attrs["percent_difference"] = goal.percent_difference
            attrs["cost_difference_sgd"] = goal.cost_difference_sgd
        return _omit_none(attrs)
    if key == SENSOR_KEY_GREENUP_POINTS:
        info = usage.greenup
        if info is not None:
            attrs["tier_name"] = info.tier_name
            attrs["tier_level"] = info.tier_level
            attrs["points_to_level_up"] = info.points_to_level_up
        return _omit_none(attrs)
    if key == SENSOR_KEY_EV_WALLET:
        wallet = usage.ev_wallet
        if wallet is not None:
            attrs["dollar_balance"] = wallet.dollar_balance
            attrs["current_tier_id"] = wallet.current_tier_id
        return _omit_none(attrs)
    if key == SENSOR_KEY_EV_SESSION:
        session = usage.ev_session
        if session is not None:
            attrs["kwh"] = session.kwh
            attrs["total_cost"] = session.total_cost
            attrs["start"] = session.start
            attrs["order_id"] = session.order_id
        return _omit_none(attrs)
    if key == SENSOR_KEY_EV_LAST_CHARGE:
        charge = usage.ev_last_charge
        if charge is not None:
            attrs["amount"] = charge.amount
            attrs["created_at"] = charge.created_at
            attrs["status"] = charge.status
            attrs["address"] = charge.address
        return _omit_none(attrs)
    if key == SENSOR_KEY_EV_UNPAID:
        unpaid = usage.ev_unpaid
        if unpaid is not None:
            attrs["order_count"] = unpaid.count
        return _omit_none(attrs)
    if key == SENSOR_KEY_FCU or key.startswith(f"{SENSOR_KEY_FCU}_"):
        fcu = _fcu_from_key(usage, key)
        if fcu is not None:
            attrs["thing_name"] = fcu.thing_name
            attrs["display_name"] = fcu.display_name
            attrs["is_on"] = fcu.is_on
            attrs["is_online"] = fcu.is_online
            attrs["setpoint"] = fcu.setpoint
            attrs["mode"] = fcu.mode
        return _omit_none(attrs)
    if key == SENSOR_KEY_TARIFF:
        tariff = usage.tariff
        if tariff is not None:
            attrs["monthly_price"] = tariff.monthly_price
            attrs["consumption"] = tariff.consumption
        return _omit_none(attrs)
    if key == SENSOR_KEY_BILL_DELIVERY:
        delivery = usage.bill_delivery
        if delivery is not None:
            attrs["soft_copy"] = delivery.soft_copy
            attrs["hard_copy"] = delivery.hard_copy
        return _omit_none(attrs)
    series: UtilitySeries | None
    if key in {
        SENSOR_KEY_ELECTRICITY,
        SENSOR_KEY_ELECTRICITY_LAST,
        SENSOR_KEY_ELECTRICITY_TODAY,
        SENSOR_KEY_ELECTRICITY_HOUR,
    }:
        series = usage.electricity
        if key == SENSOR_KEY_ELECTRICITY:
            graph = electricity_graph_periods(usage)
            if graph:
                last = _last_period(graph)
                attrs["period_count"] = len(graph)
                attrs["ami_half_hour_count"] = len(usage.ami_hourly)
                attrs["ami_daily_count"] = len(usage.ami_daily)
                if last is not None:
                    attrs["last_period"] = last.start.isoformat()
                    attrs["last_period_amount"] = last.amount
                today = _today_kwh(usage)
                if today is not None:
                    attrs["today_kwh"] = today
                last_slot = _last_interval(usage)
                if last_slot is not None:
                    attrs["last_interval"] = last_slot.start.isoformat()
                    attrs["last_interval_kwh"] = last_slot.amount
                return _omit_none(attrs)
    elif key in {SENSOR_KEY_WATER, SENSOR_KEY_WATER_LAST}:
        series = usage.water
    elif key in {SENSOR_KEY_GAS, SENSOR_KEY_GAS_LAST}:
        series = usage.gas
    else:
        series = None
    if series is None:
        return _omit_none(attrs)
    last = _last_period(series.periods)
    attrs["average_consumption"] = series.average
    attrs["comparison"] = series.comparison
    attrs["period_count"] = len(series.periods)
    if last is not None:
        attrs["last_period"] = last.start.isoformat()
        attrs["last_period_amount"] = last.amount
        attrs["last_period_previous"] = last.previous
        attrs["last_period_status"] = last.status
    return _omit_none(attrs)


def _last_spec(key: str, series: UtilitySeries, precision: int) -> SensorSpec:
    last = _last_period(series.periods)
    return SensorSpec(
        key=key,
        translation_key=key,
        native_value=last.amount if last is not None else None,
        device_class=None,
        state_class=STATE_CLASS_MEASUREMENT,
        unit_of_measurement=series.unit,
        suggested_display_precision=precision,
    )


def sensors_from_usage(usage: UsageReadings | None) -> list[SensorSpec]:
    """Return energy/water/gas sensors plus account diagnostics."""
    if usage is None:
        return []
    specs: list[SensorSpec] = []
    if usage.electricity is not None:
        graph = electricity_graph_periods(usage)
        elec_total = (
            sum(item.amount for item in graph) if graph else usage.electricity.total
        )
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_ELECTRICITY,
                translation_key=SENSOR_KEY_ELECTRICITY,
                native_value=elec_total,
                device_class=DEVICE_CLASS_ENERGY,
                state_class=STATE_CLASS_TOTAL_INCREASING,
                unit_of_measurement=UNIT_KWH,
                suggested_display_precision=1,
            )
        )
        specs.append(_last_spec(SENSOR_KEY_ELECTRICITY_LAST, usage.electricity, 1))
        today = _today_kwh(usage)
        if today is not None:
            specs.append(
                SensorSpec(
                    key=SENSOR_KEY_ELECTRICITY_TODAY,
                    translation_key=SENSOR_KEY_ELECTRICITY_TODAY,
                    native_value=today,
                    device_class=None,
                    state_class=STATE_CLASS_MEASUREMENT,
                    unit_of_measurement=UNIT_KWH,
                    suggested_display_precision=2,
                )
            )
        last_slot = _last_interval(usage)
        if last_slot is not None:
            specs.append(
                SensorSpec(
                    key=SENSOR_KEY_ELECTRICITY_HOUR,
                    translation_key=SENSOR_KEY_ELECTRICITY_HOUR,
                    native_value=last_slot.amount,
                    device_class=None,
                    state_class=STATE_CLASS_MEASUREMENT,
                    unit_of_measurement=UNIT_KWH,
                    suggested_display_precision=2,
                )
            )
    if usage.water is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_WATER,
                translation_key=SENSOR_KEY_WATER,
                native_value=usage.water.total,
                device_class=DEVICE_CLASS_WATER,
                state_class=None,
                unit_of_measurement=UNIT_M3,
                suggested_display_precision=2,
            )
        )
        specs.append(_last_spec(SENSOR_KEY_WATER_LAST, usage.water, 2))
    if usage.gas is not None:
        is_kwh = usage.gas.unit == UNIT_KWH
        gas_class = DEVICE_CLASS_ENERGY if is_kwh else DEVICE_CLASS_GAS
        precision = 1 if is_kwh else 2
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_GAS,
                translation_key=SENSOR_KEY_GAS,
                native_value=usage.gas.total,
                device_class=gas_class,
                state_class=STATE_CLASS_TOTAL_INCREASING,
                unit_of_measurement=usage.gas.unit,
                suggested_display_precision=precision,
            )
        )
        specs.append(_last_spec(SENSOR_KEY_GAS_LAST, usage.gas, precision))
    specs.append(
        SensorSpec(
            key=SENSOR_KEY_ACCOUNT,
            translation_key=SENSOR_KEY_ACCOUNT,
            native_value=usage.premise.account_status or "unknown",
            device_class=None,
            state_class=None,
            unit_of_measurement=None,
            entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        )
    )
    if usage.ppms_credit is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_PPMS,
                translation_key=SENSOR_KEY_PPMS,
                native_value=usage.ppms_credit,
                device_class=DEVICE_CLASS_MONETARY,
                state_class=None,
                unit_of_measurement=UNIT_SGD,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
                suggested_display_precision=2,
            )
        )
    if usage.last_bill is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_LAST_BILL,
                translation_key=SENSOR_KEY_LAST_BILL,
                native_value=usage.last_bill.amount_sgd,
                device_class=DEVICE_CLASS_MONETARY,
                state_class=STATE_CLASS_TOTAL,
                unit_of_measurement=UNIT_SGD,
                suggested_display_precision=2,
            )
        )
    if usage.amount_due is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_AMOUNT_DUE,
                translation_key=SENSOR_KEY_AMOUNT_DUE,
                native_value=usage.amount_due.amount_sgd,
                device_class=DEVICE_CLASS_MONETARY,
                state_class=None,
                unit_of_measurement=_currency(usage.amount_due.currency),
                suggested_display_precision=2,
            )
        )
    elec_meter = usage.meter("electric")
    if elec_meter is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_ELECTRICITY_METER,
                translation_key=SENSOR_KEY_ELECTRICITY_METER,
                native_value=elec_meter.value,
                device_class=DEVICE_CLASS_ENERGY,
                # A meter register is a running total; HA rejects measurement
                # for energy/water. total, not total_increasing, so a downward
                # SP correction is not taken for a meter reset.
                state_class=STATE_CLASS_TOTAL,
                unit_of_measurement=UNIT_KWH,
                suggested_display_precision=0,
            )
        )
    water_meter = usage.meter("water")
    if water_meter is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_WATER_METER,
                translation_key=SENSOR_KEY_WATER_METER,
                native_value=water_meter.value,
                device_class=DEVICE_CLASS_WATER,
                state_class=STATE_CLASS_TOTAL,
                unit_of_measurement=UNIT_M3,
                suggested_display_precision=1,
            )
        )
    elec_goal = usage.goal("elec")
    if elec_goal is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_ELECTRICITY_GOAL,
                translation_key=SENSOR_KEY_ELECTRICITY_GOAL,
                native_value=elec_goal.used,
                device_class=None,
                state_class=STATE_CLASS_MEASUREMENT,
                unit_of_measurement=UNIT_KWH,
                suggested_display_precision=1,
            )
        )
    water_goal = usage.goal("water")
    if water_goal is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_WATER_GOAL,
                translation_key=SENSOR_KEY_WATER_GOAL,
                native_value=water_goal.used,
                device_class=None,
                state_class=STATE_CLASS_MEASUREMENT,
                unit_of_measurement=UNIT_M3,
                suggested_display_precision=2,
            )
        )
    if usage.greenup is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_GREENUP_POINTS,
                translation_key=SENSOR_KEY_GREENUP_POINTS,
                native_value=usage.greenup.points,
                device_class=None,
                state_class=None,
                unit_of_measurement="points",
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
                suggested_display_precision=0,
            )
        )
    if usage.ev_wallet is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_EV_WALLET,
                translation_key=SENSOR_KEY_EV_WALLET,
                native_value=usage.ev_wallet.points,
                device_class=None,
                state_class=None,
                unit_of_measurement="points",
                suggested_display_precision=0,
            )
        )
    if usage.ev_session is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_EV_SESSION,
                translation_key=SENSOR_KEY_EV_SESSION,
                native_value=usage.ev_session.status or "unknown",
                device_class=None,
                state_class=None,
                unit_of_measurement=None,
            )
        )
    if usage.ev_last_charge is not None and usage.ev_last_charge.kwh is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_EV_LAST_CHARGE,
                translation_key=SENSOR_KEY_EV_LAST_CHARGE,
                native_value=usage.ev_last_charge.kwh,
                device_class=DEVICE_CLASS_ENERGY,
                state_class=None,
                unit_of_measurement=UNIT_KWH,
                suggested_display_precision=2,
            )
        )
    if usage.ev_unpaid is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_EV_UNPAID,
                translation_key=SENSOR_KEY_EV_UNPAID,
                native_value=(
                    usage.ev_unpaid.amount
                    if usage.ev_unpaid.amount is not None
                    else usage.ev_unpaid.count
                ),
                device_class=(
                    DEVICE_CLASS_MONETARY
                    if usage.ev_unpaid.amount is not None
                    else None
                ),
                state_class=None,
                unit_of_measurement=(
                    UNIT_SGD if usage.ev_unpaid.amount is not None else None
                ),
                suggested_display_precision=2,
            )
        )
    if usage.unread_notifications is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_UNREAD_NOTIFICATIONS,
                translation_key=SENSOR_KEY_UNREAD_NOTIFICATIONS,
                native_value=usage.unread_notifications,
                device_class=None,
                state_class=None,
                unit_of_measurement=None,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            )
        )
    if usage.bill_delivery is not None:
        delivery = (
            SENSOR_STATE_EBILL if usage.bill_delivery.soft_copy else SENSOR_STATE_PAPER
        )
        hard = usage.bill_delivery.hard_copy is True
        if usage.bill_delivery.soft_copy is None and hard:
            delivery = SENSOR_STATE_PAPER
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_BILL_DELIVERY,
                translation_key=SENSOR_KEY_BILL_DELIVERY,
                native_value=delivery,
                device_class=None,
                state_class=None,
                unit_of_measurement=None,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
            )
        )
    for fcu in usage.fcus:
        has_temp = fcu.room_temperature is not None
        specs.append(
            SensorSpec(
                key=_fcu_sensor_key(fcu.thing_name),
                translation_key=SENSOR_KEY_FCU,
                native_value=(
                    fcu.room_temperature
                    if has_temp
                    else (SENSOR_STATE_ON if fcu.is_on else SENSOR_STATE_OFF)
                ),
                device_class=DEVICE_CLASS_TEMPERATURE if has_temp else None,
                state_class=STATE_CLASS_MEASUREMENT if has_temp else None,
                unit_of_measurement=UNIT_CELSIUS if has_temp else None,
                suggested_display_precision=1,
                name=fcu.display_name or fcu.thing_name,
            )
        )
    if usage.tariff is not None and usage.tariff.kwh_price is not None:
        specs.append(
            SensorSpec(
                key=SENSOR_KEY_TARIFF,
                translation_key=SENSOR_KEY_TARIFF,
                native_value=usage.tariff.kwh_price,
                device_class=DEVICE_CLASS_MONETARY,
                state_class=None,
                unit_of_measurement=UNIT_SGD,
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
                suggested_display_precision=4,
            )
        )
    return specs
