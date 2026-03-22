"""Optimizer — plans charge/discharge schedule based on energy prices.

The optimizer takes Nordpool hourly prices, predicted consumption,
battery state, and EV connection status, and produces an hour-by-hour
schedule of actions (charge battery, discharge, start EV, etc.).

Key strategies:
  - **Solar surplus** (highest priority after negative prices): When
    solar panels are producing more than the house consumes (i.e. we
    are exporting to the grid), the battery should absorb that free
    energy.  Self-consumption mode on the inverter handles this
    automatically.  This overrides pre-discharge and force-charge.
  - **Negative prices**: Self-consumption mode — the battery absorbs
    any solar surplus (preventing grid export) and EVs charge.
    We do NOT force-charge from the grid.
  - **Pre-discharge**: When negative prices are approaching in the next
    few hours *and there is no solar surplus*, discharge the battery
    first to create room so it can absorb surplus during the negative
    window.
  - **Cheap hours (no solar)**: Charge battery from grid.
  - **Expensive hours**: Discharge battery to avoid grid import.
  - **Normal hours**: Self-consumption mode.
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
    ACTION_SET_EV_AMPS,
    ACTION_START_EV_CHARGE,
    ACTION_STOP_EV_CHARGE,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_EV_CHEAP_PRICE_THRESHOLD,
    DEFAULT_MAX_AMPS,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_AMPS,
    DEFAULT_MIN_PRICE_SPREAD,
    DEFAULT_MIN_SOC,
    DEFAULT_PLANNING_HORIZON,
    DEFAULT_SOLAR_SURPLUS_THRESHOLD,
    DEFAULT_EV_NIGHT_END,
    DEFAULT_EV_NIGHT_PREFERENCE_SEK,
    DEFAULT_EV_NIGHT_START,
    DEFAULT_EV_WEEKEND_TARGET_SOC,
    OUTPUT_EV_CHARGERS,
    OUTPUT_EASEE,
    OUTPUT_SUNGROW,
)

_LOGGER = logging.getLogger(__name__)

# How many hours ahead to look for upcoming negative prices
# when deciding whether to pre-discharge the battery.
PRE_DISCHARGE_LOOKAHEAD = 4


class Optimizer:
    """Price-aware energy optimization engine."""

    def __init__(self, params: dict, outputs: dict) -> None:
        self.params = params
        self.outputs = outputs

        self.min_price_spread = params.get("min_price_spread", DEFAULT_MIN_PRICE_SPREAD)
        self.planning_horizon = params.get("planning_horizon_hours", DEFAULT_PLANNING_HORIZON)
        self.enable_charger = params.get("enable_charger_control", True)
        self.enable_battery = params.get("enable_battery_control", True)

        # Battery config from output mapping (master inverter only)
        sg_out = outputs.get(OUTPUT_SUNGROW, {})
        self.min_soc = sg_out.get("min_soc", DEFAULT_MIN_SOC)
        self.max_soc = sg_out.get("max_soc", DEFAULT_MAX_SOC)
        self.battery_capacity = sg_out.get("capacity_kwh", DEFAULT_BATTERY_CAPACITY)

        # EV smart charging thresholds
        self.ev_cheap_price_threshold = params.get(
            "ev_cheap_price_threshold", DEFAULT_EV_CHEAP_PRICE_THRESHOLD
        )
        self.solar_surplus_threshold = params.get(
            "solar_surplus_threshold_w", DEFAULT_SOLAR_SURPLUS_THRESHOLD
        )

        # EV grid minimization: night preference & weekend target
        self.ev_night_start = params.get("ev_night_start", DEFAULT_EV_NIGHT_START)
        self.ev_night_end = params.get("ev_night_end", DEFAULT_EV_NIGHT_END)
        self.ev_night_preference = params.get(
            "ev_night_preference_sek", DEFAULT_EV_NIGHT_PREFERENCE_SEK
        )
        self.ev_weekend_target_soc = params.get(
            "ev_weekend_target_soc", DEFAULT_EV_WEEKEND_TARGET_SOC
        )

        # EV chargers — list-based config (new) or legacy "easee" dict
        self.ev_chargers_cfg = outputs.get(OUTPUT_EV_CHARGERS, [])
        if not self.ev_chargers_cfg:
            legacy = outputs.get(OUTPUT_EASEE, {})
            if legacy:
                self.ev_chargers_cfg = [legacy]

    def optimize(
        self,
        prices: dict[str, Any],
        predicted_consumption: list[float],
        battery_soc: float,
        ev_connected: bool,
        grid_export_power: float = 0.0,
        ev_vehicles: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Produce an hour-by-hour schedule and immediate actions.

        Parameters
        ----------
        grid_export_power : float
            Current grid export in Watts.  Used to detect solar surplus
            so EVs can absorb excess production instead of exporting
            at rock-bottom prices.
        ev_vehicles : list[dict]
            Per-EV data including vehicle_soc, vehicle_capacity_kwh,
            vehicle_target_soc, and vehicle_charging_power_w.

        Returns
        -------
        dict with keys:
          - hourly_plan: list of {hour, action, reason, price}
          - immediate_actions: list of service calls to execute now
          - ev_charge_schedule: per-hour EV charging plan
          - summary: human-readable summary
        """
        now = datetime.now()
        current_hour = now.hour

        # Build combined price list (today + tomorrow)
        all_prices = list(prices.get("today", []))
        tomorrow = prices.get("tomorrow", [])
        if tomorrow:
            all_prices.extend(tomorrow)

        # If no prices available, return safe defaults
        if not all_prices:
            return self._safe_default_schedule(now)

        # Slice to the planning horizon starting from current hour
        horizon_prices = all_prices[current_hour : current_hour + self.planning_horizon]
        if not horizon_prices:
            horizon_prices = all_prices[current_hour:]

        # Compute price statistics (over positive prices only for thresholds)
        avg_price = sum(horizon_prices) / len(horizon_prices) if horizon_prices else 0
        min_price = min(horizon_prices) if horizon_prices else 0
        max_price = max(horizon_prices) if horizon_prices else 0
        price_spread = max_price - min_price

        # Save initial SoC for post-processing (capacity-aware discharge limiting)
        initial_soc = battery_soc

        # Classify each hour with look-ahead for negative prices
        hourly_plan = []
        immediate_actions = []

        for i, price in enumerate(horizon_prices):
            hour = (current_hour + i) % 24
            consumption = predicted_consumption[i] if i < len(predicted_consumption) else 0

            # Look ahead: are there negative prices coming soon?
            upcoming_prices = horizon_prices[i + 1 : i + 1 + PRE_DISCHARGE_LOOKAHEAD]

            action, reason = self._classify_hour(
                price=price,
                avg_price=avg_price,
                min_price=min_price,
                max_price=max_price,
                price_spread=price_spread,
                battery_soc=battery_soc,
                consumption=consumption,
                upcoming_prices=upcoming_prices,
                grid_export_power=grid_export_power if i == 0 else 0.0,
            )

            hourly_plan.append({
                "hour": hour,
                "action": action,
                "reason": reason,
                "price": round(price, 4),
                "predicted_consumption_kwh": round(consumption, 2),
            })

            # Simulate SoC changes for future hours
            battery_soc = self._simulate_soc_change(battery_soc, action)

        # --- Post-processing: SoC-aware discharge limiting ---
        # The classification above may have planned more discharge hours
        # than the battery can actually cover.  We rank discharge hours
        # by price (most expensive first) and only keep the top-N that
        # the available battery capacity can sustain.  The rest become
        # self_consumption.
        hourly_plan = self._limit_discharge_to_capacity(
            hourly_plan, initial_soc, predicted_consumption
        )

        # --- EV charge scheduling ---
        ev_charge_plan = self._plan_ev_charging(
            hourly_plan, ev_vehicles or [], current_hour
        )

        # Build immediate actions (only for the current hour)
        if hourly_plan:
            current_plan = hourly_plan[0]
            immediate_actions = self._build_immediate_actions(
                action=current_plan["action"],
                ev_connected=ev_connected,
                current_price=current_plan["price"],
                avg_price=avg_price,
                min_price=min_price,
                price_spread=price_spread,
                grid_export_w=grid_export_power,
                ev_vehicles=ev_vehicles or [],
            )

        # Summary
        neg_hours = [h for h in hourly_plan if h["action"] == ACTION_MAXIMIZE_LOAD]
        pre_dis_hours = [h for h in hourly_plan if h["action"] == ACTION_PRE_DISCHARGE]
        cheap_hours = [h for h in hourly_plan if h["action"] == ACTION_CHARGE_BATTERY]
        expensive_hours = [h for h in hourly_plan if h["action"] == ACTION_DISCHARGE_BATTERY]
        ev_charge_hours = [h for h in ev_charge_plan.get("schedule", []) if h.get("charging")]

        summary = (
            f"Plan: {len(neg_hours)} negative-price (maximize), "
            f"{len(pre_dis_hours)} pre-discharge, "
            f"{len(cheap_hours)} charge, "
            f"{len(expensive_hours)} discharge, "
            f"{len(ev_charge_hours)} EV-charge hours. "
            f"Price range: {min_price:.2f}–{max_price:.2f} {prices.get('currency', 'SEK')}/kWh. "
            f"Spread: {price_spread:.2f}."
        )

        return {
            "hourly_plan": hourly_plan,
            "immediate_actions": immediate_actions,
            "ev_charge_schedule": ev_charge_plan,
            "summary": summary,
            "stats": {
                "avg_price": round(avg_price, 4),
                "min_price": round(min_price, 4),
                "max_price": round(max_price, 4),
                "price_spread": round(price_spread, 4),
            },
        }

    # ------------------------------------------------------------------
    # Hour classification
    # ------------------------------------------------------------------

    def _classify_hour(
        self,
        price: float,
        avg_price: float,
        min_price: float,
        max_price: float,
        price_spread: float,
        battery_soc: float,
        consumption: float,
        upcoming_prices: list[float] | None = None,
        grid_export_power: float = 0.0,
    ) -> tuple[str, str]:
        """Decide what to do in a given hour based on price position.

        Priority order:
          1. Negative price   → MAXIMIZE_LOAD (charge everything)
          2. Solar surplus     → SELF_CONSUMPTION (absorb free solar)
          3. Pre-discharge     → empty battery before upcoming negatives
          4. Cheap hour        → charge battery from grid
          5. Expensive hour    → discharge battery
          6. Otherwise         → self-consumption
        """
        if upcoming_prices is None:
            upcoming_prices = []

        # --- 1. NEGATIVE PRICE: absorb surplus, charge EVs ---
        if price < 0:
            return (
                ACTION_MAXIMIZE_LOAD,
                f"Negative price ({price:.3f}) — self-consumption "
                f"(absorb surplus, no grid export) + all EVs ON",
            )

        # --- 2. SOLAR SURPLUS: absorb free energy into battery ---
        # When solar produces more than the house consumes (exporting
        # to grid), the battery should absorb that surplus.  This takes
        # priority over pre-discharge and force-charge-from-grid because
        # solar energy is free — there is no point in discharging stored
        # energy or buying from grid while the sun provides.
        # Exception: during expensive hours we WANT to sell to the grid
        # at high prices, so the override does not apply.
        # Note: grid_export_power is only set for the *current* hour
        # (real-time data); future hours default to 0.
        if price_spread >= self.min_price_spread:
            _expensive_threshold = max_price - price_spread * 0.30
        else:
            _expensive_threshold = float("inf")

        if (
            grid_export_power > 0
            and self.enable_battery
            and battery_soc < self.max_soc
            and price < _expensive_threshold
        ):
            return (
                ACTION_SELF_CONSUMPTION,
                f"Solar surplus ({grid_export_power:.0f} W export) — "
                f"battery absorbing excess solar (SoC {battery_soc:.0f}%)",
            )

        # --- 3. PRE-DISCHARGE before upcoming negative prices ---
        # If any of the next few hours are negative, we want to empty
        # the battery *now* so we have room to charge for free later.
        has_negative_ahead = any(p < 0 for p in upcoming_prices)
        if (
            has_negative_ahead
            and self.enable_battery
            and battery_soc > self.min_soc
            and price > 0
        ):
            return (
                ACTION_PRE_DISCHARGE,
                f"Pre-discharging at {price:.2f} — negative prices ahead, "
                f"making room in battery (SoC {battery_soc:.0f}%)",
            )

        # --- 3–5. Normal positive-price logic ---
        # If spread is too small, don't bother cycling the battery
        if price_spread < self.min_price_spread:
            return ACTION_SELF_CONSUMPTION, "Price spread too small to optimise"

        # Cheap hour: price is in the bottom 30% of the range
        cheap_threshold = min_price + price_spread * 0.30
        # Expensive hour: price is in the top 30%
        expensive_threshold = max_price - price_spread * 0.30

        if self.enable_battery and price <= cheap_threshold and battery_soc < self.max_soc:
            return ACTION_CHARGE_BATTERY, f"Cheap price ({price:.2f}), charging battery"

        if self.enable_battery and price >= expensive_threshold and battery_soc > self.min_soc:
            return ACTION_DISCHARGE_BATTERY, f"Expensive price ({price:.2f}), discharging battery"

        return ACTION_SELF_CONSUMPTION, f"Normal price ({price:.2f}), self-consumption mode"

    # ------------------------------------------------------------------
    # SoC-aware discharge limiting
    # ------------------------------------------------------------------

    def _limit_discharge_to_capacity(
        self,
        hourly_plan: list[dict[str, Any]],
        initial_soc: float,
        predicted_consumption: list[float],
    ) -> list[dict[str, Any]]:
        """Limit discharge hours to what the battery can actually cover.

        Walks through the plan chronologically, tracking SoC.  Collects
        all ``discharge_battery`` hours, ranks them by price (most
        expensive first), and only retains the top-N that the battery
        can sustain.  The rest are downgraded to ``self_consumption``.

        This ensures the battery is "saved" for the truly peak-price
        hours rather than being emptied across too many discharge slots.
        """
        if not self.enable_battery:
            return hourly_plan

        # Gather indices of all discharge_battery hours with their prices
        discharge_indices = [
            (i, entry["price"])
            for i, entry in enumerate(hourly_plan)
            if entry["action"] == ACTION_DISCHARGE_BATTERY
        ]

        if len(discharge_indices) <= 1:
            # Zero or one discharge hour — nothing to optimise
            return hourly_plan

        # Sort by price descending → most expensive first
        discharge_indices.sort(key=lambda x: x[1], reverse=True)

        # Calculate available energy (kWh) the battery can deliver
        available_kwh = (initial_soc - self.min_soc) / 100.0 * self.battery_capacity
        if available_kwh <= 0:
            # Nothing to discharge — downgrade all discharge hours
            for idx, _ in discharge_indices:
                hourly_plan[idx]["action"] = ACTION_SELF_CONSUMPTION
                hourly_plan[idx]["reason"] = (
                    f"Battery depleted (SoC {initial_soc:.0f}%) — "
                    f"self-consumption (was discharge at {hourly_plan[idx]['price']:.2f})"
                )
            return hourly_plan

        # Walk through discharge hours most-expensive-first, deducting
        # predicted consumption.  Once the budget is exhausted the
        # remaining (cheaper) hours are downgraded.
        keep_set: set[int] = set()
        remaining_kwh = available_kwh

        for idx, price in discharge_indices:
            # How much energy this hour needs (predicted consumption)
            consumption = (
                predicted_consumption[idx]
                if idx < len(predicted_consumption)
                else 1.0
            )
            # Minimum 0.5 kWh per discharge hour for planning safety
            hour_need = max(consumption, 0.5)

            if remaining_kwh >= hour_need:
                keep_set.add(idx)
                remaining_kwh -= hour_need
            else:
                # Not enough battery left — downgrade to self_consumption
                hourly_plan[idx]["action"] = ACTION_SELF_CONSUMPTION
                hourly_plan[idx]["reason"] = (
                    f"Battery capacity limited — self-consumption "
                    f"(price {price:.2f}, saving battery for more expensive hours)"
                )

        _LOGGER.debug(
            "Discharge limiting: %d/%d hours retained, %.1f/%.1f kWh used",
            len(keep_set),
            len(discharge_indices),
            available_kwh - remaining_kwh,
            available_kwh,
        )

        return hourly_plan

    # ------------------------------------------------------------------
    # EV charge scheduling
    # ------------------------------------------------------------------

    def _plan_ev_charging(
        self,
        hourly_plan: list[dict[str, Any]],
        ev_vehicles: list[dict[str, Any]],
        start_hour: int,
    ) -> dict[str, Any]:
        """Plan EV charging to minimize grid energy consumption.

        Strategy:
          1. Calculate kWh needed per vehicle (SoC → target).
          2. On weekends, lower the target SoC — car is home all day
             and can top up from solar surplus during daytime.
          3. Schedule charging in cheapest hours with night preference
             (off-peak hours are less grid-intensive).
          4. Exclude discharge_battery hours (selling to grid).

        Returns a dict with:
          - schedule: per-hour list with charging flag + price
          - total_kwh_needed, total_charging_power_kw, hours_needed
          - vehicles: per-vehicle summary
        """
        empty_schedule = {
            "schedule": [
                {
                    "hour": entry["hour"],
                    "price": entry["price"],
                    "charging": False,
                    "power_kw": 0.0,
                }
                for entry in hourly_plan
            ],
            "total_kwh_needed": 0,
            "total_charging_power_kw": 0,
            "hours_needed": 0,
            "vehicles": [],
            "start_hour": start_hour,
        }

        if not ev_vehicles:
            return empty_schedule

        # Weekend: car is parked at home → lower target, charge from solar later
        now_dt = datetime.now()
        is_weekend = now_dt.weekday() >= 5  # Saturday=5, Sunday=6

        # Gather vehicles that need charging
        vehicle_plans = []
        total_kwh_needed = 0.0
        total_charging_kw = 0.0

        for ev in ev_vehicles:
            if not ev.get("connected"):
                continue

            soc = ev.get("vehicle_soc", 0)
            capacity = ev.get("vehicle_capacity_kwh", 0)
            target = ev.get("vehicle_target_soc", 100)
            charging_w = ev.get("vehicle_charging_power_w", 0)

            # Weekend: lower target (parked at home, solar tops up later)
            if is_weekend and self.ev_weekend_target_soc < target:
                target = self.ev_weekend_target_soc

            # Skip if no vehicle data available
            if soc <= 0 or capacity <= 0:
                continue
            # Skip if already at target
            if soc >= target:
                continue

            kwh_needed = (target - soc) / 100.0 * capacity
            # Use actual charging power, fall back to charger power
            charging_kw = (charging_w / 1000.0) if charging_w > 0 else (
                ev.get("power_w", 7000) / 1000.0
            )
            if charging_kw <= 0:
                charging_kw = 7.0  # fallback

            total_kwh_needed += kwh_needed
            total_charging_kw += charging_kw

            vehicle_plans.append({
                "name": ev.get("name", "ev"),
                "soc": round(soc, 1),
                "target_soc": round(target, 1),
                "capacity_kwh": round(capacity, 1),
                "kwh_needed": round(kwh_needed, 1),
                "charging_power_kw": round(charging_kw, 1),
            })

        if not vehicle_plans or total_kwh_needed <= 0:
            empty_schedule["vehicles"] = vehicle_plans
            return empty_schedule

        hours_needed = total_kwh_needed / total_charging_kw if total_charging_kw > 0 else 0

        # Candidate hours: all plan hours ranked by price, cheapest first.
        # Exclude discharge_battery hours (we want to sell to grid then).
        candidates = []
        for i, entry in enumerate(hourly_plan):
            if entry["action"] == ACTION_DISCHARGE_BATTERY:
                continue
            candidates.append((i, entry["price"]))
        # Sort by night-adjusted price to minimize grid consumption:
        # Night hours (off-peak) get a price bonus, making them preferred
        # over daytime hours when no solar surplus is available.
        def _night_adjusted_price(candidate: tuple) -> float:
            idx, price = candidate
            hour = hourly_plan[idx]["hour"]
            if hour >= self.ev_night_start or hour < self.ev_night_end:
                return price - self.ev_night_preference
            return price

        candidates.sort(key=_night_adjusted_price)

        # Build the schedule
        schedule = [
            {
                "hour": entry["hour"],
                "price": entry["price"],
                "charging": False,
                "power_kw": 0.0,
            }
            for entry in hourly_plan
        ]

        remaining_kwh = total_kwh_needed
        for idx, _price in candidates:
            if remaining_kwh <= 0:
                break
            charge_kwh = min(total_charging_kw, remaining_kwh)
            schedule[idx]["charging"] = True
            schedule[idx]["power_kw"] = round(charge_kwh, 2)
            remaining_kwh -= charge_kwh

        _LOGGER.debug(
            "EV charge plan: %.1f kWh needed, %.1f kW power, "
            "%.1f hours, scheduled %d hours",
            total_kwh_needed,
            total_charging_kw,
            hours_needed,
            len([s for s in schedule if s["charging"]]),
        )

        return {
            "schedule": schedule,
            "total_kwh_needed": round(total_kwh_needed, 1),
            "total_charging_power_kw": round(total_charging_kw, 1),
            "hours_needed": round(hours_needed, 1),
            "vehicles": vehicle_plans,
            "start_hour": start_hour,
        }

    # ------------------------------------------------------------------
    # SoC simulation
    # ------------------------------------------------------------------

    def _simulate_soc_change(self, soc: float, action: str) -> float:
        """Rough SoC simulation for planning purposes."""
        # Assume ~2 kW charge/discharge rate per hour as % of capacity
        delta_pct = (2.0 / self.battery_capacity) * 100
        # Surplus absorption is slower — roughly half of forced charge
        surplus_delta_pct = (1.0 / self.battery_capacity) * 100

        if action == ACTION_CHARGE_BATTERY:
            return min(self.max_soc, soc + delta_pct)
        elif action == ACTION_MAXIMIZE_LOAD:
            # Self-consumption: battery absorbs surplus only (not grid)
            return min(self.max_soc, soc + surplus_delta_pct)
        elif action in (ACTION_DISCHARGE_BATTERY, ACTION_PRE_DISCHARGE):
            return max(self.min_soc, soc - delta_pct)
        return soc

    # ------------------------------------------------------------------
    # Service call builder
    # ------------------------------------------------------------------

    def _build_immediate_actions(
        self,
        action: str,
        ev_connected: bool,
        current_price: float = 0.0,
        avg_price: float = 0.0,
        min_price: float = 0.0,
        price_spread: float = 0.0,
        grid_export_w: float = 0.0,
        ev_vehicles: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert the current hour's action into HA service calls.

        Battery actions follow the optimizer's hourly plan directly.
        EV charger actions are **decoupled** from the battery action
        and decided per-charger by price / solar / vehicle SoC:

        Objective: **minimize grid energy consumption**.

          - Vehicle at target SoC → STOP (ramp-down)
          - Negative price → EVs ON (unless already at full target)
          - Price ≤ ev_cheap_price_threshold → EVs ON (cheap energy)
          - Grid export ≥ solar_surplus_threshold → EVs ON (absorb surplus)
          - Expensive hour → EVs OFF
          - Otherwise → no EV change

        Weekend: target SoC is lowered (car parked at home, will
        charge from daytime solar surplus instead of grid).
        """
        actions = []
        sg_out = self.outputs.get(OUTPUT_SUNGROW, {})

        # --- Battery actions (master inverter only, slave follows) ---
        if self.enable_battery:
            if action == ACTION_MAXIMIZE_LOAD:
                # Negative price → self-consumption mode.
                # The battery absorbs any solar surplus (prevents grid
                # export at negative prices) but does NOT force-charge
                # from the grid.  EVs handle the load maximisation.
                cfg = sg_out.get("self_consumption", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {},
                    })

            elif action == ACTION_PRE_DISCHARGE:
                # Discharge battery to make room before negative prices
                cfg = sg_out.get("force_discharge", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {},
                    })
                # Ramp discharge power to maximum
                pwr_cfg = sg_out.get("set_discharge_power", {})
                if pwr_cfg.get("service") and pwr_cfg.get("entity_id"):
                    actions.append({
                        "service": pwr_cfg["service"],
                        "entity_id": pwr_cfg["entity_id"],
                        "data": {"value": pwr_cfg.get("max", 5000)},
                    })

            elif action == ACTION_CHARGE_BATTERY:
                cfg = sg_out.get("force_charge", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {},
                    })

            elif action == ACTION_DISCHARGE_BATTERY:
                cfg = sg_out.get("force_discharge", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {},
                    })

            else:
                cfg = sg_out.get("self_consumption", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {},
                    })

        # ---------------------------------------------------------------
        # EV charger actions — DECOUPLED from battery action.
        #
        # Objective: MINIMIZE GRID ENERGY CONSUMPTION.
        # Per-charger decision with ramp-down when vehicle is at target.
        # Weekend: lower target SoC (car home all day, solar later).
        # ---------------------------------------------------------------
        ev_chargers = self.ev_chargers_cfg if isinstance(self.ev_chargers_cfg, list) else []

        if not self.enable_charger or not ev_chargers:
            return actions

        # Compute cheap threshold: absolute or relative, whichever is higher.
        if price_spread > 0:
            relative_cheap = min_price + price_spread * 0.30
            cheap_threshold = max(self.ev_cheap_price_threshold, relative_cheap)
        else:
            cheap_threshold = self.ev_cheap_price_threshold

        price_is_cheap = current_price <= cheap_threshold
        price_is_negative = current_price < 0
        solar_surplus = grid_export_w >= self.solar_surplus_threshold
        price_is_expensive = action == ACTION_DISCHARGE_BATTERY

        # Build vehicle lookup for ramp-down + per-charger SoC checks
        vehicle_map: dict[str, dict[str, Any]] = {}
        for v in (ev_vehicles or []):
            vehicle_map[v.get("name", "")] = v

        now = datetime.now()
        is_weekend = now.weekday() >= 5  # Saturday=5, Sunday=6

        for charger_cfg in ev_chargers:
            charger_name = charger_cfg.get("name", "")
            vehicle = vehicle_map.get(charger_name)

            # Per-vehicle connected state; fall back to global ev_connected
            charger_connected = ev_connected
            if vehicle:
                charger_connected = vehicle.get("connected", ev_connected)

            # --- RAMP-DOWN: stop if vehicle reports SoC >= target ---
            if vehicle:
                vehicle_soc = vehicle.get("vehicle_soc", 0)
                vehicle_target = vehicle.get("vehicle_target_soc", 100)

                # Weekend: lower target (car parked at home all day)
                effective_target = vehicle_target
                if is_weekend and self.ev_weekend_target_soc < vehicle_target:
                    effective_target = self.ev_weekend_target_soc

                if vehicle_soc > 0 and vehicle_soc >= effective_target:
                    # Exception: during negative prices, charge to full
                    # vehicle target (we get paid to consume electricity)
                    if price_is_negative and vehicle_soc < vehicle_target:
                        pass  # Don't stop — exploit negative prices
                    else:
                        _LOGGER.info(
                            "EV %s: SoC %.0f%% >= target %.0f%% — "
                            "stopping charger (ramp-down)",
                            charger_name, vehicle_soc, effective_target,
                        )
                        stop_cfg = charger_cfg.get("stop_charging", {})
                        if stop_cfg.get("service"):
                            actions.append({
                                "service": stop_cfg["service"],
                                "entity_id": stop_cfg["entity_id"],
                                "data": {},
                            })
                        continue  # Skip price logic for this charger

            # --- Price-based charging decision ---
            if price_is_negative:
                _LOGGER.info(
                    "EV %s: Negative price (%.3f) — charging",
                    charger_name, current_price,
                )
                start_cfg = charger_cfg.get("start_charging", {})
                if start_cfg.get("service"):
                    actions.append({
                        "service": start_cfg["service"],
                        "entity_id": start_cfg["entity_id"],
                        "data": {},
                    })

            elif charger_connected and price_is_cheap:
                _LOGGER.info(
                    "EV %s: Cheap price (%.3f ≤ %.3f) — charging",
                    charger_name, current_price, cheap_threshold,
                )
                start_cfg = charger_cfg.get("start_charging", {})
                if start_cfg.get("service"):
                    actions.append({
                        "service": start_cfg["service"],
                        "entity_id": start_cfg["entity_id"],
                        "data": {},
                    })

            elif charger_connected and solar_surplus and not price_is_expensive:
                _LOGGER.info(
                    "EV %s: Solar surplus (%.0f W) — charging",
                    charger_name, grid_export_w,
                )
                start_cfg = charger_cfg.get("start_charging", {})
                if start_cfg.get("service"):
                    actions.append({
                        "service": start_cfg["service"],
                        "entity_id": start_cfg["entity_id"],
                        "data": {},
                    })

            elif charger_connected and price_is_expensive:
                _LOGGER.info(
                    "EV %s: Expensive price (%.3f) — stopping",
                    charger_name, current_price,
                )
                stop_cfg = charger_cfg.get("stop_charging", {})
                if stop_cfg.get("service"):
                    actions.append({
                        "service": stop_cfg["service"],
                        "entity_id": stop_cfg["entity_id"],
                        "data": {},
                    })

            # else: no EV action (mid-range price, no surplus)

        return actions

    def _safe_default_schedule(self, now: datetime) -> dict:
        """Return a safe default when no price data is available."""
        return {
            "hourly_plan": [{
                "hour": now.hour,
                "action": ACTION_SELF_CONSUMPTION,
                "reason": "No price data available",
                "price": 0,
                "predicted_consumption_kwh": 0,
            }],
            "immediate_actions": [],
            "summary": "No price data available — defaulting to self-consumption",
            "stats": {"avg_price": 0, "min_price": 0, "max_price": 0, "price_spread": 0},
        }
