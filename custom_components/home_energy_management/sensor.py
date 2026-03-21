"""Sensor platform for Home Energy Management.

Exposes the optimizer's schedule, predictions, and log as HA sensor entities.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EnergyManagementCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities from a config entry."""
    coordinator: EnergyManagementCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    entities = [
        OptimizationStatusSensor(coordinator, entry),
        CurrentPriceSensor(coordinator, entry),
        NextActionSensor(coordinator, entry),
        PredictedConsumptionSensor(coordinator, entry),
        BatteryPlanSensor(coordinator, entry),
        ChargerPlanSensor(coordinator, entry),
        DailySavingsSensor(coordinator, entry),
        PredictionLogSensor(coordinator, entry),
    ]

    async_add_entities(entities)


class EnergyManagementSensor(CoordinatorEntity, SensorEntity):
    """Base class for energy management sensors."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnergyManagementCoordinator,
        entry: ConfigEntry,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Home Energy Management",
            "manufacturer": "Custom",
            "model": "Energy Optimizer",
        }


class OptimizationStatusSensor(EnergyManagementSensor):
    """Shows whether the optimizer is running correctly."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "optimization_status", "Optimization Status")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        return data.get("status", "unknown")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        schedule = data.get("schedule", {})
        return {
            "summary": schedule.get("summary", ""),
            "stats": schedule.get("stats", {}),
            "last_error": data.get("error"),
        }

    @property
    def icon(self) -> str:
        return "mdi:flash-auto"


class CurrentPriceSensor(EnergyManagementSensor):
    """Current Nordpool energy price."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "current_energy_price", "Current Energy Price")

    @property
    def native_value(self) -> float:
        data = self.coordinator.data or {}
        prices = data.get("prices", {})
        return prices.get("current", 0)

    @property
    def native_unit_of_measurement(self) -> str:
        data = self.coordinator.data or {}
        currency = data.get("prices", {}).get("currency", "SEK")
        return f"{currency}/kWh"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        prices = data.get("prices", {})
        return {
            "today_prices": prices.get("today", []),
            "tomorrow_prices": prices.get("tomorrow", []),
        }

    @property
    def icon(self) -> str:
        return "mdi:currency-usd"


class NextActionSensor(EnergyManagementSensor):
    """The action planned for the current hour."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "next_planned_action", "Next Planned Action")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        schedule = data.get("schedule", {})
        plan = schedule.get("hourly_plan", [])
        if plan:
            return plan[0].get("action", "unknown")
        return "none"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        schedule = data.get("schedule", {})
        plan = schedule.get("hourly_plan", [])
        if plan:
            return {
                "reason": plan[0].get("reason", ""),
                "price": plan[0].get("price", 0),
                "predicted_consumption": plan[0].get("predicted_consumption_kwh", 0),
            }
        return {}

    @property
    def icon(self) -> str:
        value = self.native_value
        if value == "charge_battery":
            return "mdi:battery-charging"
        if value == "discharge_battery":
            return "mdi:battery-arrow-down"
        if value == "start_ev_charge":
            return "mdi:ev-station"
        return "mdi:home-lightning-bolt"


class PredictedConsumptionSensor(EnergyManagementSensor):
    """Predicted consumption for the next hour."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "predicted_consumption", "Predicted Consumption")

    @property
    def native_value(self) -> float:
        data = self.coordinator.data or {}
        predictions = data.get("predicted_consumption", [])
        return predictions[0] if predictions else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        predictions = data.get("predicted_consumption", [])
        return {
            "next_24h": predictions[:24],
            "total_predicted_24h": round(sum(predictions[:24]), 2) if predictions else 0,
        }

    @property
    def icon(self) -> str:
        return "mdi:chart-line"


class BatteryPlanSensor(EnergyManagementSensor):
    """Battery charge/discharge schedule overview."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "battery_plan", "Battery Plan")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        schedule = data.get("schedule", {})
        plan = schedule.get("hourly_plan", [])
        charge_hours = [h["hour"] for h in plan if h.get("action") == "charge_battery"]
        discharge_hours = [h["hour"] for h in plan if h.get("action") == "discharge_battery"]
        return f"Charge:{len(charge_hours)} Discharge:{len(discharge_hours)}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        schedule = data.get("schedule", {})
        plan = schedule.get("hourly_plan", [])
        return {
            "charge_hours": [h["hour"] for h in plan if h.get("action") == "charge_battery"],
            "discharge_hours": [h["hour"] for h in plan if h.get("action") == "discharge_battery"],
            "full_plan": plan,
        }

    @property
    def icon(self) -> str:
        return "mdi:battery-clock"


class ChargerPlanSensor(EnergyManagementSensor):
    """EV charger schedule overview."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "charger_plan", "EV Charger Plan")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        sensor = data.get("sensor_data", {})
        if not sensor.get("ev_connected"):
            return "disconnected"

        schedule = data.get("schedule", {})
        plan = schedule.get("hourly_plan", [])
        charge_hours = [h["hour"] for h in plan if h.get("action") == "charge_battery"]
        return f"Charging in {len(charge_hours)} hours" if charge_hours else "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        sensor = data.get("sensor_data", {})
        return {
            "ev_connected": sensor.get("ev_connected", False),
            "ev_power_kw": sensor.get("ev_power", 0),
            "ev_status": sensor.get("ev_status", "unknown"),
        }

    @property
    def icon(self) -> str:
        if self.native_value == "disconnected":
            return "mdi:ev-plug-type2"
        return "mdi:ev-station"


class DailySavingsSensor(EnergyManagementSensor):
    """Estimated daily savings from optimisation."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "daily_savings", "Estimated Daily Savings")

    @property
    def native_value(self) -> float:
        data = self.coordinator.data or {}
        schedule = data.get("schedule", {})
        stats = schedule.get("stats", {})
        spread = stats.get("price_spread", 0)
        # Rough estimate: savings = spread × battery_capacity × cycles
        plan = schedule.get("hourly_plan", [])
        charge_hours = len([h for h in plan if h.get("action") == "charge_battery"])
        capacity = self.coordinator.optimizer.battery_capacity
        # Each charge hour moves ~2 kWh
        energy_shifted = min(charge_hours * 2, capacity)
        return round(energy_shifted * spread, 2)

    @property
    def native_unit_of_measurement(self) -> str:
        data = self.coordinator.data or {}
        currency = data.get("prices", {}).get("currency", "SEK")
        return currency

    @property
    def icon(self) -> str:
        return "mdi:piggy-bank"


class PredictionLogSensor(EnergyManagementSensor):
    """Exposes the internal prediction log for dashboard use."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "prediction_log", "Prediction Log")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        entries = data.get("log_entries", [])
        return f"{len(entries)} entries"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        entries = data.get("log_entries", [])
        accuracy = self.coordinator.prediction_logger.get_prediction_accuracy()
        return {
            "recent_entries": entries[-10:],
            "accuracy": accuracy,
        }

    @property
    def icon(self) -> str:
        return "mdi:notebook"
