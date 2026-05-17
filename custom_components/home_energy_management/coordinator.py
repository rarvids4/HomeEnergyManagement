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
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DEFAULT_CLOUD_OPACITY,
    DEFAULT_ENTRIES_PER_HOUR,
    DEFAULT_FAST_EV_UPDATE_INTERVAL,
    DEFAULT_PV_PEAK_POWER_KW,
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
    OUTPUT_EV_CHARGERS,
)
from .logger import PredictionLogger
from .optimizer import Optimizer
from .predictor import ConsumptionPredictor, STREAM_EV, STREAM_HOUSE
from .solar_predictor import SolarPredictor
from .surplus_controller import SurplusController

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

        # --- Surplus EV charging controller ---
        # Standalone state machine that owns surplus EV charging end-to-end.
        # See surplus_controller.py and docs/SURPLUS_ARCHITECTURE.md.
        # The controller drives each charger's current setpoint itself via
        # ``set_dynamic_limit`` on every fast-loop tick — it never delegates
        # surplus control to the charger hardware (Easee "smart" / smart-meter
        # surplus switches are intentionally left untouched).
        ev_chargers_cfg_for_surplus = self.outputs.get(OUTPUT_EV_CHARGERS, [])
        self.surplus_controller = SurplusController(
            self.params,
            ev_chargers_cfg_for_surplus,
        )

        # --- Solar predictor ---
        # Use HA's configured lat/lon, fall back to mapping params.
        lat = getattr(hass.config, "latitude", None) or self.params.get("latitude", 58.0)
        lon = getattr(hass.config, "longitude", None) or self.params.get("longitude", 16.0)
        self.solar_predictor = SolarPredictor(
            pv_peak_power_kw=self.params.get("pv_peak_power_kw", DEFAULT_PV_PEAK_POWER_KW),
            latitude=float(lat),
            longitude=float(lon),
            cloud_opacity=self.params.get("pv_cloud_opacity", DEFAULT_CLOUD_OPACITY),
        )

        # --- Manual override tracking ---
        # When a user manually turns off a charger switch, we respect
        # that choice for one full optimisation cycle.  Maps entity_id
        # to the datetime of the manual override.
        self._manual_overrides: dict[str, Any] = {}

        # --- HEM-owned per-charger override flags ---
        # Toggled by switch entities created in switch.py. When True for
        # a charger name, _execute_actions FORCE-CHARGES that charger
        # every cycle: any stop_charging action is dropped, a
        # start_charging action is injected, the dynamic current limit is
        # raised to max (if configured), and the user-manual-off cooldown
        # is bypassed for that charger's start entity.
        # Persisted across restarts via RestoreEntity.
        self.charger_overrides: dict[str, bool] = {}

        # --- Auto-replan: re-run optimisation when UI settings change ---
        self._unsub_state_listeners: list = []
        watched = self._collect_watched_entities()
        if watched:
            _LOGGER.info("Auto-replan: watching %s", watched)
            unsub = async_track_state_change_event(
                hass, watched, self._on_setting_changed,
            )
            self._unsub_state_listeners.append(unsub)

        # --- Fast EV surplus current adjustment loop ---
        # Runs every ``fast_ev_update_interval`` seconds (default & ceiling
        # 10 s) to re-read grid export and re-apply each active charger's
        # current setpoint.  Per the surplus architecture (Rule 3) the
        # controller MUST re-issue the limit at least every 10 s so the
        # cars never draw from the grid while we're tracking solar.
        configured_interval = int(self.params.get(
            "fast_ev_update_interval", DEFAULT_FAST_EV_UPDATE_INTERVAL
        ))
        fast_interval_s = max(1, min(configured_interval, DEFAULT_FAST_EV_UPDATE_INTERVAL))
        if configured_interval != fast_interval_s:
            _LOGGER.warning(
                "fast_ev_update_interval=%ds is above the 10s ceiling — "
                "clamping so the surplus controller keeps the cars off the grid",
                configured_interval,
            )
        self._unsub_fast_ev = async_track_time_interval(
            hass,
            self._fast_ev_current_update,
            timedelta(seconds=fast_interval_s),
        )
        _LOGGER.info(
            "Fast EV current loop: every %d s", fast_interval_s,
        )

    # ------------------------------------------------------------------
    # Fast EV surplus current adjustment
    # ------------------------------------------------------------------

    async def _fast_ev_current_update(self, now=None) -> None:
        """Lightweight loop: drive the SurplusController state machine.

        Runs every ~10 s independently of the full optimisation cycle.
        Reads grid export/import + per-EV power, then asks the
        SurplusController to advance its state machine and produce any
        HA service calls needed (start/stop chargers, set current limit,
        flip the surplus indicator switch).
        """
        # 1. Read grid sensors
        sg = self.inputs.get(INPUT_SUNGROW, {})
        grid_export_w = self._get_state_float(sg.get("grid_export_power"))
        grid_import_w = self._get_state_float(sg.get("grid_import_power"))

        # 2. Build EV vehicle snapshots from live sensors so the
        #    controller can read each charger's current power_w/SoC.
        ev_chargers_cfg = self.inputs.get(INPUT_EV_CHARGERS, [])
        if not isinstance(ev_chargers_cfg, list):
            ev_chargers_cfg = []

        ev_vehicles: list[dict[str, Any]] = []
        any_connected = False
        for charger in ev_chargers_cfg:
            name = charger.get("name", "")
            raw_power = self._get_state_float(charger.get("power"))
            unit = charger.get("power_unit", "W")
            power_w = raw_power * 1000 if unit == "kW" else raw_power
            soc = self._get_state_float(charger.get("vehicle_soc"))
            target = self._get_state_float(charger.get("vehicle_target_soc"))
            # Mirror the slow-loop detection in _read_sensors: a vehicle is
            # "connected" when the charger reports a plugged-in status OR
            # the charger switch is on OR it's already drawing power.
            # (The mapping has no "connected" key — earlier code read it
            # blindly, so plugged-in-but-idle cars like the Zoe were
            # treated as disconnected and never surplus-charged.)
            status = self._get_state_str(charger.get("status"))
            switch_state = self._get_state_str(charger.get("charger_switch"))
            connected = (
                status in ("charging", "awaiting_start", "connected", "ready")
                or switch_state == "on"
                or power_w > 0
            )
            if connected:
                any_connected = True
            ev_vehicles.append({
                "name": name,
                "power_w": power_w,
                "vehicle_soc": soc,
                "vehicle_target_soc": target,
                "connected": connected,
            })

        # 3. Current spot price (for logging)
        latest_data = self.data or {}
        current_price = (latest_data.get("prices") or {}).get("current", 0.0)

        # 4. Tick the state machine
        actions = self.surplus_controller.tick(
            grid_export_w=grid_export_w,
            grid_import_w=grid_import_w,
            ev_vehicles=ev_vehicles,
            ev_connected=any_connected,
            current_price=current_price,
        )

        # 4b. Apply HEM force-charge override even when SurplusController
        # produced no actions — we still want to keep overridden
        # chargers ON every fast tick.
        actions, _force_start_eids = self._apply_charger_overrides(
            list(actions or [])
        )

        if not actions:
            return

        # 5. Execute the resulting service calls
        for action in actions:
            service = action.get("service", "")
            entity_id = action.get("entity_id")
            device_id = action.get("device_id")
            service_data = action.get("data", {})

            if not service or "." not in service:
                continue
            domain, svc_name = service.split(".", 1)
            if not svc_name:
                continue

            call_data = dict(service_data)
            if device_id and not entity_id:
                call_data["device_id"] = device_id
            elif entity_id:
                call_data["entity_id"] = entity_id

            try:
                await self.hass.services.async_call(
                    domain, svc_name, call_data, blocking=True,
                )
            except Exception as exc:
                _LOGGER.error("SurplusController: failed %s: %s", service, exc)

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

            # 5b. Predict solar production from weather forecast
            predicted_solar = await self._predict_solar(horizon)

            # 6. Run optimizer to produce a schedule
            schedule = self.optimizer.optimize(
                prices=prices,
                predicted_consumption=predicted_consumption,
                battery_soc=sensor_data.get("battery_soc", 50),
                ev_connected=sensor_data.get("ev_connected", False),
                grid_export_power=sensor_data.get("grid_export_power", 0.0),
                ev_vehicles=sensor_data.get("ev_chargers", []),
                predicted_solar=predicted_solar,
                surplus_active=self.surplus_controller.is_active,
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
    # Solar prediction
    # ------------------------------------------------------------------

    async def _predict_solar(self, hours_ahead: int) -> list[float]:
        """Fetch weather forecast and predict solar production.

        Calls the ``weather.get_forecasts`` HA service to obtain
        hourly cloud-coverage data, then feeds it to the
        :class:`SolarPredictor`.  Returns zeros gracefully if the
        service call fails or PV is not configured.
        """
        if self.solar_predictor.pv_peak_power_kw <= 0:
            return [0.0] * hours_ahead

        weather = self.inputs.get(INPUT_WEATHER, {})
        weather_entity = weather.get("entity")
        if not weather_entity:
            _LOGGER.debug("No weather entity configured — skipping solar prediction")
            return [0.0] * hours_ahead

        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                service_data={"type": "hourly"},
                target={"entity_id": weather_entity},
                blocking=True,
                return_response=True,
            )
            forecasts = response.get(weather_entity, {}).get("forecast", [])
        except Exception as exc:
            _LOGGER.warning("Weather forecast service call failed: %s", exc)
            forecasts = []

        if not forecasts:
            _LOGGER.debug("No weather forecast data — using 50%% cloud default")
            return self.solar_predictor.predict(hours_ahead)

        from datetime import datetime
        return self.solar_predictor.predict_from_forecast(
            hours_ahead=hours_ahead,
            forecasts=forecasts,
            now=datetime.now(),
        )

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    def _apply_charger_overrides(
        self, actions: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Force-charge transform applied to any action list.

        For each charger whose override switch is ON:
          - drop any stop_charging action
          - inject start_charging (always — even if charger switch is
            already off; HA will no-op a redundant turn_on)
          - replace any dynamic-limit action with max_current

        Returns the (possibly mutated) action list AND the set of
        start entity_ids that the manual-user-off cooldown should
        bypass.
        """
        force_start_eids: set[str] = set()
        if not self.charger_overrides:
            return actions, force_start_eids

        ev_chargers_out = self.outputs.get(OUTPUT_EV_CHARGERS, []) or []
        for ch in ev_chargers_out:
            name = ch.get("name")
            if not name or not self.charger_overrides.get(name):
                continue
            start_cfg = ch.get("start_charging") or {}
            stop_cfg = ch.get("stop_charging") or {}
            start_eid = start_cfg.get("entity_id")
            stop_eid = stop_cfg.get("entity_id")

            # Drop any stop_charging actions for this charger.
            if stop_eid:
                actions = [
                    a for a in actions
                    if a.get("entity_id") != stop_eid
                ]

            # Inject start_charging if not already present.
            if start_cfg.get("service") and start_eid:
                already = any(
                    a.get("entity_id") == start_eid
                    and a.get("service") == start_cfg["service"]
                    for a in actions
                )
                if not already:
                    actions.append({
                        "service": start_cfg["service"],
                        "entity_id": start_eid,
                        "data": {},
                    })
                force_start_eids.add(start_eid)
                _LOGGER.info(
                    "HEM override ACTIVE for %s — force-charging "
                    "(start=%s)", name, start_eid,
                )

            # Raise dynamic current limit to max if configured.
            dyn_cfg = ch.get("set_dynamic_limit") or {}
            dyn_service = dyn_cfg.get("service")
            dyn_device = dyn_cfg.get("device_id")
            if dyn_service and dyn_device:
                max_current = dyn_cfg.get("max_current", 32)
                actions = [
                    a for a in actions
                    if not (
                        a.get("service") == dyn_service
                        and a.get("device_id") == dyn_device
                    )
                ]
                actions.append({
                    "service": dyn_service,
                    "device_id": dyn_device,
                    "data": {"current": max_current},
                })

        return actions, force_start_eids

    async def _execute_actions(self, schedule: dict) -> None:
        """Execute the immediate actions from the optimizer schedule."""
        from datetime import datetime, timedelta

        actions = list(schedule.get("immediate_actions", []))
        cooldown = timedelta(minutes=max(
            self.params.get("optimization_interval_minutes", 15) * 2, 30
        ))

        # Apply HEM-owned per-charger override (force-charge).
        actions, force_start_eids = self._apply_charger_overrides(actions)

        for action in actions:
            service = action.get("service")
            entity_id = action.get("entity_id")
            device_id = action.get("device_id")
            service_data = action.get("data", {})

            if not service or (not entity_id and not device_id):
                continue

            domain, service_name = service.split(".", 1) if "." in service else (service, "")
            if not service_name:
                continue

            # --- Manual override detection ---
            # If this is a switch.turn_on and the switch is currently
            # OFF because a *user* manually turned it off, respect
            # their choice for a cooldown period (2× optimisation
            # interval, minimum 30 min).
            # NOTE: bypassed for chargers whose HEM override switch is ON
            # (force-charge mode wins over the user-cooldown).
            is_switch_on = (domain == "switch" and service_name == "turn_on")
            if (
                is_switch_on
                and entity_id
                and entity_id.startswith("switch.")
                and entity_id not in force_start_eids
            ):
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

            target = entity_id or device_id
            _LOGGER.info(
                "Executing action: %s on %s with data %s",
                service,
                target,
                service_data,
            )

            try:
                # Build service call data — use device_id or entity_id
                call_data = dict(service_data)
                if device_id and not entity_id:
                    call_data["device_id"] = device_id
                else:
                    call_data["entity_id"] = entity_id

                await self.hass.services.async_call(
                    domain,
                    service_name,
                    call_data,
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
