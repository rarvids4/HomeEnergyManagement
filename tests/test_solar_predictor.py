"""Tests for the solar predictor module."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from custom_components.home_energy_management.solar_predictor import SolarPredictor


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def predictor():
    """A solar predictor for a ~20 kWp system at 58°N (SE3)."""
    return SolarPredictor(
        pv_peak_power_kw=20.0,
        latitude=58.0,
        longitude=18.0,
        cloud_opacity=0.75,
    )


@pytest.fixture
def noon_april():
    """Noon on 5 April 2025 (day-of-year 95), CET+2 (CEST)."""
    return datetime(2025, 4, 5, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))


@pytest.fixture
def midnight_april():
    """Midnight on 5 April 2025 (CEST)."""
    return datetime(2025, 4, 5, 0, 0, 0, tzinfo=timezone(timedelta(hours=2)))


# ── Clear-sky model tests ────────────────────────────────────────────

class TestClearSkyFactor:
    """Tests for the static clear-sky factor computation."""

    def test_noon_april_58n(self):
        """At solar noon on day 95 at 58°N, factor should be ~0.6."""
        factor = SolarPredictor._clear_sky_factor(58.0, 95, 12.0)
        assert 0.55 < factor < 0.65

    def test_midnight_is_zero(self):
        """At midnight (solar hour 0) the sun is below the horizon."""
        factor = SolarPredictor._clear_sky_factor(58.0, 95, 0.0)
        assert factor == 0.0

    def test_sunset_is_zero(self):
        """Late evening should have zero output."""
        factor = SolarPredictor._clear_sky_factor(58.0, 95, 21.0)
        assert factor == 0.0

    def test_early_morning_small(self):
        """6 AM solar time should have a small but positive factor."""
        factor = SolarPredictor._clear_sky_factor(58.0, 95, 6.0)
        # Sun is just above horizon, factor should be small
        assert 0.0 <= factor <= 0.15

    def test_summer_solstice_higher(self):
        """June 21 (day 172) should have a higher noon factor than April."""
        april = SolarPredictor._clear_sky_factor(58.0, 95, 12.0)
        june = SolarPredictor._clear_sky_factor(58.0, 172, 12.0)
        assert june > april

    def test_lower_latitude_higher(self):
        """A lower latitude should produce a higher noon factor."""
        high_lat = SolarPredictor._clear_sky_factor(58.0, 95, 12.0)
        low_lat = SolarPredictor._clear_sky_factor(45.0, 95, 12.0)
        assert low_lat > high_lat

    def test_symmetric_around_noon(self):
        """Morning and evening at same offset from noon should be equal."""
        morning = SolarPredictor._clear_sky_factor(58.0, 172, 9.0)
        evening = SolarPredictor._clear_sky_factor(58.0, 172, 15.0)
        assert abs(morning - evening) < 0.001


# ── Predict (low-level) tests ────────────────────────────────────────

class TestPredict:

    def test_no_pv_returns_zeros(self):
        """If pv_peak_power_kw == 0, all predictions should be 0."""
        sp = SolarPredictor(pv_peak_power_kw=0.0)
        result = sp.predict(24)
        assert result == [0.0] * 24

    def test_clear_day_total(self, predictor, midnight_april):
        """A clear day (0% cloud) should produce ~100 kWh total."""
        clouds = [0.0] * 24
        result = predictor.predict(24, clouds, midnight_april)
        total = sum(result)
        # With 20 kW peak at 58°N in April, expect 80–120 kWh
        assert 80 < total < 120, f"Clear day total = {total:.1f} kWh"

    def test_fully_cloudy_day_total(self, predictor, midnight_april):
        """100% clouds should yield ~25% of clear-sky output."""
        clear = predictor.predict(24, [0.0] * 24, midnight_april)
        cloudy = predictor.predict(24, [100.0] * 24, midnight_april)
        clear_total = sum(clear)
        cloudy_total = sum(cloudy)
        ratio = cloudy_total / clear_total if clear_total > 0 else 0
        # With cloud_opacity=0.75, expect ratio ≈ 0.25
        assert 0.20 < ratio < 0.30, f"Cloudy/clear ratio = {ratio:.2f}"

    def test_cloudy_matches_real_data(self, predictor, midnight_april):
        """100% cloudy April day at 58°N should produce ~25 kWh (real: 24.7)."""
        result = predictor.predict(24, [100.0] * 24, midnight_april)
        total = sum(result)
        assert 20 < total < 35, f"Cloudy day total = {total:.1f} kWh"

    def test_nighttime_hours_zero(self, predictor, midnight_april):
        """Hours 0–4 (midnight to 4 AM) should produce 0."""
        result = predictor.predict(24, [0.0] * 24, midnight_april)
        for h in range(5):
            assert result[h] == 0.0, f"Hour {h} should be 0, got {result[h]}"

    def test_noon_is_peak(self, predictor, midnight_april):
        """The highest production should be near solar noon."""
        result = predictor.predict(24, [0.0] * 24, midnight_april)
        peak_hour = result.index(max(result))
        # Solar noon at 18°E in CEST is ~12:48, so peak should be hour 12 or 13
        assert 11 <= peak_hour <= 14, f"Peak at hour {peak_hour}"

    def test_partial_clouds_intermediate(self, predictor, midnight_april):
        """50% clouds should produce ~62.5% of clear-sky output."""
        clear = predictor.predict(24, [0.0] * 24, midnight_april)
        partial = predictor.predict(24, [50.0] * 24, midnight_april)
        clear_total = sum(clear)
        partial_total = sum(partial)
        ratio = partial_total / clear_total if clear_total > 0 else 0
        # Expected: 1 - 0.5 * 0.75 = 0.625
        assert 0.55 < ratio < 0.70, f"50% cloud ratio = {ratio:.2f}"

    def test_output_length_matches_horizon(self, predictor, midnight_april):
        """Output list should match the requested horizon."""
        assert len(predictor.predict(12, [0.0] * 12, midnight_april)) == 12
        assert len(predictor.predict(48, [0.0] * 48, midnight_april)) == 48

    def test_cloud_coverage_extended(self, predictor, midnight_april):
        """If cloud_coverage is shorter than hours_ahead, it's extended."""
        result = predictor.predict(24, [50.0, 30.0], midnight_april)
        assert len(result) == 24
        # Last values should use the final cloud coverage (30%)


# ── Forecast integration tests ───────────────────────────────────────

class TestPredictFromForecast:

    @pytest.fixture
    def sample_forecast(self):
        """Sample weather forecast entries (simplified)."""
        base = datetime(2025, 4, 6, 0, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        entries = []
        # Generate 24 hours of forecast
        cloud_profile = [
            100, 100, 100, 100, 100, 100,  # 00-05: night, full cloud
            80, 44, 29, 22, 1, 7,           # 06-11: clearing
            11, 22, 14, 3, 0, 1,            # 12-17: mostly sunny
            0, 0, 0, 0, 0, 0,              # 18-23: clear night
        ]
        for h, cloud in enumerate(cloud_profile):
            dt = base + timedelta(hours=h)
            entries.append({
                "datetime": dt.isoformat(),
                "cloud_coverage": cloud,
                "condition": "sunny" if cloud < 30 else "cloudy",
                "temperature": 10.0,
            })
        return entries

    def test_forecast_produces_nonzero(self, predictor, sample_forecast):
        """With clearing forecast, total solar should be substantial."""
        now = datetime(2025, 4, 6, 0, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        result = predictor.predict_from_forecast(24, sample_forecast, now)
        total = sum(result)
        assert total > 50, f"Expected > 50 kWh from clearing forecast, got {total:.1f}"

    def test_forecast_night_hours_zero(self, predictor, sample_forecast):
        """Night hours should still be zero despite forecast data."""
        now = datetime(2025, 4, 6, 0, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        result = predictor.predict_from_forecast(24, sample_forecast, now)
        # First few hours (midnight to ~5 AM) should be zero
        for h in range(5):
            assert result[h] == 0.0, f"Hour {h} should be 0"

    def test_empty_forecast_fallback(self, predictor):
        """Empty forecast should still return values (using 50% cloud default)."""
        now = datetime(2025, 4, 6, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        result = predictor.predict_from_forecast(24, [], now)
        assert len(result) == 24
        # With 50% default, should still produce some solar during day
        assert sum(result) > 0


# ── Cloud extraction tests ───────────────────────────────────────────

class TestExtractCloudCoverage:

    def test_basic_extraction(self):
        """Forecast entries are mapped to correct planning hours."""
        base = datetime(2025, 4, 6, 10, 0, 0)
        entries = [
            {"datetime": "2025-04-06T10:00:00+02:00", "cloud_coverage": 20},
            {"datetime": "2025-04-06T11:00:00+02:00", "cloud_coverage": 40},
            {"datetime": "2025-04-06T12:00:00+02:00", "cloud_coverage": 60},
        ]
        result = SolarPredictor._extract_cloud_coverage(entries, base, 4)
        assert len(result) == 4
        assert result[0] == 20
        assert result[1] == 40
        assert result[2] == 60
        # Hour 3 should be forward-filled from hour 2
        assert result[3] == 60

    def test_missing_entries_forward_fill(self):
        """Gaps in forecast are filled with last known value."""
        base = datetime(2025, 4, 6, 10, 0, 0)
        entries = [
            {"datetime": "2025-04-06T10:00:00+02:00", "cloud_coverage": 30},
            # Skip hour 11
            {"datetime": "2025-04-06T12:00:00+02:00", "cloud_coverage": 70},
        ]
        result = SolarPredictor._extract_cloud_coverage(entries, base, 4)
        assert result[0] == 30
        assert result[1] == 30  # forward-filled
        assert result[2] == 70
        assert result[3] == 70  # forward-filled

    def test_no_forecast_entries(self):
        """Empty forecast uses 50% default."""
        base = datetime(2025, 4, 6, 10, 0, 0)
        result = SolarPredictor._extract_cloud_coverage([], base, 3)
        assert result == [50.0, 50.0, 50.0]


# ── Clock-to-solar-hour tests ────────────────────────────────────────

class TestClockToSolarHour:

    def test_stockholm_cest(self):
        """At clock noon CEST in Stockholm (18°E), solar hour should be ~11.2."""
        sp = SolarPredictor(longitude=18.0)
        dt = datetime(2025, 4, 6, 12, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        solar = sp._clock_to_solar_hour(dt)
        # Standard meridian for CEST (UTC+2) = 30°E
        # Correction = (18 - 30) / 15 = -0.8 hours
        # Solar hour = 12 - 0.8 = 11.2
        assert abs(solar - 11.2) < 0.1

    def test_solar_noon_is_at_correct_clock_time(self):
        """Solar noon (solar_hour=12) should occur at ~12:48 CEST for Stockholm."""
        sp = SolarPredictor(longitude=18.0)
        # Solar noon = clock_time + correction = 12
        # clock_time = 12 - correction = 12 + 0.8 = 12.8 → 12:48
        dt = datetime(2025, 4, 6, 12, 48, 0, tzinfo=timezone(timedelta(hours=2)))
        solar = sp._clock_to_solar_hour(dt)
        assert abs(solar - 12.0) < 0.1

    def test_naive_datetime_uses_cet_fallback(self):
        """Naive datetime assumes CET (UTC+1) timezone."""
        sp = SolarPredictor(longitude=15.0)  # standard CET meridian
        dt = datetime(2025, 4, 6, 12, 0, 0)  # naive
        solar = sp._clock_to_solar_hour(dt)
        # longitude=15°E matches CET meridian, so correction = 0
        assert abs(solar - 12.0) < 0.1
