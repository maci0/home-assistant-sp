"""Serializable HA field presentation, independent of server validation rules."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector


class ValidatedTextSelector(selector.Selector[selector.TextSelectorConfig]):
    """Render a native text selector while retaining a server-side validator.

    Both legacy and modern HA custom serializers recognize Selector instances
    and call serialize(), without inspecting the validator stored here. The
    original object remains on the server and validates submitted values.
    """

    selector_type = "text"
    CONFIG_SCHEMA = selector.TextSelector.CONFIG_SCHEMA

    def __init__(self, validator: Callable[[Any], Any]) -> None:
        super().__init__({})
        self._validator = validator

    def __call__(self, value: Any) -> Any:
        """Keep normalization and domain validation in the submitted schema."""
        try:
            return self._validator(value)
        except ValueError as exc:
            raise vol.Invalid(str(exc)) from exc
