"""Energy, water, gas, and account sensors."""

# mypy: ignore-errors

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, DOMAIN
from .coordinator import SpGroupCoordinator
from .mapper import SensorSpec, extra_attributes, sensors_from_usage

PARALLEL_UPDATES = 0

_UNITS = {
    "kWh": UnitOfEnergy.KILO_WATT_HOUR,
    "m³": UnitOfVolume.CUBIC_METERS,
    "°C": UnitOfTemperature.CELSIUS,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: SpGroupCoordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _async_add_new() -> None:
        specs = sensors_from_usage(coordinator.data)
        fresh = [spec for spec in specs if spec.key not in known]
        if not fresh:
            return
        known.update(spec.key for spec in fresh)
        async_add_entities([SpGroupSensor(coordinator, spec) for spec in fresh])

    _async_add_new()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))


class SpGroupSensor(CoordinatorEntity[SpGroupCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_attribution = ATTRIBUTION

    def __init__(self, coordinator: SpGroupCoordinator, spec: SensorSpec) -> None:
        super().__init__(coordinator)
        self._key = spec.key
        usage = coordinator.data
        premise_id = usage.premise_id if usage else "unknown"
        address = usage.premise.address if usage else None
        premise_type = usage.premise.premise_type if usage else None
        self._attr_unique_id = f"{premise_id}_{spec.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, premise_id)},
            name=address or "SP Group utilities",
            manufacturer="SP Group",
            model=premise_type or "e-account",
        )
        self._apply_spec(spec)

    def _apply_spec(self, spec: SensorSpec) -> None:
        """Take the description and the current value from the mapper spec."""
        self._spec = spec
        self._attr_translation_key = spec.translation_key
        if spec.name:
            self._attr_name = spec.name
        unit = spec.unit_of_measurement
        self.entity_description = SensorEntityDescription(
            key=spec.key,
            device_class=(
                SensorDeviceClass(spec.device_class) if spec.device_class else None
            ),
            state_class=(
                SensorStateClass(spec.state_class) if spec.state_class else None
            ),
            native_unit_of_measurement=_UNITS.get(unit, unit) if unit else None,
            entity_category=(
                EntityCategory(spec.entity_category) if spec.entity_category else None
            ),
            suggested_display_precision=spec.suggested_display_precision,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        spec = next(
            (
                item
                for item in sensors_from_usage(self.coordinator.data)
                if item.key == self._key
            ),
            None,
        )
        if spec is None:
            self._spec = None
        else:
            self._apply_spec(spec)
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float | str | None:
        return self._spec.native_value if self._spec else None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        usage = self.coordinator.data
        if usage is None:
            return {}
        return extra_attributes(usage, self._key)
