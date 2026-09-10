"""SP Group Auth0 + Jarvis HTTP client.

Reads billed usage, AMI half-hours, Njord bills, SMRD registers, Green Goals,
and optional GreenUP / Eva / Frosty / notification payloads from the same
hosts as Android app sg.com.singaporepower.spservices 15.10.0.

Eva ChargeHistoryV2.transaction_amount and UnpaidOrders.amount are Java String.
Dotted values are dollars. Integer-only values with abs >= EVA_INTEGER_CENTS_MIN
are cents. Frosty getPairedFCUs can return more than one Tengah coil; each
thingName is its own sensor. Optional hosts use OPTIONAL_HTTP_TIMEOUT_SECONDS
so a hung Eva or Frosty call does not block the 30-minute poll.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.client import HTTPException
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .const import (
    ACCEPT_LANGUAGE,
    AMI_DAILY_MONTHS,
    AMI_DATE_FORMAT,
    AMI_GROUPED_BY_DAILY,
    AMI_GROUPED_BY_HALF_HOUR,
    AMI_HALF_HOUR_DAYS,
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
    AUTH_REJECT_STATUSES,
    B2C_HOST,
    BILL_PREFERENCES_PATH,
    CONTENT_TYPE_JSON,
    ERROR_VALUE_CHARS,
    EVA_CHARGE_HISTORY_PATH,
    EVA_INTEGER_CENTS_MIN,
    EVA_LATEST_SESSION_PATH,
    EVA_UNPAID_PATH,
    FROSTY_FCU_STATUS_PATH,
    FROSTY_GRAPHQL_PATH,
    GREENUP_GRAPHQL_PATH,
    HEADER_ID_TOKEN,
    HTTP_TIMEOUT_SECONDS,
    IDENTITY_HOST,
    JARVIS_AMI_PATH,
    JARVIS_CHARTS_PATH,
    JARVIS_GREEN_GOALS_PATH,
    JARVIS_ME_PATH,
    JARVIS_PPMS_PATH,
    JARVIS_SMRD_PATH,
    NJORD_HISTORY_PATH,
    NJORD_PAYABLES_PATH,
    NOTIFICATIONS_PATH,
    OAUTH_TOKEN_PATH,
    OPTIONAL_HTTP_TIMEOUT_SECONDS,
    PRICEPLAN_PATH,
    PUBLIC_HOST,
    TARIFF_DEFAULT_CONSUMPTION_KWH,
    TOKEN_EXPIRY_BUFFER_SECONDS,
    TYCHE_WALLET_PATH,
    USER_AGENT,
)
from .models import (
    SG_TZ,
    BillDeliveryInfo,
    BillInfo,
    EvChargeInfo,
    EvSessionInfo,
    EvUnpaidInfo,
    EvWalletInfo,
    FcuInfo,
    GreenGoal,
    GreenUpInfo,
    MeterReadingInfo,
    MeterRegister,
    MfaChallenge,
    OptionalReads,
    PayableInfo,
    PeriodReading,
    PremiseInfo,
    TariffInfo,
    UsageReadings,
    UtilitySeries,
)

_LOGGER = logging.getLogger(__name__)

GREENUP_ACCOUNT_QUERY = (
    "query { account { node { totalPoints projectedLevelStatus "
    "tier { node { level name pointsToLevelUp } } } } }"
)
TENGAH_PAIRED_FCUS_QUERY = (
    "query($utilityAccountNumber: String!) { "
    "getPairedFCUs(utilityAccountNumber: $utilityAccountNumber)"
    "{ displayName thingName } }"
)


class AuthError(Exception):
    """Login rejected by identity.spdigital.sg (invalid credentials or Auth0 error)."""

    def __init__(
        self, error: str, error_description: str = "", mfa_token: str | None = None
    ) -> None:
        self.error = error
        self.error_description = error_description
        self.mfa_token = mfa_token
        super().__init__(error_description or error)


class UsageError(Exception):
    """Usage payload missing billed utility readings."""


class TransportError(UsageError):
    """An SP Group host could not be reached, or returned no usable response.

    A ``UsageError`` so callers that already treat a bad read as a retriable
    update failure (coordinator, config flow) keep doing so: a DNS failure, a
    connect timeout, or a 5xx from the token endpoint is transient and must not
    be reported to the user as bad credentials.
    """


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout: int | None = None,
    ) -> HttpResponse: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout: int | None = None,
    ) -> HttpResponse:
        request = Request(url, data=body, method=method, headers=dict(headers))
        context = ssl.create_default_context()
        seconds = HTTP_TIMEOUT_SECONDS if timeout is None else timeout
        try:
            with urlopen(request, timeout=seconds, context=context) as response:
                return HttpResponse(status=int(response.status), body=response.read())
        except HTTPError as exc:
            with exc:
                return HttpResponse(status=int(exc.code), body=exc.read())
        except (OSError, HTTPException) as exc:
            # URLError, socket timeout, and TLS failures all land here. Name the
            # call and the timeout so the log says which host stalled the poll.
            raise TransportError(
                f"{method} {url.split('?', 1)[0]} failed"
                f" (timeout {seconds}s): {exc}"
            ) from exc


@dataclass(frozen=True)
class Session:
    access_token: str
    id_token: str
    refresh_token: str | None
    scope: str | None
    expires_at: int | None = None

    def is_expired(self, now: int | None = None) -> bool:
        if self.expires_at is None:
            return False
        current = int(time.time() if now is None else now)
        return current >= self.expires_at - TOKEN_EXPIRY_BUFFER_SECONDS


def _decode_json(body: bytes) -> object:
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def _require_json(response: HttpResponse, label: str) -> object:
    """Decode a response the read cannot continue without."""
    try:
        return _decode_json(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UsageError(
            f"{label} HTTP {response.status}: response was not JSON ({exc})"
        ) from exc


def _optional_json(response: HttpResponse) -> object | None:
    if response.status >= 400:
        return None
    try:
        return _decode_json(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _eva_scope_denied(response: HttpResponse) -> bool:
    if response.status != 403:
        return False
    try:
        body = _decode_json(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(body, dict):
        return False
    error = str(body.get("error") or "")
    description = str(body.get("error_description") or "")
    return error == "scope_not_found" or "scope_not_found" in description


def _eva_integer_sgd(number: int) -> float | None:
    """Integers at or above the threshold are cents; smaller ones are dollars."""
    dollars = _optional_float(number)
    if dollars is None:
        return None
    if abs(number) >= EVA_INTEGER_CENTS_MIN:
        return round(dollars / 100.0, 2)
    return dollars


def _eva_sgd(value: object) -> float | None:
    """Eva money fields are Java String, not Njord integer cents."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        return round(value, 2) if math.isfinite(value) else None
    if isinstance(value, int):
        return _eva_integer_sgd(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if "." not in text:
            try:
                return _eva_integer_sgd(int(text))
            except ValueError:
                pass
        dollars = _optional_float(text)
        return round(dollars, 2) if dollars is not None else None
    return None


def _tariff_consumption(electricity: UtilitySeries | None) -> str:
    if electricity is None or not electricity.periods:
        return str(TARIFF_DEFAULT_CONSUMPTION_KWH)
    last = max(electricity.periods, key=lambda item: item.start)
    kwh = int(round(last.amount))
    if kwh <= 0:
        return str(TARIFF_DEFAULT_CONSUMPTION_KWH)
    return str(kwh)


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise UsageError(f"{label} is not an object")
    return value


def _float(value: object) -> float:
    """Billed consumption: a malformed number fails the read, never reads as zero."""
    if isinstance(value, bool) or value is None:
        return 0.0
    if not isinstance(value, (int, float, str)) or value == "":
        return 0.0
    number = _optional_float(value)
    if number is None:
        raise UsageError(f"expected a number, got {repr(value)[:ERROR_VALUE_CHARS]}")
    return number


def _optional_float(value: object) -> float | None:
    """None unless the value is a finite number.

    json.loads accepts the NaN and Infinity literals, "1e400" parses to inf and
    an out-of-range JSON integer overflows float(); none of the three can be a
    sensor state.
    """
    if isinstance(value, bool) or value is None:
        return None
    if not isinstance(value, (int, float, str)) or value == "":
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _cents_to_sgd(value: object) -> float | None:
    cents = _optional_float(value)
    if cents is None:
        return None
    return round(cents / 100.0, 2)


def _account_digits(value: str | None) -> str:
    if not value:
        return ""
    stripped = value.lstrip("0")
    return stripped or "0"


def _jwt_exp(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + ("=" * (-len(parts[1]) % 4))
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    try:
        return int(exp) if exp is not None else None
    except (TypeError, ValueError):
        return None


def _session_from_oauth(
    mapping: dict[str, object], fallback_refresh: str | None
) -> Session:
    access_token = mapping.get("access_token")
    id_token = mapping.get("id_token")
    if not isinstance(access_token, str) or not access_token:
        raise AuthError("invalid_grant", "access_token missing")
    if not isinstance(id_token, str) or not id_token:
        raise AuthError("invalid_grant", "id_token missing")
    refresh = mapping.get("refresh_token")
    scope = mapping.get("scope")
    return Session(
        access_token=access_token,
        id_token=id_token,
        refresh_token=refresh if isinstance(refresh, str) else fallback_refresh,
        scope=scope if isinstance(scope, str) else None,
        expires_at=_jwt_exp(access_token),
    )


def _oauth_headers() -> dict[str, str]:
    return {
        "Content-Type": CONTENT_TYPE_JSON,
        "Accept-Language": ACCEPT_LANGUAGE,
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }


def _factor_usable(factor: dict[str, object]) -> bool:
    """Enrolled and not explicitly disabled."""
    return bool(factor.get("id")) and factor.get("active") is not False


def _pick_mfa_factor(
    authenticators: tuple[dict[str, object], ...],
) -> dict[str, object] | None:
    """Choose the login factor an account can use.

    Prefers the TOTP factor (stable, no short-lived out-of-band code), then a
    usable out-of-band factor (SMS, then email). A recovery-code factor is not
    a usable login factor and is ignored. Returns None when only recovery codes
    (or nothing) are enrolled. Factors with ``active: false`` are skipped.
    """
    for factor in authenticators:
        if factor.get("authenticator_type") in {"otp", "totp"} and _factor_usable(
            factor
        ):
            return factor
    oob: dict[str, dict[str, object]] = {}
    for factor in authenticators:
        if factor.get("authenticator_type") != "oob" or not _factor_usable(factor):
            continue
        channel = factor.get("oob_channel")
        if channel in {"sms", "email"}:
            oob[channel] = factor
    sms = oob.get("sms")
    if sms is not None:
        return sms
    return oob.get("email")


def _oob_factor_authenticator_id(factor: dict[str, object] | None) -> str | None:
    """The authenticator_id to challenge, or None when the factor is not OOB.

    Only an out-of-band factor (``authenticator_type == "oob"``) with a
    non-empty id is challengable; returns None for TOTP, recovery-code, or a
    malformed factor. This is the gate that decides whether a challenge should
    be attempted at all.
    """
    if factor is None or factor.get("authenticator_type") != "oob":
        return None
    authenticator_id = factor.get("id")
    if not isinstance(authenticator_id, str) or not authenticator_id:
        return None
    return authenticator_id


def _mfa_channel_from_challenge(
    factor: dict[str, object] | None, challenge: MfaChallenge | None
) -> tuple[str, str | None]:
    """Decide the MFA channel and OOB code from a (maybe failed) challenge.

    Returns ``("oob", oob_code)`` only when the factor is OOB and the challenge
    produced a usable code; otherwise ``("totp", None)``. The invariant this
    enforces is that the channel is NEVER ``"oob"`` without a usable oob_code,
    so a later step can always read ``context["mfa_oob_code"]`` safely.
    """
    if _oob_factor_authenticator_id(factor) is None:
        return "totp", None
    if (
        challenge is None
        or not isinstance(challenge.oob_code, str)
        or not challenge.oob_code.strip()
    ):
        return "totp", None
    return "oob", challenge.oob_code


def _normalize_volume_unit(unit: str) -> str:
    compact = unit.replace(" ", "").lower()
    if compact in {"m3", "m³", "cum", "cu.m", "cbm"}:
        return "m³"
    return unit or "m³"


def _energy_or_volume_unit(unit: str, kind: str) -> str:
    compact = unit.replace(" ", "").lower()
    if kind == "elec":
        if unit and compact not in {"kwh", "kw·h"}:
            raise UsageError(f"unexpected electricity unit {unit!r}")
        return "kWh"
    if kind == "water":
        return _normalize_volume_unit(unit)
    if compact in {"kwh", "kw·h"}:
        return "kWh"
    return _normalize_volume_unit(unit)


def _parse_period_start(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SG_TZ)
    try:
        parsed.astimezone(SG_TZ)
    except (OverflowError, OSError):
        # Timestamps within a UTC offset of datetime.min/max cannot be shifted
        # to SGT, and every downstream fold does exactly that.
        return None
    return parsed


def _parse_utility(utility: object, kind: str) -> UtilitySeries | None:
    if not isinstance(utility, dict):
        return None
    data = utility.get("data")
    if not isinstance(data, list) or not data:
        return None
    total = 0.0
    unit = ""
    periods: list[PeriodReading] = []
    for row in data:
        item = _require_mapping(row, f"{kind} period")
        raw_unit = item.get("unit")
        if isinstance(raw_unit, str) and raw_unit:
            unit = raw_unit
        consumption = item.get("consumption")
        if not isinstance(consumption, dict):
            raise UsageError(f"{kind} period missing consumption")
        amount = _float(consumption.get("current"))
        total += amount
        start = _parse_period_start(item.get("period"))
        status = _optional_str(item.get("status"))
        previous = _optional_float(consumption.get("previous"))
        if start is not None:
            periods.append(
                PeriodReading(
                    start=start, amount=amount, previous=previous, status=status
                )
            )
    if not periods:
        return None
    return UtilitySeries(
        total=total,
        unit=_energy_or_volume_unit(unit, kind),
        periods=tuple(periods),
        average=_optional_float(utility.get("average_consumption")),
        comparison=_optional_str(utility.get("comparison_message"))
        or _optional_str(utility.get("comparison_type")),
    )


def _first_account(premise: dict[str, object]) -> dict[str, object]:
    accounts = premise.get("accounts")
    if isinstance(accounts, list) and accounts and isinstance(accounts[0], dict):
        return accounts[0]
    return {}


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item)


def _parse_premise(premise: dict[str, object]) -> PremiseInfo:
    premise_id = premise.get("id")
    if not isinstance(premise_id, str) or not premise_id:
        raise UsageError("premise id missing")
    account = _first_account(premise)
    contestable = premise.get("contestable_details")
    contestable_map = contestable if isinstance(contestable, dict) else {}
    meter = premise.get("meter_details")
    meter_map = meter if isinstance(meter, dict) else {}
    ami = meter_map.get("ami_elec")
    ppms = premise.get("ppms_details")
    ppms_exists = False
    if isinstance(ppms, dict) and ppms.get("exists") is True:
        ppms_exists = True
    return PremiseInfo(
        id=premise_id,
        address=_optional_str(premise.get("address"))
        or _optional_str(account.get("address")),
        account_number=_optional_str(account.get("account_number")),
        account_status=_optional_str(account.get("account_status")),
        account_type=_optional_str(account.get("account_type")),
        premise_type=_optional_str(premise.get("type")),
        utilities=_string_tuple(account.get("utilities")),
        ami_elec=ami if isinstance(ami, bool) else None,
        retailer_name=_optional_str(contestable_map.get("retailer_name")),
        ppms_exists=ppms_exists,
    )


def _parse_meter_reading(body: object) -> MeterReadingInfo | None:
    if not isinstance(body, dict):
        return None
    period = body.get("latest_submission_period")
    period_map = period if isinstance(period, dict) else {}
    info = MeterReadingInfo(
        message=_optional_str(body.get("message")),
        title=_optional_str(period_map.get("title")),
        start=_optional_str(period_map.get("start_date")),
        end=_optional_str(period_map.get("end_date")),
    )
    if not any((info.message, info.title, info.start, info.end)):
        return None
    return info


def _parse_meter_registers(body: object) -> tuple[MeterRegister, ...]:
    if not isinstance(body, dict):
        return ()
    rows = body.get("meters")
    if not isinstance(rows, list):
        return ()
    registers: list[MeterRegister] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        utility = _optional_str(row.get("utility_type"))
        value = _optional_float(row.get("last_actual_value"))
        if not utility or value is None:
            continue
        registers.append(
            MeterRegister(
                utility=utility,
                meter_id=_optional_str(row.get("meter_id")),
                value=value,
                last_actual_at=_optional_str(row.get("last_actual_at")),
            )
        )
    return tuple(registers)


def _parse_green_goals(body: object, premise_id: str) -> tuple[GreenGoal, ...]:
    if not isinstance(body, dict):
        return ()
    rows = body.get("goals")
    if not isinstance(rows, list):
        return ()
    goals: list[GreenGoal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = _optional_str(row.get("type"))
        if not kind:
            continue
        premises = row.get("premises")
        if not isinstance(premises, list):
            continue
        matched: dict[str, object] | None = None
        for premise in premises:
            if not isinstance(premise, dict):
                continue
            if str(premise.get("id")) == premise_id:
                matched = premise
                break
        if matched is None and premises and isinstance(premises[0], dict):
            matched = premises[0]
        if matched is None:
            continue
        data = matched.get("data")
        data_map = data if isinstance(data, dict) else {}
        used = _optional_float(data_map.get("used"))
        target = _optional_float(data_map.get("target"))
        if used is None or target is None:
            continue
        if used == 0.0 and target == 0.0:
            continue
        unit = _optional_str(row.get("unit")) or ("kWh" if kind == "elec" else "m³")
        if kind == "water":
            unit = _normalize_volume_unit(unit)
        cost_cents = _optional_float(matched.get("cost_difference_in_cents"))
        goals.append(
            GreenGoal(
                kind=kind,
                month=_optional_str(row.get("month")),
                used=used,
                target=target,
                percent_difference=_optional_float(
                    matched.get("consumption_percentage_difference")
                ),
                cost_difference_sgd=(
                    round(cost_cents / 100.0, 2) if cost_cents is not None else None
                ),
                unit=unit,
            )
        )
    return tuple(goals)


def _graphql_data(body: object) -> dict[str, object] | None:
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    return data


def _parse_greenup(body: object) -> GreenUpInfo | None:
    data = _graphql_data(body)
    if data is None:
        return None
    account = data.get("account")
    if not isinstance(account, dict):
        return None
    node = account.get("node")
    if not isinstance(node, dict):
        return None
    points = _optional_float(node.get("totalPoints"))
    if points is None:
        return None
    tier = node.get("tier")
    tier_node = tier.get("node") if isinstance(tier, dict) else None
    tier_map = tier_node if isinstance(tier_node, dict) else {}
    return GreenUpInfo(
        points=points,
        tier_name=_optional_str(tier_map.get("name")),
        tier_level=_optional_float(tier_map.get("level")),
        points_to_level_up=_optional_float(tier_map.get("pointsToLevelUp")),
    )


def _parse_ev_wallet(body: object) -> EvWalletInfo | None:
    if not isinstance(body, dict):
        return None
    points = _optional_float(body.get("points_balance"))
    dollars = _optional_float(body.get("dollar_balance"))
    if points is None and dollars is None:
        return None
    if (points or 0) == 0 and (dollars or 0) == 0:
        return None
    return EvWalletInfo(
        points=points or 0.0,
        dollar_balance=dollars,
        current_tier_id=_optional_float(body.get("current_tier_id")),
    )


def _session_map(body: object) -> dict[str, object] | None:
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if isinstance(data, dict):
        return data
    if body.get("status") or body.get("kwh") or body.get("order_id"):
        return body
    return None


def _parse_ev_session(body: object) -> EvSessionInfo | None:
    data = _session_map(body)
    if data is None:
        return None
    status = _optional_str(data.get("status"))
    kwh = _optional_float(data.get("kwh"))
    order_id = _optional_str(data.get("order_id"))
    if status is None and kwh is None and order_id is None:
        return None
    return EvSessionInfo(
        status=status,
        kwh=kwh,
        total_cost=_optional_str(data.get("total_cost")),
        start=_optional_str(data.get("start_datetime")),
        order_id=order_id,
    )


def _parse_ev_last_charge(body: object) -> EvChargeInfo | None:
    if not isinstance(body, dict):
        return None
    rows = body.get("data")
    if not isinstance(rows, list) or not rows:
        return None
    first = rows[0]
    if not isinstance(first, dict):
        return None
    kwh = _optional_float(first.get("total_consumption")) or _optional_float(
        first.get("connector_kwh")
    )
    amount = _eva_sgd(first.get("transaction_amount"))
    if kwh is None and amount is None:
        return None
    return EvChargeInfo(
        kwh=kwh,
        amount=amount,
        created_at=_optional_str(first.get("created_at")),
        status=_optional_str(first.get("transaction_status")),
        address=_optional_str(first.get("address")),
    )


def _parse_ev_unpaid(body: object) -> EvUnpaidInfo | None:
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    payload = data if isinstance(data, dict) else body
    rows = payload.get("orders") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return None
    total = 0.0
    found = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        amount = _eva_sgd(row.get("amount"))
        if amount is not None:
            total += amount
            found = True
    return EvUnpaidInfo(count=len(rows), amount=total if found else None)


def _parse_unread(body: object) -> int | None:
    if not isinstance(body, dict):
        return None
    value = body.get("total_unread_notifications")
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value:
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _parse_bill_delivery(
    body: object, account_number: str | None
) -> BillDeliveryInfo | None:
    if not isinstance(body, dict):
        return None
    rows = body.get("preferences")
    if not isinstance(rows, list) or not rows:
        return None
    wanted = _account_digits(account_number)
    chosen: dict[str, object] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if chosen is None:
            chosen = row
        if wanted and _account_digits(_optional_str(row.get("accountNo"))) == wanted:
            chosen = row
            break
    if chosen is None:
        return None
    return BillDeliveryInfo(
        soft_copy=_optional_bool(chosen.get("isSoftCopy")),
        hard_copy=_optional_bool(chosen.get("isHardCopy")),
    )


def _parse_paired_fcus(body: object) -> list[tuple[str, str | None]]:
    data = _graphql_data(body)
    if data is None:
        return []
    rows = data.get("getPairedFCUs")
    if not isinstance(rows, list):
        return []
    out: list[tuple[str, str | None]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        thing = _optional_str(row.get("thingName"))
        if not thing:
            continue
        out.append((thing, _optional_str(row.get("displayName"))))
    return out


def _parse_fcu_status(
    body: object, thing_name: str, display_name: str | None
) -> FcuInfo | None:
    if not isinstance(body, dict):
        return None
    if body.get("fcu_not_paired") is True:
        return None
    return FcuInfo(
        thing_name=thing_name,
        display_name=display_name or _optional_str(body.get("display_name")),
        is_on=_optional_bool(body.get("is_on")),
        is_online=_optional_bool(body.get("is_online")),
        room_temperature=_optional_float(body.get("room_temperature")),
        setpoint=_optional_float(body.get("temperature")),
        mode=_optional_str(body.get("operation_mode")),
    )


def _parse_tariff(body: object) -> TariffInfo | None:
    if not isinstance(body, dict):
        return None
    kwh = _optional_float(body.get("sp_kwh_price"))
    monthly = _optional_float(body.get("sp_monthly_price"))
    if kwh is None and monthly is None:
        return None
    return TariffInfo(
        kwh_price=kwh,
        monthly_price=monthly,
        consumption=_optional_str(body.get("consumption")),
    )


def _parse_bills(body: object) -> tuple[BillInfo, ...]:
    if not isinstance(body, dict):
        return ()
    history = body.get("history")
    if not isinstance(history, list):
        return ()
    bills: list[tuple[str, BillInfo]] = []
    for row in history:
        if not isinstance(row, dict):
            continue
        if row.get("type") != "bill":
            continue
        bill = row.get("bill")
        if not isinstance(bill, dict):
            continue
        amount = _cents_to_sgd(bill.get("amount"))
        if amount is None:
            continue
        date = _optional_str(bill.get("date"))
        period = _optional_str(bill.get("period"))
        created = _optional_str(row.get("created_at"))
        info = BillInfo(
            amount_sgd=amount,
            date=date,
            period=period,
            due_date=_optional_str(bill.get("due_date")),
            account_number=_optional_str(bill.get("account_number")),
            issued_at=_parse_period_start(date)
            or _parse_period_start(period)
            or _parse_period_start(created),
        )
        stamp = created or date or ""
        bills.append((stamp, info))
    bills.sort(key=lambda item: item[0])
    return tuple(info for _, info in bills)


def _parse_payable(
    body: object, premise_id: str, account_number: str | None
) -> PayableInfo | None:
    if not isinstance(body, dict):
        return None
    rows = body.get("payables")
    if not isinstance(rows, list):
        return None
    wanted_account = _account_digits(account_number)
    matched: PayableInfo | None = None
    fallback: PayableInfo | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        amount = _cents_to_sgd(row.get("amount"))
        if amount is None:
            continue
        info = PayableInfo(
            amount_sgd=amount,
            currency=_optional_str(row.get("currency")),
            giro_enabled=_optional_bool(row.get("giro_enabled")),
            recurring_enabled=_optional_bool(row.get("recurring_enabled")),
        )
        if fallback is None:
            fallback = info
        row_premise = _optional_str(row.get("premises_id"))
        row_account = _account_digits(_optional_str(row.get("account_number")))
        if row_premise == premise_id or (
            wanted_account and row_account == wanted_account
        ):
            matched = info
            break
    return matched or fallback


def _ami_stamp(value: datetime) -> str:
    return value.astimezone(SG_TZ).strftime(AMI_DATE_FORMAT)


def _parse_ami_rows(body: object) -> tuple[PeriodReading, ...]:
    if not isinstance(body, dict):
        return ()
    history = body.get("history")
    if not isinstance(history, list):
        return ()
    periods: list[PeriodReading] = []
    for block in history:
        if not isinstance(block, dict):
            continue
        rows = block.get("data")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            start = _parse_period_start(row.get("date"))
            amount = _optional_float(row.get("consumption"))
            if start is None or amount is None:
                continue
            periods.append(PeriodReading(start=start, amount=amount))
    return tuple(sorted(periods, key=lambda item: item.start))


class SpGroupClient:
    def __init__(
        self,
        transport: Transport | None = None,
        session: Session | None = None,
    ) -> None:
        self._transport = transport or UrllibTransport()
        self._session = session

    @property
    def session(self) -> Session | None:
        return self._session

    def login(self, username: str, password: str) -> Session:
        payload = {
            "client_id": AUTH0_CLIENT_ID,
            "audience": AUTH0_AUDIENCE,
            "username": username,
            "password": password,
            "scope": AUTH0_SCOPE,
            "grant_type": AUTH0_GRANT_TYPE,
            "realm": AUTH0_REALM,
        }
        mapping = self._oauth_post(payload)
        session = _session_from_oauth(mapping, None)
        self._session = session
        return session

    def submit_mfa(self, mfa_token: str, otp: str) -> Session:
        """Exchange an Auth0 MFA token and one-time password for a session."""
        payload = {
            "grant_type": AUTH0_MFA_OTP_GRANT,
            "client_id": AUTH0_CLIENT_ID,
            "mfa_token": mfa_token,
            "otp": otp,
        }
        mapping = self._oauth_post(payload)
        session = _session_from_oauth(mapping, None)
        self._session = session
        return session

    def _auth0_mfa_request(
        self,
        method: str,
        path: str,
        mfa_token: str,
        *,
        body: dict[str, str] | None = None,
    ) -> object:
        """Request to the Auth0 MFA host (a different host than _oauth_post).

        Returns the parsed JSON body (a dict, or a bare list for the
        authenticators endpoint).
        """
        headers = _oauth_headers()
        headers["Authorization"] = f"Bearer {mfa_token}"
        response = self._transport.request(
            method,
            f"{AUTH0_MFA_OAUTH_HOST}{path}",
            headers,
            json.dumps(body).encode("utf-8") if body is not None else None,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        url = f"{AUTH0_MFA_OAUTH_HOST}{path}"
        if response.status >= 400 and response.status not in AUTH_REJECT_STATUSES:
            # 429 or 5xx is Auth0 being unavailable, not a factor problem.
            raise TransportError(f"{method} {url} returned HTTP {response.status}")
        parsed = _require_json(response, "mfa")
        if response.status >= 400:
            detail = parsed if isinstance(parsed, dict) else {}
            error = str(detail.get("error") or detail.get("code") or "invalid_grant")
            description = str(
                detail.get("error_description")
                or detail.get("description")
                or "authentication failed"
            )
            raise AuthError(error, description)
        return parsed

    def list_mfa_authenticators(self, mfa_token: str) -> tuple[dict[str, object], ...]:
        """List the enrolled Auth0 MFA factors for an in-progress login."""
        parsed = self._auth0_mfa_request(
            "GET", AUTH0_MFA_AUTHENTICATORS_PATH, mfa_token
        )
        # Auth0 returns a bare array of factors here, not {"authenticators": [...]}.
        rows: object = (
            parsed.get("authenticators") if isinstance(parsed, dict) else parsed
        )
        if not isinstance(rows, list):
            return ()
        authenticators: list[dict[str, object]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            authenticators.append(
                {
                    "id": row.get("id"),
                    "authenticator_type": row.get("authenticator_type"),
                    "oob_channel": row.get("oob_channel"),
                    "type": row.get("type"),
                }
            )
        return tuple(authenticators)

    def challenge_mfa(self, mfa_token: str, authenticator_id: str) -> MfaChallenge:
        """Trigger the SMS/email OOB challenge and return the code handle."""
        parsed = self._auth0_mfa_request(
            "POST",
            AUTH0_MFA_CHALLENGE_PATH,
            mfa_token,
            body={
                "client_id": AUTH0_CLIENT_ID,
                "mfa_token": mfa_token,
                "challenge_type": "oob",
                "authenticator_id": authenticator_id,
            },
        )
        mapping = parsed if isinstance(parsed, dict) else {}
        oob_code = mapping.get("oob_code")
        binding_method = mapping.get("binding_method")
        if not isinstance(oob_code, str) or not oob_code:
            raise AuthError("challenge_failed", "oob_code missing in challenge")
        binding = str(binding_method).lower() if binding_method else "prompt"
        if binding != "prompt":
            # Only a "prompt" OOB challenge can be satisfied by a single code
            # entered in the form; anything else cannot, so fall back to TOTP
            # rather than present an unsatisfiable code form.
            raise AuthError(
                "challenge_failed", f"unsupported binding_method: {binding}"
            )
        return MfaChallenge(
            oob_code=oob_code,
            binding_method=str(binding_method) if binding_method else "prompt",
        )

    def submit_mfa_oob(
        self, mfa_token: str, oob_code: str, binding_code: str
    ) -> Session:
        """Exchange an Auth0 MFA token and OOB code for a session."""
        payload = {
            "grant_type": AUTH0_MFA_OOB_GRANT,
            "client_id": AUTH0_CLIENT_ID,
            "mfa_token": mfa_token,
            "oob_code": oob_code,
            "binding_code": binding_code,
        }
        mapping = self._oauth_post(payload)
        session = _session_from_oauth(mapping, None)
        self._session = session
        return session

    def prepare_mfa(self, mfa_token: str) -> tuple[str, str | None]:
        """Pick a factor and send the SMS/email challenge when that is the path.

        Returns ``("oob", oob_code)`` only when the challenge produced a usable
        code. Probe or challenge failures fall back to ``("totp", None)``.
        """
        try:
            factor = _pick_mfa_factor(self.list_mfa_authenticators(mfa_token))
        except (AuthError, UsageError, OSError):
            return "totp", None
        authenticator_id = _oob_factor_authenticator_id(factor)
        if authenticator_id is None:
            return "totp", None
        try:
            challenge = self.challenge_mfa(mfa_token, authenticator_id)
        except (AuthError, UsageError, OSError):
            return "totp", None
        return _mfa_channel_from_challenge(factor, challenge)

    def refresh(self) -> Session:
        current = self._session
        if current is None or not current.refresh_token:
            raise AuthError("invalid_grant", "refresh_token missing")
        payload = {
            "client_id": AUTH0_CLIENT_ID,
            "refresh_token": current.refresh_token,
            "scope": AUTH0_SCOPE,
            "grant_type": AUTH0_REFRESH_GRANT,
        }
        mapping = self._oauth_post(payload)
        session = _session_from_oauth(mapping, current.refresh_token)
        self._session = session
        return session

    def ensure_session(self) -> Session:
        current = self._session
        if current is not None and not current.is_expired() and current.access_token:
            return current
        if current is not None and current.refresh_token:
            try:
                return self.refresh()
            except AuthError as exc:
                raise AuthError(
                    "invalid_grant",
                    f"stored session expired and refresh was rejected: {exc}",
                ) from exc
        raise AuthError("invalid_grant", "login credentials required")

    def _oauth_post(self, payload: dict[str, str]) -> dict[str, object]:
        response = self._transport.request(
            "POST",
            f"{IDENTITY_HOST}{OAUTH_TOKEN_PATH}",
            _oauth_headers(),
            json.dumps(payload).encode("utf-8"),
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        url = f"{IDENTITY_HOST}{OAUTH_TOKEN_PATH}"
        if response.status >= 400 and response.status not in AUTH_REJECT_STATUSES:
            # 429 or 5xx is Auth0 being unavailable, not a bad password. Reporting
            # it as an auth failure would push the user into a pointless reauth.
            raise TransportError(f"POST {url} returned HTTP {response.status}")
        body = _require_json(response, "oauth token")
        mapping = body if isinstance(body, dict) else {}
        if response.status >= 400:
            error = str(mapping.get("error") or mapping.get("code") or "invalid_grant")
            description = str(
                mapping.get("error_description")
                or mapping.get("description")
                or "authentication failed"
            )
            mfa_token = mapping.get("mfa_token")
            raise AuthError(
                error,
                description,
                mfa_token=mfa_token if isinstance(mfa_token, str) else None,
            )
        return mapping

    def fetch_usage(self) -> UsageReadings:
        session = self.ensure_session()
        try:
            return self._fetch_usage_with(session)
        except AuthError:
            if session.refresh_token:
                session = self.refresh()
                return self._fetch_usage_with(session)
            raise

    def _auth_headers(self, session: Session) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {session.access_token}",
            HEADER_ID_TOKEN: session.id_token,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }

    def _jarvis_get(
        self,
        session: Session,
        path: str,
        timeout: int = HTTP_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        return self._transport.request(
            "GET",
            f"{B2C_HOST}{path}",
            self._auth_headers(session),
            None,
            timeout=timeout,
        )

    def _jarvis_post(
        self,
        session: Session,
        path: str,
        payload: Mapping[str, object],
        timeout: int = HTTP_TIMEOUT_SECONDS,
    ) -> HttpResponse:
        headers = dict(self._auth_headers(session))
        headers["Content-Type"] = CONTENT_TYPE_JSON
        return self._transport.request(
            "POST",
            f"{B2C_HOST}{path}",
            headers,
            json.dumps(payload).encode("utf-8"),
            timeout=timeout,
        )

    def _optional_get(
        self,
        session: Session,
        path: str,
        timeout: int = OPTIONAL_HTTP_TIMEOUT_SECONDS,
    ) -> object | None:
        """GET a read the poll can do without: any failure logs and yields None."""
        try:
            response = self._jarvis_get(session, path, timeout=timeout)
        except TransportError as exc:
            _LOGGER.warning("skipping optional read: %s", exc)
            return None
        return _optional_json(response)

    def _optional_post(
        self,
        session: Session,
        path: str,
        payload: Mapping[str, object],
        timeout: int = OPTIONAL_HTTP_TIMEOUT_SECONDS,
    ) -> object | None:
        try:
            response = self._jarvis_post(session, path, payload, timeout=timeout)
        except TransportError as exc:
            _LOGGER.warning("skipping optional read: %s", exc)
            return None
        return _optional_json(response)

    def _fetch_usage_with(self, session: Session) -> UsageReadings:
        me_response = self._jarvis_get(session, JARVIS_ME_PATH)
        self._raise_auth_if_denied(me_response, "utility account")
        me_body = _require_mapping(
            _require_json(me_response, "utility account"), "utility account"
        )
        premise = self._select_premise(me_body)
        info = _parse_premise(premise)
        charts_response = self._jarvis_get(session, f"{JARVIS_CHARTS_PATH}/{info.id}")
        self._raise_auth_if_denied(charts_response, "charts")
        charts = _require_mapping(_require_json(charts_response, "charts"), "charts")
        electricity = _parse_utility(charts.get("elec"), "elec")
        water = _parse_utility(charts.get("water"), "water")
        gas = _parse_utility(charts.get("gas"), "gas")
        if electricity is None and water is None and gas is None:
            raise UsageError("no billed utilities")
        meter_reading, meter_registers = self._fetch_meter_reading(session, info.id)
        ppms_credit, ppms_updated = self._fetch_ppms(session, info)
        ami_hourly, ami_daily = self._fetch_ami(session, info)
        bills = self._fetch_bills(session, info.account_number)
        last_bill = bills[-1] if bills else None
        amount_due = self._fetch_amount_due(session, info)
        green_goals = self._fetch_green_goals(session, info.id)
        extras = self._fetch_optional(session, info, electricity)
        return UsageReadings(
            premise=info,
            electricity=electricity,
            water=water,
            gas=gas,
            meter_reading=meter_reading,
            ppms_credit=ppms_credit,
            ppms_updated_at=ppms_updated,
            ami_hourly=ami_hourly,
            ami_daily=ami_daily,
            last_bill=last_bill,
            bills=bills,
            amount_due=amount_due,
            meter_registers=meter_registers,
            green_goals=green_goals,
            greenup=extras.greenup,
            ev_wallet=extras.ev_wallet,
            ev_session=extras.ev_session,
            ev_last_charge=extras.ev_last_charge,
            ev_unpaid=extras.ev_unpaid,
            unread_notifications=extras.unread_notifications,
            bill_delivery=extras.bill_delivery,
            fcus=extras.fcus,
            tariff=extras.tariff,
        )

    def _fetch_meter_reading(
        self, session: Session, premise_id: str
    ) -> tuple[MeterReadingInfo | None, tuple[MeterRegister, ...]]:
        body = self._optional_get(
            session, f"{JARVIS_SMRD_PATH}/{premise_id}", timeout=HTTP_TIMEOUT_SECONDS
        )
        return _parse_meter_reading(body), _parse_meter_registers(body)

    def _fetch_green_goals(
        self, session: Session, premise_id: str
    ) -> tuple[GreenGoal, ...]:
        body = self._optional_get(session, JARVIS_GREEN_GOALS_PATH)
        if body is None:
            return ()
        return _parse_green_goals(body, premise_id)

    def _fetch_optional(
        self,
        session: Session,
        premise: PremiseInfo,
        electricity: UtilitySeries | None,
    ) -> OptionalReads:
        greenup = _parse_greenup(
            self._optional_post(
                session, GREENUP_GRAPHQL_PATH, {"query": GREENUP_ACCOUNT_QUERY}
            )
        )
        ev_wallet = _parse_ev_wallet(self._optional_get(session, TYCHE_WALLET_PATH))
        ev_session, ev_last_charge, ev_unpaid = self._fetch_eva(session)
        unread_path = f"{NOTIFICATIONS_PATH}?" + urlencode(
            {
                "limit": "1",
                "include_totals_unread_notifications": "true",
                "include_notifications": "false",
            }
        )
        unread = _parse_unread(self._optional_get(session, unread_path))
        bill_delivery = _parse_bill_delivery(
            self._optional_get(session, BILL_PREFERENCES_PATH),
            premise.account_number,
        )
        fcus = self._fetch_fcus(session, premise.account_number)
        tariff = self._fetch_tariff(electricity)
        return OptionalReads(
            greenup=greenup,
            ev_wallet=ev_wallet,
            ev_session=ev_session,
            ev_last_charge=ev_last_charge,
            ev_unpaid=ev_unpaid,
            unread_notifications=unread,
            bill_delivery=bill_delivery,
            fcus=fcus,
            tariff=tariff,
        )

    def _fetch_eva(
        self, session: Session
    ) -> tuple[EvSessionInfo | None, EvChargeInfo | None, EvUnpaidInfo | None]:
        try:
            response = self._jarvis_get(
                session,
                EVA_LATEST_SESSION_PATH,
                timeout=OPTIONAL_HTTP_TIMEOUT_SECONDS,
            )
        except TransportError as exc:
            _LOGGER.warning("skipping optional read: %s", exc)
            return None, None, None
        if _eva_scope_denied(response):
            return None, None, None
        ev_session = _parse_ev_session(_optional_json(response))
        history_qs = urlencode({"offSet": "0", "pageSize": "5"})
        history_path = f"{EVA_CHARGE_HISTORY_PATH}?{history_qs}"
        history_body = self._optional_get(session, history_path)
        ev_last_charge = _parse_ev_last_charge(history_body)
        ev_unpaid = _parse_ev_unpaid(self._optional_get(session, EVA_UNPAID_PATH))
        return ev_session, ev_last_charge, ev_unpaid

    def _fetch_fcus(
        self, session: Session, account_number: str | None
    ) -> tuple[FcuInfo, ...]:
        if not account_number:
            return ()
        body = self._optional_post(
            session,
            FROSTY_GRAPHQL_PATH,
            {
                "query": TENGAH_PAIRED_FCUS_QUERY,
                "variables": {"utilityAccountNumber": account_number},
            },
        )
        paired = _parse_paired_fcus(body)
        if not paired:
            return ()
        out: list[FcuInfo] = []
        for thing, display in paired:
            query = urlencode({"thingName": thing, "utility_acc_id": account_number})
            status = self._optional_get(session, f"{FROSTY_FCU_STATUS_PATH}?{query}")
            info = _parse_fcu_status(status, thing, display)
            if info is not None:
                out.append(info)
        return tuple(out)

    def _fetch_tariff(self, electricity: UtilitySeries | None) -> TariffInfo | None:
        consumption = _tariff_consumption(electricity)
        try:
            response = self._transport.request(
                "GET",
                f"{PUBLIC_HOST}{PRICEPLAN_PATH}"
                f"?{urlencode({'consumption': consumption})}",
                {
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
                None,
                timeout=OPTIONAL_HTTP_TIMEOUT_SECONDS,
            )
        except TransportError as exc:
            _LOGGER.warning("skipping optional read: %s", exc)
            return None
        body = _optional_json(response)
        if body is None:
            return None
        return _parse_tariff(body)

    def _fetch_ppms(
        self, session: Session, premise: PremiseInfo
    ) -> tuple[float | None, str | None]:
        if not premise.ppms_exists:
            return None, None
        body = self._optional_get(
            session, f"{JARVIS_PPMS_PATH}/{premise.id}", timeout=HTTP_TIMEOUT_SECONDS
        )
        if not isinstance(body, dict):
            return None, None
        return _optional_float(body.get("amount")), _optional_str(
            body.get("updated_at")
        )

    def _fetch_bills(
        self, session: Session, account_number: str | None
    ) -> tuple[BillInfo, ...]:
        if not account_number:
            return ()
        query = urlencode({"account_numbers": account_number})
        return _parse_bills(
            self._optional_get(
                session, f"{NJORD_HISTORY_PATH}?{query}", timeout=HTTP_TIMEOUT_SECONDS
            )
        )

    def _fetch_amount_due(
        self, session: Session, premise: PremiseInfo
    ) -> PayableInfo | None:
        body = self._optional_get(
            session, NJORD_PAYABLES_PATH, timeout=HTTP_TIMEOUT_SECONDS
        )
        return _parse_payable(body, premise.id, premise.account_number)

    def _fetch_ami(
        self, session: Session, premise: PremiseInfo
    ) -> tuple[tuple[PeriodReading, ...], tuple[PeriodReading, ...]]:
        if premise.ami_elec is not True:
            return (), ()
        now = datetime.now(SG_TZ)
        day_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
        day_start = (now - timedelta(days=AMI_HALF_HOUR_DAYS - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        month = now.month - (AMI_DAILY_MONTHS - 1)
        year = now.year
        while month <= 0:
            month += 12
            year -= 1
        month_start = now.replace(
            year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        hourly = self._fetch_ami_range(
            session, premise.id, AMI_GROUPED_BY_HALF_HOUR, day_start, day_end
        )
        daily = self._fetch_ami_range(
            session, premise.id, AMI_GROUPED_BY_DAILY, month_start, day_end
        )
        return hourly, daily

    def _fetch_ami_range(
        self,
        session: Session,
        premise_id: str,
        grouped_by: str,
        start: datetime,
        end: datetime,
    ) -> tuple[PeriodReading, ...]:
        payload = {
            "premise_id": premise_id,
            "start": _ami_stamp(start),
            "end": _ami_stamp(end),
            "grouped_by": grouped_by,
            "utility_type": "electric",
        }
        body = self._optional_post(
            session, JARVIS_AMI_PATH, payload, timeout=HTTP_TIMEOUT_SECONDS
        )
        return _parse_ami_rows(body)

    def _raise_auth_if_denied(self, response: HttpResponse, label: str) -> None:
        if response.status < 400:
            return
        mapping: dict[str, object] = {}
        try:
            decoded = _decode_json(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            decoded = {}
        if isinstance(decoded, dict):
            mapping = decoded
        extra = str(
            mapping.get("error_description")
            or mapping.get("error")
            or mapping.get("message")
            or ""
        )
        if response.status in {401, 403}:
            raise AuthError(
                str(mapping.get("error") or "unauthorized"),
                extra or f"{label} HTTP {response.status}",
            )
        raise UsageError(
            f"{label} HTTP {response.status}" + (f": {extra}" if extra else "")
        )

    def _select_premise(self, account: dict[str, object]) -> dict[str, object]:
        premises = account.get("premises")
        if not isinstance(premises, list) or not premises:
            raise UsageError("no premises on utility account")
        active: list[dict[str, object]] = []
        fallback: list[dict[str, object]] = []
        for raw in premises:
            if not isinstance(raw, dict):
                continue
            premise_id = raw.get("id")
            if not isinstance(premise_id, str) or not premise_id:
                continue
            fallback.append(raw)
            if raw.get("active") is True:
                active.append(raw)
        chosen = active or fallback
        if not chosen:
            raise UsageError("premise id missing")
        return chosen[0]
