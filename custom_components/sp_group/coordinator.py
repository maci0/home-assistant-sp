"""Poll SP Group usage on a fixed interval."""

# mypy: ignore-errors

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import datetime
from typing import TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import AuthError, SpGroupClient, UsageError, session_entry_data
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
    parse_electricity_price,
    translated_error,
)
from .history import (
    HasStart,
    cost_points,
    cumulative_points,
    external_statistic_id,
    monthly_bill_points,
    unimported,
)
from .mapper import (
    SensorSpec,
    SensorSpecCache,
    electricity_graph_periods,
    extra_attributes,
)
from .models import UsageReadings

_LOGGER = logging.getLogger(__name__)

_PointT = TypeVar("_PointT", bound=HasStart)


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
        self._stats_lock = asyncio.Lock()
        self._stats_task: asyncio.Task[None] | None = None
        self._stats_stopped = False
        self._imported_price: float | None = None
        self._imported_premise: str | None = None
        self._imported_points: dict[str, datetime] = {}
        self._spec_cache = SensorSpecCache()

    @property
    def sensor_specs(self) -> tuple[SensorSpec, ...]:
        """The current sensor specs, built once per poll for every entity."""
        return self._spec_cache.specs(self.data, self.now)

    @property
    def now(self) -> datetime:
        """The clock reading the poll ran at, shared by specs and attributes."""
        return self.client.clock.now()

    def extra_attributes(self, key: str) -> dict[str, object]:
        usage = self.data
        return {} if usage is None else extra_attributes(usage, key, self.now)

    @property
    def electricity_price(self) -> float | None:
        """Fixed SGD/kWh price from the entry options, or None when unset."""
        raw = self.entry.options.get(CONF_ELECTRICITY_PRICE)
        try:
            return parse_electricity_price(raw)
        except ValueError as exc:
            # Options come from the flow as floats, but .storage can be edited.
            _LOGGER.warning("%s; no electricity cost series will be written", exc)
            return None

    async def async_options_updated(self) -> None:
        """Re-import the cost series when the configured price changed."""
        if self.electricity_price != self._imported_price:
            await self.async_import_billed_history()

    async def async_stop_stats_import(self) -> None:
        """Cancel an import still writing statistics when the entry goes away."""
        self._stats_stopped = True
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
        payload = session_entry_data(session, data[CONF_USERNAME], data[CONF_PASSWORD])
        self.hass.config_entries.async_update_entry(self.entry, data=payload)

    def _schedule_stats_import(self) -> None:
        # A poll still in flight when the entry unloads finishes after
        # async_stop_stats_import, so the stop flag gates the reschedule too.
        if self._stats_stopped:
            return
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

    def _imported_through(self, premise_id: str) -> dict[str, datetime]:
        """The newest start already written per statistic id for this premise.

        Dropped when the premise changes so a re-auth against another account
        cannot leave the previous one's keys behind.
        """
        if self._imported_premise != premise_id:
            self._imported_premise = premise_id
            self._imported_points = {}
        return self._imported_points

    def _add_external_statistics(
        self,
        premise_id: str,
        statistic_id: str,
        metadata: dict[str, object],
        points: Sequence[_PointT],
        value: Callable[[_PointT], float],
    ) -> None:
        """Write the points the recorder has not seen yet, then remember the last."""
        from homeassistant.components.recorder.statistics import (
            async_add_external_statistics,
        )

        imported = self._imported_through(premise_id)
        fresh = unimported(points, imported.get(statistic_id))
        if not fresh:
            return
        stats = [
            {"start": point.start, "state": value(point), "sum": value(point)}
            for point in fresh
        ]
        async_add_external_statistics(self.hass, metadata, stats)
        imported[statistic_id] = fresh[-1].start

    async def _async_import_billed_history(self, usage: UsageReadings) -> None:
        from homeassistant.components.recorder.models.statistics import (
            StatisticMeanType,
        )

        def metadata(
            key: str, name: str, unit: str, unit_class: str | None
        ) -> dict[str, object]:
            return {
                "has_sum": True,
                "mean_type": StatisticMeanType.NONE,
                "name": name,
                "source": DOMAIN,
                "statistic_id": external_statistic_id(usage.premise_id, key),
                "unit_class": unit_class,
                "unit_of_measurement": unit,
            }

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

        price = self.electricity_price
        for key, periods, unit, unit_class in series:
            points = cumulative_points(periods)
            if not points:
                continue
            self._add_external_statistics(
                usage.premise_id,
                external_statistic_id(usage.premise_id, key),
                metadata(key, f"SP Group {key}", unit, unit_class),
                points,
                lambda point: point.cumulative,
            )
            if key == SENSOR_KEY_ELECTRICITY and price is not None:
                self._add_external_statistics(
                    usage.premise_id,
                    external_statistic_id(
                        usage.premise_id, STATISTIC_KEY_ELECTRICITY_COST
                    ),
                    metadata(
                        STATISTIC_KEY_ELECTRICITY_COST,
                        "SP Group electricity cost",
                        UNIT_SGD,
                        None,
                    ),
                    cost_points(points, price),
                    lambda point: point.cumulative,
                )
        bill_points = monthly_bill_points(usage.bills)
        if bill_points:
            self._add_external_statistics(
                usage.premise_id,
                external_statistic_id(usage.premise_id, SENSOR_KEY_LAST_BILL),
                metadata(SENSOR_KEY_LAST_BILL, "SP Group bill", UNIT_SGD, None),
                bill_points,
                lambda point: point.amount,
            )
        self._imported_price = self.electricity_price
