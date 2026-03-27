"""EV scheduler — plans per-vehicle charging in cheapest available hours.

Strategy per vehicle:
  1. Calculate kWh needed (current SoC → target SoC).
  2. On Fridays, lower target (car parked at home on Saturday, solar fills rest).
  3. Schedule in cheapest hours with night preference (off-peak bonus).
  4. Exclude discharge_battery hours (selling to grid).
  5. Two-pass scheduling when min_charge_level is set:
     - Urgent pass: reach the floor SoC using cheapest near-term hours.
     - Deferred pass: floor → target using cheapest across full window
       (including day-2 when optimization_days=2).
  6. Compute charge start/stop times with midnight wraparound detection.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from .const import (
    ACTION_DISCHARGE_BATTERY,
    ACTION_SELF_CONSUMPTION,
    DEFAULT_EV_CHEAP_PRICE_THRESHOLD,
    DEFAULT_EV_DEPARTURE_TIME,
    DEFAULT_EV_MIN_CHARGE_LEVEL,
    DEFAULT_EV_MIN_DEPARTURE_SOC,
    DEFAULT_EV_NIGHT_END,
    DEFAULT_EV_NIGHT_PREFERENCE_SEK,
    DEFAULT_EV_NIGHT_START,
    DEFAULT_EV_OPTIMIZATION_WINDOW,
    DEFAULT_EV_TARGET_SOC,
    DEFAULT_EV_WEEKEND_TARGET_SOC,
)

_LOGGER = logging.getLogger(__name__)


class EVScheduler:
    """Plans per-vehicle EV charging across the price window."""

    def __init__(self, params: dict[str, Any]) -> None:
        # Night preference
        self.ev_night_start = params.get("ev_night_start", DEFAULT_EV_NIGHT_START)
        self.ev_night_end = params.get("ev_night_end", DEFAULT_EV_NIGHT_END)
        self.ev_night_preference = params.get(
            "ev_night_preference_sek", DEFAULT_EV_NIGHT_PREFERENCE_SEK
        )

        # Target SoC
        self.ev_default_target_soc = params.get(
            "ev_default_target_soc", DEFAULT_EV_TARGET_SOC
        )
        self.ev_weekend_target_soc = params.get(
            "ev_weekend_target_soc", DEFAULT_EV_WEEKEND_TARGET_SOC
        )

        # Departure & charge floor
        self.ev_default_departure_time = params.get(
            "ev_default_departure_time", DEFAULT_EV_DEPARTURE_TIME
        )
        self.ev_default_min_departure_soc = params.get(
            "ev_default_min_departure_soc", DEFAULT_EV_MIN_DEPARTURE_SOC
        )
        self.ev_default_min_charge_level = params.get(
            "ev_default_min_charge_level", DEFAULT_EV_MIN_CHARGE_LEVEL
        )

        # Optimization window
        self.ev_optimization_window = params.get(
            "ev_optimization_window", DEFAULT_EV_OPTIMIZATION_WINDOW
        )

        # Cheap price threshold
        self.ev_cheap_price_threshold = params.get(
            "ev_cheap_price_threshold", DEFAULT_EV_CHEAP_PRICE_THRESHOLD
        )

    # ------------------------------------------------------------------
    # Public: plan EV charging
    # ------------------------------------------------------------------

    def plan(
        self,
        hourly_plan: list[dict[str, Any]],
        ev_vehicles: list[dict[str, Any]],
        start_hour: int,
        near_term_hours: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Plan EV charging per vehicle to minimize grid energy cost.

        Parameters
        ----------
        hourly_plan : list[dict]
            Full plan including day-2 extension entries if applicable.
        ev_vehicles : list[dict]
            Per-vehicle data from the coordinator.
        start_hour : int
            Current hour (0-23).
        near_term_hours : int | None
            Number of entries from the original (battery) horizon.
            Hours beyond this are day-2 extension slots.
        now : datetime | None
            Current datetime (injected from orchestrator for testability).

        Returns
        -------
        dict with schedule, vehicles, totals, charge window.
        """
        schedule = self._build_empty_schedule(hourly_plan)
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

        now_dt = now or datetime.now()
        is_friday = now_dt.weekday() == 4

        # Build candidate hours (shared): all except discharge hours
        all_candidates = self._build_candidates(hourly_plan)
        boundary = near_term_hours if near_term_hours is not None else len(hourly_plan)

        vehicle_plans = []
        for ev in ev_vehicles:
            plan = self._schedule_vehicle(
                ev, hourly_plan, schedule, all_candidates,
                start_hour, boundary, is_friday,
            )
            vehicle_plans.append(plan)

        # Round totals
        for entry in schedule:
            entry["total_power_kw"] = round(entry["total_power_kw"], 2)

        total_kwh = sum(v["kwh_needed"] for v in vehicle_plans)
        total_kw = sum(
            v["charging_power_kw"]
            for v in vehicle_plans if v["kwh_needed"] > 0
        )

        # Overall charge window across all vehicles
        all_starts = [v["charge_start_time"] for v in vehicle_plans if v.get("charge_start_time")]
        all_stops = [v["charge_stop_time"] for v in vehicle_plans if v.get("charge_stop_time")]

        _LOGGER.debug(
            "EV charge plan: %d vehicles, %.1f kWh needed, %.1f kW total, "
            "scheduled %d hours",
            len(vehicle_plans), total_kwh, total_kw,
            len([s for s in schedule if s["charging"]]),
        )

        return {
            "schedule": schedule,
            "total_kwh_needed": round(total_kwh, 1),
            "total_charging_power_kw": round(total_kw, 1),
            "hours_needed": round(total_kwh / total_kw, 1) if total_kw > 0 else 0,
            "vehicles": vehicle_plans,
            "start_hour": start_hour,
            "charge_start_time": min(all_starts) if all_starts else None,
            "charge_stop_time": max(all_stops) if all_stops else None,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_empty_schedule(
        self, hourly_plan: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Build the base per-hour schedule structure."""
        return [
            {
                "hour": entry["hour"],
                "price": entry["price"],
                "spot_price": entry.get("spot_price", entry["price"]),
                "charging": False,
                "total_power_kw": 0.0,
                "vehicles": {},
            }
            for entry in hourly_plan
        ]

    def _build_candidates(
        self, hourly_plan: list[dict[str, Any]]
    ) -> list[tuple[int, float, int]]:
        """Build candidate hour list sorted by night-adjusted price.

        Excludes discharge_battery hours (we don't charge EVs while
        selling battery power to the grid).

        Returns list of (plan_index, price, hour) tuples.
        """
        candidates = [
            (i, entry["price"], entry["hour"])
            for i, entry in enumerate(hourly_plan)
            if entry["action"] != ACTION_DISCHARGE_BATTERY
        ]
        candidates.sort(key=self._night_adjusted_key)
        return candidates

    def _night_adjusted_key(self, candidate: tuple) -> float:
        """Sort key: apply night preference bonus to off-peak hours."""
        _idx, price, hour = candidate
        if hour >= self.ev_night_start or hour < self.ev_night_end:
            return price - self.ev_night_preference
        return price

    def _schedule_vehicle(
        self,
        ev: dict[str, Any],
        hourly_plan: list[dict[str, Any]],
        schedule: list[dict[str, Any]],
        all_candidates: list[tuple[int, float, int]],
        start_hour: int,
        boundary: int,
        is_friday: bool,
    ) -> dict[str, Any]:
        """Schedule a single vehicle and return its plan summary."""
        name = ev.get("name", "ev")
        soc = ev.get("vehicle_soc", 0)
        capacity = ev.get("vehicle_capacity_kwh", 0)
        charging_w = ev.get("vehicle_charging_power_w", 0)
        connected = ev.get("connected", False)

        # Per-vehicle departure config
        departure_str = ev.get("departure_time") or ""
        departure_hour = self._parse_departure(departure_str) if departure_str else None
        min_dep_soc = ev.get(
            "min_departure_soc", self.ev_default_min_departure_soc
        )
        min_charge_level = ev.get(
            "min_charge_level", self.ev_default_min_charge_level
        )

        # Target SoC
        target = min_dep_soc if min_dep_soc > 0 else self.ev_default_target_soc
        if is_friday and self.ev_weekend_target_soc < target:
            target = self.ev_weekend_target_soc

        # Charging power (kW)
        charging_kw = (charging_w / 1000.0) if charging_w > 0 else (
            ev.get("power_w", 7000) / 1000.0
        )
        if charging_kw <= 0:
            charging_kw = 7.0

        needs_charge = soc > 0 and capacity > 0 and soc < target

        if not needs_charge:
            return self._make_vehicle_plan(
                name, soc, target, capacity, 0, charging_kw if connected else 0,
                0, connected, [], departure_str, min_dep_soc, min_charge_level,
            )

        kwh_needed = (target - soc) / 100.0 * capacity

        # Filter candidates by departure time
        if departure_hour is not None:
            vehicle_candidates = [
                (idx, price, hour)
                for idx, price, hour in all_candidates
                if self._is_before_departure(hour, departure_hour, start_hour)
            ]
        else:
            vehicle_candidates = all_candidates

        # --- Schedule the hours ---
        scheduled_hours = self._allocate_hours(
            name, soc, capacity, charging_kw, kwh_needed,
            min_charge_level, target,
            hourly_plan, schedule, vehicle_candidates, all_candidates,
            boundary,
        )

        hours_needed_f = kwh_needed / charging_kw if charging_kw > 0 else 0
        start_time, stop_time = self._compute_charge_window(scheduled_hours)

        return self._make_vehicle_plan(
            name, soc, target, capacity, kwh_needed, charging_kw,
            hours_needed_f, connected, scheduled_hours,
            departure_str, min_dep_soc, min_charge_level,
            start_time, stop_time,
        )

    def _allocate_hours(
        self,
        name: str,
        soc: float,
        capacity: float,
        charging_kw: float,
        kwh_needed: float,
        min_charge_level: float,
        target: float,
        hourly_plan: list[dict[str, Any]],
        schedule: list[dict[str, Any]],
        vehicle_candidates: list[tuple[int, float, int]],
        all_candidates: list[tuple[int, float, int]],
        boundary: int,
    ) -> list[int]:
        """Allocate charging hours using two-pass or single-pass strategy.

        Returns sorted list of scheduled hours.
        """
        scheduled_hours: list[int] = []
        used_indices: set[int] = set()

        has_extended_window = boundary < len(hourly_plan)

        # Build the deferred pool (day-1 filtered + day-2 unfiltered)
        if has_extended_window and min_charge_level > 0:
            day1_filtered = [c for c in vehicle_candidates if c[0] < boundary]
            day2_all = [c for c in all_candidates if c[0] >= boundary]
            deferred_pool = day1_filtered + day2_all
            deferred_pool.sort(key=self._night_adjusted_key)
        else:
            deferred_pool = vehicle_candidates

        if min_charge_level > 0 and soc < min_charge_level:
            # --- TWO-PASS: urgent (floor) + deferred (target) ---
            urgent_kwh = (min_charge_level - soc) / 100.0 * capacity
            deferred_kwh = max(0, kwh_needed - urgent_kwh)

            _LOGGER.debug(
                "EV %s: two-pass — urgent %.1f kWh (SoC %.0f%% → "
                "floor %.0f%%), deferred %.1f kWh (→ target %.0f%%), "
                "extended=%s",
                name, urgent_kwh, soc, min_charge_level,
                deferred_kwh, target, has_extended_window,
            )

            # Pass 1: urgent — cheapest near-term hours
            near_candidates = [
                (idx, p, h) for idx, p, h in vehicle_candidates
                if idx < boundary
            ]
            if soc < 10:
                near_candidates.sort(key=lambda c: c[0])  # ASAP
            else:
                near_candidates.sort(key=self._night_adjusted_key)

            scheduled_hours, used_indices = self._fill_hours(
                name, schedule, hourly_plan, near_candidates,
                urgent_kwh, charging_kw, scheduled_hours, used_indices,
            )

            # Pass 2: deferred — cheapest across full window
            scheduled_hours, used_indices = self._fill_hours(
                name, schedule, hourly_plan, deferred_pool,
                deferred_kwh, charging_kw, scheduled_hours, used_indices,
            )

        elif min_charge_level > 0:
            # Above floor — all charge is deferred
            scheduled_hours, _ = self._fill_hours(
                name, schedule, hourly_plan, deferred_pool,
                kwh_needed, charging_kw, scheduled_hours, used_indices,
            )
        else:
            # No floor — standard cheapest-hour scheduling
            scheduled_hours, _ = self._fill_hours(
                name, schedule, hourly_plan, vehicle_candidates,
                kwh_needed, charging_kw, scheduled_hours, used_indices,
            )

        return sorted(scheduled_hours)

    def _fill_hours(
        self,
        name: str,
        schedule: list[dict[str, Any]],
        hourly_plan: list[dict[str, Any]],
        candidates: list[tuple[int, float, int]],
        kwh_remaining: float,
        charging_kw: float,
        scheduled_hours: list[int],
        used_indices: set[int],
    ) -> tuple[list[int], set[int]]:
        """Fill schedule slots from candidate list until kWh is met."""
        remaining = kwh_remaining
        for idx, _price, _hour in candidates:
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
        return scheduled_hours, used_indices

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_departure(dep_str: str) -> int:
        """Parse 'HH:MM' → hour int."""
        try:
            return int(dep_str.split(":")[0])
        except (ValueError, IndexError, AttributeError):
            return 6

    @staticmethod
    def _is_before_departure(
        hour: int, departure_hour: int, start_hour: int
    ) -> bool:
        """True if *hour* falls in the window before departure."""
        if departure_hour > start_hour:
            return start_hour <= hour < departure_hour
        else:
            return hour >= start_hour or hour < departure_hour

    @staticmethod
    def _compute_charge_window(
        sorted_hours: list[int],
    ) -> tuple[str | None, str | None]:
        """Compute charge start/stop times with midnight wraparound.

        Finds the largest gap in the sorted hours to detect the real
        contiguous block (which may wrap around midnight).
        """
        if not sorted_hours:
            return None, None

        gaps = []
        for i in range(len(sorted_hours) - 1):
            gaps.append((sorted_hours[i + 1] - sorted_hours[i], i))
        # Wrap gap
        wrap_gap = (
            sorted_hours[0] + 24 - sorted_hours[-1],
            len(sorted_hours) - 1,
        )
        gaps.append(wrap_gap)

        max_gap_size, max_gap_idx = max(gaps, key=lambda g: g[0])

        if max_gap_size > 1:
            start_hour = sorted_hours[(max_gap_idx + 1) % len(sorted_hours)]
            stop_hour = (sorted_hours[max_gap_idx] + 1) % 24
        else:
            start_hour = sorted_hours[0]
            stop_hour = (sorted_hours[-1] + 1) % 24

        return f"{start_hour:02d}:00", f"{stop_hour:02d}:00"

    @staticmethod
    def _make_vehicle_plan(
        name: str,
        soc: float,
        target: float,
        capacity: float,
        kwh_needed: float,
        charging_kw: float,
        hours_needed: float,
        connected: bool,
        scheduled_hours: list[int],
        departure_str: str,
        min_dep_soc: float,
        min_charge_level: float,
        charge_start_time: str | None = None,
        charge_stop_time: str | None = None,
    ) -> dict[str, Any]:
        """Build the vehicle plan dict."""
        return {
            "name": name,
            "soc": round(soc, 1),
            "target_soc": round(target, 1),
            "capacity_kwh": round(capacity, 1),
            "kwh_needed": round(kwh_needed, 1),
            "charging_power_kw": round(charging_kw, 1),
            "hours_needed": round(hours_needed, 1),
            "connected": connected,
            "scheduled_hours": scheduled_hours,
            "charge_start_time": charge_start_time,
            "charge_stop_time": charge_stop_time,
            "departure_time": departure_str,
            "min_departure_soc": min_dep_soc,
            "min_charge_level": min_charge_level,
        }
