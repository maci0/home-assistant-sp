"""Optional EV, GreenUP, FCU, and notification parsers skip empty accounts."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

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
    OPTIONAL_HTTP_TIMEOUT_SECONDS,
    SENSOR_KEY_EV_LAST_CHARGE,
    SENSOR_KEY_FCU,
    SENSOR_KEY_GREENUP_POINTS,
    SENSOR_KEY_UNREAD_NOTIFICATIONS,
    TARIFF_DEFAULT_CONSUMPTION_KWH,
    UNIT_CELSIUS,
)
from custom_components.sp_group.mapper import sensors_from_usage

from .conftest import FixtureTransport, fixture_client


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
    by_key = {spec.key: spec for spec in sensors_from_usage(usage)}
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
        def request(self, method, url, headers, body, *, timeout=None):
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
    by_key = {spec.key: spec for spec in sensors_from_usage(usage)}
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
