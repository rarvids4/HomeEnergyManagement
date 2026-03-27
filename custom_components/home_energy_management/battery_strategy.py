"""Battery strategy — decides when to charge, discharge, or idle.

This module contains all battery-related decision logic:
  - Hour classification (what action to take at each price point)
  - SoC simulation (rough tracking for planning)
  - Capacity-aware discharge limiting (don't plan more discharge
    than the battery can actually deliver)

Priority order for each hour:
  1. Negative price   → MAXIMIZE_LOAD (absorb everything)
  2. Solar surplus     → SELF_CONSUMPTION (free energy from panels)
  3. Pre-discharge     → Empty battery before upcoming negative prices
  4. Cheap hour        → CHARGE from grid (limited by SoC cap & price cap)
  5. Expensive hour    → DISCHARGE battery (self-consumption covers load)
  6. Normal            → SELF_CONSUMPTION
"""

from __future__ import annotations

import logging
from typing import Any

from .const import (
    ACTION_CHARGE_BATTERY,
    ACTION_DISCHARGE_BATTERY,
    ACTION_MAXIMIZE_LOAD,
    ACTION_PRE_DISCHARGE,
    ACTION_SELF_CONSUMPTION,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_GRID_CHARGE_MAX_PRICE,
    DEFAULT_GRID_CHARGE_MAX_SOC,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_PRICE_SPREAD,
    DEFAULT_MIN_SOC,
    DEFAULT_SOLAR_SURPLUS_THRESHOLD,
    OUTPUT_SUNGROW,
)
from .price_analysis import PriceWindow

_LOGGER = logging.getLogger(__name__)

# How many hours ahead to look for upcoming negative prices
PRE_DISCHARGE_LOOKAHEAD = 4


class BatteryStrategy:
    """Decides battery charge/discharge actions based on price signals."""

    def __init__(self, params: dict[str, Any], outputs: dict[str, Any]) -> None:
        self.enable_battery = params.get("enable_battery_control", True)
        self.min_price_spread = params.get("min_price_spread", DEFAULT_MIN_PRICE_SPREAD)

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

        self.solar_surplus_threshold = params.get(
            "solar_surplus_threshold_w", DEFAULT_SOLAR_SURPLUS_THRESHOLD
        )

    # ------------------------------------------------------------------
    # Public: build the full hourly battery plan
    # ------------------------------------------------------------------

    def plan_battery(
        self,
        pw: PriceWindow,
        predicted_consumption: list[float],
        battery_soc: float,
        grid_export_power: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Produce an hour-by-hour battery action plan.

        Parameters
        ----------
        pw : PriceWindow
            Pre-computed price window from PriceAnalyzer.
        predicted_consumption : list[float]
            Predicted house consumption per hour (kWh).
        battery_soc : float
            Current battery state of charge (%).
        grid_export_power : float
            Current grid export in Watts (real-time, hour-0 only).

        Returns
        -------
        list[dict]
            Hourly plan entries with action, reason, price, etc.
        """
        initial_soc = battery_soc
        hourly_plan = []

        for i, price in enumerate(pw.effective):
            hour = (pw.current_hour + i) % 24
            spot = pw.spot[i] if i < len(pw.spot) else price
            consumption = (
                predicted_consumption[i]
                if i < len(predicted_consumption) else 0
            )

            # Look ahead for negative prices
            upcoming = pw.effective[i + 1: i + 1 + PRE_DISCHARGE_LOOKAHEAD]

            action, reason = self._classify_hour(
                price=price,
                avg_price=pw.avg,
                min_price=pw.min,
                max_price=pw.max,
                price_spread=pw.spread,
                battery_soc=battery_soc,
                consumption=consumption,
                upcoming_prices=upcoming,
                # Only real-time solar data for the current hour
                grid_export_power=grid_export_power if i == 0 else 0.0,
            )

            hourly_plan.append({
                "hour": hour,
                "action": action,
                "reason": reason,
                "price": round(price, 4),
                "spot_price": round(spot, 4),
                "predicted_consumption_kwh": round(consumption, 2),
            })

            # Simulate SoC for future hours
            battery_soc = self._simulate_soc_change(battery_soc, action)

        # Post-processing: don't plan more discharge than battery can cover
        hourly_plan = self._limit_discharge_to_capacity(
            hourly_plan, initial_soc, predicted_consumption
        )

        return hourly_plan

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
        """Decide what to do in a given hour based on price position."""
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

        # --- 4-6. Normal positive-price logic ---
        if price_spread < self.min_price_spread:
            return ACTION_SELF_CONSUMPTION, "Price spread too small to optimise"

        cheap_threshold = min_price + price_spread * 0.30
        expensive_threshold = max_price - price_spread * 0.30

        # Cheap: charge from grid (capped by SoC & price limits)
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

        # Expensive: discharge (self-consumption covers house load)
        if (
            self.enable_battery
            and price >= expensive_threshold
            and battery_soc > self.min_soc
        ):
            return (
                ACTION_DISCHARGE_BATTERY,
                f"Expensive price ({price:.2f}), discharging battery",
            )

        return (
            ACTION_SELF_CONSUMPTION,
            f"Normal price ({price:.2f}), self-consumption mode",
        )

    # ------------------------------------------------------------------
    # SoC simulation
    # ------------------------------------------------------------------

    def _simulate_soc_change(self, soc: float, action: str) -> float:
        """Rough SoC simulation for planning purposes."""
        delta_pct = (2.0 / self.battery_capacity) * 100
        surplus_delta_pct = (1.0 / self.battery_capacity) * 100

        if action == ACTION_CHARGE_BATTERY:
            return min(self.grid_charge_max_soc, soc + delta_pct)
        elif action == ACTION_MAXIMIZE_LOAD:
            return min(self.max_soc, soc + surplus_delta_pct)
        elif action in (ACTION_DISCHARGE_BATTERY, ACTION_PRE_DISCHARGE):
            return max(self.min_soc, soc - delta_pct)
        return soc

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

        Ranks discharge hours by price (most expensive first) and only
        retains the top-N that the battery can sustain.  The rest are
        downgraded to self_consumption.
        """
        if not self.enable_battery:
            return hourly_plan

        discharge_indices = [
            (i, entry["price"])
            for i, entry in enumerate(hourly_plan)
            if entry["action"] == ACTION_DISCHARGE_BATTERY
        ]

        if len(discharge_indices) == 0:
            return hourly_plan

        # Most expensive first
        discharge_indices.sort(key=lambda x: x[1], reverse=True)

        available_kwh = (initial_soc - self.min_soc) / 100.0 * self.battery_capacity
        if available_kwh <= 0:
            for idx, _ in discharge_indices:
                hourly_plan[idx]["action"] = ACTION_SELF_CONSUMPTION
                hourly_plan[idx]["reason"] = (
                    f"Battery depleted (SoC {initial_soc:.0f}%) — "
                    f"self-consumption (was discharge at {hourly_plan[idx]['price']:.2f})"
                )
            return hourly_plan

        if len(discharge_indices) == 1:
            return hourly_plan

        keep_set: set[int] = set()
        remaining_kwh = available_kwh

        for idx, price in discharge_indices:
            consumption = (
                predicted_consumption[idx]
                if idx < len(predicted_consumption)
                else 1.0
            )
            hour_need = max(consumption, 0.5)

            if remaining_kwh >= hour_need:
                keep_set.add(idx)
                remaining_kwh -= hour_need
            else:
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
