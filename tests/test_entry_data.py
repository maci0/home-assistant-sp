"""What the config entry keeps: the session, and nothing that reopens the account."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from custom_components.sp_group import _forget_password
from custom_components.sp_group.client import Session, session_entry_data
from custom_components.sp_group.const import (
    CONF_ACCESS_TOKEN,
    CONF_ID_TOKEN,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
)


def test_entry_data_carries_the_session_and_not_the_password() -> None:
    """The password buys one token exchange; the refresh token buys the rest.

    Everything in the entry lands in ``.storage`` and in every Home Assistant
    backup, so a field nothing reads back does not belong there.
    """
    session = Session(access_token="access", id_token="ident", refresh_token="refresh")

    data = session_entry_data(session, "user@example.com")

    assert data == {
        CONF_USERNAME: "user@example.com",
        CONF_ACCESS_TOKEN: "access",
        CONF_ID_TOKEN: "ident",
        CONF_REFRESH_TOKEN: "refresh",
    }
    assert CONF_PASSWORD not in data


@dataclass
class _Entry:
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class _ConfigEntries:
    entry: _Entry
    written: list[dict[str, Any]] = field(default_factory=list)

    def async_update_entry(self, entry: _Entry, *, data: dict[str, Any]) -> None:
        entry.data = data
        self.written.append(data)


@dataclass
class _Hass:
    config_entries: _ConfigEntries


def test_a_password_an_earlier_version_stored_is_dropped_on_load() -> None:
    entry = _Entry(
        {
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_ACCESS_TOKEN: "access",
            CONF_ID_TOKEN: "ident",
            CONF_REFRESH_TOKEN: "refresh",
        }
    )
    hass = _Hass(_ConfigEntries(entry))

    _forget_password(hass, entry)

    assert entry.data == {
        CONF_USERNAME: "user@example.com",
        CONF_ACCESS_TOKEN: "access",
        CONF_ID_TOKEN: "ident",
        CONF_REFRESH_TOKEN: "refresh",
    }


def test_an_entry_without_a_password_is_left_alone() -> None:
    entry = _Entry({CONF_USERNAME: "user@example.com", CONF_ACCESS_TOKEN: "access"})
    hass = _Hass(_ConfigEntries(entry))

    _forget_password(hass, entry)

    assert hass.config_entries.written == []
