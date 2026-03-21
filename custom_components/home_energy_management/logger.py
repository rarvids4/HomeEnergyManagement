"""Internal prediction & decision logger.

Stores a rolling log of every optimisation decision **and** the actual
measured outcomes so you can track prediction error over time and verify
that algorithm changes improve accuracy.

Key metrics exposed:
- MAE  (Mean Absolute Error in kWh)
- MAPE (Mean Absolute Percentage Error)
- Rolling 24 h / 7 d windows so you see recent trend vs long-term
- Per-stream breakdown (house_base vs ev_charging vs total)
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Any

from .const import LOG_MAX_ENTRIES

_LOGGER = logging.getLogger(__name__)


class PredictionLogger:
    """In-memory rolling log of prediction & scheduling decisions."""

    def __init__(
        self,
        max_entries: int = LOG_MAX_ENTRIES,
        log_level: str = "info",
    ) -> None:
        self.max_entries = max_entries
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._log_level = log_level

        # Dedicated accuracy tracking — one record per observation cycle
        # Each record: {timestamp, stream, predicted_kwh, actual_kwh, error, pct_error}
        self._accuracy_records: deque[dict[str, Any]] = deque(maxlen=max_entries * 3)

        # Store the most recent prediction per stream so we can compare
        # against the actual measured value on the *next* coordinator cycle.
        self._last_predictions: dict[str, float] = {}
        self._last_prediction_ts: str | None = None

    # ------------------------------------------------------------------
    # Decision / event logging  (unchanged public API)
    # ------------------------------------------------------------------

    def log_decision(
        self,
        prices: dict[str, Any],
        predicted_consumption: list[float],
        schedule: dict[str, Any],
        sensor_data: dict[str, Any],
        prediction_split: dict[str, list[float]] | None = None,
    ) -> None:
        """Log a complete optimisation decision."""
        now = datetime.now()

        entry = {
            "timestamp": now.isoformat(),
            "type": "decision",
            "current_price": prices.get("current", 0),
            "price_stats": schedule.get("stats", {}),
            "immediate_action": (
                schedule.get("hourly_plan", [{}])[0].get("action", "unknown")
                if schedule.get("hourly_plan")
                else "none"
            ),
            "immediate_reason": (
                schedule.get("hourly_plan", [{}])[0].get("reason", "")
                if schedule.get("hourly_plan")
                else ""
            ),
            "predicted_consumption_next_hour": (
                predicted_consumption[0] if predicted_consumption else 0
            ),
            "battery_soc": sensor_data.get("battery_soc", 0),
            "pv_power": sensor_data.get("pv_power", 0),
            "house_load": sensor_data.get("house_load", 0),
            "ev_connected": sensor_data.get("ev_connected", False),
            "plan_summary": schedule.get("summary", ""),
            "num_charge_hours": len([
                h for h in schedule.get("hourly_plan", [])
                if h.get("action") == "charge_battery"
            ]),
            "num_discharge_hours": len([
                h for h in schedule.get("hourly_plan", [])
                if h.get("action") == "discharge_battery"
            ]),
        }

        self._entries.append(entry)

        # Store per-stream predictions for accuracy tracking
        self._last_prediction_ts = now.isoformat()
        self._last_predictions["total"] = (
            predicted_consumption[0] if predicted_consumption else 0
        )
        if prediction_split:
            for stream, values in prediction_split.items():
                if values:
                    self._last_predictions[stream] = values[0]

        if self._log_level == "debug":
            _LOGGER.debug("Decision logged: %s", entry)
        else:
            _LOGGER.info(
                "Energy plan: %s | Price: %.2f | SoC: %.0f%% | Action: %s",
                entry["plan_summary"][:80],
                entry["current_price"],
                entry["battery_soc"],
                entry["immediate_action"],
            )

    def log_actual(
        self,
        actual_consumption_kwh: float,
        actual_price: float,
        actual_soc: float,
        actual_house_kwh: float | None = None,
        actual_ev_kwh: float | None = None,
    ) -> None:
        """Log actual measured values and compute prediction error.

        Call this every coordinator cycle with the *measured* consumption
        that corresponds to the *previous* cycle's prediction.
        """
        now = datetime.now()

        entry: dict[str, Any] = {
            "timestamp": now.isoformat(),
            "type": "actual",
            "actual_consumption_kwh": actual_consumption_kwh,
            "actual_price": actual_price,
            "actual_soc": actual_soc,
        }
        if actual_house_kwh is not None:
            entry["actual_house_kwh"] = actual_house_kwh
        if actual_ev_kwh is not None:
            entry["actual_ev_kwh"] = actual_ev_kwh

        self._entries.append(entry)

        # --- Record accuracy for each stream that has a previous prediction ---
        streams_to_check = {
            "total": actual_consumption_kwh,
        }
        if actual_house_kwh is not None:
            streams_to_check["house_base"] = actual_house_kwh
        if actual_ev_kwh is not None:
            streams_to_check["ev_charging"] = actual_ev_kwh

        for stream, actual_val in streams_to_check.items():
            predicted = self._last_predictions.get(stream)
            if predicted is not None:
                error = abs(predicted - actual_val)
                pct_error = (
                    (error / actual_val * 100.0) if actual_val > 0.01 else 0.0
                )
                self._accuracy_records.append({
                    "timestamp": now.isoformat(),
                    "prediction_ts": self._last_prediction_ts,
                    "stream": stream,
                    "predicted_kwh": round(predicted, 3),
                    "actual_kwh": round(actual_val, 3),
                    "error_kwh": round(error, 3),
                    "pct_error": round(pct_error, 1),
                })

    def log_error(self, message: str) -> None:
        """Log an error event."""
        self._entries.append({
            "timestamp": datetime.now().isoformat(),
            "type": "error",
            "message": message,
        })

    # ------------------------------------------------------------------
    # Retrieval helpers
    # ------------------------------------------------------------------

    def get_recent_entries(self, count: int = 20) -> list[dict[str, Any]]:
        """Return the most recent log entries."""
        entries = list(self._entries)
        return entries[-count:] if len(entries) > count else entries

    def get_all_entries(self) -> list[dict[str, Any]]:
        """Return all log entries."""
        return list(self._entries)

    # ------------------------------------------------------------------
    # Accuracy metrics
    # ------------------------------------------------------------------

    def get_prediction_accuracy(
        self,
        stream: str | None = None,
        last_n: int | None = None,
    ) -> dict[str, Any]:
        """Compute prediction accuracy metrics.

        Parameters
        ----------
        stream : str | None
            Filter to a specific stream (``total``, ``house_base``,
            ``ev_charging``).  ``None`` = all streams combined.
        last_n : int | None
            Only consider the last *n* records (for rolling windows).

        Returns
        -------
        dict with ``pairs``, ``mae_kwh``, ``mape_pct``,
        ``max_error_kwh``, ``min_error_kwh``, ``recent_errors``.
        """
        records = list(self._accuracy_records)

        if stream is not None:
            records = [r for r in records if r["stream"] == stream]

        if last_n is not None:
            records = records[-last_n:]

        if not records:
            return {
                "pairs": 0,
                "mae_kwh": None,
                "mape_pct": None,
                "max_error_kwh": None,
                "min_error_kwh": None,
                "recent_errors": [],
            }

        errors = [r["error_kwh"] for r in records]
        pct_errors = [r["pct_error"] for r in records if r["pct_error"] > 0]

        mae = round(sum(errors) / len(errors), 3)
        mape = round(sum(pct_errors) / len(pct_errors), 1) if pct_errors else 0.0

        return {
            "pairs": len(records),
            "mae_kwh": mae,
            "mape_pct": mape,
            "max_error_kwh": round(max(errors), 3),
            "min_error_kwh": round(min(errors), 3),
            "recent_errors": [
                {
                    "ts": r["timestamp"],
                    "stream": r["stream"],
                    "predicted": r["predicted_kwh"],
                    "actual": r["actual_kwh"],
                    "error": r["error_kwh"],
                }
                for r in records[-10:]
            ],
        }

    def get_accuracy_summary(self) -> dict[str, Any]:
        """High-level accuracy summary with rolling windows.

        Returns per-stream and overall metrics for:
        - all-time, last 24 records (~24 h), last 168 records (~7 d)
        """
        summary: dict[str, Any] = {}

        for stream in ("total", "house_base", "ev_charging"):
            summary[stream] = {
                "all_time": self.get_prediction_accuracy(stream=stream),
                "last_24h": self.get_prediction_accuracy(stream=stream, last_n=24),
                "last_7d": self.get_prediction_accuracy(stream=stream, last_n=168),
            }

        summary["combined"] = {
            "all_time": self.get_prediction_accuracy(),
            "last_24h": self.get_prediction_accuracy(last_n=24),
            "last_7d": self.get_prediction_accuracy(last_n=168),
        }

        return summary
