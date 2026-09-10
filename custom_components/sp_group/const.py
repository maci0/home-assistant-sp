"""SP Group integration constants extracted from APK 15.10.0."""

from datetime import timedelta

DOMAIN = "sp_group"
ATTRIBUTION = "Data provided by SP Group"

IDENTITY_HOST = "https://identity.spdigital.sg"
B2C_HOST = "https://b2c.api.spdigital.sg"
PUBLIC_HOST = "https://public.api.spdigital.sg"

OAUTH_TOKEN_PATH = "/oauth/token"
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
# DAILY -> grouped_by "month" (one point per day).
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
TOKEN_EXPIRY_BUFFER_SECONDS = 60

CONF_ACCESS_TOKEN = "access_token"
CONF_ID_TOKEN = "id_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_MFA_CODE = "mfa_code"

USER_AGENT = "Infinity/15.10.0 (Android)"
HEADER_ID_TOKEN = "X-id-token"
CONTENT_TYPE_JSON = "application/json; charset=utf-8"
ACCEPT_LANGUAGE = "en_US"

HTTP_TIMEOUT_SECONDS = 30
OPTIONAL_HTTP_TIMEOUT_SECONDS = 8

# Auth0 /oauth/token statuses that are a verdict on the credentials. Anything
# else (429, 5xx) is the identity host being unavailable.
AUTH_REJECT_STATUSES = frozenset({400, 401, 403})

TARIFF_DEFAULT_CONSUMPTION_KWH = 350
EVA_INTEGER_CENTS_MIN = 100

# How much of an offending value a parse error quotes back, so a hostile
# response cannot push a megabyte of text into the log.
ERROR_VALUE_CHARS = 60

UPDATE_INTERVAL = timedelta(minutes=30)

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

# States this integration generates itself. Each one needs a matching
# entity.sensor.<key>.state entry in strings.json, or the raw value shows
# untranslated. Values reported by SP (account status, EV session) are passed
# through and cannot be translated.
SENSOR_STATE_EBILL = "ebill"
SENSOR_STATE_PAPER = "paper"
SENSOR_STATE_ON = "on"
SENSOR_STATE_OFF = "off"


def translated_error(key: str, exc: Exception) -> dict[str, object]:
    """Kwargs that show the matching strings.json exceptions entry, user language."""
    return {
        "translation_domain": DOMAIN,
        "translation_key": key,
        "translation_placeholders": {"error": str(exc)},
    }
