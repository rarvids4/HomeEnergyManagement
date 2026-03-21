"""Consumption predictor — estimates future energy usage from patterns.

Uses a weighted-average approach over historical hourly consumption data.
When enough history is collected, it considers day-of-week and hour-of-day
patterns, weighting recent days more heavily.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)


class ConsumptionPredictor:
    """Predict hourly energy consumption based on historical patterns."""

    def __init__(
        self,
        history_days: int = 14,
        recency_weight: float = 0.7,
    ) -> None:
        self.history_days = history_days
        self.recency_weight = recency_weight

        # Hourly consumption history: {(day_of_week, hour): [values]}
        # day_of_week: 0=Monday, 6=Sunday
        self._history: dict[tuple[int, int], list[float]] = defaultdict(list)

        # Flat ordered history for recency weighting
        self._recent_entries: list[dict[str, Any]] = []

    def add_observation(
        self,
        timestamp: datetime,
        consumption_kwh: float,
    ) -> None:
        """Record an hourly consumption observation.

        Call this every hour with the actual measured consumption to
        improve future predictions.
        """
        dow = timestamp.weekday()
        hour = timestamp.hour
        key = (dow, hour)

        self._history[key].append(consumption_kwh)

        # Keep only the last N days of data per slot
        max_entries = self.history_days
        if len(self._history[key]) > max_entries:
            self._history[key] = self._history[key][-max_entries:]

        self._recent_entries.append({
            "timestamp": timestamp.isoformat(),
            "dow": dow,
            "hour": hour,
            "kwh": consumption_kwh,
        })

        # Trim recent entries
        max_recent = self.history_days * 24
        if len(self._recent_entries) > max_recent:
            self._recent_entries = self._recent_entries[-max_recent:]

    def predict(
        self,
        hours_ahead: int = 24,
        current_load: float = 0.0,
    ) -> list[float]:
        """Predict consumption for the next N hours.

        Parameters
        ----------
        hours_ahead : int
            Number of hours to predict.
        current_load : float
            Current instantaneous load in watts (used as fallback).

        Returns
        -------
        list[float]
            Predicted consumption in kWh for each hour.
        """
        now = datetime.now()
        predictions = []

        for i in range(hours_ahead):
            future_hour = (now.hour + i) % 24
            # Use the same day-of-week for today, next day for hours past midnight
            days_offset = (now.hour + i) // 24
            future_dow = (now.weekday() + days_offset) % 7

            prediction = self._predict_hour(future_dow, future_hour, current_load)
            predictions.append(round(prediction, 3))

        return predictions

    def _predict_hour(
        self,
        day_of_week: int,
        hour: int,
        fallback_load: float,
    ) -> float:
        """Predict consumption for a specific day-of-week + hour slot."""
        key = (day_of_week, hour)
        values = self._history.get(key, [])

        if not values:
            # No history for this exact slot — try same hour, any day
            any_day_values = []
            for dow in range(7):
                any_day_values.extend(self._history.get((dow, hour), []))

            if any_day_values:
                return self._weighted_average(any_day_values)

            # No history at all — use current load as rough estimate
            # Convert W to kWh (1 hour)
            return max(fallback_load / 1000.0, 0.5)  # min 0.5 kWh as baseline

        return self._weighted_average(values)

    def _weighted_average(self, values: list[float]) -> float:
        """Compute a recency-weighted average.

        More recent values get exponentially higher weight.
        """
        if not values:
            return 0.0

        n = len(values)
        if n == 1:
            return values[0]

        # Exponential weights: most recent gets highest weight
        weights = []
        for i in range(n):
            # i=0 is oldest, i=n-1 is newest
            w = (1 - self.recency_weight) + self.recency_weight * (i / (n - 1))
            weights.append(w)

        total_weight = sum(weights)
        weighted_sum = sum(v * w for v, w in zip(values, weights))

        return weighted_sum / total_weight if total_weight > 0 else 0.0

    def get_statistics(self) -> dict[str, Any]:
        """Return prediction statistics for logging/debugging."""
        total_observations = sum(len(v) for v in self._history.values())
        covered_slots = len(self._history)

        return {
            "total_observations": total_observations,
            "covered_slots": covered_slots,
            "total_possible_slots": 7 * 24,
            "coverage_pct": round(covered_slots / (7 * 24) * 100, 1),
            "history_days_configured": self.history_days,
            "recency_weight": self.recency_weight,
        }
