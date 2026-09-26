"""Redacted diagnostics for a config entry."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_REFRESH_TOKEN
from .coordinator import SpGroupCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: SpGroupCoordinator = entry.runtime_data
    usage = coordinator.data
    if usage is None:
        return {
            "has_refresh_token": bool(entry.data.get(CONF_REFRESH_TOKEN)),
            "usage": None,
        }
    return {
        "has_refresh_token": bool(entry.data.get(CONF_REFRESH_TOKEN)),
        "premise_id": usage.premise_id,
        "has_address": bool(usage.premise.address),
        "has_account_number": bool(usage.premise.account_number),
        "account_status": usage.premise.account_status,
        "account_type": usage.premise.account_type,
        "premise_type": usage.premise.premise_type,
        "utilities": list(usage.premise.utilities),
        "ami_elec": usage.premise.ami_elec,
        "has_retailer": bool(usage.premise.retailer_name),
        "ppms_exists": usage.premise.ppms_exists,
        "ppms_credit": usage.ppms_credit,
        "electricity_kwh": usage.electricity_kwh,
        "water_m3": usage.water_m3,
        "has_gas": usage.gas is not None,
        "electricity_periods": len(usage.electricity_periods),
        "water_periods": len(usage.water_periods),
        "gas_periods": len(usage.gas_periods),
        "has_meter_reading": usage.meter_reading is not None,
        "bill_count": len(usage.bills),
        "has_amount_due": usage.amount_due is not None,
        "meter_register_count": len(usage.meter_registers),
        "green_goal_kinds": [goal.kind for goal in usage.green_goals],
        "has_greenup": usage.greenup is not None,
        "has_ev_wallet": usage.ev_wallet is not None,
        "has_ev_session": usage.ev_session is not None,
        "has_ev_last_charge": usage.ev_last_charge is not None,
        "has_ev_unpaid": usage.ev_unpaid is not None,
        "has_unread_notifications": usage.unread_notifications is not None,
        "has_bill_delivery": usage.bill_delivery is not None,
        "fcu_count": len(usage.fcus),
        "has_tariff": usage.tariff is not None,
    }
