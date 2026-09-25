"""SP Group Home Assistant integration.

Setup imports homeassistant and the sibling modules inside ``async_setup_entry``
rather than at module scope: this is the package ``__init__``, so every
``custom_components.sp_group.*`` import runs it, and the tests exercise the
client, mapper, and history modules without homeassistant installed.
"""

# mypy: ignore-errors

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN, translated_error

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import SpGroupCoordinator

    type SpGroupConfigEntry = ConfigEntry[SpGroupCoordinator]

PLATFORMS = ["sensor"]

__all__ = ["DOMAIN", "PLATFORMS"]


async def async_setup_entry(hass: HomeAssistant, entry: SpGroupConfigEntry) -> bool:
    from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

    from .client import AuthError, Session, SpGroupClient, UsageError
    from .const import CONF_ACCESS_TOKEN, CONF_ID_TOKEN, CONF_REFRESH_TOKEN
    from .coordinator import SpGroupCoordinator

    access = entry.data.get(CONF_ACCESS_TOKEN)
    ident = entry.data.get(CONF_ID_TOKEN)
    refresh = entry.data.get(CONF_REFRESH_TOKEN)
    session = None
    if isinstance(access, str) and isinstance(ident, str) and access and ident:
        session = Session(
            access_token=access,
            id_token=ident,
            refresh_token=refresh if isinstance(refresh, str) else None,
            scope=None,
        )
    client = SpGroupClient(
        session=session,
    )
    coordinator = SpGroupCoordinator(hass, client, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
    except AuthError as exc:
        if exc.error == "requires_verification":
            raise ConfigEntryNotReady(
                str(exc), **translated_error("requires_verification", exc)
            ) from exc
        raise ConfigEntryAuthFailed(
            str(exc), **translated_error("auth_failed", exc)
        ) from exc
    except UsageError as exc:
        raise ConfigEntryNotReady(
            str(exc), **translated_error("usage_failed", exc)
        ) from exc
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.mark_platforms_ready()
    await coordinator.async_import_billed_history()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SpGroupConfigEntry) -> bool:
    await entry.runtime_data.async_stop_stats_import()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_entry_updated(hass: HomeAssistant, entry: SpGroupConfigEntry) -> None:
    """Options changed (the coordinator also updates data to persist tokens)."""
    await entry.runtime_data.async_options_updated()
