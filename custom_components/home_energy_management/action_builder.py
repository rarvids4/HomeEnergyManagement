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
        spot_price: float = 0.0,
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
            actions.extend(self._battery_actions(
                sg_out, action, predicted_consumption, predicted_solar,
                target_soc, spot_price=spot_price,
            ))

        # --- EV charger actions ---
        ev_chargers = (
            self.ev_chargers_cfg
            if isinstance(self.ev_chargers_cfg, list)
            else []
        )
        if not self.enable_charger or not ev_chargers:
            return actions

        # Use raw SPOT price for negative-price detection — the
        # effective price (spot + tariff) may still be positive.
        price_is_negative = spot_price < 0
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
            charger_actions = self._decide_charger_action(
                charger_cfg, vehicle_map, scheduled_vehicles,
                ev_connected, price_is_negative, solar_surplus,
                price_is_expensive, current_price, grid_export_w,
                is_friday,
            )
            if charger_actions:
                actions.extend(charger_actions)

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
        *,
        spot_price: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Build battery-control service calls for the inverter."""
        actions: list[dict[str, Any]] = []

        if action == ACTION_MAXIMIZE_LOAD:
            # Negative price — absorb as much as possible, minimize grid export.
            # If solar is actively exporting (grid_export_w > 0), force-charge
            # the battery to soak up surplus; otherwise self-consumption is fine.
            net_solar_w = (predicted_solar - predicted_consumption) * 1000
            battery_soc = sg_out.get("soc", 0)
            max_soc = sg_out.get("max_soc", 100)
            can_charge = battery_soc < max_soc

            if can_charge and net_solar_w > 0:
                # Solar exceeds consumption — force-charge battery to absorb it
                cfg = sg_out.get("force_charge", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {},
                    })
                inverter_max = sg_out.get("set_forced_power", {}).get("max", 5000)
                charge_power = min(int(net_solar_w), inverter_max)
                self._set_forced_power(sg_out, actions, charge_power)
                _LOGGER.info(
                    "Negative price + solar surplus: force-charging battery "
                    "at %d W to minimize grid export (SoC %.0f%%)",
                    charge_power, battery_soc,
                )
            else:
                # No solar surplus or battery full — self-consumption is fine
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
            # predicted values are kWh (1 h slot) → multiply by 1000 for W
            net_consumption_w = (predicted_consumption - predicted_solar) * 1000
            battery_charge_power = min(w_to_charge, grid_limit - net_consumption_w, inverter_max)
            battery_charge_power = max(0, battery_charge_power)
            self._set_forced_power(sg_out, actions, int(battery_charge_power))

        elif action == ACTION_DISCHARGE_BATTERY:
            # Force-discharge — actively drain battery during expensive hours.
            # The battery MUST NOT charge during this mode, even if solar
            # production exceeds household consumption.
            cfg = sg_out.get("force_discharge", {})
            if cfg.get("service") and cfg.get("entity_id"):
                actions.append({
                    "service": cfg["service"],
                    "entity_id": cfg["entity_id"],
                    "data": {},
                })
            inverter_max = sg_out.get("set_forced_power", {}).get("max", 5000)
            # Minimum forced discharge — guarantees the battery never charges
            # (solar surplus would otherwise be absorbed by the battery).
            min_discharge_w = 500

            # Net household load after solar (W).
            # predicted_consumption/predicted_solar are in kWh (1 h slot)
            # → multiply by 1000 to get average watts.
            net_load_w = (predicted_consumption - predicted_solar) * 1000

            if net_load_w > 0:
                # House needs more than solar → discharge to cover the gap
                discharge_power = int(net_load_w)
            else:
                # Solar covers (or exceeds) house load → still discharge
                # to export stored energy at the expensive grid price
                discharge_power = inverter_max

            # Clamp: at least min_discharge, at most inverter_max
            discharge_power = max(min_discharge_w, min(discharge_power, inverter_max))

            self._set_forced_power(sg_out, actions, discharge_power)
            pwr_limit = sg_out.get("set_discharge_power", {})
            if pwr_limit.get("service") and pwr_limit.get("entity_id"):
                actions.append({
                    "service": pwr_limit["service"],
                    "entity_id": pwr_limit["entity_id"],
                    "data": {"value": discharge_power},
                })

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

        # ── Export power limit (based on raw Nordpool spot price) ──
        # When the *spot* price is negative, exporting to the grid
        # means we pay — cap the inverter's grid feed-in.  Grid
        # tariffs and VAT only apply to import, so we compare against
        # the raw spot price, not the effective consumer price.
        if spot_price < 0:
            neg_limit = sg_out.get("set_export_limit", {}).get(
                "negative_price_limit", 100
            )
            _LOGGER.info(
                "Spot price %.4f < 0 — capping grid export to %d W",
                spot_price, neg_limit,
            )
            self._set_export_limit(sg_out, actions, neg_limit)
        else:
            # Spot price ≥ 0 — exporting earns money, remove cap
            self._set_export_limit(sg_out, actions, None)

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

    @staticmethod
    def _set_export_limit(
        sg_out: dict[str, Any],
        actions: list[dict[str, Any]],
        value: int | None,
    ) -> None:
        """Set the inverter grid export power limit (register 13088).

        If the set_export_limit output is not configured, this is a no-op.
        Pass *value* in watts.  Pass None to reset to max (uncapped).
        """
        cfg = sg_out.get("set_export_limit", {})
        service = cfg.get("service")
        entity_id = cfg.get("entity_id")
        if not service or not entity_id:
            return

        max_export = cfg.get("max", 5000)
        limit_w = value if value is not None else max_export
        limit_w = max(cfg.get("min", 0), min(limit_w, max_export))

        _LOGGER.info("Setting grid export limit to %d W", limit_w)
        actions.append({
            "service": service,
            "entity_id": entity_id,
            "data": {"value": limit_w},
        })

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
    ) -> list[dict[str, Any]] | None:
        """Decide the action(s) for a single EV charger.

        Returns a list of service-call dicts, or None.
        May include a dynamic current limit action alongside start/stop.
        """
        charger_name = charger_cfg.get("name", "")
        vehicle = vehicle_map.get(charger_name)

        charger_connected = ev_connected
        if vehicle:
            charger_connected = vehicle.get("connected", ev_connected)

        # Current charger power (W) for dynamic limit calculation
        current_ev_power_w = 0.0
        if vehicle:
            current_ev_power_w = vehicle.get("power_w", 0.0)

        # --- FREE ENERGY OVERRIDES (checked first — never waste free power) ---
        # Negative prices: always charge at max — we get paid, ignore SoC limits
        if price_is_negative:
            vehicle_target = 100
            vehicle_soc = 0
            if vehicle:
                vehicle_target = vehicle.get("vehicle_target_soc", 100)
                vehicle_soc = vehicle.get("vehicle_soc", 0)
            if vehicle_soc <= 0 or vehicle_soc < vehicle_target:
                _LOGGER.info(
                    "EV %s: Negative price (%.3f) — charging at max",
                    charger_name, current_price,
                )
                actions = self._start_charger(charger_cfg)
                # Negative price → charge at max current
                limit_action = self._set_charger_dynamic_limit(
                    charger_cfg, None,
                )
                if limit_action:
                    actions.append(limit_action)
                return actions

        # Solar surplus: charge up to the vehicle's own target SoC
        # (disregard the departure/min SoC limits — this is free solar)
        if charger_connected and solar_surplus and not price_is_expensive:
            vehicle_target = 100
            vehicle_soc = 0
            if vehicle:
                vehicle_target = vehicle.get("vehicle_target_soc", 100)
                vehicle_soc = vehicle.get("vehicle_soc", 0)
            if vehicle_soc <= 0 or vehicle_soc < vehicle_target:
                # Calculate dynamic current based on available surplus
                target_amps = self._calc_surplus_amps(
                    charger_cfg, grid_export_w, current_ev_power_w,
                )
                _LOGGER.info(
                    "EV %s: Solar surplus (%.0f W export, %.0f W charger) "
                    "— charging at %dA (SoC %.0f%% < vehicle limit %.0f%%)",
                    charger_name, grid_export_w, current_ev_power_w,
                    target_amps, vehicle_soc, vehicle_target,
                )
                actions = self._start_charger(charger_cfg)
                limit_action = self._set_charger_dynamic_limit(
                    charger_cfg, target_amps,
                )
                if limit_action:
                    actions.append(limit_action)
                return actions

        # --- RAMP-DOWN: stop if vehicle at/above target SoC ---
        if vehicle:
            vehicle_soc = vehicle.get("vehicle_soc", 0)
            min_dep_soc = vehicle.get("min_departure_soc", 0)
            effective_target = (
                min_dep_soc if min_dep_soc > 0
                else self.ev_default_target_soc
            )
            # Weekend optimization: only lower the target on Fridays when
            # the user hasn't explicitly set a departure SoC.
            # (min_dep_soc == 0 means no explicit setting from the user.)
            if is_friday and min_dep_soc <= 0 and self.ev_weekend_target_soc < effective_target:
                effective_target = self.ev_weekend_target_soc

            if vehicle_soc > 0 and vehicle_soc >= effective_target:
                _LOGGER.info(
                    "EV %s: SoC %.0f%% >= target %.0f%% — stopping (ramp-down)",
                    charger_name, vehicle_soc, effective_target,
                )
                actions = self._stop_charger(charger_cfg)
                # Reset dynamic limit to max for next charge session
                limit_action = self._set_charger_dynamic_limit(
                    charger_cfg, None,
                )
                if limit_action:
                    actions.append(limit_action)
                return actions

        # --- Schedule-based + remaining overrides ---
        is_scheduled = charger_name in scheduled_vehicles

        if charger_connected and is_scheduled:
            _LOGGER.info(
                "EV %s: Scheduled this hour (%.2f kWh)",
                charger_name, scheduled_vehicles.get(charger_name, 0),
            )
            actions = self._start_charger(charger_cfg)
            # Scheduled charging → max current
            limit_action = self._set_charger_dynamic_limit(
                charger_cfg, None,
            )
            if limit_action:
                actions.append(limit_action)
            return actions

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
    def _start_charger(cfg: dict[str, Any]) -> list[dict[str, Any]]:
        start = cfg.get("start_charging", {})
        if start.get("service"):
            return [{
                "service": start["service"],
                "entity_id": start["entity_id"],
                "data": {},
            }]
        return []

    @staticmethod
    def _stop_charger(cfg: dict[str, Any]) -> list[dict[str, Any]]:
        stop = cfg.get("stop_charging", {})
        if stop.get("service"):
            return [{
                "service": stop["service"],
                "entity_id": stop["entity_id"],
                "data": {},
            }]
        return []

    @staticmethod
    def _calc_surplus_amps(
        charger_cfg: dict[str, Any],
        grid_export_w: float,
        current_ev_power_w: float,
    ) -> int:
        """Calculate target charger amps from available solar surplus.

        total_available = grid_export + what the charger is already drawing.
        target_amps = floor(total_available / (voltage × phases)).
        """
        dyn_cfg = charger_cfg.get("set_dynamic_limit", {})
        voltage = dyn_cfg.get("voltage", 230)
        phases = dyn_cfg.get("phases", 3)
        min_current = dyn_cfg.get("min_current", 6)
        max_current = dyn_cfg.get("max_current", 32)

        total_available_w = grid_export_w + current_ev_power_w
        target_amps = int(total_available_w / (voltage * phases))
        return max(min_current, min(max_current, target_amps))

    @staticmethod
    def _set_charger_dynamic_limit(
        charger_cfg: dict[str, Any],
        target_amps: int | None,
    ) -> dict[str, Any] | None:
        """Build a dynamic current limit action for the charger.

        If target_amps is None, resets to max_current (full power).
        Returns None if the charger has no dynamic limit configured.
        """
        dyn_cfg = charger_cfg.get("set_dynamic_limit", {})
        service = dyn_cfg.get("service")
        device_id = dyn_cfg.get("device_id")

        if not service or not device_id:
            return None

        max_current = dyn_cfg.get("max_current", 32)
        amps = target_amps if target_amps is not None else max_current

        _LOGGER.info("Setting charger dynamic limit to %dA", amps)
        return {
            "service": service,
            "device_id": device_id,
            "data": {"current": amps},
        }
