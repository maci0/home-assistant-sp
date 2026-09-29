"""Config flow: SP e-account username and password."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from functools import partial
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback

from .client import AuthError, Session, SpGroupClient, UsageError, session_entry_data
from .const import (
    CONF_ELECTRICITY_PRICE,
    CONF_MFA_CODE,
    DOMAIN,
    OAUTH_ERROR_MFA_REQUIRED,
    OAUTH_ERROR_REQUIRES_VERIFICATION,
    fold_text,
    parse_electricity_price,
    validate_username,
)

# The password bound only stops a pasted blob from being sent as a request
# body, so it stays a count of code points; the username is bounded in UTF-8
# octets by validate_username, which is the unit RFC 5321 caps.
MAX_PASSWORD_CHARS = 1024

_PASSWORD_VALIDATOR = vol.All(str, vol.Length(min=1, max=MAX_PASSWORD_CHARS))

_CREDENTIALS = {
    vol.Required(CONF_USERNAME): vol.All(str, validate_username),
    vol.Required(CONF_PASSWORD): _PASSWORD_VALIDATOR,
}

STEP_USER_DATA_SCHEMA = vol.Schema(dict(_CREDENTIALS))

_LOGGER = logging.getLogger(__name__)


def _entry_data(client: SpGroupClient, username: str) -> dict[str, str]:
    session = client.session
    if session is None:
        raise UsageError("session missing after login")
    return session_entry_data(session, username)


async def _validate(
    hass: HomeAssistant,
    username: str,
    password: str,
    exchange: Callable[[SpGroupClient], Session] | None = None,
) -> dict[str, str]:
    """Log in (or finish an MFA exchange) and read usage, in one executor job."""
    client = SpGroupClient()

    def _login_and_fetch() -> None:
        if exchange is None:
            client.login(username, password)
        else:
            exchange(client)
        client.fetch_usage()

    await hass.async_add_executor_job(_login_and_fetch)
    return _entry_data(client, username)


def _price_field(value: object) -> float | str:
    """The options form's price field: a non-negative number, or ``""``.

    The shipped option description tells the user to leave the field empty to
    disable the cost series, and the form framework submits an untouched
    optional field as an empty string. Coercing that with ``float()`` raised a
    form error instead, so the one way the description offers to turn the cost
    series off was a validation failure. Zero is accepted here and carries no
    price, as ``parse_electricity_price`` decides for the reader.
    """
    if isinstance(value, str):
        if not value.strip():
            return ""
        try:
            number = float(value.strip())
        except ValueError as exc:
            raise vol.Invalid(f"{CONF_ELECTRICITY_PRICE} must be a number") from exc
    elif isinstance(value, bool) or not isinstance(value, int | float):
        raise vol.Invalid(f"{CONF_ELECTRICITY_PRICE} must be a number")
    else:
        number = float(value)
    if not math.isfinite(number) or number < 0:
        raise vol.Invalid(f"{CONF_ELECTRICITY_PRICE} must be zero or more")
    return number


def _auth_error_key(exc: AuthError) -> str:
    """The strings.json error key: the Auth0 code, or invalid_auth for the rest."""
    if exc.error_folded in {OAUTH_ERROR_REQUIRES_VERIFICATION, "too_many_attempts"}:
        return exc.error_folded
    return "invalid_auth"


def _report_failure(step: str, exc: Exception, errors: dict[str, str]) -> None:
    """Set the form error and log the cause of a failed validation.

    The form key is what the user acts on, and it is one of two strings, so on
    its own it says nothing about why SP Group refused the login. The log line
    carries the exception and its traceback, so an Auth0 outage or a rejected
    password is diagnosable from the log alone.
    """
    key = _auth_error_key(exc) if isinstance(exc, AuthError) else "cannot_connect"
    errors["base"] = key
    _LOGGER.warning("SP Group %s failed: %s", step, exc, exc_info=exc)


class SpGroupOptionsFlow(config_entries.OptionsFlow):
    """A fixed electricity price so the Energy dashboard can show cost."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is not None:
            price = parse_electricity_price(user_input.get(CONF_ELECTRICITY_PRICE))
            if price is not None:
                options = {**self.config_entry.options, CONF_ELECTRICITY_PRICE: price}
            else:
                options = dict(self.config_entry.options)
                options.pop(CONF_ELECTRICITY_PRICE, None)
            return self.async_create_entry(title="", data=options)
        current = self.config_entry.options.get(CONF_ELECTRICITY_PRICE)
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ELECTRICITY_PRICE,
                    description={"suggested_value": current},
                ): _price_field,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class SpGroupConfigFlow(  # type: ignore[call-arg]  # domain= is ConfigFlow's own
    config_entries.ConfigFlow,
    domain=DOMAIN,
):
    VERSION = 1
    # Written by _start_mfa and read only by async_step_mfa, which the flow
    # cannot reach without it.
    _mfa_context: dict[str, Any]

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SpGroupOptionsFlow:
        return SpGroupOptionsFlow()

    def _mfa_form(
        self, errors: dict[str, str] | None = None
    ) -> config_entries.ConfigFlowResult:
        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema({vol.Required(CONF_MFA_CODE): str}),
            errors=errors,
        )

    async def _start_mfa(
        self,
        exc: AuthError,
        user_input: dict[str, Any],
        entry: config_entries.ConfigEntry | None,
    ) -> config_entries.ConfigFlowResult | None:
        """The verification form when Auth0 asked for a code, else None to report.

        The challenge that sends an SMS or email code is triggered here, before
        the user-facing form, so the code arrives while the form is shown. It is
        only triggered when the account has no usable authenticator-app factor,
        since the client prefers TOTP over out-of-band. Any failure to probe or
        challenge falls back to the TOTP single-code form and lets the server
        tell the user. A new account has no ``entry``; the MFA step then creates
        one instead of updating.
        """
        if exc.error_folded != OAUTH_ERROR_MFA_REQUIRED or not exc.mfa_token:
            return None
        self._mfa_context = {
            CONF_USERNAME: user_input[CONF_USERNAME],
            CONF_PASSWORD: user_input[CONF_PASSWORD],
            "mfa_token": exc.mfa_token,
            "entry": entry,
        }
        client = SpGroupClient()
        mfa_channel, mfa_oob_code = await self.hass.async_add_executor_job(
            client.prepare_mfa, exc.mfa_token
        )
        self._mfa_context["mfa_channel"] = mfa_channel
        if mfa_oob_code is not None:
            self._mfa_context["mfa_oob_code"] = mfa_oob_code
        return self._mfa_form()

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            context = self._mfa_context
            try:
                if context.get("mfa_channel") == "oob":
                    exchange = partial(
                        SpGroupClient.submit_mfa_oob,
                        mfa_token=context["mfa_token"],
                        oob_code=context["mfa_oob_code"],
                        binding_code=user_input[CONF_MFA_CODE],
                    )
                else:
                    exchange = partial(
                        SpGroupClient.submit_mfa,
                        mfa_token=context["mfa_token"],
                        otp=user_input[CONF_MFA_CODE],
                    )
                data = await _validate(
                    self.hass,
                    context[CONF_USERNAME],
                    context[CONF_PASSWORD],
                    exchange,
                )
            except (AuthError, UsageError, OSError) as exc:
                # _report_failure picks the form key off the type, so the arms
                # do not need splitting here.
                _report_failure("MFA code exchange", exc, errors)
            else:
                entry = context["entry"]
                if entry is None:
                    return self.async_create_entry(title="SP Group", data=data)
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self._mfa_form(errors)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(fold_text(user_input[CONF_USERNAME]))
            self._abort_if_unique_id_configured()
            try:
                data = await _validate(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except AuthError as exc:
                mfa = await self._start_mfa(exc, user_input, None)
                if mfa is not None:
                    return mfa
                _report_failure("login", exc, errors)
            except (UsageError, OSError) as exc:
                _report_failure("login", exc, errors)
            else:
                return self.async_create_entry(
                    title="SP Group",
                    data=data,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        return await self._credentials_step(self._get_reauth_entry(), "reauth_confirm")

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return await self._credentials_step(
            self._get_reauth_entry(), "reauth_confirm", user_input
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        return await self._credentials_step(
            self._get_reconfigure_entry(), "reconfigure", user_input
        )

    async def _credentials_step(
        self,
        entry: config_entries.ConfigEntry,
        step_id: str,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Ask for the credentials again and update the entry on success.

        The reauth and the reconfigure flow ask the same question and differ
        only in which step they show and which entry they update.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await _validate(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except AuthError as exc:
                mfa = await self._start_mfa(exc, user_input, entry)
                if mfa is not None:
                    return mfa
                _report_failure(step_id, exc, errors)
            except (UsageError, OSError) as exc:
                _report_failure(step_id, exc, errors)
            else:
                await self.async_set_unique_id(fold_text(user_input[CONF_USERNAME]))
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    **_CREDENTIALS,
                    vol.Required(
                        CONF_USERNAME,
                        default=entry.data.get(CONF_USERNAME, ""),
                    ): vol.All(str, validate_username),
                }
            ),
            errors=errors,
        )
