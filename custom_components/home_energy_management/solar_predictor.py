"""Solar production predictor — forecasts PV output from weather data.

Uses a clear-sky irradiance model scaled by:
  - Installed PV peak power (kW)
  - Location latitude / longitude (from HA config or mapping)
  - Hourly cloud coverage from the weather forecast

The clear-sky model computes the solar altitude angle for each hour
based on latitude, day-of-year, and longitude-corrected solar time.
The resulting sin(altitude) curve approximates the relative output
of a well-oriented PV system on a perfectly clear day.

Cloud coverage from the HA weather forecast is applied as a linear
reduction:

  cloud_factor = 1 − (cloud% / 100) × cloud_opacity

With the default cloud_opacity = 0.75, 100 % cloud cover still
yields 25 % of clear-sky output (diffuse radiation), which matches
real-world observations for typical home PV systems.

============================================================
CALIBRATION EXAMPLE  (Rickard's system, 2 × Sungrow, SE3)
============================================================

  pv_peak_power_kw = 20
  latitude = 58°N
  Day 95 (5 Apr) — 100 % clouds all day
    Model:  20 kW × 5.1 clear-sky-hours × 0.25 cloud = 25.5 kWh
    Actual: 24.7 kWh  ✓

  Same day, 0 % clouds (clear):
    Model:  20 × 5.1 × 1.0 = 102 kWh
    User reports: "20–100 kWh on a good day"  ✓
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

_LOGGER = logging.getLogger(__name__)


class SolarPredictor:
    """Predict hourly PV production using weather cloud-coverage forecast."""

    def __init__(
        self,
        pv_peak_power_kw: float = 0.0,
        latitude: float = 58.0,
        longitude: float = 16.0,
        cloud_opacity: float = 0.75,
    ) -> None:
        """Initialise the predictor.

        Parameters
        ----------
        pv_peak_power_kw
            Effective peak power of the PV system in kW.  Set this so
            that a perfectly clear day produces a realistic total.
            0 = no PV system → always returns zeros.
        latitude
            Site latitude in decimal degrees (north positive).
        longitude
            Site longitude in decimal degrees (east positive).
            Used only for clock-to-solar-time conversion.
        cloud_opacity
            How strongly clouds reduce output (0–1).
            0.75 means 100 % clouds → 25 % of clear-sky output.
        """
        self.pv_peak_power_kw = pv_peak_power_kw
        self.latitude = latitude
        self.longitude = longitude
        self.cloud_opacity = cloud_opacity

    # ==================================================================
    # Public API
    # ==================================================================

    def predict_from_forecast(
        self,
        hours_ahead: int,
        forecasts: list[dict[str, Any]],
        now: datetime | None = None,
    ) -> list[float]:
        """Predict solar kWh per hour from HA weather forecast entries.

        Each forecast entry is expected to have at least:
          - ``datetime``: ISO 8601 timestamp
          - ``cloud_coverage``: 0–100 (optional; defaults to 50)

        Parameters
        ----------
        hours_ahead
            Number of hourly slots in the planning horizon.
        forecasts
            Raw forecast list from ``weather.get_forecasts`` service.
        now
            Reference time (defaults to ``datetime.now()``).

        Returns
        -------
        list[float]
            Predicted solar production in kWh for each hour.
        """
        if now is None:
            now = datetime.now()

        cloud_coverage = self._extract_cloud_coverage(forecasts, now, hours_ahead)
        return self.predict(hours_ahead, cloud_coverage, now)

    def predict(
        self,
        hours_ahead: int,
        cloud_coverage: list[float] | None = None,
        now: datetime | None = None,
    ) -> list[float]:
        """Predict solar kWh per hour from a cloud-coverage array.

        Parameters
        ----------
        hours_ahead
            Number of hourly slots to predict.
        cloud_coverage
            Cloud coverage percentage (0–100) for each hour.
            If ``None``, defaults to 50 % (cautious mid-estimate).
        now
            Reference time (defaults to ``datetime.now()``).

        Returns
        -------
        list[float]
            Predicted solar production in kWh for each hour.
        """
        if self.pv_peak_power_kw <= 0:
            return [0.0] * hours_ahead

        if now is None:
            now = datetime.now()

        # Default / extend cloud_coverage to full horizon
        if cloud_coverage is None:
            cloud_coverage = [50.0] * hours_ahead
        while len(cloud_coverage) < hours_ahead:
            cloud_coverage.append(cloud_coverage[-1] if cloud_coverage else 50.0)

        result: list[float] = []
        for h in range(hours_ahead):
            dt = now + timedelta(hours=h)
            doy = dt.timetuple().tm_yday

            # Convert clock hour → solar hour
            solar_hour = self._clock_to_solar_hour(dt)

            # Clear-sky factor (0 = night, ~0.6 = noon in April @ 58°N)
            cs = self._clear_sky_factor(self.latitude, doy, solar_hour)

            # Cloud reduction
            cloud_pct = max(0.0, min(100.0, cloud_coverage[h]))
            cloud_factor = 1.0 - (cloud_pct / 100.0) * self.cloud_opacity

            solar_kwh = self.pv_peak_power_kw * cs * cloud_factor
            result.append(round(max(0.0, solar_kwh), 2))

        total = sum(result)
        if total > 0:
            _LOGGER.info(
                "Solar prediction: %.1f kWh total over %d hours "
                "(peak=%.1f kWh, pv_peak=%.1f kW, lat=%.1f°)",
                total,
                hours_ahead,
                max(result),
                self.pv_peak_power_kw,
                self.latitude,
            )

        return result

    # ==================================================================
    # Internal helpers
    # ==================================================================

    def _clock_to_solar_hour(self, dt: datetime) -> float:
        """Convert a local clock time to approximate solar time.

        Accounts for the longitude offset from the time-zone's
        standard meridian.  Ignores the Equation of Time (max ±16 min)
        which is negligible for hourly planning.
        """
        # Timezone offset in hours (assume CET = +1 if naive)
        tz_offset = dt.utcoffset()
        if tz_offset is not None:
            tz_hours = tz_offset.total_seconds() / 3600.0
        else:
            tz_hours = 1.0  # CET fallback

        # The standard meridian for this timezone
        standard_meridian = tz_hours * 15.0

        # Longitude correction: +1 hour per 15° east of the meridian
        longitude_correction = (self.longitude - standard_meridian) / 15.0

        clock_decimal = dt.hour + dt.minute / 60.0
        return clock_decimal + longitude_correction

    @staticmethod
    def _clear_sky_factor(latitude: float, day_of_year: int, solar_hour: float) -> float:
        """Clear-sky relative output (0–1) for one hour.

        Uses the solar altitude angle:
          sin(alt) = sin(lat)·sin(δ) + cos(lat)·cos(δ)·cos(ω)
        where δ is the solar declination and ω is the hour angle.

        Returns sin(altitude) clamped to [0, 1].  This naturally
        models the dawn/dusk ramp-up and air-mass effect.
        """
        lat_rad = math.radians(latitude)

        # Solar declination (Spencer, 1971 — simplified)
        dec = math.radians(
            23.45 * math.sin(math.radians(360.0 / 365.0 * (284 + day_of_year)))
        )

        # Hour angle: 0° at solar noon, 15° per hour
        ha = math.radians((solar_hour - 12.0) * 15.0)

        sin_alt = (
            math.sin(lat_rad) * math.sin(dec)
            + math.cos(lat_rad) * math.cos(dec) * math.cos(ha)
        )
        altitude = math.asin(max(-1.0, min(1.0, sin_alt)))

        if altitude <= 0:
            return 0.0

        return max(0.0, math.sin(altitude))

    @staticmethod
    def _extract_cloud_coverage(
        forecasts: list[dict[str, Any]],
        now: datetime,
        hours_ahead: int,
    ) -> list[float]:
        """Map weather forecast entries to planning-hour cloud coverage.

        Forecast timestamps are parsed and aligned to the planning
        grid (hour 0 = current hour, hour 1 = next hour, …).
        Gaps are forward-filled with the last known value.
        """
        base = now.replace(minute=0, second=0, microsecond=0)

        cloud_map: dict[int, float] = {}
        for entry in forecasts:
            dt_str = entry.get("datetime", "")
            cloud = entry.get("cloud_coverage")
            if cloud is None:
                continue
            try:
                fc_dt = datetime.fromisoformat(dt_str)
                # Make both datetimes naive-local for subtraction
                fc_local = fc_dt.astimezone().replace(tzinfo=None)
                base_naive = base.replace(tzinfo=None) if base.tzinfo else base

                delta = fc_local - base_naive
                hour_idx = round(delta.total_seconds() / 3600)

                if 0 <= hour_idx < hours_ahead:
                    cloud_map[hour_idx] = float(cloud)
            except (ValueError, TypeError, OSError):
                continue

        # Build array with forward-fill
        result: list[float] = []
        last_known = cloud_map.get(0, 50.0)
        for h in range(hours_ahead):
            if h in cloud_map:
                last_known = cloud_map[h]
            result.append(last_known)

        return result
