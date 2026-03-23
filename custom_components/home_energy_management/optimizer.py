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
  - **Expensive hours**: Self-consumption mode — the inverter covers
    house load from battery automatically, avoiding grid import.
    This is more efficient and gentler on the battery than forced
    discharge, which would dump power to the grid unnecessarily.
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
    DEFAULT_EV_DEPARTURE_TIME,
    DEFAULT_EV_MIN_CHARGE_LEVEL,
    DEFAULT_EV_MIN_DEPARTURE_SOC,
    DEFAULT_EV_OPTIMIZATION_WINDOW,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_GRID_CHARGE_MAX_PRICE,
    DEFAULT_GRID_CHARGE_MAX_SOC,
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

        # Grid charge limits — only pull from grid up to this SoC,
        # and only when price is below the absolute cap.
        self.grid_charge_max_soc = params.get(
            "grid_charge_max_soc", DEFAULT_GRID_CHARGE_MAX_SOC
        )
        self.grid_charge_max_price = params.get(
            "grid_charge_max_price", DEFAULT_GRID_CHARGE_MAX_PRICE
        )

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
        self.ev_default_target_soc = params.get(
            "ev_default_target_soc", DEFAULT_EV_TARGET_SOC
        )
        self.ev_optimization_window = params.get(
            "ev_optimization_window", DEFAULT_EV_OPTIMIZATION_WINDOW
        )
        self.ev_default_departure_time = params.get(
            "ev_default_departure_time", DEFAULT_EV_DEPARTURE_TIME
        )
        self.ev_default_min_departure_soc = params.get(
            "ev_default_min_departure_soc", DEFAULT_EV_MIN_DEPARTURE_SOC
        )
        self.ev_default_min_charge_level = params.get(
            "ev_default_min_charge_level", DEFAULT_EV_MIN_CHARGE_LEVEL
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
        # When 2-day optimisation is enabled, extend the plan with
        # day-2 price entries so EV charging can be deferred to
        # cheaper hours tomorrow.  The battery plan is unaffected.
        ev_plan_for_scheduling = hourly_plan
        near_term_hours = len(hourly_plan)

        if self.ev_optimization_window >= 2:
            extended_prices = all_prices[current_hour : current_hour + 48]
            if len(extended_prices) > len(hourly_plan):
                ev_plan_for_scheduling = list(hourly_plan)  # copy
                for i in range(len(hourly_plan), len(extended_prices)):
                    hour = (current_hour + i) % 24
                    ev_plan_for_scheduling.append({
                        "hour": hour,
                        "action": ACTION_SELF_CONSUMPTION,
                        "reason": "Extended window (day 2)",
                        "price": round(extended_prices[i], 4),
                        "predicted_consumption_kwh": 0,
                    })

        ev_charge_plan = self._plan_ev_charging(
            ev_plan_for_scheduling, ev_vehicles or [], current_hour,
            near_term_hours=near_term_hours,
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
                ev_charge_plan=ev_charge_plan,
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

        # Grid charging is limited:
        #   - Price must be below the absolute cap (grid_charge_max_price)
        #   - SoC must be below the grid charge target (grid_charge_max_soc)
        # This prevents filling the battery from the grid when solar
        # can do it for free during the day.  We only pull enough to
        # survive the expensive morning/evening peaks.
        if (
            self.enable_battery
            and price <= cheap_threshold
            and price <= self.grid_charge_max_price
            and battery_soc < self.grid_charge_max_soc
        ):
            return (
                ACTION_CHARGE_BATTERY,
                f"Cheap price ({price:.2f} ≤ {self.grid_charge_max_price:.2f}), "
                f"charging battery (SoC {battery_soc:.0f}% → {self.grid_charge_max_soc}%)",
            )

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

        if len(discharge_indices) == 0:
            return hourly_plan

        # Sort by price descending → most expensive first
        discharge_indices.sort(key=lambda x: x[1], reverse=True)

        # Calculate available energy (kWh) the battery can deliver
        available_kwh = (initial_soc - self.min_soc) / 100.0 * self.battery_capacity
        if available_kwh <= 0:
            # Nothing to discharge — downgrade ALL discharge hours
            for idx, _ in discharge_indices:
                hourly_plan[idx]["action"] = ACTION_SELF_CONSUMPTION
                hourly_plan[idx]["reason"] = (
                    f"Battery depleted (SoC {initial_soc:.0f}%) — "
                    f"self-consumption (was discharge at {hourly_plan[idx]['price']:.2f})"
                )
            return hourly_plan

        if len(discharge_indices) == 1:
            # Single discharge hour with available energy — keep it
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
        near_term_hours: int | None = None,
    ) -> dict[str, Any]:
        """Plan EV charging per vehicle to minimize grid energy consumption.

        Strategy per vehicle:
          1. Calculate kWh needed (SoC → ``ev_default_target_soc``).
          2. On weekends, lower the target SoC — car is home all day
             and can top up from solar surplus during daytime.
          3. Schedule charging in cheapest available hours with night
             preference (off-peak hours are less grid-intensive).
          4. Exclude ``discharge_battery`` hours (selling to grid).
          5. When min_charge_level is set and SoC is below the floor,
             urgently schedule enough in near-term hours to reach the
             floor.  Remaining charge uses cheapest hours across the
             full window (possibly day 2 when optimization_days=2).
          6. When optimization_window ≥ 2 and min_charge_level > 0,
             the deferred portion (floor → target) is NOT constrained
             by departure_time — the car already has min_charge_level
             for the next trip and can wait for the cheapest prices
             anywhere in the extended window.

        Parameters
        ----------
        near_term_hours : int | None
            Number of entries from the start of hourly_plan that belong
            to the original (battery) planning horizon.  Used to split
            urgent vs deferred EV charging.  None means all entries are
            near-term (single-day optimisation).

        Returns a dict with:
          - schedule: per-hour list with per-vehicle kWh breakdown
          - total_kwh_needed, total_charging_power_kw, hours_needed
          - vehicles: per-vehicle summary with scheduled_hours
        """
        # Build base schedule
        schedule = [
            {
                "hour": entry["hour"],
                "price": entry["price"],
                "charging": False,
                "total_power_kw": 0.0,
                "vehicles": {},
            }
            for entry in hourly_plan
        ]

        empty_result = {
            "schedule": schedule,
            "total_kwh_needed": 0,
            "total_charging_power_kw": 0,
            "hours_needed": 0,
            "vehicles": [],
            "start_hour": start_hour,
        }

        if not ev_vehicles:
            return empty_result

        now_dt = datetime.now()
        is_friday = now_dt.weekday() == 4  # Friday=4 — lower target so Sat starts lower

        # Build candidate hours (shared across vehicles):
        # all plan hours except discharge_battery, ranked by night-adjusted price.
        all_candidates = []
        for i, entry in enumerate(hourly_plan):
            if entry["action"] == ACTION_DISCHARGE_BATTERY:
                continue
            all_candidates.append((i, entry["price"], entry["hour"]))

        def _night_adjusted(candidate: tuple) -> float:
            idx, price, hour = candidate
            if hour >= self.ev_night_start or hour < self.ev_night_end:
                return price - self.ev_night_preference
            return price

        all_candidates.sort(key=_night_adjusted)

        # Helper: parse "HH:MM" → hour int for deadline filtering
        def _parse_departure(dep_str: str) -> int:
            """Return the deadline hour from 'HH:MM' string."""
            try:
                parts = dep_str.split(":")
                return int(parts[0])
            except (ValueError, IndexError, AttributeError):
                return self.ev_night_end

        # Helper: check if a plan-hour falls before the departure deadline.
        # Hours wrap around midnight (start_hour → 23 → 0 → departure).
        def _is_before_departure(hour: int, departure_hour: int) -> bool:
            """True if *hour* falls in the charging window before departure."""
            if departure_hour > start_hour:
                # Same-day window: start_hour .. departure_hour
                return start_hour <= hour < departure_hour
            else:
                # Crosses midnight: start_hour..23, 0..departure_hour
                return hour >= start_hour or hour < departure_hour

        # Per-vehicle scheduling
        vehicle_plans = []

        # Near-term boundary: entries from the original planning horizon.
        # Hours beyond this boundary are day-2 extension slots.
        boundary = near_term_hours if near_term_hours is not None else len(hourly_plan)

        for ev in ev_vehicles:
            name = ev.get("name", "ev")
            soc = ev.get("vehicle_soc", 0)
            capacity = ev.get("vehicle_capacity_kwh", 0)
            charging_w = ev.get("vehicle_charging_power_w", 0)
            connected = ev.get("connected", False)

            # Per-vehicle departure config (from variable_mapping).
            # departure_time is optional — when absent or empty, all
            # candidate hours are eligible (no deadline filtering).
            departure_str = ev.get("departure_time") or ""
            departure_hour = _parse_departure(departure_str) if departure_str else None
            min_dep_soc = ev.get(
                "min_departure_soc", self.ev_default_min_departure_soc
            )

            # Min charge level: SoC floor the car must maintain.
            # When soc < floor, urgent charging is scheduled in
            # near-term hours regardless of day-2 prices.
            min_charge_level = ev.get(
                "min_charge_level", self.ev_default_min_charge_level
            )

            # Target SoC: use per-vehicle min_departure_soc if set,
            # otherwise fall back to the global default target.
            target = min_dep_soc if min_dep_soc > 0 else self.ev_default_target_soc

            # Friday: lower target so Saturday starts lower (solar fills rest)
            if is_friday and self.ev_weekend_target_soc < target:
                target = self.ev_weekend_target_soc

            # Determine charging power (kW)
            # Use vehicle_charging_power_w when connected, else fall
            # back to the charger's rated power (power_w) for planning.
            charging_kw = (charging_w / 1000.0) if charging_w > 0 else (
                ev.get("power_w", 7000) / 1000.0
            )
            if charging_kw <= 0:
                charging_kw = 7.0  # fallback

            # Schedule any vehicle that needs energy — even if
            # disconnected.  The schedule is a *plan*; immediate
            # actions still gate on the connected state.
            needs_charge = (
                soc > 0
                and capacity > 0
                and soc < target
            )

            if not needs_charge:
                vehicle_plans.append({
                    "name": name,
                    "soc": round(soc, 1),
                    "target_soc": round(target, 1),
                    "capacity_kwh": round(capacity, 1),
                    "kwh_needed": 0,
                    "charging_power_kw": round(charging_kw, 1) if connected else 0,
                    "hours_needed": 0,
                    "connected": connected,
                    "scheduled_hours": [],
                    "departure_time": departure_str,
                    "min_departure_soc": min_dep_soc,
                    "min_charge_level": min_charge_level,
                })
                continue

            kwh_needed = (target - soc) / 100.0 * capacity

            # Filter candidates to hours before this vehicle's departure
            # (only when a departure time is explicitly configured).
            if departure_hour is not None:
                vehicle_candidates = [
                    (idx, price, hour)
                    for idx, price, hour in all_candidates
                    if _is_before_departure(hour, departure_hour)
                ]
            else:
                vehicle_candidates = all_candidates

            # --- Two-pass scheduling for min_charge_level ---
            # When the vehicle is below the floor, we must charge
            # urgently (near-term) to reach the floor, then defer
            # the rest (floor→target) to cheapest hours across the
            # full window (which may include day-2 when opt_days=2).
            #
            # Urgent pass: schedule CHRONOLOGICALLY (earliest first)
            # so the car reaches min_charge_level as fast as possible,
            # regardless of price.
            #
            # Deferred pass: schedule by CHEAPEST price across the
            # full optimisation window (including day-2 when enabled).
            # When optimization_window >= 2 and min_charge_level > 0,
            # the deferred portion is NOT constrained by departure_time
            # — the car already has min_charge_level for the next trip
            # and the rest can wait for the cheapest prices anywhere
            # in the extended window (including hours after departure).
            scheduled_hours = []
            used_indices: set[int] = set()

            # When the plan was extended beyond the near-term boundary,
            # the deferred/opportunistic portion can use the full
            # window without departure filtering.
            has_extended_window = boundary < len(hourly_plan)
            deferred_pool = (
                all_candidates
                if has_extended_window and min_charge_level > 0
                else vehicle_candidates
            )

            if min_charge_level > 0 and soc < min_charge_level:
                urgent_kwh = (min_charge_level - soc) / 100.0 * capacity
                deferred_kwh = max(0, kwh_needed - urgent_kwh)

                _LOGGER.debug(
                    "EV %s: two-pass scheduling — urgent %.1f kWh "
                    "(SoC %.0f%% → floor %.0f%%), deferred %.1f kWh "
                    "(floor → target %.0f%%), extended_window=%s",
                    name, urgent_kwh, soc, min_charge_level,
                    deferred_kwh, target, has_extended_window,
                )

                # Pass 1: urgent — near-term candidates sorted by
                # INDEX (chronological) so charging starts ASAP.
                near_candidates = [
                    (idx, p, h) for idx, p, h in vehicle_candidates
                    if idx < boundary
                ]
                near_candidates.sort(key=lambda c: c[0])

                remaining = urgent_kwh
                for idx, _price, _hour in near_candidates:
                    if remaining <= 0:
                        break
                    charge_kwh = min(charging_kw, remaining)
                    schedule[idx]["vehicles"][name] = round(charge_kwh, 2)
                    schedule[idx]["total_power_kw"] += charge_kwh
                    schedule[idx]["charging"] = True
                    scheduled_hours.append(hourly_plan[idx]["hour"])
                    used_indices.add(idx)
                    remaining -= charge_kwh

                # Pass 2: deferred — cheapest across full window.
                # When extended window is active, this pool includes
                # ALL hours (no departure filter) so the car can
                # charge at the cheapest prices across the full
                # optimization window.
                remaining = deferred_kwh
                for idx, _price, _hour in deferred_pool:
                    if remaining <= 0:
                        break
                    if idx in used_indices:
                        continue
                    charge_kwh = min(charging_kw, remaining)
                    schedule[idx]["vehicles"][name] = round(charge_kwh, 2)
                    schedule[idx]["total_power_kw"] += charge_kwh
                    schedule[idx]["charging"] = True
                    scheduled_hours.append(hourly_plan[idx]["hour"])
                    used_indices.add(idx)
                    remaining -= charge_kwh
            elif min_charge_level > 0:
                # SoC is already above the floor — all remaining
                # charge is "deferred" and can use the full window
                # when extended (no departure filter needed since
                # the car already meets the minimum for driving).
                remaining = kwh_needed
                for idx, _price, _hour in deferred_pool:
                    if remaining <= 0:
                        break
                    charge_kwh = min(charging_kw, remaining)
                    schedule[idx]["vehicles"][name] = round(charge_kwh, 2)
                    schedule[idx]["total_power_kw"] += charge_kwh
                    schedule[idx]["charging"] = True
                    scheduled_hours.append(hourly_plan[idx]["hour"])
                    remaining -= charge_kwh
            else:
                # No floor set — normal departure-filtered scheduling
                remaining = kwh_needed
                for idx, _price, _hour in vehicle_candidates:
                    if remaining <= 0:
                        break
                    charge_kwh = min(charging_kw, remaining)
                    schedule[idx]["vehicles"][name] = round(charge_kwh, 2)
                    schedule[idx]["total_power_kw"] += charge_kwh
                    schedule[idx]["charging"] = True
                    scheduled_hours.append(hourly_plan[idx]["hour"])
                    remaining -= charge_kwh

            hours_needed_f = kwh_needed / charging_kw if charging_kw > 0 else 0

            vehicle_plans.append({
                "name": name,
                "soc": round(soc, 1),
                "target_soc": round(target, 1),
                "capacity_kwh": round(capacity, 1),
                "kwh_needed": round(kwh_needed, 1),
                "charging_power_kw": round(charging_kw, 1),
                "hours_needed": round(hours_needed_f, 1),
                "connected": connected,
                "scheduled_hours": sorted(scheduled_hours),
                "departure_time": departure_str,
                "min_departure_soc": min_dep_soc,
                "min_charge_level": min_charge_level,
            })

        # Round totals in schedule
        for entry in schedule:
            entry["total_power_kw"] = round(entry["total_power_kw"], 2)

        total_kwh = sum(v["kwh_needed"] for v in vehicle_plans)
        total_kw = sum(v["charging_power_kw"] for v in vehicle_plans if v["kwh_needed"] > 0)

        _LOGGER.debug(
            "EV charge plan: %d vehicles, %.1f kWh needed, %.1f kW total, "
            "scheduled %d hours",
            len(vehicle_plans),
            total_kwh,
            total_kw,
            len([s for s in schedule if s["charging"]]),
        )

        return {
            "schedule": schedule,
            "total_kwh_needed": round(total_kwh, 1),
            "total_charging_power_kw": round(total_kw, 1),
            "hours_needed": round(total_kwh / total_kw, 1) if total_kw > 0 else 0,
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
            # Grid charging caps at grid_charge_max_soc (solar fills the rest)
            return min(self.grid_charge_max_soc, soc + delta_pct)
        elif action == ACTION_MAXIMIZE_LOAD:
            # Self-consumption: battery absorbs surplus only (not grid)
            return min(self.max_soc, soc + surplus_delta_pct)
        elif action in (ACTION_DISCHARGE_BATTERY, ACTION_PRE_DISCHARGE):
            return max(self.min_soc, soc - delta_pct)
        return soc

    # ------------------------------------------------------------------
    # Service call builder
    # ------------------------------------------------------------------

    @staticmethod
    def _stop_forced_cmd(sg_out: dict[str, Any]) -> list[dict[str, Any]]:
        """Build action(s) to reset the forced charge/discharge command to Stop.

        When switching from a forced mode (charge/discharge) to
        self-consumption, the Sungrow inverter may keep the previous
        forced command latched.  Explicitly setting the input_select
        to "Stop (default)" clears it so self-consumption works.
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
        ev_charge_plan: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert the current hour's action into HA service calls.

        Battery actions follow the optimizer's hourly plan directly.
        EV charger actions are driven by the pre-computed EV charge
        schedule (cheapest hours, night preference, per-vehicle kWh).

        Overrides (real-time):
          - Vehicle at target SoC → STOP (ramp-down)
          - Negative price → EVs ON (charge everything)
          - Solar surplus → EVs ON (absorb free energy)
          - Scheduled this hour → EVs ON
          - Not scheduled + expensive → EVs OFF
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
                # Explicitly clear forced charge/discharge command
                actions.extend(self._stop_forced_cmd(sg_out))
                # Reset forced power to 0 (not in forced mode)
                pwr_cfg = sg_out.get("set_forced_power", {})
                if pwr_cfg.get("service") and pwr_cfg.get("entity_id"):
                    actions.append({
                        "service": pwr_cfg["service"],
                        "entity_id": pwr_cfg["entity_id"],
                        "data": {"value": 0},
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
                # Set forced charge/discharge power to maximum
                pwr_cfg = sg_out.get("set_forced_power", {})
                if pwr_cfg.get("service") and pwr_cfg.get("entity_id"):
                    actions.append({
                        "service": pwr_cfg["service"],
                        "entity_id": pwr_cfg["entity_id"],
                        "data": {"value": pwr_cfg.get("max", 5000)},
                    })
                # Also set max discharge power limit
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
                # Set forced charge/discharge power to maximum
                pwr_cfg = sg_out.get("set_forced_power", {})
                if pwr_cfg.get("service") and pwr_cfg.get("entity_id"):
                    actions.append({
                        "service": pwr_cfg["service"],
                        "entity_id": pwr_cfg["entity_id"],
                        "data": {"value": pwr_cfg.get("max", 5000)},
                    })

            elif action == ACTION_DISCHARGE_BATTERY:
                # Expensive hour → self-consumption mode.
                # The inverter dynamically covers house load from the
                # battery (second-by-second), avoiding grid import.
                # This is gentler on the battery than forced discharge
                # and avoids dumping excess power to the grid.
                cfg = sg_out.get("self_consumption", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {},
                    })
                # Explicitly clear forced charge/discharge command
                actions.extend(self._stop_forced_cmd(sg_out))
                # Reset forced power to 0 (self-consumption handles it)
                pwr_cfg = sg_out.get("set_forced_power", {})
                if pwr_cfg.get("service") and pwr_cfg.get("entity_id"):
                    actions.append({
                        "service": pwr_cfg["service"],
                        "entity_id": pwr_cfg["entity_id"],
                        "data": {"value": 0},
                    })

            else:
                cfg = sg_out.get("self_consumption", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {},
                    })
                # Explicitly clear forced charge/discharge command
                actions.extend(self._stop_forced_cmd(sg_out))
                # Reset forced power to 0 (not in forced mode)
                pwr_cfg = sg_out.get("set_forced_power", {})
                if pwr_cfg.get("service") and pwr_cfg.get("entity_id"):
                    actions.append({
                        "service": pwr_cfg["service"],
                        "entity_id": pwr_cfg["entity_id"],
                        "data": {"value": 0},
                    })

        # ---------------------------------------------------------------
        # EV charger actions — driven by the pre-computed schedule.
        #
        # The schedule already optimised for cheapest hours with night
        # preference.  Real-time overrides: ramp-down (vehicle at
        # target), negative prices, and solar surplus.
        # ---------------------------------------------------------------
        ev_chargers = self.ev_chargers_cfg if isinstance(self.ev_chargers_cfg, list) else []

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

        # Build vehicle lookup for ramp-down + per-charger SoC checks
        vehicle_map: dict[str, dict[str, Any]] = {}
        for v in (ev_vehicles or []):
            vehicle_map[v.get("name", "")] = v

        now = datetime.now()
        is_friday = now.weekday() == 4  # Friday=4

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

                # Per-vehicle departure target (matches the schedule)
                min_dep_soc = vehicle.get(
                    "min_departure_soc", self.ev_default_min_departure_soc
                )
                effective_target = min_dep_soc if min_dep_soc > 0 else self.ev_default_target_soc
                if is_friday and self.ev_weekend_target_soc < effective_target:
                    effective_target = self.ev_weekend_target_soc

                if vehicle_soc > 0 and vehicle_soc >= effective_target:
                    # Exception: during negative prices, charge to full
                    # capacity (we get paid to consume electricity)
                    vehicle_target = vehicle.get("vehicle_target_soc", 100)
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
                        continue  # Skip further logic for this charger

            # --- Schedule-based + real-time override decisions ---
            is_scheduled = charger_name in scheduled_vehicles

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

            elif charger_connected and is_scheduled:
                _LOGGER.info(
                    "EV %s: Scheduled for charging this hour (%.2f kWh)",
                    charger_name, scheduled_vehicles.get(charger_name, 0),
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

            elif charger_connected and not is_scheduled:
                # Not scheduled and not a special case → stop to save grid
                _LOGGER.info(
                    "EV %s: Not scheduled this hour — stopping",
                    charger_name, 
                )
                stop_cfg = charger_cfg.get("stop_charging", {})
                if stop_cfg.get("service"):
                    actions.append({
                        "service": stop_cfg["service"],
                        "entity_id": stop_cfg["entity_id"],
                        "data": {},
                    })

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
