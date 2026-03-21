"""Tests for the ConsumptionPredictor."""

import pytest
from datetime import datetime
from custom_components.home_energy_management.predictor import ConsumptionPredictor


class TestConsumptionPredictor:
    """Test the pattern-based consumption predictor."""

    def test_predict_returns_correct_length(self):
        """predict() should return exactly hours_ahead values."""
        pred = ConsumptionPredictor(history_days=7, recency_weight=0.7)
        result = pred.predict(hours_ahead=24, current_load=500)
        assert len(result) == 24

    def test_predict_returns_correct_length_48h(self):
        """predict() should work for 48-hour horizons."""
        pred = ConsumptionPredictor()
        result = pred.predict(hours_ahead=48, current_load=1000)
        assert len(result) == 48

    def test_fallback_when_no_history(self):
        """With no history, predictions should use the current load."""
        pred = ConsumptionPredictor()
        result = pred.predict(hours_ahead=1, current_load=2000)  # 2000W
        # Should be ~2.0 kWh (2000W / 1000)
        assert result[0] == 2.0

    def test_fallback_minimum(self):
        """With no history and zero load, should use a 0.5 kWh baseline."""
        pred = ConsumptionPredictor()
        result = pred.predict(hours_ahead=1, current_load=0)
        assert result[0] == 0.5

    def test_learns_from_observations(self):
        """After adding observations, predictions should reflect them."""
        pred = ConsumptionPredictor(history_days=7, recency_weight=0.5)

        # Add observations for Monday hour 10 — always ~3 kWh
        for week in range(4):
            pred.add_observation(
                timestamp=datetime(2026, 3, 2 + week * 7, 10, 0),  # Mondays
                consumption_kwh=3.0,
            )

        # Predict for Monday hour 10
        # We need to align "now" — the predictor uses datetime.now()
        # so we test the internal method directly
        prediction = pred._predict_hour(
            day_of_week=0,  # Monday
            hour=10,
            fallback_load=0,
        )

        assert abs(prediction - 3.0) < 0.1

    def test_recency_weight(self):
        """Recent observations should have more influence."""
        pred = ConsumptionPredictor(history_days=7, recency_weight=0.9)

        # Old observations: 1 kWh
        pred.add_observation(datetime(2026, 3, 2, 14, 0), 1.0)
        pred.add_observation(datetime(2026, 3, 9, 14, 0), 1.0)
        # Recent observation: 5 kWh
        pred.add_observation(datetime(2026, 3, 16, 14, 0), 5.0)

        prediction = pred._predict_hour(
            day_of_week=0,  # Monday
            hour=14,
            fallback_load=0,
        )

        # With high recency weight, prediction should be closer to 5 than 1
        assert prediction > 2.5

    def test_weighted_average_single_value(self):
        """Weighted average of a single value should return that value."""
        pred = ConsumptionPredictor()
        result = pred._weighted_average([4.2])
        assert result == 4.2

    def test_weighted_average_empty(self):
        """Weighted average of empty list should return 0."""
        pred = ConsumptionPredictor()
        result = pred._weighted_average([])
        assert result == 0.0

    def test_statistics_empty(self):
        """Statistics should report zero coverage with no data."""
        pred = ConsumptionPredictor()
        stats = pred.get_statistics()
        assert stats["total_observations"] == 0
        assert stats["covered_slots"] == 0
        assert stats["coverage_pct"] == 0.0
        assert stats["total_possible_slots"] == 168  # 7 * 24

    def test_statistics_after_observations(self):
        """Statistics should update after adding data."""
        pred = ConsumptionPredictor()
        pred.add_observation(datetime(2026, 3, 16, 10, 0), 2.0)
        pred.add_observation(datetime(2026, 3, 16, 11, 0), 2.5)

        stats = pred.get_statistics()
        assert stats["total_observations"] == 2
        assert stats["covered_slots"] == 2

    def test_history_trimming(self):
        """Observations beyond history_days should be trimmed."""
        pred = ConsumptionPredictor(history_days=3)

        # Add 5 observations for the same slot (Monday 08:00)
        for i in range(5):
            pred.add_observation(
                datetime(2026, 3, 2 + i * 7, 8, 0),  # 2026-03-02 is Monday
                float(i),
            )

        # Should only keep the last 3
        key = (0, 8)  # Monday, 8:00
        assert len(pred._history[key]) == 3

    def test_all_values_are_floats(self):
        """All predictions should be float values."""
        pred = ConsumptionPredictor()
        pred.add_observation(datetime(2026, 3, 16, 10, 0), 2.0)
        result = pred.predict(hours_ahead=24, current_load=500)
        for val in result:
            assert isinstance(val, float)
