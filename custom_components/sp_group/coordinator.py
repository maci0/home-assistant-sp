"""Poll SP Group usage on a fixed interval."""

# mypy: ignore-errors

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AuthError, SpGroupClient, UsageError
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ELECTRICITY_PRICE,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    SENSOR_KEY_ELECTRICITY,
    SENSOR_KEY_GAS,
    SENSOR_KEY_LAST_BILL,
    STATISTIC_KEY_ELECTRICITY_COST,
    UNIT_KWH,
    UNIT_SGD,
    UPDATE_INTERVAL,
    translated_error,
)
from .history import (
    cost_points,
    cumulative_points,
    external_statistic_id,
    monthly_bill_points,
)
from .mapper import electricity_graph_periods
from .models import UsageReadings

_LOGGER = logging.getLogger(__name__)


class SpGroupCoordinator(DataUpdateCoordinator[UsageReadings]):
    def __init__(
        self, hass: HomeAssistant, client: SpGroupClient, entry: ConfigEntry
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client
        self.entry = entry
        self._platforms_ready = False
        self._stats_lock = asyncio.Lock()
        self._stats_task: asyncio.Task[None] | None = None
        self._imported_price: float | None = None

    def mark_platforms_ready(self) -> None:
        self._platforms_ready = True

    @property
    def electricity_price(self) -> float | None:
        """Fixed SGD/kWh price from the entry options, or None when unset."""
        raw = self.entry.options.get(CONF_ELECTRICITY_PRICE)
        try:
            price = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None
        return price if price and price > 0 else None

    async def async_options_updated(self) -> None:
        """Re-import the cost series when the configured price changed."""
        if self.electricity_price != self._imported_price:
            await self.async_import_billed_history()

    async def async_stop_stats_import(self) -> None:
        """Cancel an import still writing statistics when the entry goes away."""
        self._platforms_ready = False
        task = self._stats_task
        self._stats_task = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _async_update_data(self) -> UsageReadings:
        try:
            usage = await self.hass.async_add_executor_job(self.client.fetch_usage)
        except AuthError as exc:
            if exc.error == "requires_verification":
                raise UpdateFailed(
                    str(exc), **translated_error("requires_verification", exc)
                ) from exc
            raise ConfigEntryAuthFailed(
                str(exc), **translated_error("auth_failed", exc)
            ) from exc
        except UsageError as exc:
            raise UpdateFailed(
                str(exc), **translated_error("usage_failed", exc)
            ) from exc
        self._persist_session_if_changed()
        if self._platforms_ready:
            self._schedule_stats_import()
        return usage

    def _persist_session_if_changed(self) -> None:
        session = self.client.session
        if session is None:
            return
        data = self.entry.data
        refresh = session.refresh_token or None
        stored_refresh = data.get(CONF_REFRESH_TOKEN) or None
        if (
            data.get(CONF_ACCESS_TOKEN) == session.access_token
            and data.get(CONF_ID_TOKEN) == session.id_token
            and stored_refresh == refresh
        ):
            return
        payload = {
            CONF_USERNAME: data[CONF_USERNAME],
            CONF_PASSWORD: data[CONF_PASSWORD],
            CONF_ACCESS_TOKEN: session.access_token,
            CONF_ID_TOKEN: session.id_token,
        }
        if session.refresh_token:
            payload[CONF_REFRESH_TOKEN] = session.refresh_token
        self.hass.config_entries.async_update_entry(self.entry, data=payload)

    def _schedule_stats_import(self) -> None:
        task = self._stats_task
        if task is not None and not task.done():
            return
        self._stats_task = self.hass.async_create_task(
            self.async_import_billed_history()
        )

    async def async_import_billed_history(self) -> None:
        """Write billed period totals into recorder long-term statistics."""
        async with self._stats_lock:
            usage = self.data
            if usage is None:
                return
            try:
                await self._async_import_billed_history(usage)
            except Exception:
                _LOGGER.exception("failed to import billed statistics")

    def _history_series(
        self, usage: UsageReadings
    ) -> list[tuple[str, tuple, str, str]]:
        series: list[tuple[str, tuple, str, str]] = []
        if usage.electricity is not None:
            periods = electricity_graph_periods(usage)
            series.append(
                (
                    SENSOR_KEY_ELECTRICITY,
                    periods if periods else usage.electricity.periods,
                    UNIT_KWH,
                    "energy",
                )
            )
        if usage.gas is not None:
            series.append(
                (
                    SENSOR_KEY_GAS,
                    usage.gas.periods,
                    usage.gas.unit,
                    "energy" if usage.gas.unit == UNIT_KWH else "volume",
                )
            )
        return series

    async def _async_import_billed_history(self, usage: UsageReadings) -> None:
        from homeassistant.components.recorder.models.statistics import (
            StatisticMeanType,
        )
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
        )

        for key, periods, unit, unit_class in self._history_series(usage):
            points = cumulative_points(periods)
            if not points:
                continue
            metadata = {
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
                "name": f"SP Group {key}",
                "source": DOMAIN,
                "statistic_id": external_statistic_id(usage.premise_id, key),
                "unit_class": unit_class,
                "unit_of_measurement": unit,
            }
            stats = [
                {
                    "start": point.start,
                    "state": point.cumulative,
                    "sum": point.cumulative,
                }
                for point in points
            ]
            async_add_external_statistics(self.hass, metadata, stats)
            price = self.electricity_price
            if key == SENSOR_KEY_ELECTRICITY and price is not None:
                cost_metadata = {
                    "has_sum": True,
                    "mean_type": StatisticMeanType.NONE,
                    "name": "SP Group electricity cost",
                    "source": DOMAIN,
                    "statistic_id": external_statistic_id(
                        usage.premise_id, STATISTIC_KEY_ELECTRICITY_COST
                    ),
                    "unit_class": None,
                    "unit_of_measurement": UNIT_SGD,
                }
                cost_stats = [
                    {
                        "start": point.start,
                        "state": point.cumulative,
                        "sum": point.cumulative,
                    }
                    for point in cost_points(points, price)
                ]
                async_add_external_statistics(self.hass, cost_metadata, cost_stats)
            self._imported_price = price
        bill_points = monthly_bill_points(usage.bills)
        if bill_points:
            metadata = {
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
                "name": "SP Group bill",
                "source": DOMAIN,
                "statistic_id": external_statistic_id(
                    usage.premise_id, SENSOR_KEY_LAST_BILL
                ),
                "unit_class": None,
                "unit_of_measurement": UNIT_SGD,
            }
            stats = [
                {
                    "start": point.start,
                    "state": point.amount,
                    "sum": point.amount,
                }
                for point in bill_points
            ]
            async_add_external_statistics(self.hass, metadata, stats)
