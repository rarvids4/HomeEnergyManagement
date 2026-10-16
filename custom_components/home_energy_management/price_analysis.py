"""Price analysis — builds price horizons and computes statistics.

This module handles all price-related logic:
  - Building the combined price list from today + tomorrow
  - Slicing to the planning horizon
  - Applying grid transfer tariffs (time-of-use network fees)
  - Computing price statistics (avg, min, max, spread)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .const import (
    BUY_PRICE_MODE_MONTHLY_AVERAGE,
    DEFAULT_BUY_PRICE_FLOOR_SEK,
    DEFAULT_BUY_PRICE_MODE,
    DEFAULT_BUY_PRICE_MONTHLY_AVG_SEK,
    DEFAULT_GRID_TARIFF_OFFPEAK_SEK,
    DEFAULT_GRID_TARIFF_PEAK_END,
    DEFAULT_GRID_TARIFF_PEAK_SEK,
    DEFAULT_GRID_TARIFF_PEAK_START,
)

_LOGGER = logging.getLogger(__name__)


def grid_charge_break_even(
    peak_tariff: float,
    offpeak_tariff: float,
    round_trip_efficiency: float,
) -> float | None:
    """Flat energy price below which off-peak → peak battery cycling pays off.

    With a flat energy price *P* (monthly-average contract) the only
    hourly difference is the grid tariff.  Buying 1 kWh off-peak and
    using it at peak saves money when

        P + offpeak  <  rt × (P + peak)
        ⇔  P  <  (rt × peak − offpeak) / (1 − rt)

    Returns None when the round trip is lossless (no break-even).
    """
    if round_trip_efficiency >= 1.0:
        return None
    return (
        (round_trip_efficiency * peak_tariff - offpeak_tariff)
        / (1.0 - round_trip_efficiency)
    )


@dataclass
class PriceWindow:
    """Immutable snapshot of price data for the planning horizon.

    Attributes
    ----------
    effective : list[float]
        Buy energy price + grid tariff per hour (what you really pay).
    spot : list[float]
        Raw spot prices per hour (no tariff).
    current_hour : int
        The hour (0-23) the window starts from.
    avg : float
        Mean effective price over the window.
    min : float
        Lowest effective price.
    max : float
        Highest effective price.
    spread : float
        max − min.
    currency : str
        Price currency code.
    """

    effective: list[float] = field(default_factory=list)
    spot: list[float] = field(default_factory=list)
    current_hour: int = 0
    avg: float = 0.0
    min: float = 0.0
    max: float = 0.0
    spread: float = 0.0
    currency: str = "SEK"

    @property
    def is_empty(self) -> bool:
        return len(self.effective) == 0


class PriceAnalyzer:
    """Builds price horizons and computes statistics."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.planning_horizon = params.get("planning_horizon_hours", 24)

        # Grid transfer tariffs (SEK/kWh) — time-of-use network fees
        self.grid_tariff_peak = params.get(
            "grid_tariff_peak_sek", DEFAULT_GRID_TARIFF_PEAK_SEK
        )
        self.grid_tariff_offpeak = params.get(
            "grid_tariff_offpeak_sek", DEFAULT_GRID_TARIFF_OFFPEAK_SEK
        )
        self.grid_tariff_peak_start = params.get(
            "grid_tariff_peak_start", DEFAULT_GRID_TARIFF_PEAK_START
        )
        self.grid_tariff_peak_end = params.get(
            "grid_tariff_peak_end", DEFAULT_GRID_TARIFF_PEAK_END
        )

        # Buy-price contract (energy part, before the grid tariff)
        self.buy_price_mode = params.get("buy_price_mode", DEFAULT_BUY_PRICE_MODE)
        self.buy_price_floor = params.get(
            "buy_price_floor_sek", DEFAULT_BUY_PRICE_FLOOR_SEK
        )
        self.buy_price_monthly_avg = params.get(
            "buy_price_monthly_avg_sek", DEFAULT_BUY_PRICE_MONTHLY_AVG_SEK
        )
        # Rolling month-to-date average spot — set by the coordinator
        # from MonthlyPriceTracker.  None = no history yet.
        self.buy_price_rolling_avg: float | None = None

    def monthly_average(self, known_spot: list[float]) -> float | None:
        """Monthly average spot used in ``monthly_average`` mode.

        A fixed ``buy_price_monthly_avg_sek`` wins; otherwise the rolling
        month-to-date average; before any history exists, the mean of
        *known_spot* (the Nordpool prices currently available).
        """
        if self.buy_price_monthly_avg > 0:
            return self.buy_price_monthly_avg
        if self.buy_price_rolling_avg is not None:
            return self.buy_price_rolling_avg
        if known_spot:
            return sum(known_spot) / len(known_spot)
        return None

    def buy_energy_price(self, spot_price: float, known_spot: list[float]) -> float:
        """Energy price paid per kWh for *spot_price*, before the grid tariff.

        In ``monthly_average`` mode every hour costs the same monthly
        average (see :meth:`monthly_average`).  The contract floor, when
        set (> 0), is applied last.
        """
        price = spot_price
        if self.buy_price_mode == BUY_PRICE_MODE_MONTHLY_AVERAGE:
            avg = self.monthly_average(known_spot)
            if avg is not None:
                price = avg
        if self.buy_price_floor > 0:
            price = max(price, self.buy_price_floor)
        return price

    def get_grid_tariff(self, hour: int) -> float:
        """Return the grid transfer tariff (SEK/kWh) for *hour* of day.

        Peak hours carry ``grid_tariff_peak``; off-peak carry
        ``grid_tariff_offpeak``.
        """
        if self.grid_tariff_peak_start <= hour < self.grid_tariff_peak_end:
            return self.grid_tariff_peak
        return self.grid_tariff_offpeak

    def effective_price(self, spot_price: float, hour: int) -> float:
        """Spot price + applicable grid tariff."""
        return spot_price + self.get_grid_tariff(hour)

    def build_price_window(
        self,
        prices: dict[str, Any],
        current_hour: int,
    ) -> PriceWindow:
        """Build a PriceWindow from the Nordpool price dict.

        Combines today + tomorrow, slices to the planning horizon,
        and applies grid tariffs.
        """
        all_prices = list(prices.get("today", []))
        tomorrow = prices.get("tomorrow", [])
        if tomorrow:
            all_prices.extend(tomorrow)

        if not all_prices:
            return PriceWindow(currency=prices.get("currency", "SEK"))

        # Slice to planning horizon starting from current hour
        horizon_spot = all_prices[current_hour: current_hour + self.planning_horizon]
        if not horizon_spot:
            horizon_spot = all_prices[current_hour:]

        # Apply buy-price contract + grid tariffs → effective prices
        horizon_effective = [
            self.buy_energy_price(price, all_prices)
            + self.get_grid_tariff((current_hour + i) % 24)
            for i, price in enumerate(horizon_spot)
        ]

        avg = sum(horizon_effective) / len(horizon_effective)
        mn = min(horizon_effective)
        mx = max(horizon_effective)

        return PriceWindow(
            effective=horizon_effective,
            spot=horizon_spot,
            current_hour=current_hour,
            avg=avg,
            min=mn,
            max=mx,
            spread=mx - mn,
            currency=prices.get("currency", "SEK"),
        )

    def build_extended_plan_entries(
        self,
        prices: dict[str, Any],
        current_hour: int,
        base_plan_length: int,
    ) -> list[dict[str, Any]]:
        """Build day-2 extension entries for EV scheduling.

        When 2-day optimisation is enabled, this extends the plan with
        additional price entries beyond the base battery horizon.

        Returns a list of plan-like dicts with hour, price, spot_price.
        """
        from .const import ACTION_SELF_CONSUMPTION

        all_prices = list(prices.get("today", []))
        tomorrow = prices.get("tomorrow", [])
        if tomorrow:
            all_prices.extend(tomorrow)

        extended_prices = all_prices[current_hour: current_hour + 48]
        entries = []

        for i in range(base_plan_length, len(extended_prices)):
            hour = (current_hour + i) % 24
            spot = extended_prices[i]
            eff = self.buy_energy_price(spot, all_prices) + self.get_grid_tariff(hour)
            entries.append({
                "hour": hour,
                "action": ACTION_SELF_CONSUMPTION,
                "reason": "Extended window (day 2)",
                "price": round(eff, 4),
                "spot_price": round(spot, 4),
                "predicted_consumption_kwh": 0,
            })

        return entries
