"""Data update coordinator for Home Energy Management.

Periodically reads sensor data, runs the optimizer and predictor,
and exposes the results to HA sensor entities.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DEFAULT_UPDATE_INTERVAL,
    INPUT_EASEE,
    INPUT_NORDPOOL,
    INPUT_SUNGROW,
    INPUT_SMART_METER,
    INPUT_WEATHER,
    MAPPING_INPUTS,
    MAPPING_OUTPUTS,
    MAPPING_PARAMETERS,
)
from .logger import PredictionLogger
from .optimizer import Optimizer
from .predictor import ConsumptionPredictor

_LOGGER = logging.getLogger(__name__)


class EnergyManagementCoordinator(DataUpdateCoordinator):
    """Coordinator that fetches data, runs prediction + optimisation."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        mapping: dict,
    ) -> None:
        """Initialise coordinator."""
        self.mapping = mapping
        self.inputs = mapping.get(MAPPING_INPUTS, {})
        self.outputs = mapping.get(MAPPING_OUTPUTS, {})
        self.params = mapping.get(MAPPING_PARAMETERS, {})

        interval_min = self.params.get("optimization_interval_minutes", DEFAULT_UPDATE_INTERVAL // 60)
        update_interval = timedelta(minutes=interval_min)

        super().__init__(
            hass,
            _LOGGER,
            name="home_energy_management",
            update_interval=update_interval,
        )

        self.predictor = ConsumptionPredictor(
            history_days=self.params.get("prediction_history_days", 14),
            recency_weight=self.params.get("prediction_recency_weight", 0.7),
        )
        self.optimizer = Optimizer(self.params, self.outputs)
        self.prediction_logger = PredictionLogger(
            max_entries=500,
            log_level=self.params.get("log_level", "info"),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from HA sensors, run prediction & optimization."""
        try:
            # 1. Read current sensor values
            sensor_data = self._read_sensors()

            # 2. Get price data
            prices = self._read_prices()

            # 3. Predict consumption for the planning horizon
            predicted_consumption = self.predictor.predict(
                hours_ahead=self.params.get("planning_horizon_hours", 24),
                current_load=sensor_data.get("house_load", 0),
            )

            # 4. Run optimizer to produce a schedule
            schedule = self.optimizer.optimize(
                prices=prices,
                predicted_consumption=predicted_consumption,
                battery_soc=sensor_data.get("battery_soc", 50),
                ev_connected=sensor_data.get("ev_connected", False),
            )

            # 5. Execute immediate actions
            await self._execute_actions(schedule)

            # 6. Log the decision
            self.prediction_logger.log_decision(
                prices=prices,
                predicted_consumption=predicted_consumption,
                schedule=schedule,
                sensor_data=sensor_data,
            )

            return {
                "sensor_data": sensor_data,
                "prices": prices,
                "predicted_consumption": predicted_consumption,
                "schedule": schedule,
                "log_entries": self.prediction_logger.get_recent_entries(20),
                "status": "ok",
            }

        except Exception as exc:
            _LOGGER.error("Energy management update failed: %s", exc)
            self.prediction_logger.log_error(str(exc))
            return {
                "status": "error",
                "error": str(exc),
                "log_entries": self.prediction_logger.get_recent_entries(20),
            }

    # ------------------------------------------------------------------
    # Sensor reading helpers
    # ------------------------------------------------------------------

    def _read_sensors(self) -> dict[str, Any]:
        """Read all mapped input sensors from HA state."""
        data: dict[str, Any] = {}

        # Sungrow
        sg = self.inputs.get(INPUT_SUNGROW, {})
        data["battery_soc"] = self._get_state_float(sg.get("battery_soc"))
        data["battery_power"] = self._get_state_float(sg.get("battery_power"))
        data["pv_power"] = self._get_state_float(sg.get("pv_power"))
        data["grid_import_power"] = self._get_state_float(sg.get("grid_import_power"))
        data["grid_export_power"] = self._get_state_float(sg.get("grid_export_power"))
        data["house_load"] = self._get_state_float(sg.get("house_load"))

        # Easee
        easee = self.inputs.get(INPUT_EASEE, {})
        data["ev_power"] = self._get_state_float(easee.get("power"))
        data["ev_status"] = self._get_state_str(easee.get("status"))
        data["ev_connected"] = self._get_state_str(
            easee.get("cable_connected")
        ) in ("on", "true", "True")

        # Smart meter
        sm = self.inputs.get(INPUT_SMART_METER, {})
        data["total_import"] = self._get_state_float(sm.get("total_import"))
        data["total_export"] = self._get_state_float(sm.get("total_export"))

        # Weather (optional)
        weather = self.inputs.get(INPUT_WEATHER, {})
        data["temperature"] = self._get_state_float(weather.get("temperature"))

        return data

    def _read_prices(self) -> dict[str, Any]:
        """Read Nordpool price data from HA."""
        np_cfg = self.inputs.get(INPUT_NORDPOOL, {})
        entity_id = np_cfg.get("current_price")
        if not entity_id:
            return {"current": 0, "today": [], "tomorrow": []}

        state = self.hass.states.get(entity_id)
        if state is None:
            _LOGGER.warning("Nordpool entity %s not found", entity_id)
            return {"current": 0, "today": [], "tomorrow": []}

        current = _safe_float(state.state, 0)
        today_attr = np_cfg.get("today_prices_attribute", "today")
        tomorrow_attr = np_cfg.get("tomorrow_prices_attribute", "tomorrow")

        today_prices = state.attributes.get(today_attr, [])
        tomorrow_prices = state.attributes.get(tomorrow_attr, [])

        return {
            "current": current,
            "today": [_safe_float(p, current) for p in today_prices] if today_prices else [],
            "tomorrow": [_safe_float(p, current) for p in tomorrow_prices] if tomorrow_prices else [],
            "currency": np_cfg.get("currency", "SEK"),
        }

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def _execute_actions(self, schedule: dict) -> None:
        """Execute the immediate actions from the optimizer schedule."""
        actions = schedule.get("immediate_actions", [])
        for action in actions:
            service = action.get("service")
            entity_id = action.get("entity_id")
            service_data = action.get("data", {})

            if not service or not entity_id:
                continue

            domain, service_name = service.split(".", 1) if "." in service else (service, "")
            if not service_name:
                continue

            _LOGGER.info(
                "Executing action: %s on %s with data %s",
                service,
                entity_id,
                service_data,
            )

            try:
                await self.hass.services.async_call(
                    domain,
                    service_name,
                    {"entity_id": entity_id, **service_data},
                    blocking=True,
                )
            except Exception as exc:
                _LOGGER.error("Failed to execute %s: %s", service, exc)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _get_state_float(self, entity_id: str | None) -> float:
        """Get a numeric state value from HA."""
        if not entity_id:
            return 0.0
        state = self.hass.states.get(entity_id)
        if state is None:
            return 0.0
        return _safe_float(state.state, 0.0)

    def _get_state_str(self, entity_id: str | None) -> str:
        """Get a string state value from HA."""
        if not entity_id:
            return ""
        state = self.hass.states.get(entity_id)
        return state.state if state else ""


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Safely convert a value to float."""
    try:
        return float(value)
    except (ValueError, TypeError):
        return default
