"""Tests for the PredictionLogger."""

from custom_components.home_energy_management.logger import PredictionLogger


class TestPredictionLogger:
    """Test the internal logging system."""

    def test_log_decision(self):
        logger = PredictionLogger(max_entries=100)
        logger.log_decision(
            prices={"current": 0.50, "today": [0.5] * 24},
            predicted_consumption=[1.0] * 24,
            schedule={
                "hourly_plan": [{"action": "charge_battery", "reason": "test"}],
                "summary": "Test plan",
                "stats": {"avg_price": 0.5},
            },
            sensor_data={"battery_soc": 50, "pv_power": 1000, "house_load": 500},
        )

        entries = logger.get_recent_entries(10)
        assert len(entries) == 1
        assert entries[0]["type"] == "decision"
        assert entries[0]["battery_soc"] == 50

    def test_log_actual(self):
        logger = PredictionLogger()
        logger.log_actual(
            actual_consumption_kwh=2.5,
            actual_price=0.80,
            actual_soc=65,
        )

        entries = logger.get_all_entries()
        assert len(entries) == 1
        assert entries[0]["type"] == "actual"

    def test_log_error(self):
        logger = PredictionLogger()
        logger.log_error("Something went wrong")

        entries = logger.get_all_entries()
        assert entries[0]["type"] == "error"
        assert "wrong" in entries[0]["message"]

    def test_max_entries_trimming(self):
        logger = PredictionLogger(max_entries=5)
        for i in range(10):
            logger.log_error(f"Error {i}")

        entries = logger.get_all_entries()
        assert len(entries) == 5
        # Should keep the most recent
        assert "Error 9" in entries[-1]["message"]

    def test_get_recent_entries(self):
        logger = PredictionLogger(max_entries=100)
        for i in range(50):
            logger.log_error(f"Error {i}")

        recent = logger.get_recent_entries(10)
        assert len(recent) == 10
        assert "Error 49" in recent[-1]["message"]

    def test_prediction_accuracy_no_data(self):
        logger = PredictionLogger()
        accuracy = logger.get_prediction_accuracy()
        assert accuracy["pairs"] == 0
        assert accuracy["mean_error_kwh"] is None
