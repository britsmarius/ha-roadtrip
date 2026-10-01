"""Sensors for Road Trip vehicles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfLength
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import RoadTripConfigEntry
from .const import DOMAIN, NAME, SIGNAL_UPDATED

ECONOMY_UNIT = "L/100 km"


def _ts(ms: int | None) -> datetime | None:
    return dt_util.utc_from_timestamp(ms / 1000) if ms else None


@dataclass(frozen=True, kw_only=True)
class RoadTripSensorDescription(SensorEntityDescription):
    """Describes a per-vehicle sensor."""

    value_fn: Callable[[dict], Any]
    unit_fn: Callable[[dict], str | None] | None = None
    attrs_fn: Callable[[dict], dict] | None = None


VEHICLE_SENSORS: tuple[RoadTripSensorDescription, ...] = (
    RoadTripSensorDescription(
        key="odometer",
        name="Odometer (last fill-up)",
        icon="mdi:counter",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=0,
        value_fn=lambda v: (v.get("last_fill") or {}).get("odometer"),
    ),
    RoadTripSensorDescription(
        key="last_fill_up",
        name="Last fill-up",
        icon="mdi:gas-station",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda v: _ts((v.get("last_fill") or {}).get("timestamp")),
        attrs_fn=lambda v: {
            **{k: val for k, val in (v.get("last_fill") or {}).items() if k not in ("timestamp", "date")},
            "recent": v.get("recent", []),
        },
    ),
    RoadTripSensorDescription(
        key="economy_recent",
        name="Fuel economy (last 5 tanks)",
        icon="mdi:gauge",
        native_unit_of_measurement=ECONOMY_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda v: v.get("economy_recent"),
        attrs_fn=lambda v: {"best_tank": v.get("best_tank"), "worst_tank": v.get("worst_tank")},
    ),
    RoadTripSensorDescription(
        key="economy_lifetime",
        name="Fuel economy (lifetime)",
        icon="mdi:gauge-full",
        native_unit_of_measurement=ECONOMY_UNIT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda v: v.get("economy_lifetime"),
        attrs_fn=lambda v: {
            "distance_logged": v.get("distance_logged"),
            "total_volume": v.get("total_volume"),
            "tracked_since": v.get("first_date"),
        },
    ),
    RoadTripSensorDescription(
        key="fuel_price",
        name="Fuel price (last fill-up)",
        icon="mdi:cash",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda v: (v.get("last_fill") or {}).get("price"),
        unit_fn=lambda v: f"{v.get('currency') or ''}/L".lstrip("/"),
    ),
    RoadTripSensorDescription(
        key="spend_12m",
        name="Fuel spend (12 months)",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda v: v.get("spend_12m"),
        unit_fn=lambda v: v.get("currency") or None,
    ),
    RoadTripSensorDescription(
        key="distance_12m",
        name="Distance (12 months)",
        icon="mdi:map-marker-distance",
        device_class=SensorDeviceClass.DISTANCE,
        native_unit_of_measurement=UnitOfLength.KILOMETERS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda v: v.get("distance_12m"),
    ),
    RoadTripSensorDescription(
        key="cost_per_km_12m",
        name="Fuel cost per km (12 months)",
        icon="mdi:cash-multiple",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda v: v.get("cost_per_km_12m"),
        unit_fn=lambda v: f"{v.get('currency') or ''}/km".lstrip("/"),
        attrs_fn=lambda v: {"lifetime": v.get("cost_per_km_lifetime")},
    ),
    RoadTripSensorDescription(
        key="total_spend",
        name="Fuel spend (all time)",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=0,
        value_fn=lambda v: v.get("total_spend"),
        unit_fn=lambda v: v.get("currency") or None,
        attrs_fn=lambda v: {"yearly": v.get("yearly", [])},
    ),
    RoadTripSensorDescription(
        key="fillups",
        name="Fill-ups logged",
        icon="mdi:format-list-numbered",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda v: v.get("fillups"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RoadTripConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create sensors for every vehicle; add new vehicles as they appear."""
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        dataset = entry.runtime_data.dataset or {"vehicles": []}
        new = []
        for v in dataset["vehicles"]:
            if v["id"] in known:
                continue
            known.add(v["id"])
            new.extend(RoadTripVehicleSensor(entry, v, d) for d in VEHICLE_SENSORS)
        if new:
            async_add_entities(new)

    async_add_entities([RoadTripImportSensor(entry)])
    _add_new()
    entry.async_on_unload(
        async_dispatcher_connect(hass, SIGNAL_UPDATED.format(entry_id=entry.entry_id), _add_new)
    )


class _RoadTripEntity(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: RoadTripConfigEntry) -> None:
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATED.format(entry_id=self._entry.entry_id), self.async_write_ha_state
            )
        )


class RoadTripVehicleSensor(_RoadTripEntity):
    """One statistic for one vehicle."""

    entity_description: RoadTripSensorDescription

    def __init__(self, entry: RoadTripConfigEntry, vehicle: dict, description: RoadTripSensorDescription) -> None:
        super().__init__(entry)
        self.entity_description = description
        self._vehicle_id = vehicle["id"]
        self._attr_unique_id = f"{vehicle['id']}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, vehicle["id"])},
            name=vehicle["name"],
            manufacturer=NAME,
            model="Fuel log",
            entry_type=DeviceEntryType.SERVICE,
        )
        if description.unit_fn:
            self._attr_native_unit_of_measurement = description.unit_fn(vehicle)

    @property
    def _vehicle(self) -> dict | None:
        dataset = self._entry.runtime_data.dataset
        if not dataset:
            return None
        return next((v for v in dataset["vehicles"] if v["id"] == self._vehicle_id), None)

    @property
    def available(self) -> bool:
        return self._vehicle is not None

    @property
    def native_value(self) -> Any:
        v = self._vehicle
        return self.entity_description.value_fn(v) if v else None

    @property
    def extra_state_attributes(self) -> dict | None:
        v = self._vehicle
        if v is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(v)


class RoadTripImportSensor(_RoadTripEntity):
    """When data last arrived, plus fleet totals."""

    _attr_name = "Last import"
    _attr_icon = "mdi:cloud-download"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: RoadTripConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_last_import"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer=NAME,
            model="Backup import",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> datetime | None:
        return self._entry.runtime_data.last_import

    @property
    def extra_state_attributes(self) -> dict:
        data = self._entry.runtime_data
        ds = data.dataset or {}
        return {
            "source": data.last_source,
            "vehicles": [v["name"] for v in ds.get("vehicles", [])],
            **(ds.get("fleet") or {}),
        }
