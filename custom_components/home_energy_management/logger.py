"""Internal prediction & decision logger.

Stores a rolling log of every optimisation decision so you can review
predicted vs actual outcomes on a HA dashboard.
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

    def log_decision(
        self,
        prices: dict[str, Any],
        predicted_consumption: list[float],
        schedule: dict[str, Any],
        sensor_data: dict[str, Any],
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
    ) -> None:
        """Log actual measured values for comparison with predictions."""
        now = datetime.now()

        entry = {
            "timestamp": now.isoformat(),
            "type": "actual",
            "actual_consumption_kwh": actual_consumption_kwh,
            "actual_price": actual_price,
            "actual_soc": actual_soc,
        }

        self._entries.append(entry)

    def log_error(self, message: str) -> None:
        """Log an error event."""
        self._entries.append({
            "timestamp": datetime.now().isoformat(),
            "type": "error",
            "message": message,
        })

    def get_recent_entries(self, count: int = 20) -> list[dict[str, Any]]:
        """Return the most recent log entries."""
        entries = list(self._entries)
        return entries[-count:] if len(entries) > count else entries

    def get_all_entries(self) -> list[dict[str, Any]]:
        """Return all log entries."""
        return list(self._entries)

    def get_prediction_accuracy(self) -> dict[str, Any]:
        """Compare predicted vs actual values for accuracy reporting.

        Pairs 'decision' entries with subsequent 'actual' entries
        to compute error metrics.
        """
        decisions = [e for e in self._entries if e["type"] == "decision"]
        actuals = [e for e in self._entries if e["type"] == "actual"]

        if not decisions or not actuals:
            return {"pairs": 0, "mean_error_kwh": None}

        # Simple pairing: match by closest timestamp
        errors = []
        for actual in actuals:
            # Find the closest preceding decision
            closest = None
            for decision in decisions:
                if decision["timestamp"] <= actual["timestamp"]:
                    closest = decision

            if closest:
                predicted = closest.get("predicted_consumption_next_hour", 0)
                actual_val = actual.get("actual_consumption_kwh", 0)
                errors.append(abs(predicted - actual_val))

        if not errors:
            return {"pairs": 0, "mean_error_kwh": None}

        return {
            "pairs": len(errors),
            "mean_error_kwh": round(sum(errors) / len(errors), 3),
            "max_error_kwh": round(max(errors), 3),
            "min_error_kwh": round(min(errors), 3),
        }
