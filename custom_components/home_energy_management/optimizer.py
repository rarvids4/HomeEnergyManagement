"""Optimizer — orchestrates energy optimization using modular strategies.

The optimizer coordinates four specialized modules:
  - **PriceAnalyzer**: Builds price horizons, applies grid tariffs
  - **BatteryStrategy**: Classifies hours, plans charge/discharge
  - **EVScheduler**: Schedules per-vehicle EV charging
  - **ActionBuilder**: Translates decisions into HA service calls

This file is the public API — callers import ``Optimizer`` and call
``optimize()``.  All complex logic lives in the sub-modules.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .const import (
    ACTION_CHARGE_BATTERY,
    ACTION_DISCHARGE_BATTERY,
    ACTION_MAXIMIZE_LOAD,
    ACTION_PRE_DISCHARGE,
    ACTION_SELF_CONSUMPTION,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_EV_OPTIMIZATION_WINDOW,
    DEFAULT_GRID_TARIFF_OFFPEAK_SEK,
    DEFAULT_GRID_TARIFF_PEAK_SEK,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_SOC,
    OUTPUT_SUNGROW,
)
from .price_analysis import PriceAnalyzer
from .battery_strategy import BatteryStrategy
from .ev_scheduler import EVScheduler
from .action_builder import ActionBuilder

_LOGGER = logging.getLogger(__name__)


class Optimizer:
    """Price-aware energy optimization engine.

    Delegates to:
      - ``PriceAnalyzer``   — price horizon & tariff logic
      - ``BatteryStrategy`` — hour classification & discharge limiting
      - ``EVScheduler``     — per-vehicle charging schedule
      - ``ActionBuilder``   — HA service call generation
    """

    def __init__(self, params: dict, outputs: dict) -> None:
        self.params = params
        self.outputs = outputs

        # Sub-modules
        self._prices = PriceAnalyzer(params)
        self._battery = BatteryStrategy(params, outputs)
        self._ev = EVScheduler(params)
        self._actions = ActionBuilder(params, outputs)

        # Public properties accessed by coordinator & sensors
        sg_out = outputs.get(OUTPUT_SUNGROW, {})
        self.battery_capacity = sg_out.get("capacity_kwh", DEFAULT_BATTERY_CAPACITY)
        self.ev_optimization_window = params.get(
            "ev_optimization_window", DEFAULT_EV_OPTIMIZATION_WINDOW
        )

        # Grid tariff values — writable by coordinator (options flow)
        self.grid_tariff_peak = self._prices.grid_tariff_peak
        self.grid_tariff_offpeak = self._prices.grid_tariff_offpeak

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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

        This is the main entry point.  It orchestrates:
          1. Price analysis  — build effective price window
          2. Battery plan    — classify each hour
          3. EV scheduling   — assign cheapest hours per vehicle
          4. Action building — generate HA service calls for now

        Returns
        -------
        dict with hourly_plan, immediate_actions, ev_charge_schedule,
        summary, and stats.
        """
        now = datetime.now()
        current_hour = now.hour

        # Sync writable tariff values to price analyzer
        self._prices.grid_tariff_peak = self.grid_tariff_peak
        self._prices.grid_tariff_offpeak = self.grid_tariff_offpeak

        # ── Step 1: Price analysis ──────────────────────────────────
        pw = self._prices.build_price_window(prices, current_hour)

        if pw.is_empty:
            return self._safe_default_schedule(now)

        # ── Step 2: Battery plan ────────────────────────────────────
        hourly_plan = self._battery.plan_battery(
            pw, predicted_consumption, battery_soc, grid_export_power,
        )

        # ── Step 3: EV scheduling ──────────────────────────────────
        ev_plan_for_scheduling = hourly_plan
        near_term_hours = len(hourly_plan)

        if self.ev_optimization_window >= 2:
            day2_entries = self._prices.build_extended_plan_entries(
                prices, current_hour, len(hourly_plan),
            )
            if day2_entries:
                ev_plan_for_scheduling = list(hourly_plan) + day2_entries

        ev_charge_plan = self._ev.plan(
            ev_plan_for_scheduling,
            ev_vehicles or [],
            current_hour,
            near_term_hours=near_term_hours,
            now=now,
        )

        # ── Step 4: Immediate actions (current hour) ───────────────
        immediate_actions = []
        if hourly_plan:
            current_plan = hourly_plan[0]
            immediate_actions = self._actions.build_immediate_actions(
                action=current_plan["action"],
                ev_connected=ev_connected,
                current_price=current_plan["price"],
                avg_price=pw.avg,
                min_price=pw.min,
                price_spread=pw.spread,
                grid_export_w=grid_export_power,
                ev_vehicles=ev_vehicles or [],
                ev_charge_plan=ev_charge_plan,
                now=now,
            )

        # ── Summary ────────────────────────────────────────────────
        summary = self._build_summary(
            hourly_plan, ev_charge_plan, pw, prices,
        )

        return {
            "hourly_plan": hourly_plan,
            "immediate_actions": immediate_actions,
            "ev_charge_schedule": ev_charge_plan,
            "summary": summary,
            "stats": {
                "avg_price": round(pw.avg, 4),
                "min_price": round(pw.min, 4),
                "max_price": round(pw.max, 4),
                "price_spread": round(pw.spread, 4),
                "grid_tariff_peak": self.grid_tariff_peak,
                "grid_tariff_offpeak": self.grid_tariff_offpeak,
            },
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_summary(
        self,
        hourly_plan: list[dict[str, Any]],
        ev_charge_plan: dict[str, Any],
        pw: Any,
        prices: dict[str, Any],
    ) -> str:
        """Build a human-readable summary of the plan."""
        neg = sum(1 for h in hourly_plan if h["action"] == ACTION_MAXIMIZE_LOAD)
        pre = sum(1 for h in hourly_plan if h["action"] == ACTION_PRE_DISCHARGE)
        chg = sum(1 for h in hourly_plan if h["action"] == ACTION_CHARGE_BATTERY)
        dis = sum(1 for h in hourly_plan if h["action"] == ACTION_DISCHARGE_BATTERY)
        ev = sum(1 for h in ev_charge_plan.get("schedule", []) if h.get("charging"))

        has_tariff = self.grid_tariff_peak > 0 or self.grid_tariff_offpeak > 0
        tariff_note = (
            f" (incl. tariff {self.grid_tariff_offpeak:.2f}/"
            f"{self.grid_tariff_peak:.2f})"
            if has_tariff else ""
        )

        return (
            f"Plan: {neg} negative-price (maximize), "
            f"{pre} pre-discharge, "
            f"{chg} charge, "
            f"{dis} discharge, "
            f"{ev} EV-charge hours. "
            f"Price range: {pw.min:.2f}–{pw.max:.2f} "
            f"{prices.get('currency', 'SEK')}/kWh{tariff_note}. "
            f"Spread: {pw.spread:.2f}."
        )

    @staticmethod
    def _safe_default_schedule(now: datetime) -> dict:
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
