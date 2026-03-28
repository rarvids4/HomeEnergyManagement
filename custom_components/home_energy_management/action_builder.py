"""Action builder — translates optimizer decisions into HA service calls.

This module converts the abstract hourly plan into concrete Home
Assistant service calls:
  - Battery mode control (force charge, force discharge, self-consumption)
  - Inverter power setpoints
  - EV charger start/stop with real-time overrides:
    * Ramp-down when vehicle hits target SoC
    * Negative price → charge everything
    * Solar surplus → absorb free energy
    * Not scheduled → stop to save grid
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .const import (
    ACTION_CHARGE_BATTERY,
    ACTION_DISCHARGE_BATTERY,
    ACTION_MAXIMIZE_LOAD,
    ACTION_PRE_DISCHARGE,
    ACTION_SELF_CONSUMPTION,
    DEFAULT_EV_MIN_DEPARTURE_SOC,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_EV_WEEKEND_TARGET_SOC,
    DEFAULT_SOLAR_SURPLUS_THRESHOLD,
    OUTPUT_EV_CHARGERS,
    OUTPUT_EASEE,
    OUTPUT_SUNGROW,
)

_LOGGER = logging.getLogger(__name__)


class ActionBuilder:
    """Converts optimizer decisions into HA service calls."""

    def __init__(self, params: dict[str, Any], outputs: dict[str, Any]) -> None:
        self.enable_battery = params.get("enable_battery_control", True)
        self.enable_charger = params.get("enable_charger_control", True)
        self.outputs = outputs

        self.solar_surplus_threshold = params.get(
            "solar_surplus_threshold_w", DEFAULT_SOLAR_SURPLUS_THRESHOLD
        )
        self.ev_default_target_soc = params.get(
            "ev_default_target_soc", DEFAULT_EV_TARGET_SOC
        )
        self.ev_default_min_departure_soc = params.get(
            "ev_default_min_departure_soc", DEFAULT_EV_MIN_DEPARTURE_SOC
        )
        self.ev_weekend_target_soc = params.get(
            "ev_weekend_target_soc", DEFAULT_EV_WEEKEND_TARGET_SOC
        )

        # EV charger config from outputs
        self.ev_chargers_cfg = outputs.get(OUTPUT_EV_CHARGERS, [])
        if not self.ev_chargers_cfg:
            legacy = outputs.get(OUTPUT_EASEE, {})
            if legacy:
                self.ev_chargers_cfg = [legacy]

    # ------------------------------------------------------------------
    # Public: build all immediate actions for the current hour
    # ------------------------------------------------------------------

    def build_immediate_actions(
        self,
        action: str,
        ev_connected: bool,
        current_price: float = 0.0,
        avg_price: float = 0.0,
        min_price: float = 0.0,
        price_spread: float = 0.0,
        grid_export_w: float = 0.0,
        ev_vehicles: list[dict[str, Any]] | None = None,
        ev_charge_plan: dict[str, Any] | None = None,
        now: datetime | None = None,
        predicted_consumption: float = 0.0,
        predicted_solar: float = 0.0,
        target_soc: float = 100.0,
    ) -> list[dict[str, Any]]:
        """Convert the current hour's plan into HA service calls.

        Battery actions follow the optimizer's hourly plan directly.
        EV charger actions are driven by the pre-computed schedule
        with real-time overrides.
        """
        actions = []
        sg_out = self.outputs.get(OUTPUT_SUNGROW, {})

        # --- Battery actions ---
        if self.enable_battery:
            actions.extend(self._battery_actions(sg_out, action, predicted_consumption, predicted_solar, target_soc))

        # --- EV charger actions ---
        ev_chargers = (
            self.ev_chargers_cfg
            if isinstance(self.ev_chargers_cfg, list)
            else []
        )
        if not self.enable_charger or not ev_chargers:
            return actions

        price_is_negative = current_price < 0
        solar_surplus = grid_export_w >= self.solar_surplus_threshold
        price_is_expensive = action == ACTION_DISCHARGE_BATTERY

        # Current hour's schedule entry (index 0 = now)
        schedule_entry = {}
        if ev_charge_plan:
            sched_list = ev_charge_plan.get("schedule", [])
            if sched_list:
                schedule_entry = sched_list[0]
        scheduled_vehicles = schedule_entry.get("vehicles", {})

        # Vehicle lookup for ramp-down checks
        vehicle_map: dict[str, dict[str, Any]] = {}
        for v in (ev_vehicles or []):
            vehicle_map[v.get("name", "")] = v

        now = now or datetime.now()
        is_friday = now.weekday() == 4

        for charger_cfg in ev_chargers:
            charger_action = self._decide_charger_action(
                charger_cfg, vehicle_map, scheduled_vehicles,
                ev_connected, price_is_negative, solar_surplus,
                price_is_expensive, current_price, grid_export_w,
                is_friday,
            )
            if charger_action:
                actions.append(charger_action)

        return actions

    # ------------------------------------------------------------------
    # Battery service calls
    # ------------------------------------------------------------------

    def _battery_actions(
        self,
        sg_out: dict[str, Any],
        action: str,
        predicted_consumption: float = 0.0,
        predicted_solar: float = 0.0,
        target_soc: float = 100.0,
    ) -> list[dict[str, Any]]:
        """Build battery-control service calls for the inverter."""
        actions: list[dict[str, Any]] = []

        if action == ACTION_MAXIMIZE_LOAD:
            cfg = sg_out.get("self_consumption", {})
            if cfg.get("service") and cfg.get("entity_id"):
                actions.append({
                    "service": cfg["service"],
                    "entity_id": cfg["entity_id"],
                    "data": {},
                })
            actions.extend(self._stop_forced_cmd(sg_out))
            self._set_forced_power(sg_out, actions, 0)

        elif action == ACTION_PRE_DISCHARGE:
            cfg = sg_out.get("force_discharge", {})
            if cfg.get("service") and cfg.get("entity_id"):
                actions.append({
                    "service": cfg["service"],
                    "entity_id": cfg["entity_id"],
                    "data": {},
                })
            self._set_forced_power(sg_out, actions, sg_out.get("set_forced_power", {}).get("max", 5000))
            pwr_limit = sg_out.get("set_discharge_power", {})
            if pwr_limit.get("service") and pwr_limit.get("entity_id"):
                actions.append({
                    "service": pwr_limit["service"],
                    "entity_id": pwr_limit["entity_id"],
                    "data": {"value": pwr_limit.get("max", 5000)},
                })

        elif action == ACTION_CHARGE_BATTERY:
            cfg = sg_out.get("force_charge", {})
            if cfg.get("service") and cfg.get("entity_id"):
                actions.append({
                    "service": cfg["service"],
                    "entity_id": cfg["entity_id"],
                    "data": {},
                })
            inverter_max = sg_out.get("set_forced_power", {}).get("max", 5000)
            grid_limit = 4500
            # Clamp SoC to target_soc (do not overcharge)
            current_soc = sg_out.get("soc", 0)
            soc_to_charge = max(0, target_soc - current_soc)
            # Convert SoC % to kWh (if battery_capacity available)
            battery_capacity = sg_out.get("capacity_kwh", 10)
            kwh_to_charge = soc_to_charge / 100.0 * battery_capacity
            # Convert kWh to W for 1 hour (assume 1h slot)
            w_to_charge = kwh_to_charge * 1000
            # grid_import = predicted_consumption - predicted_solar + battery_charge_power
            battery_charge_power = min(w_to_charge, grid_limit - (predicted_consumption - predicted_solar), inverter_max)
            battery_charge_power = max(0, battery_charge_power)
            self._set_forced_power(sg_out, actions, int(battery_charge_power))

        elif action == ACTION_DISCHARGE_BATTERY:
            # Self-consumption mode — inverter covers house load from battery
            cfg = sg_out.get("self_consumption", {})
            if cfg.get("service") and cfg.get("entity_id"):
                actions.append({
                    "service": cfg["service"],
                    "entity_id": cfg["entity_id"],
                    "data": {},
                })
            actions.extend(self._stop_forced_cmd(sg_out))
            self._set_forced_power(sg_out, actions, 0)

        else:
            # Default: self-consumption
            cfg = sg_out.get("self_consumption", {})
            if cfg.get("service") and cfg.get("entity_id"):
                actions.append({
                    "service": cfg["service"],
                    "entity_id": cfg["entity_id"],
                    "data": {},
                })
            actions.extend(self._stop_forced_cmd(sg_out))
            self._set_forced_power(sg_out, actions, 0)

        return actions

    @staticmethod
    def _set_forced_power(
        sg_out: dict[str, Any],
        actions: list[dict[str, Any]],
        value: int,
    ) -> None:
        """Append a set_forced_power action if configured."""
        pwr_cfg = sg_out.get("set_forced_power", {})
        if pwr_cfg.get("service") and pwr_cfg.get("entity_id"):
            actions.append({
                "service": pwr_cfg["service"],
                "entity_id": pwr_cfg["entity_id"],
                "data": {"value": value},
            })

    @staticmethod
    def _stop_forced_cmd(sg_out: dict[str, Any]) -> list[dict[str, Any]]:
        """Reset the forced charge/discharge command to Stop.

        Clears latched mode on the Sungrow inverter so
        self-consumption works correctly.
        """
        actions: list[dict[str, Any]] = []
        entity = sg_out.get("battery_mode_select")
        options = sg_out.get("battery_mode_options", {})
        stop_option = options.get("stop")
        if entity and stop_option:
            actions.append({
                "service": "input_select.select_option",
                "entity_id": entity,
                "data": {"option": stop_option},
            })
        return actions

    # ------------------------------------------------------------------
    # EV charger decisions
    # ------------------------------------------------------------------

    def _decide_charger_action(
        self,
        charger_cfg: dict[str, Any],
        vehicle_map: dict[str, dict[str, Any]],
        scheduled_vehicles: dict[str, float],
        ev_connected: bool,
        price_is_negative: bool,
        solar_surplus: bool,
        price_is_expensive: bool,
        current_price: float,
        grid_export_w: float,
        is_friday: bool,
    ) -> dict[str, Any] | None:
        """Decide the action for a single EV charger.

        Returns a single service-call dict, or None.
        """
        charger_name = charger_cfg.get("name", "")
        vehicle = vehicle_map.get(charger_name)

        charger_connected = ev_connected
        if vehicle:
            charger_connected = vehicle.get("connected", ev_connected)

        # --- RAMP-DOWN: stop if vehicle at/above target SoC ---
        if vehicle:
            vehicle_soc = vehicle.get("vehicle_soc", 0)
            min_dep_soc = vehicle.get(
                "min_departure_soc", self.ev_default_min_departure_soc
            )
            effective_target = (
                min_dep_soc if min_dep_soc > 0
                else self.ev_default_target_soc
            )
            if is_friday and self.ev_weekend_target_soc < effective_target:
                effective_target = self.ev_weekend_target_soc

            if vehicle_soc > 0 and vehicle_soc >= effective_target:
                vehicle_target = vehicle.get("vehicle_target_soc", 100)
                if price_is_negative and vehicle_soc < vehicle_target:
                    pass  # Exploit negative prices
                else:
                    _LOGGER.info(
                        "EV %s: SoC %.0f%% >= target %.0f%% — stopping (ramp-down)",
                        charger_name, vehicle_soc, effective_target,
                    )
                    return self._stop_charger(charger_cfg)

        # --- Schedule-based + real-time overrides ---
        is_scheduled = charger_name in scheduled_vehicles

        if price_is_negative:
            _LOGGER.info(
                "EV %s: Negative price (%.3f) — charging",
                charger_name, current_price,
            )
            return self._start_charger(charger_cfg)

        if charger_connected and solar_surplus and not price_is_expensive:
            _LOGGER.info(
                "EV %s: Solar surplus (%.0f W) — charging",
                charger_name, grid_export_w,
            )
            return self._start_charger(charger_cfg)

        if charger_connected and is_scheduled:
            _LOGGER.info(
                "EV %s: Scheduled this hour (%.2f kWh)",
                charger_name, scheduled_vehicles.get(charger_name, 0),
            )
            return self._start_charger(charger_cfg)

        if charger_connected and price_is_expensive:
            _LOGGER.info(
                "EV %s: Expensive price (%.3f) — stopping",
                charger_name, current_price,
            )
            return self._stop_charger(charger_cfg)

        if charger_connected and not is_scheduled:
            _LOGGER.info("EV %s: Not scheduled — stopping", charger_name)
            return self._stop_charger(charger_cfg)

        return None

    @staticmethod
    def _start_charger(cfg: dict[str, Any]) -> dict[str, Any] | None:
        start = cfg.get("start_charging", {})
        if start.get("service"):
            return {
                "service": start["service"],
                "entity_id": start["entity_id"],
                "data": {},
            }
        return None

    @staticmethod
    def _stop_charger(cfg: dict[str, Any]) -> dict[str, Any] | None:
        stop = cfg.get("stop_charging", {})
        if stop.get("service"):
            return {
                "service": stop["service"],
                "entity_id": stop["entity_id"],
                "data": {},
            }
        return None
