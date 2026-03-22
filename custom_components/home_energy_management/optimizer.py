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
    ) -> dict[str, Any]:
        """Produce an hour-by-hour schedule and immediate actions.

        Parameters
        ----------
        grid_export_power : float
            Current grid export in Watts.  Used to detect solar surplus
            so EVs can absorb excess production instead of exporting
            at rock-bottom prices.

        Returns
        -------
        dict with keys:
          - hourly_plan: list of {hour, action, reason, price}
          - immediate_actions: list of service calls to execute now
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
            )

        # Summary
        neg_hours = [h for h in hourly_plan if h["action"] == ACTION_MAXIMIZE_LOAD]
        pre_dis_hours = [h for h in hourly_plan if h["action"] == ACTION_PRE_DISCHARGE]
        cheap_hours = [h for h in hourly_plan if h["action"] == ACTION_CHARGE_BATTERY]
        expensive_hours = [h for h in hourly_plan if h["action"] == ACTION_DISCHARGE_BATTERY]

        summary = (
            f"Plan: {len(neg_hours)} negative-price (maximize), "
            f"{len(pre_dis_hours)} pre-discharge, "
            f"{len(cheap_hours)} charge, "
            f"{len(expensive_hours)} discharge hours. "
            f"Price range: {min_price:.2f}–{max_price:.2f} {prices.get('currency', 'SEK')}/kWh. "
            f"Spread: {price_spread:.2f}."
        )

        return {
            "hourly_plan": hourly_plan,
            "immediate_actions": immediate_actions,
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
    ) -> list[dict[str, Any]]:
        """Convert the current hour's action into HA service calls.

        Battery actions follow the optimizer's hourly plan directly.
        EV charger actions are **decoupled** from the battery action
        and decided by price / solar surplus:

          - Negative price (maximize_load) → ALL EVs ON regardless
          - Price ≤ ev_cheap_price_threshold → EVs ON (absorb cheap energy)
          - Grid export ≥ solar_surplus_threshold → EVs ON (absorb surplus)
          - Expensive hour (discharge_battery) → EVs OFF
          - Otherwise → no EV change
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
        # The EVs make their own decision based on price and solar surplus.
        # Rationale: during pre-discharge the battery empties itself, but
        # any excess solar or cheap grid energy should still flow into the
        # EVs instead of being exported for pennies.
        # ---------------------------------------------------------------
        ev_chargers = self.ev_chargers_cfg if isinstance(self.ev_chargers_cfg, list) else []

        if not self.enable_charger or not ev_chargers:
            return actions

        # Compute cheap threshold: absolute or relative, whichever is higher.
        # This ensures EVs charge when electricity is objectively cheap
        # (below ev_cheap_price_threshold) OR relatively cheap within
        # today's range (bottom 30%).
        if price_spread > 0:
            relative_cheap = min_price + price_spread * 0.30
            cheap_threshold = max(self.ev_cheap_price_threshold, relative_cheap)
        else:
            # No price variation — use absolute threshold only
            cheap_threshold = self.ev_cheap_price_threshold

        # Determine whether to charge EVs
        price_is_cheap = current_price <= cheap_threshold
        price_is_negative = current_price < 0
        solar_surplus = grid_export_w >= self.solar_surplus_threshold
        price_is_expensive = action == ACTION_DISCHARGE_BATTERY

        if price_is_negative:
            # Negative price → ALL chargers ON regardless of ev_connected
            _LOGGER.info(
                "EV: Negative price (%.3f) — turning on ALL chargers",
                current_price,
            )
            for charger_cfg in ev_chargers:
                start_cfg = charger_cfg.get("start_charging", {})
                if start_cfg.get("service"):
                    actions.append({
                        "service": start_cfg["service"],
                        "entity_id": start_cfg["entity_id"],
                        "data": {},
                    })

        elif ev_connected and price_is_cheap:
            # Cheap price → charge EVs (even during pre_discharge or self_consumption)
            _LOGGER.info(
                "EV: Cheap price (%.3f ≤ %.3f) — charging EVs",
                current_price,
                cheap_threshold,
            )
            for charger_cfg in ev_chargers:
                start_cfg = charger_cfg.get("start_charging", {})
                if start_cfg.get("service"):
                    actions.append({
                        "service": start_cfg["service"],
                        "entity_id": start_cfg["entity_id"],
                        "data": {},
                    })

        elif ev_connected and solar_surplus and not price_is_expensive:
            # Exporting a lot of power → let EVs absorb the surplus
            _LOGGER.info(
                "EV: Solar surplus (%.0f W export) — charging EVs",
                grid_export_w,
            )
            for charger_cfg in ev_chargers:
                start_cfg = charger_cfg.get("start_charging", {})
                if start_cfg.get("service"):
                    actions.append({
                        "service": start_cfg["service"],
                        "entity_id": start_cfg["entity_id"],
                        "data": {},
                    })

        elif ev_connected and price_is_expensive:
            # Expensive hour → stop EV charging to preserve battery/grid savings
            _LOGGER.info(
                "EV: Expensive price (%.3f) — stopping EVs",
                current_price,
            )
            for charger_cfg in ev_chargers:
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
