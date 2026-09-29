"""Optional EV, GreenUP, FCU, and notification parsers skip empty accounts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse

import pytest

from custom_components.sp_group.client import (
    HttpResponse,
    _eva_sgd,
    _parse_bill_delivery,
    _parse_ev_last_charge,
    _parse_ev_session,
    _parse_ev_unpaid,
    _parse_ev_wallet,
    _parse_fcu_status,
    _parse_greenup,
    _parse_paired_fcus,
    _parse_unread,
    _tariff_consumption,
)
from custom_components.sp_group.const import (
    DEVICE_CLASS_TEMPERATURE,
    EVA_LATEST_SESSION_PATH,
    FROSTY_FCU_STATUS_PATH,
    MAX_FCU_STATUS_READS,
    OPTIONAL_HTTP_TIMEOUT_SECONDS,
    SENSOR_KEY_EV_LAST_CHARGE,
    SENSOR_KEY_FCU,
    SENSOR_KEY_GREENUP_POINTS,
    SENSOR_KEY_UNREAD_NOTIFICATIONS,
    TARIFF_DEFAULT_CONSUMPTION_KWH,
    UNIT_CELSIUS,
)
from custom_components.sp_group.mapper import sensors_from_usage
from custom_components.sp_group.models import PeriodReading, UtilitySeries

from .conftest import FIXED_NOW, FixtureTransport, fixture_client


def test_greenup_and_unread_appear_when_payloads_exist() -> None:
    transport = FixtureTransport(
        responses={
            "/1up/authenticated/graphql": HttpResponse(
                200,
                json.dumps(
                    {
                        "data": {
                            "account": {
                                "node": {
                                    "totalPoints": 12,
                                    "projectedLevelStatus": "MAINTAIN",
                                    "tier": {
                                        "node": {
                                            "level": 1,
                                            "name": "Sprout",
                                            "pointsToLevelUp": 150,
                                        }
                                    },
                                }
                            }
                        }
                    }
                ).encode(),
            ),
            "/notifications/v1/notifications": HttpResponse(
                200,
                b'{"total_unread_notifications": 3}',
            ),
            "/eva/v2/order/receipts": HttpResponse(
                200,
                json.dumps(
                    {
                        "data": [
                            {
                                "total_consumption": 18.5,
                                "transaction_amount": 12.3,
                                "created_at": "2026-08-01T10:00:00+08:00",
                                "transaction_status": "COMPLETED",
                                "address": "Example Hub",
                            }
                        ]
                    }
                ).encode(),
            ),
        }
    )
    usage = fixture_client(transport).fetch_usage()
    assert usage.greenup is not None
    assert usage.greenup.points == 12
    assert usage.unread_notifications == 3
    assert usage.ev_last_charge is not None
    assert usage.ev_last_charge.kwh == 18.5
    by_key = {spec.key: spec for spec in sensors_from_usage(usage, FIXED_NOW)}
    assert by_key[SENSOR_KEY_GREENUP_POINTS].native_value == 12
    assert by_key[SENSOR_KEY_UNREAD_NOTIFICATIONS].native_value == 3
    assert by_key[SENSOR_KEY_EV_LAST_CHARGE].native_value == 18.5


def test_empty_optional_payloads_are_skipped() -> None:
    assert _parse_ev_wallet({"points_balance": 0, "dollar_balance": "0.00"}) is None
    assert _parse_ev_last_charge({"data": []}) is None
    assert _parse_ev_session({"data": None}) is None
    assert _parse_ev_unpaid({"data": {"orders": []}}) is None
    assert _parse_unread({}) is None
    assert _parse_bill_delivery({"preferences": []}, "1") is None
    paired = _parse_paired_fcus({"errors": [{"message": "Unauthorized"}], "data": None})
    assert paired == []
    assert _parse_greenup({"data": {"account": None}}) is None
    assert _parse_fcu_status({"fcu_not_paired": True}, "thing", "Living") is None


def test_eva_sgd_string_rules() -> None:
    assert _eva_sgd("12.30") == 12.3
    assert _eva_sgd("1230") == 12.3
    assert _eva_sgd(1230) == 12.3
    assert _eva_sgd(12.3) == 12.3
    assert _eva_sgd("50") == 50.0
    assert _eva_sgd(50) == 50.0
    assert _eva_sgd(None) is None
    assert _eva_sgd("") is None
    unpaid = _parse_ev_unpaid({"data": {"orders": [{"amount": "1850"}]}})
    assert unpaid is not None
    assert unpaid.amount == 18.5
    charge = _parse_ev_last_charge(
        {"data": [{"total_consumption": 1.2, "transaction_amount": "12.50"}]}
    )
    assert charge is not None
    assert charge.amount == 12.5


def test_eva_last_charge_keeps_a_zero_kwh_receipt() -> None:
    """0 kWh is a reading (a voided or refunded charge), not a missing one.

    Falling through on a falsy value reported connector_kwh instead, and a
    receipt with no amount reported no kWh at all, which dropped the sensor.
    """
    voided = _parse_ev_last_charge({"data": [{"total_consumption": 0.0}]})
    assert voided is not None
    assert voided.kwh == 0.0

    charged = _parse_ev_last_charge(
        {"data": [{"total_consumption": 0.0, "connector_kwh": 7.5}]}
    )
    assert charged is not None
    assert charged.kwh == 0.0

    without_total = _parse_ev_last_charge({"data": [{"connector_kwh": 7.5}]})
    assert without_total is not None
    assert without_total.kwh == 7.5


def test_eva_scope_not_found_skips_remaining_eva() -> None:
    transport = FixtureTransport(
        responses={
            EVA_LATEST_SESSION_PATH: HttpResponse(403, b'{"error":"scope_not_found"}')
        }
    )
    usage = fixture_client(transport).fetch_usage()
    assert usage.ev_session is None
    assert usage.ev_last_charge is None
    assert usage.ev_unpaid is None
    # A denied scope must not send the remaining EVA reads.
    eva_paths = [urlparse(req.url).path for req in transport.requests]
    assert [path for path in eva_paths if path.startswith("/eva/")] == [
        EVA_LATEST_SESSION_PATH
    ]


def test_a_denied_eva_scope_is_reported_at_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 403 on the grant is not the expected "not signed up" case.

    Filed with the other 4xx it reads as a steady state, and the three Eva
    sensors stay unset with nothing in a normal log saying why.
    """
    transport = FixtureTransport(
        responses={
            EVA_LATEST_SESSION_PATH: HttpResponse(403, b'{"error":"scope_not_found"}')
        }
    )
    with caplog.at_level("WARNING"):
        usage = fixture_client(transport).fetch_usage()

    assert usage.ev_session is None
    assert "403" in caplog.text
    assert EVA_LATEST_SESSION_PATH in caplog.text


def test_the_denied_scope_code_is_matched_folded() -> None:
    """The code is server text, so its case and its width are not its identity."""
    transport = FixtureTransport(
        responses={
            EVA_LATEST_SESSION_PATH: HttpResponse(403, b'{"error":"Scope_Not_Found"}')
        }
    )
    usage = fixture_client(transport).fetch_usage()
    assert usage.ev_session is None
    eva_paths = [urlparse(req.url).path for req in transport.requests]
    assert [path for path in eva_paths if path.startswith("/eva/")] == [
        EVA_LATEST_SESSION_PATH
    ]


def test_optional_calls_use_short_timeout() -> None:
    transport = FixtureTransport()
    fixture_client(transport).fetch_usage()
    eva = [
        req
        for req in transport.requests
        if "/eva/" in req.url or "/1up/" in req.url or "/frosty/" in req.url
    ]
    assert eva
    assert all(req.timeout == OPTIONAL_HTTP_TIMEOUT_SECONDS for req in eva)
    me = next(req for req in transport.requests if req.url.endswith("/jarvis/v3/me"))
    assert me.timeout == 30


def test_paired_fcus_each_get_a_sensor() -> None:
    class TwoFcu(FixtureTransport):
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
            if method == "POST" and parsed.path == "/frosty/graphql":
                return HttpResponse(
                    200,
                    json.dumps(
                        {
                            "data": {
                                "getPairedFCUs": [
                                    {
                                        "displayName": "Living",
                                        "thingName": "tengah-living",
                                    },
                                    {
                                        "displayName": "Bedroom",
                                        "thingName": "tengah-bed",
                                    },
                                ]
                            }
                        }
                    ).encode(),
                )
            if method == "GET" and parsed.path == "/frosty/fcu_status":
                thing = parse_qs(parsed.query).get("thingName", [""])[0]
                temp = 24.5 if thing == "tengah-living" else 22.0
                return HttpResponse(
                    200,
                    json.dumps(
                        {
                            "is_on": True,
                            "is_online": True,
                            "room_temperature": temp,
                            "temperature": 25,
                            "operation_mode": "cool",
                        }
                    ).encode(),
                )
            return super().request(method, url, headers, body, timeout=timeout)

    usage = fixture_client(TwoFcu()).fetch_usage()
    assert len(usage.fcus) == 2
    by_key = {spec.key: spec for spec in sensors_from_usage(usage, FIXED_NOW)}
    living = by_key["fcu_tengah_living"]
    bed = by_key["fcu_tengah_bed"]
    assert living.native_value == 24.5
    assert living.name == "Living"
    assert bed.native_value == 22.0
    assert bed.name == "Bedroom"
    # Temperature device class so Home Assistant converts to the user unit system.
    assert living.device_class == DEVICE_CLASS_TEMPERATURE
    assert living.unit_of_measurement == UNIT_CELSIUS
    assert living.translation_key == SENSOR_KEY_FCU


def test_tariff_consumption_from_last_billed_kwh() -> None:
    transport = FixtureTransport()
    fixture_client(transport).fetch_usage()
    urls = [req.url for req in transport.requests if "priceplan" in req.url]
    assert urls
    assert "consumption=142" in urls[0]
    gas = FixtureTransport(charts_fixture="jarvis_charts_gas.json")
    fixture_client(gas).fetch_usage()
    gas_urls = [req.url for req in gas.requests if "priceplan" in req.url]
    assert f"consumption={TARIFF_DEFAULT_CONSUMPTION_KWH}" in gas_urls[0]
    assert _tariff_consumption(None) == str(TARIFF_DEFAULT_CONSUMPTION_KWH)


def test_tariff_consumption_rounds_half_up_not_to_even() -> None:
    """A .5 kWh period rounds up; round() would send 142 for 142.5."""

    def consumption_for(amount: float) -> str:
        return _tariff_consumption(
            UtilitySeries(
                total=amount,
                unit="kWh",
                periods=(PeriodReading(start=FIXED_NOW, amount=amount),),
                average=None,
                comparison=None,
            )
        )

    assert consumption_for(142.5) == "143"
    assert consumption_for(141.5) == "142"
    assert consumption_for(2.5) == "3"
    # Below half a kWh still falls back to the default, as before.
    assert consumption_for(0.4) == str(TARIFF_DEFAULT_CONSUMPTION_KWH)


def test_eva_sgd_rounds_money_half_up() -> None:
    """Banker's rounding put a .xx5 cent tie a cent under the billed amount."""
    assert _eva_sgd("2.675") == 2.68
    assert _eva_sgd("2.665") == 2.67
    assert _eva_sgd("0.005") == 0.01
    assert _eva_sgd("0.015") == 0.02


def test_eva_sgd_cents_decision_follows_the_magnitude_not_the_spelling() -> None:
    """'1e3' is the same amount as '1000'; both are cents, 10.00 either way."""
    assert _eva_sgd("1e3") == _eva_sgd("1000") == 10.0
    assert _eva_sgd("1_0") == _eva_sgd("10") == 10.0
    # A fractional value stays dollars however it is written.
    assert _eva_sgd("1.0e3") == 1000.0


def test_eva_sgd_drops_money_no_bill_can_carry() -> None:
    """A hostile magnitude is unparseable, not a poll-wide crash."""
    assert _eva_sgd(1e308) is None
    assert _eva_sgd("1e308") is None
    assert _eva_sgd(float("nan")) is None
    assert _eva_sgd("inf") is None


def test_unpaid_orders_total_sums_in_cents() -> None:
    """12.30 + 7.35 is 19.649999999999999 as floats, not 19.65."""
    unpaid = _parse_ev_unpaid(
        {"data": {"orders": [{"amount": "12.30"}, {"amount": "7.35"}]}}
    )
    assert unpaid is not None
    assert unpaid.amount == 19.65


def test_a_long_paired_fcu_list_is_read_up_to_the_cap() -> None:
    """Each coil is its own request, and the list is upstream data."""

    class ManyFcus(FixtureTransport):
        status_reads = 0

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
            if method == "POST" and parsed.path == "/frosty/graphql":
                return HttpResponse(
                    200,
                    json.dumps(
                        {
                            "data": {
                                "getPairedFCUs": [
                                    {
                                        "displayName": f"Coil {index}",
                                        "thingName": f"tengah-{index}",
                                    }
                                    for index in range(MAX_FCU_STATUS_READS + 5)
                                ]
                            }
                        }
                    ).encode(),
                )
            if method == "GET" and parsed.path == FROSTY_FCU_STATUS_PATH:
                ManyFcus.status_reads += 1
                return HttpResponse(200, json.dumps({"is_on": True}).encode())
            return super().request(method, url, headers, body, timeout=timeout)

    usage = fixture_client(ManyFcus()).fetch_usage()
    assert len(usage.fcus) == MAX_FCU_STATUS_READS
    assert ManyFcus.status_reads == MAX_FCU_STATUS_READS
