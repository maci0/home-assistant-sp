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
import threading
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from functools import cache
from http.client import HTTPException
from typing import Protocol, TypeGuard
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
    API_PATHS,
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
    CONF_ACCESS_TOKEN,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    CONTENT_TYPE_JSON,
    ERROR_VALUE_CHARS,
    EVA_CHARGE_HISTORY_PATH,
    EVA_INTEGER_CENTS_MIN,
    EVA_LATEST_SESSION_PATH,
    EVA_UNPAID_PATH,
    FROSTY_FCU_STATUS_PATH,
    FROSTY_GRAPHQL_PATH,
    GOAL_KIND_ELECTRICITY,
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
    JSON_ENCODING,
    LOGIN_RETRY_COOLDOWN_SECONDS,
    MAX_FCU_STATUS_READS,
    MAX_MONEY,
    MAX_RESPONSE_BYTES,
    MONEY_PRECISION,
    NJORD_HISTORY_PATH,
    NJORD_PAYABLES_PATH,
    NOTIFICATIONS_PATH,
    OAUTH_ERROR_MFA_REQUIRED,
    OAUTH_TOKEN_PATH,
    OPTIONAL_ABSENT_STATUSES,
    OPTIONAL_HTTP_TIMEOUT_SECONDS,
    PRICEPLAN_PATH,
    PUBLIC_HOST,
    SCOPE_NOT_FOUND,
    TARIFF_DEFAULT_CONSUMPTION_KWH,
    TOKEN_EXPIRY_BUFFER_SECONDS,
    TRANSPORT_ERROR_CHARS,
    TYCHE_WALLET_PATH,
    UNIT_KWH,
    UNIT_M3,
    USER_AGENT,
    fold_text,
)
from .models import (
    SG_TZ,
    BillDeliveryInfo,
    BillInfo,
    Clock,
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
    SystemClock,
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

# Independent reads of one poll run at most this wide. The widest fan-out is
# the optional block, which is seven reads, so this is the whole cap.
POLL_FANOUT_WORKERS = 8


@contextmanager
def _poll_pool(width: int) -> Iterator[ThreadPoolExecutor]:
    """A pool sized to one poll's fan-out, joined before the poll returns.

    A poll is roughly twenty sequential HTTPS calls spread over five hosts, and
    almost none of them depend on each other's answer. Run one after another
    the poll costs the sum of every connection setup and round trip; run
    together it costs the slowest single read. The poll already runs on an
    executor thread, so this bounds extra threads to one poll's worth.
    """
    with ThreadPoolExecutor(
        max_workers=min(max(width, 1), POLL_FANOUT_WORKERS),
        thread_name_prefix="sp_group_poll",
    ) as pool:
        yield pool


def _clean_text(value: object) -> str:
    """Remote text with control and bidi-format characters removed.

    Those characters reach the Home Assistant log and the reauth dialog
    verbatim, where a log viewer would act on an escape sequence and a
    right-to-left override could reverse how a rejection reads.
    """
    return " ".join("".join(char for char in str(value) if char.isprintable()).split())


def _safe_text(value: object, limit: int = ERROR_VALUE_CHARS) -> str:
    """Remote text, cleaned and cut short enough for a log line or a dialog."""
    text = _clean_text(value)
    return text if len(text) <= limit else text[:limit] + "..."


class AuthError(Exception):
    """Login rejected by identity.spdigital.sg (invalid credentials or Auth0 error)."""

    def __init__(
        self, error: str, error_description: str = "", mfa_token: str | None = None
    ) -> None:
        self.error = _clean_text(error)
        self.error_description = _clean_text(error_description)
        self.mfa_token = mfa_token
        super().__init__(self.error_description or self.error)

    @property
    def error_folded(self) -> str:
        """``error`` in the comparison form, for matching a protocol code.

        The code is server text, so it is matched folded: Auth0 spelling the
        same code with a different case is still the code the branch wants.
        """
        return fold_text(self.error)


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
    """Seam the client sends every request through; tests supply a fixture one."""

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        *,
        timeout: int | None = None,
    ) -> HttpResponse: ...


def _request_label(url: str) -> str:
    """The request target without its query string.

    Query strings here carry account numbers, thing names, and consumption
    values, so only the path reaches a log line or an error message.
    """
    return url.split("?", 1)[0]


def _log_optional_status(path: str, status: int) -> None:
    """Report an optional read that answered with an error status.

    A 5xx means the dependency is broken and the entities it feeds go
    unavailable, so it warns. A 4xx means the account has no enrollment for
    that service, which is the expected steady state and only a debug line
    here; ``_optional_json`` then warns again for the same response, with the
    server's message, when the body reaches it.
    """
    if status >= 500:
        _LOGGER.warning("optional read %s returned HTTP %s", path, status)
    else:
        _LOGGER.debug("optional read %s returned HTTP %s", path, status)


@cache
def _ssl_context() -> ssl.SSLContext:
    """The verified-certificate context, built once per process.

    Building one re-reads and re-parses the CA bundle from disk, and a poll
    makes a dozen requests. An SSLContext is immutable once created and safe to
    share between the requests, so there is nothing to release per call.
    """
    return ssl.create_default_context()


_LITERAL_PATH_SEGMENTS = frozenset(
    segment for path in API_PATHS for segment in path.split("/") if segment
)
IDENTIFIER_SEGMENT = "{id}"


def _loggable_url(url: str) -> str:
    """The host and the route, with the account-shaped parts taken out.

    Error messages reach the Home Assistant log and the user-facing reauth
    text, so the query string (account number, thing name, consumption) and
    every path segment that is not a fixed route are dropped: those are the
    premise id, account number, and order id.
    """
    without_query, _, _ = url.partition("?")
    scheme, separator, rest = without_query.partition("://")
    host, _, path = rest.partition("/")
    segments = [
        segment if segment in _LITERAL_PATH_SEGMENTS else IDENTIFIER_SEGMENT
        for segment in path.split("/")
        if segment
    ]
    root = f"{scheme}{separator}{host}" if separator else host
    return "/".join([root, *segments]) if segments else root


class BodyReader(Protocol):
    """The read side of an open response or an HTTPError."""

    def read(self, amount: int, /) -> bytes: ...


def _read_bounded(response: BodyReader, label: str) -> bytes:
    """The body, refused past MAX_RESPONSE_BYTES.

    A hostile or misconfigured upstream can stream an unbounded body; the
    read would grow the Home Assistant process until it is swapped out or
    killed, taking every other integration with it. One byte over the cap is
    enough to tell an oversized body from a large legitimate one.

    ``label`` names the read, because a poll makes twenty of them and the cap
    alone says nothing about which one was refused.
    """
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise TransportError(
            f"{label} response body over {MAX_RESPONSE_BYTES} bytes refused"
        )
    return body


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
        # Every caller builds url from a host in const.py plus a path, so the
        # scheme is https on every request that reaches this method.
        request = Request(url, data=body, method=method, headers=dict(headers))  # noqa: S310
        seconds = HTTP_TIMEOUT_SECONDS if timeout is None else timeout
        started = time.monotonic()
        label = _read_label(method, url)
        try:
            with urlopen(request, timeout=seconds, context=_ssl_context()) as response:  # noqa: S310
                http = HttpResponse(
                    status=int(response.status), body=_read_bounded(response, label)
                )
        except HTTPError as exc:
            with exc:
                http = HttpResponse(
                    status=int(exc.code), body=_read_bounded(exc, label)
                )
        except (OSError, HTTPException) as exc:
            # URLError, socket timeout, and TLS failures all land here. Name the
            # call and the timeout so the log says which host stalled the poll.
            raise TransportError(
                f"{method} {_loggable_url(url)} failed (timeout {seconds}s): "
                f"{_safe_text(exc, TRANSPORT_ERROR_CHARS)}"
            ) from exc
        # Debug, so the poll's cost per dependency is visible when someone turns
        # the domain up without filling a normal log with 15 lines a cycle.
        _LOGGER.debug(
            "%s %s -> HTTP %s in %d ms",
            method,
            _request_label(url),
            http.status,
            round((time.monotonic() - started) * 1000),
        )
        return http


@dataclass(frozen=True)
class Session:
    access_token: str
    id_token: str
    refresh_token: str | None
    expires_at: int | None = None
    # The scope Auth0 granted, which can be narrower than AUTH0_SCOPE. A read
    # the grant does not cover fails with a 403, and the scope is what says
    # which read, so keep it with the session instead of only the constant.
    scope: str | None = None

    def is_expired(self, epoch: int) -> bool:
        if self.expires_at is None:
            return False
        return epoch >= self.expires_at - TOKEN_EXPIRY_BUFFER_SECONDS


def _session_is_live(session: Session | None, epoch: int) -> TypeGuard[Session]:
    """A session that can still authenticate a read, so needs no refresh."""
    return (
        session is not None
        and bool(session.access_token)
        and not session.is_expired(epoch)
    )


def _decode_json(body: bytes) -> object:
    if not body:
        return {}
    return json.loads(body.decode(JSON_ENCODING))


def _require_json(response: HttpResponse, label: str) -> object:
    """Decode a response the read cannot continue without."""
    try:
        return _decode_json(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # The decoder quotes fragments of the body it choked on, which is
        # upstream text.
        raise UsageError(
            f"{label} HTTP {response.status}: response was not JSON ({_safe_text(exc)})"
        ) from exc


def _read_label(method: str, url: str) -> str:
    """Name a read for the log: the method and the redacted URL."""
    return f"{method} {_loggable_url(url)}"


def _error_text(mapping: dict[str, object]) -> str:
    """The host's own explanation of a failure, in the order SP states it."""
    return str(
        mapping.get("error_description")
        or mapping.get("error")
        or mapping.get("message")
        or ""
    )


def _server_message(response: HttpResponse) -> str:
    """Whatever the host said went wrong, quoted short and redacted.

    Error text reaches the Home Assistant log, so it is truncated: a hostile or
    broken gateway can put a megabyte in the body. It is cleaned for the same
    reason: the body is host text, and a newline or an escape sequence in it
    would forge log lines or reformat the log viewer around them.
    """
    try:
        decoded = _decode_json(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    if not isinstance(decoded, dict):
        return ""
    message = _error_text(decoded)
    text = _safe_text(message)
    return f": {text}" if text else ""


def _optional_json(response: HttpResponse, label: str) -> object | None:
    """Decode an optional read, or None when the host reported no data.

    A status in OPTIONAL_ABSENT_STATUSES means the read had nothing to report
    and is not an error. Any other error status is a failed read, and it is
    logged with the server's own message: without it a 401 on every poll drops
    a sensor with no trace of why, which reads as "no usage" rather than
    "the token was rejected".
    """
    if response.status >= 400:
        if response.status not in OPTIONAL_ABSENT_STATUSES:
            _LOGGER.warning(
                "%s returned HTTP %s%s",
                label,
                response.status,
                _server_message(response),
            )
        return None
    try:
        return _decode_json(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _LOGGER.warning(
            "%s returned a body that is not JSON: %s", label, _safe_text(exc)
        )
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
    error = fold_text(str(body.get("error") or ""))
    description = fold_text(str(body.get("error_description") or ""))
    return error == SCOPE_NOT_FOUND or SCOPE_NOT_FOUND in description


def _round_half_up(value: Decimal, exponent: str) -> float | None:
    """Round to ``exponent`` half-up, or None when the value is out of range.

    ``round()`` is half-to-even, so a 2.675 dollar amount settles at 2.67 and a
    2.665 one at 2.66, one cent away from what a bill charges. Money is rounded
    through Decimal so the tie is decided on the decimal value, not on the
    binary one, where 2.675 sits just under the half.

    quantize() raises rather than returning a number once the result needs more
    digits than the context holds, so a magnitude no bill can carry is dropped
    instead: a hostile 1e308 would abort the whole poll over one field.
    """
    if not value.is_finite() or abs(value) > MAX_MONEY:
        return None
    with localcontext() as context:
        context.prec = MONEY_PRECISION
        return float(value.quantize(Decimal(exponent), rounding=ROUND_HALF_UP))


def _round_sgd(value: Decimal) -> float | None:
    """SGD to cents, half-up."""
    return _round_half_up(value, "0.01")


def _cents_to_sgd(value: object, *, cents_min: int | None = None) -> float | None:
    """Parse a money field as cents, or None when it is not a finite number.

    ``cents_min`` is the magnitude at or above which a value is cents; without
    it every value is cents. Eva integer amounts below the threshold are whole
    dollars, not cents.
    """
    number = _optional_float(value)
    if number is None:
        return None
    amount = Decimal(str(number))
    if cents_min is not None and abs(number) < cents_min:
        return _round_sgd(amount)
    return _round_sgd(amount / 100)


def _eva_sgd(value: object) -> float | None:
    """Eva money fields are Java String, not Njord integer cents."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        return _round_sgd(Decimal(str(value))) if math.isfinite(value) else None
    if isinstance(value, int):
        return _cents_to_sgd(value, cents_min=EVA_INTEGER_CENTS_MIN)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if "." not in text:
            try:
                return _cents_to_sgd(int(text), cents_min=EVA_INTEGER_CENTS_MIN)
            except ValueError:
                # Not a plain integer literal ("1e3", "1_0"), so the cents
                # decision falls to the parsed magnitude: an integral value is
                # an integer amount, however it was spelled. Deciding on the
                # text instead read "1e3" as 1000 dollars against "1000"
                # cents, the same amount 100 times apart.
                whole = _optional_float(text)
                if whole is not None and whole.is_integer():
                    return _cents_to_sgd(int(whole), cents_min=EVA_INTEGER_CENTS_MIN)
        dollars = _optional_float(text)
        return _round_sgd(Decimal(str(dollars))) if dollars is not None else None
    return None


def _tariff_consumption(electricity: UtilitySeries | None) -> str:
    if electricity is None or not electricity.periods:
        return str(TARIFF_DEFAULT_CONSUMPTION_KWH)
    last = max(electricity.periods, key=lambda item: item.start)
    # Half-up, not ``round``: round() is half-to-even, so a 142.5 kWh period
    # asks the price-plan endpoint for 142 kWh and a 141.5 kWh one for 142.
    kwh = _round_half_up(Decimal(str(last.amount)), "1")
    if kwh is None or kwh <= 0:
        return str(TARIFF_DEFAULT_CONSUMPTION_KWH)
    return str(int(kwh))


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise UsageError(f"{label} is not an object")
    return value


def _is_number(value: object) -> TypeGuard[int | float | str]:
    """The types a reading can carry, excluding a bool and a missing value.

    bool is a subclass of int, so ``float(True)`` is 1.0 and a flag in a
    payload would read as a one-unit measurement.
    """
    return not isinstance(value, bool) and isinstance(value, (int, float, str))


def _float(value: object) -> float:
    """Billed consumption: a malformed number fails the read, never reads as zero."""
    if not _is_number(value) or value == "":
        return 0.0
    number = _optional_float(value)
    if number is None:
        raise UsageError(f"expected a number, got {_safe_text(value)}")
    return number


def _optional_float(value: object) -> float | None:
    """None unless the value is a finite number.

    json.loads accepts the NaN and Infinity literals, "1e400" parses to inf and
    an out-of-range JSON integer overflows float(); none of the three can be a
    sensor state.
    """
    if not _is_number(value) or value == "":
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
        expires_at=_jwt_exp(access_token),
        scope=scope if isinstance(scope, str) else None,
    )


def session_entry_data(session: Session, username: str) -> dict[str, str]:
    """The config entry data for a session: the account name plus its tokens.

    The password is not among them. It buys one session and the refresh token
    buys every session after it, so keeping the password would leave the
    e-account password in ``.storage`` for the life of the entry, readable by
    anything that can read the config entry and copied into every backup. A
    reauth or a reconfigure asks for it again instead.
    """
    data = {
        CONF_USERNAME: username,
        CONF_ACCESS_TOKEN: session.access_token,
        CONF_ID_TOKEN: session.id_token,
    }
    if session.refresh_token:
        data[CONF_REFRESH_TOKEN] = session.refresh_token
    return data


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


def _factor_type(factor: dict[str, object]) -> str:
    """An enrolled factor's type, in the comparison form."""
    return fold_text(str(factor.get("authenticator_type") or ""))


def _pick_mfa_factor(
    authenticators: tuple[dict[str, object], ...],
) -> dict[str, object] | None:
    """Choose the login factor an account can use.

    Prefers the TOTP factor (stable, no short-lived out-of-band code), then a
    usable out-of-band factor (SMS, then email). A recovery-code factor is not
    a usable login factor and is ignored. Returns None when only recovery codes
    (or nothing) are enrolled. Factors with ``active: false`` are skipped.

    The type and channel are server text, so both are matched folded rather
    than by byte equality.
    """
    for factor in authenticators:
        if _factor_type(factor) in {"otp", "totp"} and _factor_usable(factor):
            return factor
    oob: dict[str, dict[str, object]] = {}
    for factor in authenticators:
        if _factor_type(factor) != "oob" or not _factor_usable(factor):
            continue
        channel = fold_text(str(factor.get("oob_channel") or ""))
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
    if factor is None or _factor_type(factor) != "oob":
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


def _energy_or_volume_unit(unit: str, kind: str) -> str:
    """Canonical unit for a billed series, from the unit SP reported.

    The reported unit is server text, so compare it in the folded form and
    drop every kind of space, not only U+0020: a non-breaking space inside
    "kWh" is enough to fail the electricity read otherwise. The unit handed
    back is folded and stripped too, so no stray space reaches a sensor.
    """
    folded = fold_text(unit)
    compact = "".join(folded.split())
    if kind == "elec":
        if unit and compact not in {"kwh", "kw·h"}:
            raise UsageError(f"unexpected electricity unit {_safe_text(unit)}")
        return "kWh"
    volume = (
        "m³" if compact in {"m3", "cum", "cu.m", "cbm"} else (folded.strip() or "m³")
    )
    if kind == "water":
        return volume
    if compact in {"kwh", "kw·h"}:
        return "kWh"
    return volume


def _parse_period_start(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
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
        ppms_exists=isinstance(ppms, dict) and ppms.get("exists") is True,
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
        # The goal's type is server text and the goal is looked up folded, so
        # the unit default that hangs off the same type is decided folded too.
        # "Elec" must not pick the volume default for an electricity goal.
        folded_kind = fold_text(kind)
        unit = _optional_str(row.get("unit")) or (
            UNIT_KWH if folded_kind == GOAL_KIND_ELECTRICITY else UNIT_M3
        )
        if folded_kind == "water":
            unit = _energy_or_volume_unit(unit, "water")
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
                    _cents_to_sgd(cost_cents) if cost_cents is not None else None
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
    # A voided or refunded receipt reports 0 kWh, which is a reading, not an
    # absent one: only a missing field falls through to connector_kwh.
    kwh = _optional_float(first.get("total_consumption"))
    if kwh is None:
        kwh = _optional_float(first.get("connector_kwh"))
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
    rows = payload.get("orders")
    if not isinstance(rows, list) or not rows:
        return None
    orders = [row for row in rows if isinstance(row, dict)]
    if not orders:
        return None
    # Added in cents, not as binary floats: 12.30 + 7.35 is 19.649999999999999
    # in float, and the sensor then reports a total no bill ever matched.
    total_cents = 0
    found = False
    for row in orders:
        amount = _eva_sgd(row.get("amount"))
        if amount is not None:
            total_cents += round(amount * 100)
            found = True
    return EvUnpaidInfo(count=len(orders), amount=total_cents / 100 if found else None)


def _parse_unread(body: object) -> int | None:
    if not isinstance(body, dict):
        return None
    value = body.get("total_unread_notifications")
    if isinstance(value, bool) or not isinstance(value, int | str) or value == "":
        return None
    try:
        return int(value)
    except ValueError:
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
    bills: list[tuple[datetime, BillInfo]] = []
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
        issued_at = (
            _parse_period_start(date)
            or _parse_period_start(period)
            or _parse_period_start(created)
        )
        info = BillInfo(
            amount_sgd=amount,
            date=date,
            period=period,
            due_date=_optional_str(bill.get("due_date")),
            issued_at=issued_at,
        )
        # Order by the instant, never by the timestamp text: Njord returns
        # ``Z`` for some rows and ``+08:00`` for others, and a lexicographic
        # sort puts a 09:00+08:00 bill after a 20:00Z bill of the same day
        # even though the first was issued hours earlier. Sorting text made
        # the wrong bill the latest one and miscredited monthly statistics.
        bills.append((issued_at or datetime.min.replace(tzinfo=UTC), info))
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


def _drop_future(
    periods: tuple[PeriodReading, ...], now: datetime
) -> tuple[PeriodReading, ...]:
    """Drop AMI slots that start at or after ``now``.

    The monthly feed pads the rest of the month with zero days. A zero point
    in the future would sit in Energy statistics with a flat sum until the
    recorder tries to write that hour itself.
    """
    return tuple(item for item in periods if item.start < now)


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


_LOGIN_FAILURE_LOCK = threading.Lock()
_LOGIN_FAILURES: dict[str, float] = {}


def _login_cooldown(username: str, clock: Clock) -> float:
    """Seconds left on the username's cooldown; 0 when a login may be attempted."""
    with _LOGIN_FAILURE_LOCK:
        failed_at = _LOGIN_FAILURES.get(fold_text(username))
    if failed_at is None:
        return 0.0
    return max(0.0, LOGIN_RETRY_COOLDOWN_SECONDS - (clock.monotonic() - failed_at))


def _note_login_failure(username: str, clock: Clock) -> None:
    """Start the cooldown, dropping the entries that already expired.

    The dict is module-level and lives as long as the Home Assistant process,
    but an entry older than the cooldown no longer throttles anything: a name
    tried once years ago and never again would sit here forever. Sweeping on
    write bounds the map to the usernames that failed within one cooldown
    window, with no separate timer to keep alive.
    """
    now = clock.monotonic()
    with _LOGIN_FAILURE_LOCK:
        for name, failed_at in tuple(_LOGIN_FAILURES.items()):
            if now - failed_at >= LOGIN_RETRY_COOLDOWN_SECONDS:
                del _LOGIN_FAILURES[name]
        _LOGIN_FAILURES[fold_text(username)] = now


def _clear_login_failures(username: str) -> None:
    with _LOGIN_FAILURE_LOCK:
        _LOGIN_FAILURES.pop(fold_text(username), None)


class SpGroupClient:
    def __init__(
        self,
        transport: Transport | None = None,
        session: Session | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._transport = transport or UrllibTransport()
        self._session = session
        # Auth0 rotates the refresh token: every exchange retires the token it
        # spent, so a second exchange of the same one is rejected and would
        # discard a session that is already good. The lock serializes the
        # exchange only; reading the session never blocks.
        self._refresh_lock = threading.Lock()
        self._clock = clock or SystemClock()

    @property
    def clock(self) -> Clock:
        """The clock every time-dependent read in this client goes through."""
        return self._clock

    @property
    def session(self) -> Session | None:
        return self._session

    def _adopt_session(
        self, mapping: dict[str, object], fallback_refresh: str | None = None
    ) -> Session:
        """Take a freshly issued token set as the session every later read uses."""
        session = _session_from_oauth(mapping, fallback_refresh)
        self._session = session
        return session

    def login(self, username: str, password: str) -> Session:
        cooldown = _login_cooldown(username, self._clock)
        if cooldown > 0:
            raise AuthError(
                "too_many_attempts",
                f"a previous sign-in failed; retry in {math.ceil(cooldown)}s",
            )
        payload = {
            "client_id": AUTH0_CLIENT_ID,
            "audience": AUTH0_AUDIENCE,
            "username": username,
            "password": password,
            "scope": AUTH0_SCOPE,
            "grant_type": AUTH0_GRANT_TYPE,
            "realm": AUTH0_REALM,
        }
        try:
            mapping = self._oauth_post(payload)
        except AuthError as exc:
            # A rejected credential starts a cooldown; a transport failure is the
            # host being down and throttling it would not help the user. An MFA
            # challenge is not a rejection at all: the password was right.
            if exc.error_folded != OAUTH_ERROR_MFA_REQUIRED:
                _note_login_failure(username, self._clock)
            raise
        _clear_login_failures(username)
        return self._adopt_session(mapping)

    def submit_mfa(self, mfa_token: str, otp: str) -> Session:
        """Exchange an Auth0 MFA token and one-time password for a session."""
        payload = {
            "grant_type": AUTH0_MFA_OTP_GRANT,
            "client_id": AUTH0_CLIENT_ID,
            "mfa_token": mfa_token,
            "otp": otp,
        }
        return self._adopt_session(self._oauth_post(payload))

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
        return self._post_json(
            AUTH0_MFA_OAUTH_HOST, path, headers, body, method=method, label="mfa"
        )

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
        binding = fold_text(str(binding_method)) if binding_method else "prompt"
        if binding != "prompt":
            # Only a "prompt" OOB challenge can be satisfied by a single code
            # entered in the form; anything else cannot, so fall back to TOTP
            # rather than present an unsatisfiable code form.
            raise AuthError(
                "challenge_failed", f"unsupported binding_method: {binding}"
            )
        return MfaChallenge(oob_code=oob_code)

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
        return self._adopt_session(self._oauth_post(payload))

    def prepare_mfa(self, mfa_token: str) -> tuple[str, str | None]:
        """Pick a factor and send the SMS/email challenge when that is the path.

        Returns ``("oob", oob_code)`` only when the challenge produced a usable
        code. Probe or challenge failures fall back to ``("totp", None)`` and
        are logged with their cause, because the caller shows a single-code
        form the user would otherwise read as the account having no other
        factor.
        """
        try:
            factor = _pick_mfa_factor(self.list_mfa_authenticators(mfa_token))
        except (AuthError, UsageError, OSError) as exc:
            _LOGGER.warning(
                "listing MFA factors failed (%s); asking for a TOTP code instead", exc
            )
            return "totp", None
        authenticator_id = _oob_factor_authenticator_id(factor)
        if authenticator_id is None:
            return "totp", None
        try:
            challenge = self.challenge_mfa(mfa_token, authenticator_id)
        except (AuthError, UsageError, OSError) as exc:
            _LOGGER.warning(
                "sending the out-of-band MFA challenge failed (%s); "
                "asking for a TOTP code instead",
                exc,
            )
            return "totp", None
        return _mfa_channel_from_challenge(factor, challenge)

    def refresh(self) -> Session:
        with self._refresh_lock:
            return self._exchange(self._session)

    def _exchange(self, current: Session | None) -> Session:
        if current is None or not current.refresh_token:
            raise AuthError("invalid_grant", "refresh_token missing")
        payload = {
            "client_id": AUTH0_CLIENT_ID,
            "refresh_token": current.refresh_token,
            "scope": AUTH0_SCOPE,
            "grant_type": AUTH0_REFRESH_GRANT,
        }
        mapping = self._oauth_post(payload)
        return self._adopt_session(mapping, current.refresh_token)

    def ensure_session(self) -> Session:
        current = self._session
        if _session_is_live(current, self._clock.timestamp()):
            return current
        if current is None or not current.refresh_token:
            raise AuthError("invalid_grant", "login credentials required")
        with self._refresh_lock:
            # A concurrent fetch may have refreshed while this one waited; take
            # that session rather than spend the retired refresh token again.
            current = self._session
            if _session_is_live(current, self._clock.timestamp()):
                return current
            try:
                return self._exchange(current)
            except AuthError as exc:
                raise AuthError(
                    "invalid_grant",
                    f"stored session expired and refresh was rejected: {exc}",
                ) from exc

    def _post_json(
        self,
        host: str,
        path: str,
        headers: Mapping[str, str],
        payload: dict[str, str] | None,
        *,
        method: str = "POST",
        label: str,
    ) -> object:
        """Send a JSON body to an Auth0 host and return the parsed response.

        A 429 or 5xx is Auth0 being unavailable, not a bad password or a factor
        problem, so it raises TransportError instead of AuthError: reporting it
        as an auth failure would push the user into a pointless reauth.
        """
        url = f"{host}{path}"
        response = self._transport.request(
            method,
            url,
            headers,
            json.dumps(payload).encode("utf-8") if payload is not None else None,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        if response.status >= 400 and response.status not in AUTH_REJECT_STATUSES:
            raise TransportError(
                f"{method} {_loggable_url(url)} returned HTTP {response.status}"
            )
        parsed = _require_json(response, label)
        if response.status < 400:
            return parsed
        detail = parsed if isinstance(parsed, dict) else {}
        error = _safe_text(detail.get("error") or detail.get("code") or "invalid_grant")
        description = _safe_text(
            detail.get("error_description")
            or detail.get("description")
            or "authentication failed"
        )
        mfa_token = detail.get("mfa_token")
        raise AuthError(
            error,
            description,
            mfa_token=mfa_token if isinstance(mfa_token, str) else None,
        )

    def _oauth_post(self, payload: dict[str, str]) -> dict[str, object]:
        body = self._post_json(
            IDENTITY_HOST,
            OAUTH_TOKEN_PATH,
            _oauth_headers(),
            payload,
            label="oauth token",
        )
        return body if isinstance(body, dict) else {}

    def fetch_usage(self) -> UsageReadings:
        """One poll, blocking, from the identity host to the optional reads.

        Refreshes the session first when the stored one is unusable, and
        replaces it on the way out, so a caller that persists
        ``self.session`` afterwards keeps the tokens this poll used. Only a
        failed or empty ``/me``, ``/charts``, or a premise with no billed
        utility fails the poll; every other read that errors skips the
        sensors it feeds.
        """
        session = self.ensure_session()
        try:
            return self._fetch_usage_with(session)
        except AuthError as exc:
            if session.refresh_token:
                # A read rejected the token it carried. One refresh and one
                # retry, so the retry itself needs no log of its own.
                _LOGGER.debug("read rejected the session, refreshing it: %s", exc)
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

    def _optional_response(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object] | None,
        timeout: int = OPTIONAL_HTTP_TIMEOUT_SECONDS,
    ) -> HttpResponse | None:
        """Send a read the poll can do without: any failure logs and yields None."""
        try:
            response = self._transport.request(
                method,
                url,
                headers,
                json.dumps(payload).encode("utf-8") if payload is not None else None,
                timeout=timeout,
            )
        except TransportError as exc:
            _LOGGER.warning("skipping optional read: %s", exc)
            return None
        if response.status >= 400:
            # The status on its own, at the level that says how much it matters:
            # a 5xx takes the sensors it feeds away, a 4xx is the account not
            # being enrolled for the service. _optional_json adds the server's
            # own message for this same response.
            _log_optional_status(_request_label(url), response.status)
        return response

    def _optional_get(
        self,
        session: Session,
        path: str,
        timeout: int = OPTIONAL_HTTP_TIMEOUT_SECONDS,
    ) -> object | None:
        url = f"{B2C_HOST}{path}"
        response = self._optional_response(
            "GET", url, self._auth_headers(session), None, timeout
        )
        if response is None:
            return None
        return _optional_json(response, _read_label("GET", url))

    def _optional_post(
        self,
        session: Session,
        path: str,
        payload: Mapping[str, object],
        timeout: int = OPTIONAL_HTTP_TIMEOUT_SECONDS,
    ) -> object | None:
        headers = dict(self._auth_headers(session))
        headers["Content-Type"] = CONTENT_TYPE_JSON
        url = f"{B2C_HOST}{path}"
        response = self._optional_response("POST", url, headers, payload, timeout)
        if response is None:
            return None
        return _optional_json(response, _read_label("POST", url))

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
        with _poll_pool(3) as pool:
            meter_future = pool.submit(self._fetch_meter_reading, session, info.id)
            ppms_future = pool.submit(self._fetch_ppms, session, info)
            ami_future = pool.submit(self._fetch_ami, session, info)
            meter_reading, meter_registers = meter_future.result()
            ppms_credit, ppms_updated_at = ppms_future.result()
            ami_hourly, ami_daily = ami_future.result()
        if (
            electricity is None
            and water is None
            and gas is None
            and (ami_hourly or ami_daily)
        ):
            # A premise activated days ago has AMI slots before its first bill.
            electricity = UtilitySeries(
                total=0.0, unit=UNIT_KWH, periods=(), average=None, comparison=None
            )
        if electricity is None and water is None and gas is None:
            raise UsageError("no billed utilities")
        with _poll_pool(4) as pool:
            bills_future = pool.submit(self._fetch_bills, session, info.account_number)
            due_future = pool.submit(self._fetch_amount_due, session, info)
            goals_future = pool.submit(self._fetch_green_goals, session, info.id)
            optional_future = pool.submit(
                self._fetch_optional, session, info, electricity
            )
            bills = bills_future.result()
            amount_due = due_future.result()
            green_goals = goals_future.result()
            extras = optional_future.result()
        last_bill = bills[-1] if bills else None
        return UsageReadings(
            premise=info,
            electricity=electricity,
            water=water,
            gas=gas,
            meter_reading=meter_reading,
            ppms_credit=ppms_credit,
            ppms_updated_at=ppms_updated_at,
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
        with _poll_pool(7) as pool:
            greenup_future = pool.submit(self._fetch_greenup, session)
            wallet_future = pool.submit(self._fetch_ev_wallet, session)
            unread_future = pool.submit(self._fetch_unread, session)
            delivery_future = pool.submit(
                self._fetch_bill_delivery, session, premise.account_number
            )
            eva_future = pool.submit(self._fetch_eva, session)
            fcus_future = pool.submit(self._fetch_fcus, session, premise.account_number)
            tariff_future = pool.submit(self._fetch_tariff, electricity)
            greenup = greenup_future.result()
            wallet = wallet_future.result()
            unread = unread_future.result()
            delivery = delivery_future.result()
            eva = eva_future.result()
            fcus = fcus_future.result()
            tariff = tariff_future.result()
        ev_session, ev_last_charge, ev_unpaid = eva
        return OptionalReads(
            greenup=greenup,
            ev_wallet=wallet,
            ev_session=ev_session,
            ev_last_charge=ev_last_charge,
            ev_unpaid=ev_unpaid,
            unread_notifications=unread,
            bill_delivery=delivery,
            fcus=fcus,
            tariff=tariff,
        )

    def _fetch_greenup(self, session: Session) -> GreenUpInfo | None:
        return _parse_greenup(
            self._optional_post(
                session, GREENUP_GRAPHQL_PATH, {"query": GREENUP_ACCOUNT_QUERY}
            )
        )

    def _fetch_ev_wallet(self, session: Session) -> EvWalletInfo | None:
        return _parse_ev_wallet(self._optional_get(session, TYCHE_WALLET_PATH))

    def _fetch_unread(self, session: Session) -> int | None:
        path = f"{NOTIFICATIONS_PATH}?" + urlencode(
            {
                "limit": "1",
                "include_totals_unread_notifications": "true",
                "include_notifications": "false",
            }
        )
        return _parse_unread(self._optional_get(session, path))

    def _fetch_bill_delivery(
        self, session: Session, account_number: str | None
    ) -> BillDeliveryInfo | None:
        return _parse_bill_delivery(
            self._optional_get(session, BILL_PREFERENCES_PATH), account_number
        )

    def _fetch_eva(
        self, session: Session
    ) -> tuple[EvSessionInfo | None, EvChargeInfo | None, EvUnpaidInfo | None]:
        eva_url = f"{B2C_HOST}{EVA_LATEST_SESSION_PATH}"
        response = self._optional_response(
            "GET",
            eva_url,
            self._auth_headers(session),
            None,
            OPTIONAL_HTTP_TIMEOUT_SECONDS,
        )
        if response is None:
            return None, None, None
        if _eva_scope_denied(response):
            # A 403 the host attributes to the grant, not to an enrollment. The
            # generic optional-read line files it as the expected "not signed up"
            # case, which hides the one thing worth acting on: a token that will
            # never carry these reads until it is re-issued with the scope.
            _LOGGER.warning(
                "optional read %s returned HTTP 403: the session's grant does not "
                "cover Eva, so the Eva sensors stay unset",
                EVA_LATEST_SESSION_PATH,
            )
            return None, None, None
        ev_session = _parse_ev_session(
            _optional_json(response, _read_label("GET", eva_url))
        )
        history_qs = urlencode({"offSet": "0", "pageSize": "5"})
        history_path = f"{EVA_CHARGE_HISTORY_PATH}?{history_qs}"
        ev_last_charge = _parse_ev_last_charge(
            self._optional_get(session, history_path)
        )
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
        if len(paired) > MAX_FCU_STATUS_READS:
            _LOGGER.warning(
                "upstream listed %d paired FCUs, reading the first %d",
                len(paired),
                MAX_FCU_STATUS_READS,
            )
            paired = paired[:MAX_FCU_STATUS_READS]
        with _poll_pool(len(paired)) as pool:
            futures = [
                pool.submit(self._fcu_status, session, thing, account_number)
                for thing, _ in paired
            ]
            out: list[FcuInfo] = []
            for (thing, display), future in zip(paired, futures, strict=True):
                info = _parse_fcu_status(future.result(), thing, display)
                if info is not None:
                    out.append(info)
        return tuple(out)

    def _fcu_status(
        self, session: Session, thing: str, account_number: str | None
    ) -> object | None:
        query = urlencode({"thingName": thing, "utility_acc_id": account_number})
        return self._optional_get(session, f"{FROSTY_FCU_STATUS_PATH}?{query}")

    def _fetch_tariff(self, electricity: UtilitySeries | None) -> TariffInfo | None:
        query = urlencode({"consumption": _tariff_consumption(electricity)})
        url = f"{PUBLIC_HOST}{PRICEPLAN_PATH}?{query}"
        response = self._optional_response(
            "GET",
            url,
            {"User-Agent": USER_AGENT, "Accept": "application/json"},
            None,
        )
        if response is None:
            return None
        return _parse_tariff(_optional_json(response, _read_label("GET", url)))

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
        return (
            _optional_float(body.get("amount")),
            _optional_str(body.get("updated_at")),
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
        now = self._clock.now()
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
        return _drop_future(hourly, now), _drop_future(daily, now)

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
        periods = _parse_ami_rows(body)
        if body is not None and not periods:
            # A body that parsed but carried no usable slot is not the same as
            # no body, and only one of the two means the window is empty. Left
            # unlogged, a response shape change stops the energy statistics
            # updating with every sensor still reading normally.
            _LOGGER.warning(
                "%s read a body with no usable %s slots for %s to %s",
                _read_label("POST", f"{B2C_HOST}{JARVIS_AMI_PATH}"),
                grouped_by,
                _ami_stamp(start),
                _ami_stamp(end),
            )
        return periods

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
        extra = _safe_text(_error_text(mapping))
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
