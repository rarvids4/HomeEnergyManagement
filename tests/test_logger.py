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

    def test_log_actual_with_split(self):
        """log_actual should accept per-stream actual values."""
        logger = PredictionLogger()
        logger.log_actual(
            actual_consumption_kwh=3.0,
            actual_price=0.80,
            actual_soc=65,
            actual_house_kwh=2.0,
            actual_ev_kwh=1.0,
        )
        entries = logger.get_all_entries()
        assert entries[0]["actual_house_kwh"] == 2.0
        assert entries[0]["actual_ev_kwh"] == 1.0

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
        assert accuracy["mae_kwh"] is None


class TestAccuracyTracking:
    """Test predicted-vs-actual error tracking over time."""

    def _make_logger_with_pairs(self, pairs, prediction_split=None):
        """Helper: create a logger with N decision→actual pairs."""
        logger = PredictionLogger(max_entries=500)
        for predicted, actual in pairs:
            split = None
            if prediction_split:
                split = {
                    "house_base": [predicted * 0.7],
                    "ev_charging": [predicted * 0.3],
                    "total": [predicted],
                }
            logger.log_decision(
                prices={"current": 0.50, "today": [0.5] * 24},
                predicted_consumption=[predicted] * 24,
                schedule={
                    "hourly_plan": [{"action": "self_consumption", "reason": "test"}],
                    "summary": "Test",
                    "stats": {},
                },
                sensor_data={"battery_soc": 50, "house_load": 500},
                prediction_split=split,
            )
            logger.log_actual(
                actual_consumption_kwh=actual,
                actual_price=0.50,
                actual_soc=50,
                actual_house_kwh=actual * 0.7 if prediction_split else None,
                actual_ev_kwh=actual * 0.3 if prediction_split else None,
            )
        return logger

    def test_mae_single_pair(self):
        """MAE with one pair should equal the absolute error."""
        logger = self._make_logger_with_pairs([(2.0, 2.5)])
        acc = logger.get_prediction_accuracy(stream="total")
        assert acc["pairs"] == 1
        assert acc["mae_kwh"] == 0.5

    def test_mae_multiple_pairs(self):
        """MAE with multiple pairs should be the average error."""
        pairs = [(2.0, 2.5), (3.0, 2.5), (1.0, 1.0)]
        # errors: 0.5, 0.5, 0.0 → MAE = 0.333
        logger = self._make_logger_with_pairs(pairs)
        acc = logger.get_prediction_accuracy(stream="total")
        assert acc["pairs"] == 3
        assert abs(acc["mae_kwh"] - 0.333) < 0.01

    def test_mape_calculated(self):
        """MAPE should be computed from actual values."""
        # predicted=2.0, actual=2.5 → error=0.5, pct=20%
        logger = self._make_logger_with_pairs([(2.0, 2.5)])
        acc = logger.get_prediction_accuracy(stream="total")
        assert acc["mape_pct"] == 20.0

    def test_per_stream_accuracy(self):
        """Accuracy should be trackable per stream."""
        logger = self._make_logger_with_pairs(
            [(2.0, 2.5)], prediction_split=True,
        )

        total_acc = logger.get_prediction_accuracy(stream="total")
        house_acc = logger.get_prediction_accuracy(stream="house_base")
        ev_acc = logger.get_prediction_accuracy(stream="ev_charging")

        assert total_acc["pairs"] == 1
        assert house_acc["pairs"] == 1
        assert ev_acc["pairs"] == 1

    def test_rolling_window_last_n(self):
        """last_n should only consider the most recent records."""
        pairs = [(1.0, 1.5)] * 10 + [(1.0, 1.0)] * 5
        logger = self._make_logger_with_pairs(pairs)

        # Last 5 pairs all have 0 error
        acc_last5 = logger.get_prediction_accuracy(stream="total", last_n=5)
        assert acc_last5["mae_kwh"] == 0.0

        # All 15 pairs: 10×0.5 + 5×0.0 = avg 0.333
        acc_all = logger.get_prediction_accuracy(stream="total")
        assert acc_all["mae_kwh"] > 0.3

    def test_accuracy_summary_has_all_streams(self):
        """get_accuracy_summary should return per-stream and combined."""
        logger = self._make_logger_with_pairs(
            [(2.0, 2.5)], prediction_split=True,
        )
        summary = logger.get_accuracy_summary()

        assert "total" in summary
        assert "house_base" in summary
        assert "ev_charging" in summary
        assert "combined" in summary

        # Each should have all_time, last_24h, last_7d
        for stream in ("total", "house_base", "ev_charging", "combined"):
            assert "all_time" in summary[stream]
            assert "last_24h" in summary[stream]
            assert "last_7d" in summary[stream]

    def test_recent_errors_in_accuracy(self):
        """Accuracy result should include recent error details."""
        logger = self._make_logger_with_pairs([(2.0, 2.5)])
        acc = logger.get_prediction_accuracy(stream="total")
        assert len(acc["recent_errors"]) == 1
        assert acc["recent_errors"][0]["predicted"] == 2.0
        assert acc["recent_errors"][0]["actual"] == 2.5
        assert acc["recent_errors"][0]["error"] == 0.5

    def test_perfect_prediction_has_zero_error(self):
        """Perfect predictions should produce 0 MAE."""
        pairs = [(1.5, 1.5), (2.0, 2.0), (3.0, 3.0)]
        logger = self._make_logger_with_pairs(pairs)
        acc = logger.get_prediction_accuracy(stream="total")
        assert acc["mae_kwh"] == 0.0

    def test_decision_stores_split_predictions(self):
        """log_decision with prediction_split should store per-stream."""
        logger = PredictionLogger()
        logger.log_decision(
            prices={"current": 0.50},
            predicted_consumption=[2.0],
            schedule={"hourly_plan": [], "summary": "test", "stats": {}},
            sensor_data={"battery_soc": 50},
            prediction_split={
                "house_base": [1.4],
                "ev_charging": [0.6],
                "total": [2.0],
            },
        )
        # Internal state should have stored per-stream predictions
        assert logger._last_predictions["total"] == 2.0
        assert logger._last_predictions["house_base"] == 1.4
        assert logger._last_predictions["ev_charging"] == 0.6
