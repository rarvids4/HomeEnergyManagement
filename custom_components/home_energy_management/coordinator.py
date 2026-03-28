"""Data update coordinator for Home Energy Management.

Periodically reads sensor data, runs the optimizer and predictor,
and exposes the results to HA sensor entities.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DEFAULT_ENTRIES_PER_HOUR,
    DEFAULT_UPDATE_INTERVAL,
    INPUT_EASEE,
    INPUT_EV_CHARGERS,
    INPUT_NORDPOOL,
    INPUT_SUNGROW,
    INPUT_SUNGROW_2,
    INPUT_SMART_METER,
    INPUT_WEATHER,
    MAPPING_INPUTS,
    MAPPING_OUTPUTS,
    MAPPING_PARAMETERS,
)
from .logger import PredictionLogger
from .optimizer import Optimizer
from .predictor import ConsumptionPredictor, STREAM_EV, STREAM_HOUSE

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

        # --- Manual override tracking ---
        # When a user manually turns off a charger switch, we respect
        # that choice for one full optimisation cycle.  Maps entity_id
        # to the datetime of the manual override.
        self._manual_overrides: dict[str, Any] = {}

        # --- Auto-replan: re-run optimisation when UI settings change ---
        self._unsub_state_listeners: list = []
        watched = self._collect_watched_entities()
        if watched:
            _LOGGER.info("Auto-replan: watching %s", watched)
            unsub = async_track_state_change_event(
                hass, watched, self._on_setting_changed,
            )
            self._unsub_state_listeners.append(unsub)

    # ------------------------------------------------------------------
    # Auto-replan helpers
    # ------------------------------------------------------------------

    def _collect_watched_entities(self) -> list[str]:
        """Collect input-helper entity IDs that should trigger an immediate replan."""
        entities: list[str] = []

        # Global optimisation settings
        opt_days_entity = self.params.get("optimization_days_entity")
        if opt_days_entity:
            entities.append(opt_days_entity)

        # Grid tariff input helpers
        for key in ("grid_tariff_peak_entity", "grid_tariff_offpeak_entity"):
            entity = self.params.get(key)
            if entity:
                entities.append(entity)

        # Per-vehicle settings
        ev_chargers_cfg = self.inputs.get(INPUT_EV_CHARGERS, [])
        for charger in (ev_chargers_cfg if isinstance(ev_chargers_cfg, list) else []):
            for key in (
                "departure_time_entity",
                "min_departure_soc_entity",
                "min_charge_level_entity",
            ):
                entity = charger.get(key)
                if entity:
                    entities.append(entity)

        return entities

    @callback
    def _on_setting_changed(self, event: Event) -> None:
        """Trigger an immediate replan when a watched input helper changes."""
        entity_id = event.data.get("entity_id", "")
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        old_val = old_state.state if old_state else "?"
        new_val = new_state.state if new_state else "?"
        _LOGGER.info(
            "Setting changed: %s (%s → %s) — triggering replan",
            entity_id, old_val, new_val,
        )
        # async_request_refresh is a coroutine in some HA versions;
        # schedule it as a task so it runs in the event loop.
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from HA sensors, run prediction & optimization."""
        try:
            # 1. Read current sensor values
            sensor_data = self._read_sensors()

            # 2. Record observations — split house base load vs EV
            self._record_observations(sensor_data)

            # 3. Get price data
            prices = self._read_prices()

            # 4. Predict consumption (split by stream)
            horizon = self.params.get("planning_horizon_hours", 24)
            prediction_split = self.predictor.predict_split(
                hours_ahead=horizon,
                current_house_load=sensor_data.get("house_load", 0),
            )
            predicted_consumption = prediction_split["total"]

            # 5. Read runtime-editable parameters from input helpers
            opt_days_entity = self.params.get("optimization_days_entity")
            if opt_days_entity:
                opt_days_val = self._get_state_float(opt_days_entity)
                if opt_days_val >= 1:
                    self.optimizer.ev_optimization_window = int(opt_days_val)

            # Grid tariff overrides (input_number helpers)
            tariff_peak_entity = self.params.get("grid_tariff_peak_entity")
            if tariff_peak_entity:
                val = self._get_state_float(tariff_peak_entity)
                if val > 0:
                    self.optimizer.grid_tariff_peak = val

            tariff_offpeak_entity = self.params.get("grid_tariff_offpeak_entity")
            if tariff_offpeak_entity:
                val = self._get_state_float(tariff_offpeak_entity)
                if val > 0:
                    self.optimizer.grid_tariff_offpeak = val

            # 6. Run optimizer to produce a schedule
            schedule = self.optimizer.optimize(
                prices=prices,
                predicted_consumption=predicted_consumption,
                battery_soc=sensor_data.get("battery_soc", 50),
                ev_connected=sensor_data.get("ev_connected", False),
                grid_export_power=sensor_data.get("grid_export_power", 0.0),
                ev_vehicles=sensor_data.get("ev_chargers", []),
            )

            # 7. Execute immediate actions
            await self._execute_actions(schedule)

            # 8. Compute actual consumption in same kWh unit as predictions
            interval_h = self.update_interval.total_seconds() / 3600.0
            actual_consumption_kwh = round(
                (sensor_data.get("house_load", 0) / 1000.0) * interval_h, 3
            )

            # 9. Log actual vs previous prediction (before overwriting)
            self._log_actuals(sensor_data)

            # 10. Log the new decision (stores predictions for next comparison)
            self.prediction_logger.log_decision(
                prices=prices,
                predicted_consumption=predicted_consumption,
                schedule=schedule,
                sensor_data=sensor_data,
                prediction_split=prediction_split,
            )

            return {
                "sensor_data": sensor_data,
                "prices": prices,
                "predicted_consumption": predicted_consumption,
                "prediction_split": prediction_split,
                "actual_consumption_kwh": actual_consumption_kwh,
                "schedule": schedule,
                "log_entries": self.prediction_logger.get_recent_entries(20),
                "accuracy": self.prediction_logger.get_accuracy_summary(),
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
    # Accuracy tracking — log actual vs previous prediction
    # ------------------------------------------------------------------

    def _log_actuals(self, sensor_data: dict[str, Any]) -> None:
        """Compare the *previous* cycle's prediction against what actually happened.

        Called at the start of each new cycle, before the new prediction
        overwrites the stored values.
        """
        total_house = sensor_data.get("house_load", 0)   # W
        ev_power = sensor_data.get("ev_power", 0)         # W
        house_base_w = max(total_house - ev_power, 0)

        interval_h = self.update_interval.total_seconds() / 3600.0
        total_kwh = (total_house / 1000.0) * interval_h
        house_kwh = (house_base_w / 1000.0) * interval_h
        ev_kwh = (ev_power / 1000.0) * interval_h

        prices = self._read_prices()

        self.prediction_logger.log_actual(
            actual_consumption_kwh=total_kwh,
            actual_price=prices.get("current", 0),
            actual_soc=sensor_data.get("battery_soc", 0),
            actual_house_kwh=house_kwh,
            actual_ev_kwh=ev_kwh,
        )

    # ------------------------------------------------------------------
    # Observation recording
    # ------------------------------------------------------------------

    def _record_observations(self, sensor_data: dict[str, Any]) -> None:
        """Feed the predictor with separated house-base and EV load.

        The Sungrow *house_load* sensor typically includes everything
        behind the meter — including the EV charger.  We subtract the
        current EV power so the house-base stream only captures the
        "normal" household pattern.
        """
        from datetime import datetime

        now = datetime.now()
        total_house = sensor_data.get("house_load", 0)   # W
        ev_power = sensor_data.get("ev_power", 0)         # W

        # House base = total minus EV (clamp to zero)
        house_base_w = max(total_house - ev_power, 0)

        # Convert W → kWh (one observation per coordinator interval)
        interval_h = self.update_interval.total_seconds() / 3600.0
        house_kwh = (house_base_w / 1000.0) * interval_h
        ev_kwh = (ev_power / 1000.0) * interval_h

        self.predictor.add_observation(now, house_kwh, stream=STREAM_HOUSE)
        if ev_kwh > 0:
            self.predictor.add_observation(now, ev_kwh, stream=STREAM_EV)

    # ------------------------------------------------------------------
    # Sensor reading helpers
    # ------------------------------------------------------------------

    def _read_sensors(self) -> dict[str, Any]:
        """Read all mapped input sensors from HA state."""
        data: dict[str, Any] = {}

        # Sungrow (primary)
        sg = self.inputs.get(INPUT_SUNGROW, {})
        data["battery_soc"] = self._get_state_float(sg.get("battery_soc"))
        data["battery_power"] = self._get_state_float(sg.get("battery_power"))
        data["pv_power"] = self._get_state_float(sg.get("pv_power"))
        data["grid_import_power"] = self._get_state_float(sg.get("grid_import_power"))
        data["grid_export_power"] = self._get_state_float(sg.get("grid_export_power"))
        data["house_load"] = self._get_state_float(sg.get("house_load"))

        # Sungrow 2 (optional — add to totals if available)
        sg2 = self.inputs.get(INPUT_SUNGROW_2, {})
        if sg2:
            sg2_pv = self._get_state_float(sg2.get("pv_power"))
            sg2_load = self._get_state_float(sg2.get("house_load"))
            sg2_batt = self._get_state_float(sg2.get("battery_power"))
            # Only add if the sensor returned a real value (not 0 from "unknown")
            if sg2_pv > 0:
                data["pv_power"] += sg2_pv
            if sg2_load > 0:
                data["house_load"] += sg2_load
            data["battery_power_2"] = sg2_batt
            data["battery_soc_2"] = self._get_state_float(sg2.get("battery_soc"))

        # EV Chargers — read from list-based config (new) or legacy "easee" dict
        ev_chargers_cfg = self.inputs.get(INPUT_EV_CHARGERS, [])
        # Backward compat: if old "easee" key exists and ev_chargers is empty
        if not ev_chargers_cfg:
            legacy = self.inputs.get(INPUT_EASEE, {})
            if legacy:
                ev_chargers_cfg = [legacy]

        total_ev_power = 0.0
        ev_any_connected = False
        ev_details = []
        for charger in (ev_chargers_cfg if isinstance(ev_chargers_cfg, list) else []):
            name = charger.get("name", "ev")
            power_raw = self._get_state_float(charger.get("power"))
            power_unit = charger.get("power_unit", "W")
            # Convert kW → W if needed
            power_w = power_raw * 1000 if power_unit == "kW" else power_raw
            status = self._get_state_str(charger.get("status"))
            switch_state = self._get_state_str(charger.get("charger_switch"))
            is_connected = status in ("charging", "awaiting_start", "connected")
            if switch_state == "on":
                is_connected = True

            # Vehicle battery sensors (optional — from Volvo Cars API etc.)
            vehicle_soc = self._get_state_float(charger.get("vehicle_soc"))
            vehicle_capacity = self._get_state_float(charger.get("vehicle_capacity_kwh"))
            vehicle_target_soc = self._get_state_float(charger.get("vehicle_target_soc"))
            vehicle_charging_power = self._get_state_float(charger.get("vehicle_charging_power"))

            # Fallbacks for vehicles whose APIs report "unknown" when sleeping
            if vehicle_capacity <= 0:
                vehicle_capacity = float(charger.get("vehicle_capacity_kwh_fallback", 0))
            if vehicle_charging_power <= 0:
                vehicle_charging_power = float(charger.get("vehicle_charging_power_fallback", 0))

            total_ev_power += power_w
            if is_connected:
                ev_any_connected = True

            # Per-vehicle departure settings:
            # Prefer HA input helper entities (UI-editable) over static YAML values.
            dep_time_entity = charger.get("departure_time_entity")
            dep_time_value = self._get_state_str(dep_time_entity) if dep_time_entity else ""
            # input_datetime returns "HH:MM:SS"; strip seconds for "HH:MM"
            if dep_time_value and ":" in dep_time_value:
                dep_time_value = ":".join(dep_time_value.split(":")[:2])
            if not dep_time_value:
                dep_time_value = charger.get("departure_time", "")

            dep_soc_entity = charger.get("min_departure_soc_entity")
            dep_soc_value = self._get_state_float(dep_soc_entity) if dep_soc_entity else 0.0
            if dep_soc_value <= 0:
                dep_soc_value = float(charger.get("min_departure_soc", 0))

            # Per-vehicle min charge level (SoC floor):
            # When optimization_days=2, the car won't be drained below this.
            mcl_entity = charger.get("min_charge_level_entity")
            mcl_value = self._get_state_float(mcl_entity) if mcl_entity else 0.0
            if mcl_value <= 0:
                mcl_value = float(charger.get("min_charge_level", 0))

            ev_details.append({
                "name": name,
                "power_w": power_w,
                "status": status,
                "connected": is_connected,
                "vehicle_soc": vehicle_soc,
                "vehicle_capacity_kwh": vehicle_capacity,
                "vehicle_target_soc": vehicle_target_soc if vehicle_target_soc > 0 else 100.0,
                "vehicle_charging_power_w": vehicle_charging_power,
                "departure_time": dep_time_value,
                "min_departure_soc": int(dep_soc_value),
                "min_charge_level": int(mcl_value),
            })

        data["ev_power"] = total_ev_power
        data["ev_connected"] = ev_any_connected
        data["ev_chargers"] = ev_details

        # Smart meter
        sm = self.inputs.get(INPUT_SMART_METER, {})
        data["total_import"] = self._get_state_float(sm.get("total_import"))
        data["total_export"] = self._get_state_float(sm.get("total_export"))

        # Weather (optional)
        weather = self.inputs.get(INPUT_WEATHER, {})
        weather_entity = weather.get("entity")
        temp_entity = weather.get("temperature")

        if temp_entity:
            data["temperature"] = self._get_state_float(temp_entity)
        elif weather_entity:
            # Read temperature from weather entity's attributes
            state = self.hass.states.get(weather_entity)
            if state:
                data["temperature"] = _safe_float(
                    state.attributes.get("temperature", 0), 0.0
                )
        else:
            data["temperature"] = 0.0

        return data

    def _read_prices(self) -> dict[str, Any]:
        """Read Nordpool price data from HA.

        Handles both hourly (24 entries/day) and sub-hourly
        (e.g. 96 entries/day for 15-min intervals) price data.
        Aggregates sub-hourly prices to hourly averages for the
        optimizer and predictor.
        """
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
        entries_per_hour = np_cfg.get("entries_per_hour", DEFAULT_ENTRIES_PER_HOUR)

        raw_today = state.attributes.get(today_attr, [])
        raw_tomorrow = state.attributes.get(tomorrow_attr, [])

        # Convert to float lists
        today_floats = [_safe_float(p, current) for p in raw_today] if raw_today else []
        tomorrow_floats = [_safe_float(p, current) for p in raw_tomorrow] if raw_tomorrow else []

        # Aggregate to hourly if sub-hourly
        today_prices = _aggregate_to_hourly(today_floats, entries_per_hour)
        tomorrow_prices = _aggregate_to_hourly(tomorrow_floats, entries_per_hour)

        return {
            "current": current,
            "today": today_prices,
            "tomorrow": tomorrow_prices,
            "currency": np_cfg.get("currency", "SEK"),
        }

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def _execute_actions(self, schedule: dict) -> None:
        """Execute the immediate actions from the optimizer schedule."""
        from datetime import datetime, timedelta

        actions = schedule.get("immediate_actions", [])
        cooldown = timedelta(minutes=max(
            self.params.get("optimization_interval_minutes", 15) * 2, 30
        ))

        for action in actions:
            service = action.get("service")
            entity_id = action.get("entity_id")
            service_data = action.get("data", {})

            if not service or not entity_id:
                continue

            domain, service_name = service.split(".", 1) if "." in service else (service, "")
            if not service_name:
                continue

            # --- Manual override detection ---
            # If this is a switch.turn_on and the switch is currently
            # OFF because a *user* manually turned it off, respect
            # their choice for a cooldown period (2× optimisation
            # interval, minimum 30 min).
            is_switch_on = (domain == "switch" and service_name == "turn_on")
            if is_switch_on and entity_id.startswith("switch."):
                state = self.hass.states.get(entity_id)
                if state and state.state == "off":
                    ctx = state.context
                    if ctx and ctx.user_id:
                        # Last change was by a human user
                        override_ts = state.last_changed
                        if override_ts and (
                            datetime.now(override_ts.tzinfo) - override_ts
                        ) < cooldown:
                            _LOGGER.info(
                                "Skipping %s on %s — user manually "
                                "turned it off %s ago (cooldown %s)",
                                service, entity_id,
                                datetime.now(override_ts.tzinfo) - override_ts,
                                cooldown,
                            )
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


def _aggregate_to_hourly(prices: list[float], entries_per_hour: int) -> list[float]:
    """Aggregate sub-hourly prices to hourly averages.

    If entries_per_hour is 1, returns the list unchanged.
    If entries_per_hour is 4 (15-min), groups every 4 entries
    and returns their average.
    """
    if entries_per_hour <= 1 or not prices:
        return prices

    hourly = []
    for i in range(0, len(prices), entries_per_hour):
        chunk = prices[i : i + entries_per_hour]
        if chunk:
            hourly.append(sum(chunk) / len(chunk))
    return hourly
