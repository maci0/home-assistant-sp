"""Config flow: SP e-account username and password."""

# mypy: ignore-errors

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .client import AuthError, SpGroupClient, UsageError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ID_TOKEN,
    CONF_MFA_CODE,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _entry_data(client: SpGroupClient, username: str, password: str) -> dict[str, str]:
    session = client.session
    if session is None:
        raise UsageError("session missing after login")
    data = {
        CONF_USERNAME: username,
        CONF_PASSWORD: password,
        CONF_ACCESS_TOKEN: session.access_token,
        CONF_ID_TOKEN: session.id_token,
    }
    if session.refresh_token:
        data[CONF_REFRESH_TOKEN] = session.refresh_token
    return data


async def _validate(
    hass: HomeAssistant, username: str, password: str
) -> dict[str, str]:
    client = SpGroupClient()

    def _login_and_fetch() -> None:
        client.login(username, password)
        client.fetch_usage()

    await hass.async_add_executor_job(_login_and_fetch)
    return _entry_data(client, username, password)


async def _validate_mfa(
    hass: HomeAssistant,
    username: str,
    password: str,
    mfa_token: str,
    otp: str,
) -> dict[str, str]:
    client = SpGroupClient()

    def _submit_and_fetch() -> None:
        client.submit_mfa(mfa_token, otp)
        client.fetch_usage()

    await hass.async_add_executor_job(_submit_and_fetch)
    return _entry_data(client, username, password)


async def _validate_mfa_oob(
    hass: HomeAssistant,
    username: str,
    password: str,
    mfa_token: str,
    oob_code: str,
    binding_code: str,
) -> dict[str, str]:
    client = SpGroupClient()

    def _submit_and_fetch() -> None:
        client.submit_mfa_oob(mfa_token, oob_code, binding_code)
        client.fetch_usage()

    await hass.async_add_executor_job(_submit_and_fetch)
    return _entry_data(client, username, password)


def _auth_error_key(exc: AuthError) -> str:
    return (
        "requires_verification"
        if exc.error == "requires_verification"
        else "invalid_auth"
    )


class SpGroupConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

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
        mode: str,
        entry: config_entries.ConfigEntry | None = None,
    ) -> config_entries.ConfigFlowResult | None:
        """The verification form when Auth0 asked for a code, else None to report.

        When the account's only enrolled factor is an out-of-band (SMS/email)
        oob factor, the challenge that sends the code is triggered here, before
        the user-facing form, so the code arrives while the form is shown. Any
        failure to probe or challenge falls back to the TOTP single-code form
        and lets the server tell the user.
        """
        if exc.error != "mfa_required" or not exc.mfa_token:
            return None
        self._mfa_context = {
            CONF_USERNAME: user_input[CONF_USERNAME],
            CONF_PASSWORD: user_input[CONF_PASSWORD],
            "mfa_token": exc.mfa_token,
            "mode": mode,
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
                    data = await _validate_mfa_oob(
                        self.hass,
                        context[CONF_USERNAME],
                        context[CONF_PASSWORD],
                        context["mfa_token"],
                        context["mfa_oob_code"],
                        user_input[CONF_MFA_CODE],
                    )
                else:
                    data = await _validate_mfa(
                        self.hass,
                        context[CONF_USERNAME],
                        context[CONF_PASSWORD],
                        context["mfa_token"],
                        user_input[CONF_MFA_CODE],
                    )
            except AuthError as exc:
                errors["base"] = _auth_error_key(exc)
            except (UsageError, OSError):
                errors["base"] = "cannot_connect"
            else:
                mode = context["mode"]
                if mode == "user":
                    return self.async_create_entry(title="SP Group", data=data)
                entry = context["entry"]
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self._mfa_form(errors)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
            self._abort_if_unique_id_configured()
            try:
                data = await _validate(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except AuthError as exc:
                mfa = await self._start_mfa(exc, user_input, "user")
                if mfa is not None:
                    return mfa
                errors["base"] = _auth_error_key(exc)
            except (UsageError, OSError):
                errors["base"] = "cannot_connect"
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
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                data = await _validate(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except AuthError as exc:
                mfa = await self._start_mfa(exc, user_input, "reauth", reauth_entry)
                if mfa is not None:
                    return mfa
                errors["base"] = _auth_error_key(exc)
            except (UsageError, OSError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(
                    reauth_entry, data_updates=data
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=reauth_entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            try:
                data = await _validate(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except AuthError as exc:
                mfa = await self._start_mfa(exc, user_input, "reconfigure", entry)
                if mfa is not None:
                    return mfa
                errors["base"] = _auth_error_key(exc)
            except (UsageError, OSError):
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower())
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data_updates=data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )
