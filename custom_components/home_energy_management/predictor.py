"""Consumption predictor — estimates future energy usage from patterns.

Uses a weighted-average approach over historical hourly consumption data.
Tracks **house base load** and **EV charging load** separately so that
sporadic EV sessions don't pollute the regular household pattern.

Each stream keeps its own per-(day_of_week, hour) history and applies
recency-weighted averaging independently.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Predefined stream names used by the coordinator
STREAM_HOUSE = "house_base"
STREAM_EV = "ev_charging"


class _StreamHistory:
    """Per-stream history bucket with day-of-week × hour slots."""

    def __init__(self, history_days: int) -> None:
        self.history_days = history_days
        # {(day_of_week, hour): [values]}  —  day_of_week: 0=Mon … 6=Sun
        self.slots: dict[tuple[int, int], list[float]] = defaultdict(list)
        self.total_observations: int = 0

    def add(self, dow: int, hour: int, kwh: float) -> None:
        key = (dow, hour)
        self.slots[key].append(kwh)
        if len(self.slots[key]) > self.history_days:
            self.slots[key] = self.slots[key][-self.history_days:]
        self.total_observations += 1

    def get(self, dow: int, hour: int) -> list[float]:
        return self.slots.get((dow, hour), [])


class ConsumptionPredictor:
    """Predict hourly energy consumption based on historical patterns.

    Supports multiple named *streams* (e.g. ``house_base``,
    ``ev_charging``) so that each load category is predicted
    independently with its own weekday × hour pattern.

    Backward-compatible: the single-stream ``add_observation`` /
    ``predict`` API still works and targets the ``house_base`` stream.
    """

    def __init__(
        self,
        history_days: int = 14,
        recency_weight: float = 0.7,
    ) -> None:
        self.history_days = history_days
        self.recency_weight = recency_weight

        # Named stream histories
        self._streams: dict[str, _StreamHistory] = {}
        self._ensure_stream(STREAM_HOUSE)
        self._ensure_stream(STREAM_EV)

        # Legacy flat history (kept for backward compat with tests)
        self._history: dict[tuple[int, int], list[float]] = defaultdict(list)

        # Flat ordered history for recency weighting / logging
        self._recent_entries: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Stream helpers
    # ------------------------------------------------------------------

    def _ensure_stream(self, name: str) -> _StreamHistory:
        if name not in self._streams:
            self._streams[name] = _StreamHistory(self.history_days)
        return self._streams[name]

    # ------------------------------------------------------------------
    # Observation recording
    # ------------------------------------------------------------------

    def add_observation(
        self,
        timestamp: datetime,
        consumption_kwh: float,
        stream: str = STREAM_HOUSE,
    ) -> None:
        """Record an hourly consumption observation for *stream*.

        Parameters
        ----------
        timestamp : datetime
            The hour this observation covers.
        consumption_kwh : float
            Measured energy for that hour.
        stream : str
            Which load category (``house_base``, ``ev_charging``, …).
        """
        dow = timestamp.weekday()
        hour = timestamp.hour
        key = (dow, hour)

        # Stream-specific history
        sh = self._ensure_stream(stream)
        sh.add(dow, hour, consumption_kwh)

        # Legacy combined history (house_base stream only)
        if stream == STREAM_HOUSE:
            self._history[key].append(consumption_kwh)
            if len(self._history[key]) > self.history_days:
                self._history[key] = self._history[key][-self.history_days:]

        self._recent_entries.append({
            "timestamp": timestamp.isoformat(),
            "dow": dow,
            "hour": hour,
            "kwh": consumption_kwh,
            "stream": stream,
        })

        # Trim recent entries
        max_recent = self.history_days * 24 * 2  # ×2 for two streams
        if len(self._recent_entries) > max_recent:
            self._recent_entries = self._recent_entries[-max_recent:]

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(
        self,
        hours_ahead: int = 24,
        current_load: float = 0.0,
        stream: str | None = None,
    ) -> list[float]:
        """Predict consumption for the next *hours_ahead* hours.

        Parameters
        ----------
        hours_ahead : int
            Number of hours to predict.
        current_load : float
            Current instantaneous load in watts (fallback when no history).
        stream : str | None
            If given, predict only that stream.  If ``None`` (default),
            return the **sum** of all streams (total predicted load).

        Returns
        -------
        list[float]
            Predicted consumption in kWh per hour.
        """
        if stream is not None:
            return self._predict_stream(stream, hours_ahead, current_load)

        # Sum across all streams
        totals = [0.0] * hours_ahead
        for name in self._streams:
            fallback = current_load if name == STREAM_HOUSE else 0.0
            stream_pred = self._predict_stream(name, hours_ahead, fallback)
            for i in range(hours_ahead):
                totals[i] += stream_pred[i]
        return [round(v, 3) for v in totals]

    def _predict_stream(
        self,
        stream: str,
        hours_ahead: int,
        current_load: float,
    ) -> list[float]:
        sh = self._streams.get(stream)
        if sh is None:
            return [0.0] * hours_ahead

        now = datetime.now()
        predictions = []
        for i in range(hours_ahead):
            future_hour = (now.hour + i) % 24
            days_offset = (now.hour + i) // 24
            future_dow = (now.weekday() + days_offset) % 7

            prediction = self._predict_hour_from_stream(
                sh, future_dow, future_hour, current_load, stream,
            )
            predictions.append(round(prediction, 3))
        return predictions

    def predict_split(
        self,
        hours_ahead: int = 24,
        current_house_load: float = 0.0,
    ) -> dict[str, list[float]]:
        """Return per-stream predictions in one call.

        Returns
        -------
        dict with keys ``house_base``, ``ev_charging``, ``total``.
        """
        house = self._predict_stream(STREAM_HOUSE, hours_ahead, current_house_load)
        ev = self._predict_stream(STREAM_EV, hours_ahead, 0.0)
        total = [round(h + e, 3) for h, e in zip(house, ev)]
        return {
            STREAM_HOUSE: house,
            STREAM_EV: ev,
            "total": total,
        }

    # ------------------------------------------------------------------
    # Internal prediction logic
    # ------------------------------------------------------------------

    def _predict_hour(
        self,
        day_of_week: int,
        hour: int,
        fallback_load: float,
    ) -> float:
        """Predict consumption for a specific day-of-week + hour slot.

        Kept for backward compatibility with existing tests.
        Uses the house_base stream.
        """
        sh = self._streams.get(STREAM_HOUSE)
        if sh is None:
            return max(fallback_load / 1000.0, 0.5)
        return self._predict_hour_from_stream(
            sh, day_of_week, hour, fallback_load, STREAM_HOUSE,
        )

    def _predict_hour_from_stream(
        self,
        sh: _StreamHistory,
        day_of_week: int,
        hour: int,
        fallback_load: float,
        stream: str,
    ) -> float:
        """Predict one hour for a given stream history."""
        values = sh.get(day_of_week, hour)

        if not values:
            # Fallback: same hour across all weekdays in this stream
            any_day_values: list[float] = []
            for dow in range(7):
                any_day_values.extend(sh.get(dow, hour))

            if any_day_values:
                return self._weighted_average(any_day_values)

            # No history at all
            if stream == STREAM_EV:
                return 0.0  # EV defaults to zero (not always plugged in)
            return max(fallback_load / 1000.0, 0.5)

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

        per_stream: dict[str, dict[str, Any]] = {}
        for name, sh in self._streams.items():
            stream_obs = sum(len(v) for v in sh.slots.values())
            stream_slots = len(sh.slots)
            per_stream[name] = {
                "observations": stream_obs,
                "covered_slots": stream_slots,
                "coverage_pct": round(stream_slots / (7 * 24) * 100, 1),
            }

        return {
            "total_observations": total_observations,
            "covered_slots": covered_slots,
            "total_possible_slots": 7 * 24,
            "coverage_pct": round(covered_slots / (7 * 24) * 100, 1),
            "history_days_configured": self.history_days,
            "recency_weight": self.recency_weight,
            "streams": per_stream,
        }
