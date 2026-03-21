"""Optimizer — plans charge/discharge schedule based on energy prices.

The optimizer takes Nordpool hourly prices, predicted consumption,
battery state, and EV connection status, and produces an hour-by-hour
schedule of actions (charge battery, discharge, start EV, etc.).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .const import (
    ACTION_CHARGE_BATTERY,
    ACTION_DISCHARGE_BATTERY,
    ACTION_SELF_CONSUMPTION,
    ACTION_SET_EV_AMPS,
    ACTION_START_EV_CHARGE,
    ACTION_STOP_EV_CHARGE,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_MAX_AMPS,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_AMPS,
    DEFAULT_MIN_PRICE_SPREAD,
    DEFAULT_MIN_SOC,
    DEFAULT_PLANNING_HORIZON,
    OUTPUT_EASEE,
    OUTPUT_SUNGROW,
)

_LOGGER = logging.getLogger(__name__)


class Optimizer:
    """Price-aware energy optimization engine."""

    def __init__(self, params: dict, outputs: dict) -> None:
        self.params = params
        self.outputs = outputs

        self.min_price_spread = params.get("min_price_spread", DEFAULT_MIN_PRICE_SPREAD)
        self.planning_horizon = params.get("planning_horizon_hours", DEFAULT_PLANNING_HORIZON)
        self.enable_charger = params.get("enable_charger_control", True)
        self.enable_battery = params.get("enable_battery_control", True)

        # Battery config from output mapping
        sg_out = outputs.get(OUTPUT_SUNGROW, {})
        self.min_soc = sg_out.get("min_soc", DEFAULT_MIN_SOC)
        self.max_soc = sg_out.get("max_soc", DEFAULT_MAX_SOC)
        self.battery_capacity = sg_out.get("capacity_kwh", DEFAULT_BATTERY_CAPACITY)

        # Charger config
        easee_out = outputs.get(OUTPUT_EASEE, {})
        charge_cfg = easee_out.get("set_current_limit", {})
        self.min_amps = charge_cfg.get("min_amps", DEFAULT_MIN_AMPS)
        self.max_amps = charge_cfg.get("max_amps", DEFAULT_MAX_AMPS)

    def optimize(
        self,
        prices: dict[str, Any],
        predicted_consumption: list[float],
        battery_soc: float,
        ev_connected: bool,
    ) -> dict[str, Any]:
        """Produce an hour-by-hour schedule and immediate actions.

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

        # Compute price statistics
        avg_price = sum(horizon_prices) / len(horizon_prices) if horizon_prices else 0
        min_price = min(horizon_prices) if horizon_prices else 0
        max_price = max(horizon_prices) if horizon_prices else 0
        price_spread = max_price - min_price

        # Classify each hour
        hourly_plan = []
        immediate_actions = []

        for i, price in enumerate(horizon_prices):
            hour = (current_hour + i) % 24
            consumption = predicted_consumption[i] if i < len(predicted_consumption) else 0

            action, reason = self._classify_hour(
                price=price,
                avg_price=avg_price,
                min_price=min_price,
                max_price=max_price,
                price_spread=price_spread,
                battery_soc=battery_soc,
                consumption=consumption,
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

        # Build immediate actions (only for the current hour)
        if hourly_plan:
            current_plan = hourly_plan[0]
            immediate_actions = self._build_immediate_actions(
                current_plan["action"],
                ev_connected,
            )

        # Summary
        cheap_hours = [h for h in hourly_plan if h["action"] == ACTION_CHARGE_BATTERY]
        expensive_hours = [h for h in hourly_plan if h["action"] == ACTION_DISCHARGE_BATTERY]

        summary = (
            f"Plan: {len(cheap_hours)} charge hours, "
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

    def _classify_hour(
        self,
        price: float,
        avg_price: float,
        min_price: float,
        max_price: float,
        price_spread: float,
        battery_soc: float,
        consumption: float,
    ) -> tuple[str, str]:
        """Decide what to do in a given hour based on price position."""

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

    def _simulate_soc_change(self, soc: float, action: str) -> float:
        """Rough SoC simulation for planning purposes."""
        # Assume ~2 kW charge/discharge rate per hour as % of capacity
        delta_pct = (2.0 / self.battery_capacity) * 100

        if action == ACTION_CHARGE_BATTERY:
            return min(self.max_soc, soc + delta_pct)
        elif action == ACTION_DISCHARGE_BATTERY:
            return max(self.min_soc, soc - delta_pct)
        return soc

    def _build_immediate_actions(
        self, action: str, ev_connected: bool
    ) -> list[dict[str, Any]]:
        """Convert the current hour's action into HA service calls."""
        actions = []
        sg_out = self.outputs.get(OUTPUT_SUNGROW, {})
        easee_out = self.outputs.get(OUTPUT_EASEE, {})

        # Battery actions
        if self.enable_battery:
            if action == ACTION_CHARGE_BATTERY:
                cfg = sg_out.get("force_charge", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {"option": cfg.get("mode_value", "force_charge")},
                    })
            elif action == ACTION_DISCHARGE_BATTERY:
                cfg = sg_out.get("force_discharge", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {"option": cfg.get("mode_value", "force_discharge")},
                    })
            else:
                cfg = sg_out.get("self_consumption", {})
                if cfg.get("service") and cfg.get("entity_id"):
                    actions.append({
                        "service": cfg["service"],
                        "entity_id": cfg["entity_id"],
                        "data": {"option": cfg.get("mode_value", "self_consumption")},
                    })

        # EV charger actions — charge during cheap hours if connected
        if self.enable_charger and ev_connected:
            if action == ACTION_CHARGE_BATTERY:
                # Cheap hour → also charge EV at max amps
                start_cfg = easee_out.get("start_charging", {})
                if start_cfg.get("service"):
                    actions.append({
                        "service": start_cfg["service"],
                        "entity_id": start_cfg["entity_id"],
                        "data": {},
                    })
                limit_cfg = easee_out.get("set_current_limit", {})
                if limit_cfg.get("service"):
                    actions.append({
                        "service": limit_cfg["service"],
                        "entity_id": limit_cfg["entity_id"],
                        "data": {"value": self.max_amps},
                    })
            elif action == ACTION_DISCHARGE_BATTERY:
                # Expensive hour → stop EV charging
                stop_cfg = easee_out.get("stop_charging", {})
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
