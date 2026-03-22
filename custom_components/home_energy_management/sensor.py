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
        PredictionAccuracySensor(coordinator, entry),
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
        split = data.get("prediction_split", {})
        house = split.get("house_base", [])
        ev = split.get("ev_charging", [])
        return {
            "next_24h": predictions[:24],
            "house_base_24h": house[:24],
            "ev_charging_24h": ev[:24],
            "total_predicted_24h": round(sum(predictions[:24]), 2) if predictions else 0,
            "total_house_base_24h": round(sum(house[:24]), 2) if house else 0,
            "total_ev_charging_24h": round(sum(ev[:24]), 2) if ev else 0,
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
    """EV charger schedule overview with charging plan."""

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "charger_plan", "EV Charger Plan")

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        sensor = data.get("sensor_data", {})
        if not sensor.get("ev_connected"):
            return "disconnected"

        schedule = data.get("schedule", {})
        ev_plan = schedule.get("ev_charge_schedule", {})
        ev_schedule = ev_plan.get("schedule", [])
        charge_hours = [h for h in ev_schedule if h.get("charging")]
        if charge_hours:
            kwh_needed = ev_plan.get("total_kwh_needed", 0)
            return f"{len(charge_hours)}h charging ({kwh_needed:.0f} kWh needed)"
        return "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        sensor = data.get("sensor_data", {})
        schedule = data.get("schedule", {})
        ev_plan = schedule.get("ev_charge_schedule", {})
        ev_power_w = sensor.get("ev_power", 0)

        # Per-vehicle info
        vehicles = ev_plan.get("vehicles", [])
        chargers = sensor.get("ev_chargers", [])

        return {
            "ev_connected": sensor.get("ev_connected", False),
            "ev_power_kw": round(ev_power_w / 1000, 2) if ev_power_w else 0,
            "ev_power_w": round(ev_power_w, 0),
            "ev_chargers": chargers,
            "ev_status": sensor.get("ev_status", "unknown"),
            # EV charge schedule for dashboard graphs
            "ev_charge_schedule": ev_plan.get("schedule", []),
            "ev_kwh_needed": ev_plan.get("total_kwh_needed", 0),
            "ev_charging_power_kw": ev_plan.get("total_charging_power_kw", 0),
            "ev_hours_needed": ev_plan.get("hours_needed", 0),
            "ev_vehicles": vehicles,
            "start_hour": ev_plan.get("start_hour", 0),
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


class PredictionAccuracySensor(EnergyManagementSensor):
    """Tracks prediction error over time — MAE in kWh.

    The main state is the all-time MAE for total consumption.
    Attributes expose per-stream breakdown and rolling windows
    (last 24 h, last 7 d) so you can see if changes improve accuracy.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "kWh"

    def __init__(self, coordinator, entry):
        super().__init__(
            coordinator, entry, "prediction_accuracy", "Prediction Accuracy (MAE)",
        )

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        accuracy = data.get("accuracy", {})
        total = accuracy.get("total", {})
        all_time = total.get("all_time", {})
        return all_time.get("mae_kwh")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        accuracy = data.get("accuracy", {})

        attrs: dict[str, Any] = {}

        # Per-stream all-time MAE
        for stream in ("total", "house_base", "ev_charging"):
            s = accuracy.get(stream, {})
            at = s.get("all_time", {})
            attrs[f"{stream}_mae_kwh"] = at.get("mae_kwh")
            attrs[f"{stream}_mape_pct"] = at.get("mape_pct")
            attrs[f"{stream}_pairs"] = at.get("pairs", 0)

        # Rolling windows for total
        total = accuracy.get("total", {})
        last_24 = total.get("last_24h", {})
        last_7d = total.get("last_7d", {})
        attrs["total_mae_last_24h"] = last_24.get("mae_kwh")
        attrs["total_mape_last_24h"] = last_24.get("mape_pct")
        attrs["total_mae_last_7d"] = last_7d.get("mae_kwh")
        attrs["total_mape_last_7d"] = last_7d.get("mape_pct")

        # Recent individual errors for charting
        combined = accuracy.get("combined", {})
        at_combined = combined.get("all_time", {})
        attrs["recent_errors"] = at_combined.get("recent_errors", [])

        return attrs

    @property
    def icon(self) -> str:
        return "mdi:target"
