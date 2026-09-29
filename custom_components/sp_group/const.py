"""SP Group integration constants extracted from APK 15.10.0."""

from __future__ import annotations

import math
import unicodedata
from datetime import timedelta
from decimal import Decimal

DOMAIN = "sp_group"
ATTRIBUTION = "Data provided by SP Group"

IDENTITY_HOST = "https://identity.spdigital.sg"
B2C_HOST = "https://b2c.api.spdigital.sg"
PUBLIC_HOST = "https://public.api.spdigital.sg"

OAUTH_TOKEN_PATH = "/oauth/token"  # noqa: S105
JARVIS_ME_PATH = "/jarvis/v3/me"
JARVIS_CHARTS_PATH = "/jarvis/v4/charts"
JARVIS_PPMS_PATH = "/jarvis/v3/ppms/balance"
JARVIS_SMRD_PATH = "/jarvis/v3/smrd-uportal"
JARVIS_AMI_PATH = "/jarvis/v3/ami/charts"
JARVIS_GREEN_GOALS_PATH = "/jarvis/v5/greengoals/targets"
NJORD_PAYABLES_PATH = "/njord/v4/payables"
NJORD_HISTORY_PATH = "/njord/v3/history"
GREENUP_GRAPHQL_PATH = "/1up/authenticated/graphql"
TYCHE_WALLET_PATH = "/tyche/v1/wallet-summary"
EVA_LATEST_SESSION_PATH = "/eva/v1/sessions/latest"
EVA_CHARGE_HISTORY_PATH = "/eva/v2/order/receipts"
EVA_UNPAID_PATH = "/eva/v1/order/unpaid"
NOTIFICATIONS_PATH = "/notifications/v1/notifications"
BILL_PREFERENCES_PATH = "/skalbox/b2c/account/v1/retrieveBillPreferences"
FROSTY_GRAPHQL_PATH = "/frosty/graphql"
FROSTY_FCU_STATUS_PATH = "/frosty/fcu_status"
PRICEPLAN_PATH = "/priceplan/v2/plans/price"

# SmartMeterChartRequestModel: HOURLY -> grouped_by "day" (30-min slots),
# DAILY -> grouped_by "month" (one point per day). The client asks for the
# half-hour feed over AMI_HALF_HOUR_DAYS and the daily feed over
# AMI_DAILY_MONTHS, which together set the window the statistics cover.
AMI_GROUPED_BY_HALF_HOUR = "day"
AMI_GROUPED_BY_DAILY = "month"
AMI_HALF_HOUR_DAYS = 31
AMI_DAILY_MONTHS = 13
AMI_DATE_FORMAT = "%Y%m%d%H%M%S"

AUTH0_CLIENT_ID = "z1tl6I1V6HI201ule9tmSALb97hw8Biu"
AUTH0_AUDIENCE = "https://profile.up.spdigital.sg/"
# Mh.a.h() joins these for Auth0Api.login. Missing me:* scopes yields
# MuleSoft 403 invalid_claim on /jarvis/v3/me.
AUTH0_SCOPE = (
    "openid email profile offline_access enroll read:authenticators "
    "user_metadata me me:uportal me:eva me:rbac"
)
AUTH0_GRANT_TYPE = "http://auth0.com/oauth/grant-type/password-realm"
AUTH0_MFA_OTP_GRANT = "http://auth0.com/oauth/grant-type/mfa-otp"
AUTH0_MFA_OOB_GRANT = "http://auth0.com/oauth/grant-type/mfa-oob"
AUTH0_MFA_OAUTH_HOST = "https://identity.spdigital.auth0.com"
AUTH0_MFA_AUTHENTICATORS_PATH = "/mfa/authenticators"
AUTH0_MFA_CHALLENGE_PATH = "/mfa/challenge"
AUTH0_REFRESH_GRANT = "refresh_token"
AUTH0_REALM = "Username-Password-Authentication"

# Every protocol path, so a caller can tell a fixed route segment from a
# per-account identifier appended to it (premise id, account number, order id).
API_PATHS = (
    OAUTH_TOKEN_PATH,
    JARVIS_ME_PATH,
    JARVIS_CHARTS_PATH,
    JARVIS_PPMS_PATH,
    JARVIS_SMRD_PATH,
    JARVIS_AMI_PATH,
    JARVIS_GREEN_GOALS_PATH,
    NJORD_PAYABLES_PATH,
    NJORD_HISTORY_PATH,
    GREENUP_GRAPHQL_PATH,
    TYCHE_WALLET_PATH,
    EVA_LATEST_SESSION_PATH,
    EVA_CHARGE_HISTORY_PATH,
    EVA_UNPAID_PATH,
    NOTIFICATIONS_PATH,
    BILL_PREFERENCES_PATH,
    FROSTY_GRAPHQL_PATH,
    FROSTY_FCU_STATUS_PATH,
    PRICEPLAN_PATH,
    AUTH0_MFA_AUTHENTICATORS_PATH,
    AUTH0_MFA_CHALLENGE_PATH,
)
TOKEN_EXPIRY_BUFFER_SECONDS = 60

# The config entry keys and the header name below are key names, not secrets;
# the credential values reach the client from the config entry at runtime.
CONF_ACCESS_TOKEN = "access_token"  # noqa: S105
CONF_ID_TOKEN = "id_token"  # noqa: S105
CONF_REFRESH_TOKEN = "refresh_token"  # noqa: S105
CONF_MFA_CODE = "mfa_code"
CONF_ELECTRICITY_PRICE = "electricity_price"
# The same values as homeassistant.const, so the Home-Assistant-free client
# layer can build the config entry data dict.
CONF_USERNAME = "username"
CONF_PASSWORD = "password"  # noqa: S105

USER_AGENT = "Infinity/15.10.0 (Android)"
HEADER_ID_TOKEN = "X-id-token"  # noqa: S105
CONTENT_TYPE_JSON = "application/json; charset=utf-8"
# The encoding every SP response body is decoded with. utf-8-sig is utf-8 that
# also drops a leading byte-order mark, which a .NET gateway in front of an
# endpoint is free to send and which json.loads rejects.
JSON_ENCODING = "utf-8-sig"
ACCEPT_LANGUAGE = "en_US"

HTTP_TIMEOUT_SECONDS = 30
OPTIONAL_HTTP_TIMEOUT_SECONDS = 8

# Auth0 /oauth/token statuses that are a verdict on the credentials. Anything
# else (429, 5xx) is the identity host being unavailable.
AUTH_REJECT_STATUSES = frozenset({400, 401, 403})

# Auth0 error codes this integration branches on.
OAUTH_ERROR_MFA_REQUIRED = "mfa_required"
OAUTH_ERROR_REQUIRES_VERIFICATION = "requires_verification"
# Eva answers a token that lacks the read scope with this code, in ``error``
# or inside ``error_description``.
SCOPE_NOT_FOUND = "scope_not_found"

# How an optional read reports that the thing it reads does not exist: no paired
# FCU, no charge receipt yet. Any other error status on an optional read is a
# failed read rather than an absent one, and is logged instead of dropped.
OPTIONAL_ABSENT_STATUSES = frozenset({404})

TARIFF_DEFAULT_CONSUMPTION_KWH = 350
EVA_INTEGER_CENTS_MIN = 100

# Ceiling and precision for money rounded through Decimal. Decimal.quantize
# raises InvalidOperation once the result needs more digits than the context
# holds, so a magnitude past the ceiling is dropped as unparseable instead of
# aborting the poll. A trillion dollars is far past any premise's bill,
# balance, or credit, and the digits still fit with room to spare.
MAX_MONEY = Decimal("1e12")
MONEY_PRECISION = 20

# RFC 5321 caps an address at 254 octets, so the username bound is counted in
# UTF-8 bytes, not in code points: 254 astral code points is over a thousand
# bytes on the wire, and 64 code points of one is already past the cap.
MAX_USERNAME_OCTETS = 254

# How SP spells each utility in each payload. The SMRD registers say "electric"
# and Green Goals say "elec"; both lookups fold before comparing, so these are
# the wire spellings, not two names for one thing.
METER_UTILITY_ELECTRICITY = "electric"
METER_UTILITY_WATER = "water"
GOAL_KIND_ELECTRICITY = "elec"
GOAL_KIND_WATER = "water"

# How much of an offending value a parse error quotes back, so a hostile
# response cannot push a megabyte of text into the log.
ERROR_VALUE_CHARS = 60
# Same bound for a transport failure's own text, which is longer than a parse
# error's (it carries the errno and the address it failed on).
TRANSPORT_ERROR_CHARS = 160

# Largest response body the client will buffer. Every SP Group read is well
# under this (a month of half-hour AMI is a few hundred kilobytes), so a body
# past it is a hostile or broken upstream streaming without an end.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# A failed password login blocks the next one for the same account this long.
# Auth0 counts retries per account and answers bot detection with a lockout,
# so a mistyped password, a stale stored one, or a reauth loop must not turn
# into a locked utility account.
LOGIN_RETRY_COOLDOWN_SECONDS = 60

# How many paired-FCU coils one poll reads the status of. Each is a separate
# request at OPTIONAL_HTTP_TIMEOUT_SECONDS, and the coil list is upstream data,
# so an unbounded fan-out would let a hostile list stall the poll executor.
MAX_FCU_STATUS_READS = 8

UPDATE_INTERVAL = timedelta(minutes=30)

# A poll that succeeds this slowly still refreshed every sensor, so the
# entities look healthy and nothing else records the cost. The required reads
# time out after HTTP_TIMEOUT_SECONDS and the optional ones after
# OPTIONAL_HTTP_TIMEOUT_SECONDS, and they run in fan-out pools, so a poll
# several times over that is upstream latency, not a slow network.
SLOW_POLL_MS = 60_000

DEVICE_CLASS_ENERGY = "energy"
DEVICE_CLASS_WATER = "water"
DEVICE_CLASS_GAS = "gas"
DEVICE_CLASS_MONETARY = "monetary"
DEVICE_CLASS_TEMPERATURE = "temperature"
STATE_CLASS_TOTAL_INCREASING = "total_increasing"
STATE_CLASS_TOTAL = "total"
STATE_CLASS_MEASUREMENT = "measurement"
UNIT_KWH = "kWh"
UNIT_M3 = "m³"
UNIT_SGD = "SGD"
UNIT_POINTS = "points"
UNIT_CELSIUS = "°C"
ENTITY_CATEGORY_DIAGNOSTIC = "diagnostic"

SENSOR_KEY_ELECTRICITY = "electricity"
SENSOR_KEY_WATER = "water"
SENSOR_KEY_GAS = "gas"
SENSOR_KEY_ELECTRICITY_LAST = "electricity_last_period"
SENSOR_KEY_WATER_LAST = "water_last_period"
SENSOR_KEY_GAS_LAST = "gas_last_period"
SENSOR_KEY_ELECTRICITY_TODAY = "electricity_today"
# Unique id suffix stays electricity_last_hour so existing entities keep their
# id. The translated name is the last published 30-minute AMI slot.
SENSOR_KEY_ELECTRICITY_HOUR = "electricity_last_hour"
SENSOR_KEY_ACCOUNT = "account"
SENSOR_KEY_PPMS = "ppms_credit"
SENSOR_KEY_LAST_BILL = "last_bill"
STATISTIC_KEY_ELECTRICITY_COST = "electricity_cost"
SENSOR_KEY_AMOUNT_DUE = "amount_due"
SENSOR_KEY_ELECTRICITY_METER = "electricity_meter"
SENSOR_KEY_WATER_METER = "water_meter"
SENSOR_KEY_ELECTRICITY_GOAL = "electricity_goal"
SENSOR_KEY_WATER_GOAL = "water_goal"
SENSOR_KEY_GREENUP_POINTS = "greenup_points"
SENSOR_KEY_EV_WALLET = "ev_wallet"
SENSOR_KEY_EV_SESSION = "ev_session"
SENSOR_KEY_EV_LAST_CHARGE = "ev_last_charge"
SENSOR_KEY_EV_UNPAID = "ev_unpaid"
SENSOR_KEY_UNREAD_NOTIFICATIONS = "unread_notifications"
SENSOR_KEY_BILL_DELIVERY = "bill_delivery"
SENSOR_KEY_FCU = "fcu"
SENSOR_KEY_TARIFF = "tariff"

# Display names for the imported recorder statistics. Unlike an entity name,
# a statistic name is written to the database with the first row of the
# series and is never translated afterwards, so these stay English and stay
# fixed: the README and the options description tell the user which name to
# pick in the Energy settings. A sensor key is not a name, so each series
# names itself rather than interpolating its key.
STATISTIC_NAME_ELECTRICITY = "SP Group electricity"
STATISTIC_NAME_GAS = "SP Group gas"
STATISTIC_NAME_ELECTRICITY_COST = "SP Group electricity cost"
STATISTIC_NAME_BILL = "SP Group bill"

# States this integration generates itself. Each one needs a matching
# entity.sensor.<key>.state entry in strings.json, or the raw value shows
# untranslated. Values reported by SP (account status, EV session) are passed
# through and cannot be translated.
SENSOR_STATE_EBILL = "ebill"
SENSOR_STATE_PAPER = "paper"
SENSOR_STATE_ON = "on"
SENSOR_STATE_OFF = "off"
# Stands in for an account or EV status the API left empty. It is a state the
# integration writes, so it needs a state entry like the others.
SENSOR_STATE_UNKNOWN = "unknown"


def parse_electricity_price(raw: object) -> float | None:
    """The configured SGD/kWh price, or None when the option carries no price.

    One rule for both the options flow and the reader: unset, empty, zero, and
    negative all mean "no price", because the option holds a price and not a
    switch. Anything else that is not a finite number raises, so a value a user
    hand-edited into .storage is reported instead of quietly costing nothing.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ValueError(f"{CONF_ELECTRICITY_PRICE} must be a number, got {raw!r}")
    try:
        price = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{CONF_ELECTRICITY_PRICE} must be a number, got {raw!r}"
        ) from exc
    if not math.isfinite(price):
        raise ValueError(f"{CONF_ELECTRICITY_PRICE} must be finite, got {raw!r}")
    return price if price > 0 else None


def validate_username(raw: str) -> str:
    """The username as typed, or a ValueError when it cannot be an address.

    The length is in UTF-8 octets, because that is the unit RFC 5321 caps. A
    lone surrogate has no UTF-8 form and is rejected here rather than raising
    out of the flow when the credential is sent.
    """
    if not raw:
        raise ValueError("username must not be empty")
    try:
        octets = len(raw.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("username is not valid Unicode") from exc
    if octets > MAX_USERNAME_OCTETS:
        raise ValueError(f"username must be at most {MAX_USERNAME_OCTETS} bytes")
    return raw


def fold_text(value: str) -> str:
    """The one comparison form for text that came from outside this source.

    NFKC so the NFC and NFD spellings of an accented name are one value, and
    compatibility spellings (full-width, superscript) match their plain form;
    casefold so the match does not depend on case; stripped so a pasted
    trailing space or a non-breaking space does not make one account a
    second account.
    """
    return unicodedata.normalize("NFKC", value).casefold().strip()


def translated_error(key: str, exc: Exception) -> dict[str, object]:
    """Kwargs that show the matching strings.json exceptions entry, user language."""
    return {
        "translation_domain": DOMAIN,
        "translation_key": key,
        "translation_placeholders": {"error": str(exc)},
    }
