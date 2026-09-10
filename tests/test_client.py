"""Drive the shipped SpGroupClient against APK-shaped HTTP fixtures."""

from __future__ import annotations

import json
import time

import pytest

from custom_components.sp_group.client import (
    AuthError,
    MfaChallenge,
    Session,
    SpGroupClient,
    _mfa_channel_from_challenge,
    _oob_factor_authenticator_id,
    _pick_mfa_factor,
)
from custom_components.sp_group.const import (
    AUTH0_AUDIENCE,
    AUTH0_CLIENT_ID,
    AUTH0_GRANT_TYPE,
    AUTH0_MFA_AUTHENTICATORS_PATH,
    AUTH0_MFA_CHALLENGE_PATH,
    AUTH0_MFA_OAUTH_HOST,
    AUTH0_MFA_OOB_GRANT,
    AUTH0_MFA_OTP_GRANT,
    AUTH0_REALM,
    AUTH0_REFRESH_GRANT,
    AUTH0_SCOPE,
    B2C_HOST,
    CONTENT_TYPE_JSON,
    HEADER_ID_TOKEN,
    IDENTITY_HOST,
    JARVIS_AMI_PATH,
    JARVIS_CHARTS_PATH,
    JARVIS_GREEN_GOALS_PATH,
    JARVIS_ME_PATH,
    NJORD_HISTORY_PATH,
    NJORD_PAYABLES_PATH,
    OAUTH_TOKEN_PATH,
    USER_AGENT,
)

from .conftest import (
    FixtureTransport,
    billed_totals_from_charts_payload,
    fixture_client,
    load_fixture,
)


def test_login_returns_access_token_from_fixture() -> None:
    token_payload = json.loads(load_fixture("oauth_token_success.json"))
    transport = FixtureTransport()
    client = SpGroupClient(transport=transport)
    session = client.login("user@example.com", "secret")
    assert session.access_token == token_payload["access_token"]
    assert session.id_token == token_payload["id_token"]
    assert session.refresh_token == token_payload["refresh_token"]


def test_login_sends_auth0_password_realm_body() -> None:
    transport = FixtureTransport()
    client = SpGroupClient(transport=transport)
    client.login("user@example.com", "secret")
    recorded = transport.requests[0]
    assert recorded.method == "POST"
    assert recorded.url == f"{IDENTITY_HOST}{OAUTH_TOKEN_PATH}"
    assert recorded.headers["Content-Type"] == CONTENT_TYPE_JSON
    assert recorded.headers["Accept-Language"] == "en_US"
    assert recorded.headers["User-Agent"] == USER_AGENT
    assert recorded.body is not None
    body = json.loads(recorded.body.decode("utf-8"))
    assert body == {
        "client_id": AUTH0_CLIENT_ID,
        "audience": AUTH0_AUDIENCE,
        "username": "user@example.com",
        "password": "secret",
        "scope": AUTH0_SCOPE,
        "grant_type": AUTH0_GRANT_TYPE,
        "realm": AUTH0_REALM,
    }


def test_login_scope_includes_me_rbac() -> None:
    assert "me:rbac" in AUTH0_SCOPE
    assert "me:uportal" in AUTH0_SCOPE


def test_mfa_challenge_exposes_token_and_submit_sends_otp() -> None:
    transport = FixtureTransport(require_mfa=True, mfa_success=True)
    client = SpGroupClient(transport=transport)

    with pytest.raises(AuthError) as raised:
        client.login("user@example.com", "secret")

    assert raised.value.error == "mfa_required"
    assert raised.value.mfa_token == "mfa-token"

    session = client.submit_mfa(raised.value.mfa_token, "123456")
    assert session.access_token == "mfa-access-token"
    assert session.id_token == "mfa-id-token"
    assert session.refresh_token == "mfa-refresh-token"
    recorded = transport.requests[1]
    assert recorded.body is not None
    body = json.loads(recorded.body.decode("utf-8"))
    assert body == {
        "grant_type": AUTH0_MFA_OTP_GRANT,
        "client_id": AUTH0_CLIENT_ID,
        "mfa_token": "mfa-token",
        "otp": "123456",
    }


def test_mfa_oob_lists_challenges_and_submits_binding_code() -> None:
    transport = FixtureTransport(mfa_oob=True, mfa_success=True)
    client = SpGroupClient(transport=transport)

    with pytest.raises(AuthError) as raised:
        client.login("user@example.com", "secret")

    assert raised.value.error == "mfa_required"
    assert raised.value.mfa_token == "mfa-token"

    authenticators = client.list_mfa_authenticators("mfa-token")
    assert authenticators
    sms = next(
        factor
        for factor in authenticators
        if factor["authenticator_type"] == "oob" and factor["oob_channel"] == "sms"
    )
    assert sms["id"] == "sms|dev_abc123"

    listed = transport.requests[1]
    assert listed.method == "GET"
    assert listed.url == f"{AUTH0_MFA_OAUTH_HOST}{AUTH0_MFA_AUTHENTICATORS_PATH}"
    assert listed.headers["Authorization"] == "Bearer mfa-token"

    challenge = client.challenge_mfa("mfa-token", "sms|dev_abc123")
    assert challenge.oob_code == "oob-code"
    assert challenge.binding_method == "prompt"

    challenged = transport.requests[2]
    assert challenged.method == "POST"
    assert challenged.url == f"{AUTH0_MFA_OAUTH_HOST}{AUTH0_MFA_CHALLENGE_PATH}"
    assert challenged.headers["Authorization"] == "Bearer mfa-token"
    assert challenged.body is not None
    challenge_body = json.loads(challenged.body.decode("utf-8"))
    assert challenge_body == {
        "client_id": AUTH0_CLIENT_ID,
        "mfa_token": "mfa-token",
        "challenge_type": "oob",
        "authenticator_id": "sms|dev_abc123",
    }

    session = client.submit_mfa_oob("mfa-token", "oob-code", "654321")
    assert session.access_token == "mfa-access-token"
    assert session.id_token == "mfa-id-token"
    assert session.refresh_token == "mfa-refresh-token"

    submitted = transport.requests[3]
    assert submitted.url == f"{IDENTITY_HOST}{OAUTH_TOKEN_PATH}"
    assert submitted.body is not None
    submit_body = json.loads(submitted.body.decode("utf-8"))
    assert submit_body == {
        "grant_type": AUTH0_MFA_OOB_GRANT,
        "client_id": AUTH0_CLIENT_ID,
        "mfa_token": "mfa-token",
        "oob_code": "oob-code",
        "binding_code": "654321",
    }


def test_list_mfa_authenticators_parses_bare_array_response() -> None:
    # Regression: Auth0 /mfa/authenticators returns a BARE JSON array, not
    # {"authenticators": [...]}. The old parser coerced it to {} and listed
    # zero factors, so no OOB challenge (SMS) was ever fired.
    transport = FixtureTransport(authenticators_bare=True)
    client = SpGroupClient(transport=transport)

    authenticators = client.list_mfa_authenticators("mfa-token")

    assert {factor["id"] for factor in authenticators} == {
        "recovery-code|dev_abc123",
        "sms|dev_abc123",
        "email|dev_abc123",
    }
    sms = next(
        factor
        for factor in authenticators
        if factor["authenticator_type"] == "oob" and factor["oob_channel"] == "sms"
    )
    assert sms["id"] == "sms|dev_abc123"
    assert sms["type"] == "phone"


def test_pick_mfa_factor_sms_only() -> None:
    factor = _pick_mfa_factor(
        (
            {
                "id": "sms|dev_abc123",
                "authenticator_type": "oob",
                "oob_channel": "sms",
                "type": "phone",
            },
            {
                "id": "recovery-code|dev_abc123",
                "authenticator_type": "recovery-code",
                "active": True,
                "type": "recovery-code",
            },
        )
    )
    assert factor is not None
    assert factor["oob_channel"] == "sms"
    assert factor["authenticator_type"] == "oob"


def test_pick_mfa_factor_email_only() -> None:
    factor = _pick_mfa_factor(
        (
            {
                "id": "email|dev_abc123",
                "authenticator_type": "oob",
                "oob_channel": "email",
                "active": True,
                "type": "email",
            },
        )
    )
    assert factor is not None
    assert factor["oob_channel"] == "email"


def test_pick_mfa_factor_prefers_sms_over_email() -> None:
    factor = _pick_mfa_factor(
        (
            {
                "id": "email|dev_abc123",
                "authenticator_type": "oob",
                "oob_channel": "email",
                "active": True,
                "type": "email",
            },
            {
                "id": "sms|dev_abc123",
                "authenticator_type": "oob",
                "oob_channel": "sms",
                "active": True,
                "type": "phone",
            },
        )
    )
    assert factor is not None
    assert factor["oob_channel"] == "sms"


def test_pick_mfa_factor_prefers_totp_over_sms() -> None:
    factor = _pick_mfa_factor(
        (
            {
                "id": "sms|dev_abc123",
                "authenticator_type": "oob",
                "oob_channel": "sms",
                "active": True,
                "type": "phone",
            },
            {
                "id": "totp|dev_abc123",
                "authenticator_type": "otp",
                "active": True,
                "type": "totp",
            },
        )
    )
    assert factor is not None
    assert factor["authenticator_type"] == "otp"
    assert factor["id"] == "totp|dev_abc123"


def test_pick_mfa_factor_totp_blank_id_falls_through_to_sms() -> None:
    factor = _pick_mfa_factor(
        (
            {
                "id": "",
                "authenticator_type": "otp",
                "active": True,
                "type": "totp",
            },
            {
                "id": "sms|dev_abc123",
                "authenticator_type": "oob",
                "oob_channel": "sms",
                "active": True,
                "type": "phone",
            },
        )
    )
    assert factor is not None
    assert factor["authenticator_type"] == "oob"
    assert factor["oob_channel"] == "sms"


def test_pick_mfa_factor_prefers_totp_literal_over_sms() -> None:
    factor = _pick_mfa_factor(
        (
            {
                "id": "sms|dev_abc123",
                "authenticator_type": "oob",
                "oob_channel": "sms",
                "active": True,
                "type": "phone",
            },
            {
                "id": "totp|dev_abc123",
                "authenticator_type": "totp",
                "active": True,
                "type": "totp",
            },
        )
    )
    assert factor is not None
    assert factor["authenticator_type"] == "totp"
    assert factor["id"] == "totp|dev_abc123"


def test_pick_mfa_factor_otp_only_returns_otp_factor() -> None:
    factor = _pick_mfa_factor(
        (
            {
                "id": "totp|dev_abc123",
                "authenticator_type": "otp",
                "active": True,
                "type": "totp",
            },
            {
                "id": "recovery-code|dev_abc123",
                "authenticator_type": "recovery-code",
                "active": True,
                "type": "recovery-code",
            },
        )
    )
    assert factor is not None
    assert factor["authenticator_type"] == "otp"
    assert factor["id"] == "totp|dev_abc123"


def test_pick_mfa_factor_recovery_code_only_is_not_a_factor() -> None:
    factor = _pick_mfa_factor(
        (
            {
                "id": "recovery-code|dev_abc123",
                "authenticator_type": "recovery-code",
                "active": True,
                "type": "recovery-code",
            },
        )
    )
    assert factor is None


def test_pick_mfa_factor_skips_inactive_sms() -> None:
    factor = _pick_mfa_factor(
        (
            {
                "id": "sms|dev_inactive",
                "authenticator_type": "oob",
                "oob_channel": "sms",
                "active": False,
                "type": "phone",
            },
            {
                "id": "email|dev_abc123",
                "authenticator_type": "oob",
                "oob_channel": "email",
                "active": True,
                "type": "email",
            },
        )
    )
    assert factor is not None
    assert factor["oob_channel"] == "email"


def _oob_sms_factor() -> dict[str, object]:
    return {
        "id": "sms|dev_abc123",
        "authenticator_type": "oob",
        "oob_channel": "sms",
        "active": True,
        "type": "phone",
    }


def _totp_factor() -> dict[str, object]:
    return {
        "id": "totp|dev_abc123",
        "authenticator_type": "otp",
        "active": True,
        "type": "totp",
    }


def _recovery_factor() -> dict[str, object]:
    return {
        "id": "recovery-code|dev_abc123",
        "authenticator_type": "recovery-code",
        "active": True,
        "type": "recovery-code",
    }


def test_oob_factor_authenticator_id_extracts_oob_id() -> None:
    assert _oob_factor_authenticator_id(_oob_sms_factor()) == "sms|dev_abc123"


def test_oob_factor_authenticator_id_returns_none_for_totp_and_recovery() -> None:
    assert _oob_factor_authenticator_id(_totp_factor()) is None
    assert _oob_factor_authenticator_id(_recovery_factor()) is None
    assert _oob_factor_authenticator_id(None) is None


def test_oob_factor_authenticator_id_requires_string_id() -> None:
    factor: dict[str, object] = {
        "id": "",
        "authenticator_type": "oob",
        "oob_channel": "sms",
    }
    assert _oob_factor_authenticator_id(factor) is None
    factor["id"] = 123
    assert _oob_factor_authenticator_id(factor) is None


def test_mfa_channel_oob_with_valid_challenge() -> None:
    channel, oob_code = _mfa_channel_from_challenge(
        _oob_sms_factor(), MfaChallenge(oob_code="oob-code", binding_method="prompt")
    )
    assert channel == "oob"
    assert oob_code == "oob-code"


def test_mfa_channel_falls_back_when_challenge_is_none() -> None:
    channel, oob_code = _mfa_channel_from_challenge(_oob_sms_factor(), None)
    assert channel == "totp"
    assert oob_code is None


def test_mfa_channel_never_oob_without_a_code() -> None:
    no_code = MfaChallenge(oob_code="", binding_method="prompt")
    channel, oob_code = _mfa_channel_from_challenge(_oob_sms_factor(), no_code)
    assert channel == "totp"
    assert oob_code is None
    blank = MfaChallenge(oob_code="   ", binding_method="prompt")
    channel, oob_code = _mfa_channel_from_challenge(_oob_sms_factor(), blank)
    assert channel == "totp"
    assert oob_code is None


def test_mfa_channel_totp_for_non_oob_factor() -> None:
    channel, oob_code = _mfa_channel_from_challenge(
        _totp_factor(), MfaChallenge(oob_code="oob-code", binding_method="prompt")
    )
    assert channel == "totp"
    assert oob_code is None
    channel, _ = _mfa_channel_from_challenge(_recovery_factor(), None)
    assert channel == "totp"


def test_challenge_mfa_rejects_non_prompt_binding_method() -> None:
    transport = FixtureTransport(mfa_oob=True, mfa_challenge_binding="enter_code")
    client = SpGroupClient(transport=transport)

    with pytest.raises(AuthError) as raised:
        client.login("user@example.com", "secret")
    mfa_token = raised.value.mfa_token

    with pytest.raises(AuthError) as exc_info:
        client.challenge_mfa(mfa_token, "sms|dev_abc123")
    assert exc_info.value.error == "challenge_failed"

    channel, oob_code = _mfa_channel_from_challenge(_oob_sms_factor(), None)
    assert channel == "totp"
    assert oob_code is None


def test_prepare_mfa_challenges_sms_and_returns_oob() -> None:
    transport = FixtureTransport(mfa_oob=True)
    client = SpGroupClient(transport=transport)

    channel, oob_code = client.prepare_mfa("mfa-token")

    assert channel == "oob"
    assert oob_code == "oob-code"
    assert transport.requests[0].method == "GET"
    assert transport.requests[1].method == "POST"
    challenged = transport.requests[1]
    assert challenged.url == f"{AUTH0_MFA_OAUTH_HOST}{AUTH0_MFA_CHALLENGE_PATH}"


def test_prepare_mfa_falls_back_when_binding_is_not_prompt() -> None:
    transport = FixtureTransport(mfa_oob=True, mfa_challenge_binding="enter_code")
    client = SpGroupClient(transport=transport)

    channel, oob_code = client.prepare_mfa("mfa-token")

    assert channel == "totp"
    assert oob_code is None


def test_stored_session_skips_password_login() -> None:
    token_payload = json.loads(load_fixture("oauth_token_success.json"))
    session = Session(
        access_token=token_payload["access_token"],
        id_token=token_payload["id_token"],
        refresh_token=token_payload["refresh_token"],
        scope=token_payload["scope"],
        expires_at=int(time.time()) + 3600,
    )
    transport = FixtureTransport()
    client = SpGroupClient(transport=transport, session=session)
    client.fetch_usage()
    assert all(not req.url.endswith(OAUTH_TOKEN_PATH) for req in transport.requests)


def test_refresh_sends_refresh_token_grant() -> None:
    token_payload = json.loads(load_fixture("oauth_token_success.json"))
    transport = FixtureTransport()
    client = SpGroupClient(
        transport=transport,
        session=Session(
            access_token=token_payload["access_token"],
            id_token=token_payload["id_token"],
            refresh_token=token_payload["refresh_token"],
            scope=token_payload["scope"],
        ),
    )
    client.refresh()
    recorded = transport.requests[0]
    assert recorded.body is not None
    body = json.loads(recorded.body.decode("utf-8"))
    assert body["grant_type"] == AUTH0_REFRESH_GRANT
    assert body["refresh_token"] == token_payload["refresh_token"]
    assert body["client_id"] == AUTH0_CLIENT_ID


def test_fetch_usage_returns_kwh_and_water_from_charts_fixture() -> None:
    charts = json.loads(load_fixture("jarvis_charts.json"))
    expected_kwh, expected_m3 = billed_totals_from_charts_payload(charts)
    token_payload = json.loads(load_fixture("oauth_token_success.json"))
    me_payload = json.loads(load_fixture("jarvis_me.json"))
    premise_id = me_payload["premises"][0]["id"]

    transport = FixtureTransport()
    client = fixture_client(transport)
    usage = client.fetch_usage()

    assert usage.electricity_kwh == expected_kwh
    assert usage.water_m3 == expected_m3
    assert usage.electricity_unit == "kWh"
    assert usage.water_unit == "m³"
    assert usage.premise_id == premise_id

    by_path = {urlparse_path(req.url): req for req in transport.requests}
    me_req = by_path[JARVIS_ME_PATH]
    charts_req = by_path[f"{JARVIS_CHARTS_PATH}/{premise_id}"]
    bearer = f"Bearer {token_payload['access_token']}"
    assert me_req.method == "GET"
    assert me_req.url == f"{B2C_HOST}{JARVIS_ME_PATH}"
    assert me_req.headers["Authorization"] == bearer
    assert me_req.headers[HEADER_ID_TOKEN] == token_payload["id_token"]
    assert charts_req.method == "GET"
    assert charts_req.url == f"{B2C_HOST}{JARVIS_CHARTS_PATH}/{premise_id}"
    assert charts_req.headers["Authorization"] == bearer
    assert charts_req.headers[HEADER_ID_TOKEN] == token_payload["id_token"]
    assert usage.premise.account_number == "1234567890"
    assert usage.premise.address == "1 Example Road, Singapore"
    assert usage.premise.ami_elec is True
    assert usage.premise.ppms_exists is False
    assert usage.meter_reading is not None
    assert usage.meter_reading.title == "Sep 2026"
    assert usage.ppms_credit is None
    assert usage.gas is None
    smrd_paths = [
        urlparse_path(req.url)
        for req in transport.requests
        if urlparse_path(req.url).startswith("/jarvis/v3/smrd-uportal/")
    ]
    assert smrd_paths == [f"/jarvis/v3/smrd-uportal/{premise_id}"]
    assert all(
        not urlparse_path(req.url).startswith("/jarvis/v3/ppms/balance/")
        for req in transport.requests
    )
    ami_reqs = [
        req for req in transport.requests if urlparse_path(req.url) == JARVIS_AMI_PATH
    ]
    assert len(ami_reqs) == 2
    groups = []
    for req in ami_reqs:
        assert req.method == "POST"
        assert req.body is not None
        payload = json.loads(req.body.decode("utf-8"))
        groups.append(payload["grouped_by"])
        assert payload["utility_type"] == "electric"
        assert payload["premise_id"] == premise_id
        assert payload["start"].isdigit()
        assert len(payload["start"]) == 14
    assert sorted(groups) == ["day", "month"]
    assert len(usage.ami_hourly) == 4
    assert len(usage.ami_daily) == 2
    assert usage.ami_hourly[0].amount == pytest.approx(0.4)
    assert usage.last_bill is not None
    assert usage.last_bill.amount_sgd == pytest.approx(203.69)
    assert usage.last_bill.period == "2026-07-31T16:00:00Z"
    assert len(usage.bills) == 2
    assert usage.bills[0].amount_sgd == pytest.approx(323.26)
    assert usage.bills[-1].amount_sgd == pytest.approx(203.69)
    assert usage.amount_due is not None
    assert usage.amount_due.amount_sgd == pytest.approx(203.69)
    assert any(
        urlparse_path(req.url) == NJORD_PAYABLES_PATH for req in transport.requests
    )
    history_reqs = [
        req
        for req in transport.requests
        if urlparse_path(req.url) == NJORD_HISTORY_PATH
    ]
    assert len(history_reqs) == 1
    assert "account_numbers=1234567890" in history_reqs[0].url
    elec_meter = usage.meter("electric")
    water_meter = usage.meter("water")
    assert elec_meter is not None
    assert elec_meter.value == pytest.approx(14256)
    assert elec_meter.meter_id == "PA0000001"
    assert water_meter is not None
    assert water_meter.value == pytest.approx(931.4)
    elec_goal = usage.goal("elec")
    assert elec_goal is not None
    assert elec_goal.used == pytest.approx(1160.77)
    assert elec_goal.target == pytest.approx(672.47)
    assert elec_goal.cost_difference_sgd == pytest.approx(103.10)
    assert usage.goal("water") is None
    assert any(
        urlparse_path(req.url) == JARVIS_GREEN_GOALS_PATH for req in transport.requests
    )


def test_me_forbidden_uses_server_error_description() -> None:
    class ForbiddenMeTransport(FixtureTransport):
        def request(
            self,
            method: str,
            url: str,
            headers: dict[str, str],
            body: bytes | None,
            *,
            timeout: int | None = None,
        ):
            from urllib.parse import urlparse

            from custom_components.sp_group.client import HttpResponse

            parsed = urlparse(url)
            if method == "GET" and parsed.path == JARVIS_ME_PATH:
                return HttpResponse(
                    403,
                    b'{"error":"invalid_claim","error_description":"claim error"}',
                )
            return super().request(method, url, headers, body, timeout=timeout)

    client = fixture_client(ForbiddenMeTransport())
    with pytest.raises(AuthError) as exc_info:
        client.fetch_usage()
    assert exc_info.value.error == "invalid_claim"
    assert "claim error" in exc_info.value.error_description


def test_invalid_credentials_raise_auth_error() -> None:
    fail_payload = json.loads(load_fixture("oauth_token_invalid_grant.json"))
    transport = FixtureTransport(fail_login=True)
    client = SpGroupClient(transport=transport)
    with pytest.raises(AuthError) as exc_info:
        client.login("user@example.com", "wrong")
    assert exc_info.value.error == fail_payload["error"]
    assert exc_info.value.error_description == fail_payload["error_description"]
    with pytest.raises(AuthError):
        client.fetch_usage()
    assert all(urlparse_path(req.url) != JARVIS_ME_PATH for req in transport.requests)
    assert all(
        not urlparse_path(req.url).startswith(JARVIS_CHARTS_PATH)
        for req in transport.requests
    )


def test_empty_gas_does_not_fail_fetch() -> None:
    client = fixture_client()
    usage = client.fetch_usage()
    assert usage.gas is None
    assert usage.electricity is not None
    assert usage.water is not None


def test_gas_only_charts_return_gas_series() -> None:
    client = fixture_client(FixtureTransport(charts_fixture="jarvis_charts_gas.json"))
    usage = client.fetch_usage()
    assert usage.electricity is None
    assert usage.water is None
    assert usage.gas is not None
    assert usage.gas.total == pytest.approx(17.7)
    assert usage.gas.unit == "kWh"


def test_amount_due_credit_is_negative_sgd() -> None:
    class CreditTransport(FixtureTransport):
        def request(self, method, url, headers, body, *, timeout=None):
            from urllib.parse import urlparse

            from custom_components.sp_group.client import HttpResponse

            parsed = urlparse(url)
            if method == "GET" and parsed.path == NJORD_PAYABLES_PATH:
                return HttpResponse(
                    200,
                    b'{"payables":[{"account_number":"1234567890","currency":"SGD",'
                    b'"amount":-29631,"premises_id":"premise-001","system":"EBS",'
                    b'"recurring_enabled":false,"giro_enabled":false,'
                    b'"is_owner":true}]}',
                )
            return super().request(method, url, headers, body, timeout=timeout)

    usage = fixture_client(CreditTransport()).fetch_usage()
    assert usage.amount_due is not None
    assert usage.amount_due.amount_sgd == pytest.approx(-296.31)


def urlparse_path(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).path
