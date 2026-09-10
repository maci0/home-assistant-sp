"""HTTP fixture transport matching APK 15.10.0 hosts and paths."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from custom_components.sp_group.client import HttpResponse, SpGroupClient
from custom_components.sp_group.const import (
    AUTH0_GRANT_TYPE,
    AUTH0_MFA_AUTHENTICATORS_PATH,
    AUTH0_MFA_CHALLENGE_PATH,
    AUTH0_MFA_OAUTH_HOST,
    AUTH0_MFA_OOB_GRANT,
    AUTH0_MFA_OTP_GRANT,
    B2C_HOST,
    IDENTITY_HOST,
    JARVIS_AMI_PATH,
    JARVIS_CHARTS_PATH,
    JARVIS_GREEN_GOALS_PATH,
    JARVIS_ME_PATH,
    JARVIS_PPMS_PATH,
    JARVIS_SMRD_PATH,
    NJORD_HISTORY_PATH,
    NJORD_PAYABLES_PATH,
    OAUTH_TOKEN_PATH,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_client(transport: FixtureTransport | None = None) -> SpGroupClient:
    client = SpGroupClient(transport=transport or FixtureTransport())
    client.login("user@example.com", "secret")
    return client


@dataclass
class RecordedRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: bytes | None
    timeout: int | None = None


@dataclass
class FixtureTransport:
    """Serves recorded Auth0/Jarvis JSON. Does not implement client logic."""

    fail_login: bool = False
    require_mfa: bool = False
    mfa_success: bool = False
    mfa_oob: bool = False
    mfa_challenge_binding: str = "prompt"
    authenticators_bare: bool = False
    charts_fixture: str = "jarvis_charts.json"
    me_fixture: str = "jarvis_me.json"
    smrd_fixture: str | None = "jarvis_smrd.json"
    requests: list[RecordedRequest] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout: int | None = None,
    ) -> HttpResponse:
        self.requests.append(
            RecordedRequest(
                method=method,
                url=url,
                headers=dict(headers),
                body=body,
                timeout=timeout,
            )
        )
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        path = parsed.path
        if method == "POST" and origin == IDENTITY_HOST and path == OAUTH_TOKEN_PATH:
            import json

            request_body = json.loads(body.decode("utf-8")) if body else {}
            if (self.require_mfa or self.mfa_oob) and request_body.get(
                "grant_type"
            ) == AUTH0_GRANT_TYPE:
                return HttpResponse(
                    status=403,
                    body=load_fixture("oauth_token_mfa_required.json"),
                )
            if (
                self.mfa_success
                and request_body.get("grant_type") == AUTH0_MFA_OOB_GRANT
            ):
                return HttpResponse(
                    status=200,
                    body=load_fixture("oauth_token_mfa_success.json"),
                )
            if (
                self.mfa_success
                and request_body.get("grant_type") == AUTH0_MFA_OTP_GRANT
            ):
                return HttpResponse(
                    status=200,
                    body=load_fixture("oauth_token_mfa_success.json"),
                )
            if self.fail_login:
                return HttpResponse(
                    status=403,
                    body=load_fixture("oauth_token_invalid_grant.json"),
                )
            return HttpResponse(
                status=200,
                body=load_fixture("oauth_token_success.json"),
            )
        if (
            method == "GET"
            and origin == AUTH0_MFA_OAUTH_HOST
            and path == AUTH0_MFA_AUTHENTICATORS_PATH
        ):
            fixture = (
                "oauth_token_mfa_authenticators_bare.json"
                if self.authenticators_bare
                else "oauth_token_mfa_authenticators_sms.json"
            )
            return HttpResponse(status=200, body=load_fixture(fixture))
        if (
            method == "POST"
            and origin == AUTH0_MFA_OAUTH_HOST
            and path == AUTH0_MFA_CHALLENGE_PATH
        ):
            import json

            challenge = {
                "challenge_type": "oob",
                "oob_code": "oob-code",
                "binding_method": self.mfa_challenge_binding,
            }
            return HttpResponse(
                status=200,
                body=json.dumps(challenge).encode("utf-8"),
            )
        if method == "GET" and origin == B2C_HOST and path == JARVIS_ME_PATH:
            return HttpResponse(
                status=200,
                body=load_fixture(self.me_fixture),
            )
        if (
            method == "GET"
            and origin == B2C_HOST
            and path.startswith(f"{JARVIS_CHARTS_PATH}/")
        ):
            return HttpResponse(
                status=200,
                body=load_fixture(self.charts_fixture),
            )
        if (
            method == "GET"
            and origin == B2C_HOST
            and path.startswith(f"{JARVIS_SMRD_PATH}/")
        ):
            if self.smrd_fixture is None:
                return HttpResponse(status=404, body=b"{}")
            return HttpResponse(
                status=200,
                body=load_fixture(self.smrd_fixture),
            )
        if method == "POST" and origin == B2C_HOST and path == JARVIS_AMI_PATH:
            grouped = "day"
            if body:
                import json

                payload = json.loads(body.decode("utf-8"))
                grouped = str(payload.get("grouped_by") or "day")
            name = (
                "jarvis_ami_month.json" if grouped == "month" else "jarvis_ami_day.json"
            )
            return HttpResponse(
                status=200,
                body=load_fixture(name),
            )
        if (
            method == "GET"
            and origin == B2C_HOST
            and path.startswith(f"{JARVIS_PPMS_PATH}/")
        ):
            return HttpResponse(
                status=401,
                body=b'{"error":"no_ppms_account"}',
            )
        if method == "GET" and origin == B2C_HOST and path == JARVIS_GREEN_GOALS_PATH:
            return HttpResponse(
                status=200,
                body=load_fixture("jarvis_greengoals.json"),
            )
        if method == "GET" and origin == B2C_HOST and path == NJORD_PAYABLES_PATH:
            return HttpResponse(
                status=200,
                body=load_fixture("njord_payables.json"),
            )
        if method == "GET" and origin == B2C_HOST and path == NJORD_HISTORY_PATH:
            return HttpResponse(
                status=200,
                body=load_fixture("njord_history.json"),
            )
        return HttpResponse(status=404, body=b"{}")


def billed_totals_from_charts_payload(
    payload: dict[str, object],
) -> tuple[float, float]:
    """Read consumption.current from the fixture JSON (field names from the APK)."""

    def _sum(section_key: str) -> float:
        section = payload[section_key]
        assert isinstance(section, dict)
        data = section["data"]
        assert isinstance(data, list)
        total = 0.0
        for row in data:
            assert isinstance(row, dict)
            consumption = row["consumption"]
            assert isinstance(consumption, dict)
            total += float(consumption["current"])
        return total

    return _sum("elec"), _sum("water")
