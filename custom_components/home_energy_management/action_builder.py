"""Action builder — translates optimizer decisions into HA service calls.

This module converts the abstract hourly plan into concrete Home
Assistant service calls:
  - Battery mode control (force charge, self-consumption)
  - Inverter power setpoints
  - EV charger start/stop with real-time overrides:
    * Ramp-down when vehicle hits target SoC
    * Negative price → charge everything
    * Solar surplus → absorb free energy
    * Not scheduled → stop to save grid

Battery discharge policy
------------------------
The battery NEVER force-discharges or exports to the grid.
When the LP labels an hour as "discharge_battery" (expecting stored
energy to offset consumption), the inverter is set to **self-consumption
mode** — it naturally covers household load from stored energy and
charges from any solar surplus.

Only ACTION_PRE_DISCHARGE uses force-discharge: making room in the
battery before negative-price hours when we must absorb maximum solar.
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
    DEFAULT_MIN_CHARGE_POWER_W,
    DEFAULT_MIN_SURPLUS_POWER_W,
    DEFAULT_SOLAR_SURPLUS_THRESHOLD,
    DEFAULT_SURPLUS_SAFETY_MARGIN_W,
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
        self.min_surplus_power_w = params.get(
            "min_surplus_power_w", DEFAULT_MIN_SURPLUS_POWER_W
        )
        self.surplus_safety_margin_w = params.get(
            "surplus_safety_margin_w", DEFAULT_SURPLUS_SAFETY_MARGIN_W
        )
        self.min_charge_power_w = params.get(
            "min_charge_power_w", DEFAULT_MIN_CHARGE_POWER_W
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

        # --- Surplus charger tracking ---
        # After build_immediate_actions, this holds the name of the
        # charger that was started for solar surplus (if any).  The
        # coordinator’s fast-update loop reads this to know which
        # charger’s current to adjust in real time.
        self.surplus_charger_name: str | None = None
        self.surplus_charger_cfg: dict[str, Any] | None = None

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

        # --- Solar surplus: single-dispatch, lowest SoC first ---
        # Sort chargers by vehicle SoC ascending so the car that needs
        # the most energy gets first dibs on solar surplus.  Only ONE
        # charger runs on surplus at a time to avoid grid import.
        self.surplus_charger_name = None
        self.surplus_charger_cfg = None

        def _charger_sort_key(cfg: dict[str, Any]) -> float:
            """Sort key: vehicle SoC ascending (lowest first)."""
            v = vehicle_map.get(cfg.get("name", ""))
            if v:
                soc = v.get("vehicle_soc", 0)
                return soc if soc > 0 else 999.0
            return 999.0  # unknown SoC → lowest priority

        sorted_chargers = sorted(ev_chargers, key=_charger_sort_key)

        for charger_cfg in sorted_chargers:
            # Only the first eligible charger gets surplus
            surplus_for_this = solar_surplus and self.surplus_charger_name is None

            charger_actions = self._decide_charger_action(
                charger_cfg, vehicle_map, scheduled_vehicles,
                ev_connected, price_is_negative, surplus_for_this,
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

            if battery_charge_power < self.min_charge_power_w:
                # Charge power is too low to be useful — the inverter
                # would sit in "forced charge" mode drawing a trickle.
                # Self-consumption is better: solar naturally charges
                # the battery while serving the house load.
                _LOGGER.info(
                    "charge_battery: computed power %d W < min %d W "
                    "— switching to self-consumption (solar will charge naturally)",
                    battery_charge_power, self.min_charge_power_w,
                )
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
                cfg = sg_out.get("force_charge", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {},
                    })
                self._set_forced_power(sg_out, actions, int(battery_charge_power))

        elif action == ACTION_DISCHARGE_BATTERY:
            # Self-consumption mode — the inverter naturally covers
            # household load from stored battery energy and charges
            # from any solar surplus.  We NEVER force-discharge because
            # the battery must not export to the grid.
            #
            # The LP labels hours as "discharge_battery" when it
            # expects stored energy to offset consumption.  The
            # inverter's self-consumption mode achieves this without
            # actively pushing energy out.
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

        # ── Export power limit (based on raw Nordpool spot price) ──
        # When the *spot* price is negative, exporting to the grid
        # means we pay — cap the inverter's grid feed-in.  Grid
        # tariffs and VAT only apply to import, so we compare against
        # the raw spot price, not the effective consumer price.
        if spot_price < 0:
            neg_limit = sg_out.get("set_export_limit", {}).get(
                "negative_price_limit", 0
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
        """Set the inverter grid export power limit.

        If the set_export_limit output is not configured, this is a no-op.
        Pass *value* in watts to enable the limit (mode → Enabled).
        Pass ``None`` to remove the cap (mode → Disabled).

        Handles two Sungrow registers:
          - 13088  export power limit value (input_number)
          - 13087  export power limit mode  (input_select)
        """
        cfg = sg_out.get("set_export_limit", {})
        service = cfg.get("service")
        entity_id = cfg.get("entity_id")
        if not service or not entity_id:
            return

        # ── Mode toggle (Enabled / Disabled) ──
        mode_entity = cfg.get("mode_entity_id")
        if mode_entity:
            if value is not None:
                # Limiting — enable the cap
                mode_option = cfg.get("mode_enabled", "Enabled")
            else:
                # Uncapping — disable the limit
                mode_option = cfg.get("mode_disabled", "Disabled")
            _LOGGER.info(
                "Setting export limit mode to '%s' on %s",
                mode_option, mode_entity,
            )
            actions.append({
                "service": "input_select.select_option",
                "entity_id": mode_entity,
                "data": {"option": mode_option},
            })

        # ── Value ──
        max_export = cfg.get("max", 10000)
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

        Priority order:
          1. Negative price → charge at max (we get paid)
          2. Ramp-down → stop if SoC ≥ effective target
          3. Solar surplus → charge (single dispatch, min power)
          4. Scheduled → charge
          5. Expensive price → stop
          6. Not scheduled → stop
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

        # --- Compute effective target SoC (used by multiple checks) ---
        effective_target = self.ev_default_target_soc
        vehicle_soc = 0.0
        if vehicle:
            vehicle_soc = vehicle.get("vehicle_soc", 0)
            min_dep_soc = vehicle.get("min_departure_soc", 0)
            effective_target = (
                min_dep_soc if min_dep_soc > 0
                else self.ev_default_target_soc
            )
            if is_friday and min_dep_soc <= 0 and self.ev_weekend_target_soc < effective_target:
                effective_target = self.ev_weekend_target_soc

        # --- 1. FREE ENERGY: Negative prices → charge at max ---
        # We get paid to consume — ignore SoC limits, charge everything.
        if price_is_negative:
            vehicle_target = 100
            if vehicle:
                vehicle_target = vehicle.get("vehicle_target_soc", 100)
            v_soc = vehicle_soc
            if v_soc <= 0 or v_soc < vehicle_target:
                _LOGGER.info(
                    "EV %s: Negative price (%.3f) — charging at max",
                    charger_name, current_price,
                )
                actions = self._start_charger(charger_cfg)
                limit_action = self._set_charger_dynamic_limit(
                    charger_cfg, None,
                )
                if limit_action:
                    actions.append(limit_action)
                return actions

        # --- 2. RAMP-DOWN: stop if vehicle at/above effective target ---
        # Checked BEFORE solar surplus so we don’t start a fully-charged
        # car just because the sun is shining.
        if vehicle and vehicle_soc > 0 and vehicle_soc >= effective_target:
            _LOGGER.info(
                "EV %s: SoC %.0f%% >= target %.0f%% — stopping (ramp-down)",
                charger_name, vehicle_soc, effective_target,
            )
            actions = self._stop_charger(charger_cfg)
            limit_action = self._set_charger_dynamic_limit(
                charger_cfg, None,
            )
            if limit_action:
                actions.append(limit_action)
            return actions

        # --- 3. SOLAR SURPLUS: absorb free solar into EV ---
        # Only one charger at a time (surplus_for_this is False for
        # the second charger).  Requires minimum viable power to avoid
        # oscillation and grid import.
        if charger_connected and solar_surplus:
            # Check minimum power threshold
            dyn_cfg = charger_cfg.get("set_dynamic_limit", {})
            voltage = dyn_cfg.get("voltage", 230)
            phases = dyn_cfg.get("phases", 3)
            min_current = dyn_cfg.get("min_current", 6)
            charger_min_w = min_current * voltage * phases
            min_viable_w = max(self.min_surplus_power_w, charger_min_w)

            if grid_export_w >= min_viable_w:
                target_amps = self._calc_surplus_amps(
                    charger_cfg, grid_export_w, current_ev_power_w,
                )
                actual_power_w = target_amps * voltage * phases
                _LOGGER.info(
                    "EV %s: Solar surplus (%.0f W export, %.0f W charger) "
                    "— charging at %dA / %.0f W (SoC %.0f%% < target %.0f%%)",
                    charger_name, grid_export_w, current_ev_power_w,
                    target_amps, actual_power_w, vehicle_soc, effective_target,
                )
                actions = self._start_charger(charger_cfg)
                limit_action = self._set_charger_dynamic_limit(
                    charger_cfg, target_amps,
                )
                if limit_action:
                    actions.append(limit_action)
                # Track which charger got surplus
                self.surplus_charger_name = charger_name
                self.surplus_charger_cfg = charger_cfg
                return actions
            else:
                _LOGGER.debug(
                    "EV %s: Solar surplus %.0f W < min viable %.0f W — skipping",
                    charger_name, grid_export_w, min_viable_w,
                )

        # --- 4. SCHEDULED: charge at max current ---
        is_scheduled = charger_name in scheduled_vehicles

        if charger_connected and is_scheduled:
            _LOGGER.info(
                "EV %s: Scheduled this hour (%.2f kWh)",
                charger_name, scheduled_vehicles.get(charger_name, 0),
            )
            actions = self._start_charger(charger_cfg)
            limit_action = self._set_charger_dynamic_limit(
                charger_cfg, None,
            )
            if limit_action:
                actions.append(limit_action)
            return actions

        # --- 5. EXPENSIVE: stop to avoid costly grid draw ---
        if charger_connected and price_is_expensive:
            _LOGGER.info(
                "EV %s: Expensive price (%.3f) — stopping",
                charger_name, current_price,
            )
            return self._stop_charger(charger_cfg)

        # --- 6. NOT SCHEDULED: stop ---
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

    def _calc_surplus_amps(
        self,
        charger_cfg: dict[str, Any],
        grid_export_w: float,
        current_ev_power_w: float,
    ) -> int:
        """Calculate target charger amps from available solar surplus.

        total_available = grid_export + what the charger is already drawing
                          − safety_margin.
        target_amps = floor(total_available / (voltage × phases)).
        """
        dyn_cfg = charger_cfg.get("set_dynamic_limit", {})
        voltage = dyn_cfg.get("voltage", 230)
        phases = dyn_cfg.get("phases", 3)
        min_current = dyn_cfg.get("min_current", 6)
        max_current = dyn_cfg.get("max_current", 32)

        total_available_w = (
            grid_export_w + current_ev_power_w - self.surplus_safety_margin_w
        )
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

    # ------------------------------------------------------------------
    # Fast surplus current update (called every ~30 s by coordinator)
    # ------------------------------------------------------------------

    def build_surplus_current_update(
        self,
        grid_export_w: float,
        current_ev_power_w: float,
    ) -> list[dict[str, Any]]:
        """Re-calculate the surplus charger's current from live grid export.

        Called by the coordinator's fast-update timer to keep the
        charger current tracking the available solar surplus in real
        time.  Returns an empty list if no surplus charger is active
        or if the charger has no dynamic-limit config.

        If the surplus drops below the minimum viable power, returns
        a *stop* action so the charger doesn't pull from the grid.
        """
        cfg = self.surplus_charger_cfg
        name = self.surplus_charger_name
        if not cfg or not name:
            return []

        dyn_cfg = cfg.get("set_dynamic_limit", {})
        voltage = dyn_cfg.get("voltage", 230)
        phases = dyn_cfg.get("phases", 3)
        min_current = dyn_cfg.get("min_current", 6)
        charger_min_w = min_current * voltage * phases
        min_viable_w = max(self.min_surplus_power_w, charger_min_w)

        # total_available = what's being exported + what the charger
        # is already drawing (it's *part of* the house load, so it
        # doesn't show up in export).
        total_available_w = (
            grid_export_w + current_ev_power_w - self.surplus_safety_margin_w
        )

        if total_available_w < min_viable_w:
            # Surplus collapsed — stop charger to avoid grid import
            _LOGGER.info(
                "Fast EV: surplus %.0f W < min %.0f W for %s — stopping",
                total_available_w, min_viable_w, name,
            )
            self.surplus_charger_name = None
            self.surplus_charger_cfg = None
            return self._stop_charger(cfg)

        target_amps = self._calc_surplus_amps(
            cfg, grid_export_w, current_ev_power_w,
        )
        limit_action = self._set_charger_dynamic_limit(cfg, target_amps)
        if limit_action:
            _LOGGER.debug(
                "Fast EV: adjusting %s to %dA (export %.0f W, charger %.0f W)",
                name, target_amps, grid_export_w, current_ev_power_w,
            )
            return [limit_action]
        return []
