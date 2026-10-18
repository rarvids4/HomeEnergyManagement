"""Tests for the rolling month-to-date spot average (monthly-average tariff mode)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.home_energy_management import _apply_options
from custom_components.home_energy_management.monthly_price import (
    MonthlyPriceTracker,
    daily_averages_from_history,
    daily_averages_from_statistics,
)
from custom_components.home_energy_management.optimizer import Optimizer
from custom_components.home_energy_management.price_analysis import PriceAnalyzer

TODAY = date(2026, 10, 16)


# ------------------------------------------------------------------
# MonthlyPriceTracker
# ------------------------------------------------------------------

def test_month_average_is_the_mean_of_every_hour_from_the_1st():
    t = MonthlyPriceTracker()
    t.record_day(date(2026, 10, 1), [1.0] * 24)
    t.record_day(date(2026, 10, 2), [0.0] * 12 + [2.0] * 12)   # avg 1.0
    t.record_day(date(2026, 10, 3), [4.0] * 23)                # DST-short day
    expected = (24 * 1.0 + 24 * 1.0 + 23 * 4.0) / 71
    assert t.month_average(date(2026, 10, 3)) == pytest.approx(expected)
    assert t.days_recorded(date(2026, 10, 3)) == 3


def test_month_average_ignores_other_months_and_future_days():
    t = MonthlyPriceTracker()
    t.record_day(date(2026, 9, 30), [9.0] * 24)    # last month
    t.record_day(date(2026, 10, 15), [1.0] * 24)
    t.record_day(date(2026, 10, 16), [3.0] * 24)
    t.record_day(date(2026, 10, 17), [9.0] * 24)   # tomorrow
    assert t.month_average(TODAY) == pytest.approx(2.0)


def test_month_average_none_without_data():
    assert MonthlyPriceTracker().month_average(TODAY) is None


def test_record_day_reports_changes_only():
    t = MonthlyPriceTracker()
    assert t.record_day(TODAY, [1.0, 2.0]) is True
    assert t.record_day(TODAY, [1.0, 2.0]) is False
    assert t.record_day(TODAY, [1.0, 3.0]) is True
    assert t.record_day(TODAY, []) is False


def test_prune_drops_previous_months():
    t = MonthlyPriceTracker()
    t.record_day(date(2026, 9, 30), [1.0])
    t.record_day(date(2026, 10, 1), [1.0])
    assert t.prune(date(2026, 10, 1)) is True
    assert list(t.days) == ["2026-10-01"]
    assert t.prune(date(2026, 10, 1)) is False


def test_new_month_starts_from_scratch():
    t = MonthlyPriceTracker()
    t.record_day(date(2026, 10, 31), [2.0] * 24)
    t.prune(date(2026, 11, 1))
    t.record_day(date(2026, 11, 1), [0.5] * 24)
    assert t.month_average(date(2026, 11, 1)) == pytest.approx(0.5)


def test_missing_days_up_to_but_not_including_today():
    t = MonthlyPriceTracker()
    t.record_day(date(2026, 10, 2), [1.0])
    assert t.missing_days(date(2026, 10, 4)) == [date(2026, 10, 1), date(2026, 10, 3)]
    assert t.missing_days(date(2026, 10, 1)) == []


def test_tracker_round_trips_through_storage_dict():
    t = MonthlyPriceTracker()
    t.record_day(TODAY, [1.0, 2.0])
    restored = MonthlyPriceTracker(dict(t.days))
    assert restored.month_average(TODAY) == t.month_average(TODAY)


# ------------------------------------------------------------------
# Back-fill from recorded price states
# ------------------------------------------------------------------

def _quarter_hours(day: date, price_at) -> list[tuple[datetime, float]]:
    start = datetime.combine(day, datetime.min.time())
    return [(start + timedelta(minutes=15 * i), price_at(i)) for i in range(96)]


def test_history_average_is_time_weighted():
    day = date(2026, 10, 10)
    points = _quarter_hours(day, lambda i: 1.0 if i < 32 else 2.5)   # 8 h at 1.0
    end = datetime.combine(day + timedelta(days=1), datetime.min.time())
    avgs = daily_averages_from_history(points, end)
    assert avgs == {day: pytest.approx((8 * 1.0 + 16 * 2.5) / 24)}


def test_history_unchanged_price_holds_until_next_change():
    """Identical consecutive prices create no state change — still weighted."""
    day = date(2026, 10, 10)
    start = datetime.combine(day, datetime.min.time())
    points = [(start, 1.0), (start + timedelta(hours=18), 3.0)]
    end = start + timedelta(days=1)
    assert daily_averages_from_history(points, end) == {
        day: pytest.approx((18 * 1.0 + 6 * 3.0) / 24),
    }


def test_history_spans_days_and_skips_partial_ones():
    first = date(2026, 10, 6)
    # History starts 04:15 on the 6th (recorder purge) → that day is partial.
    start = datetime.combine(first, datetime.min.time()) + timedelta(hours=4, minutes=15)
    points = [(start, 1.0), (start + timedelta(days=1), 2.0)]
    end = datetime.combine(first + timedelta(days=3), datetime.min.time())
    avgs = daily_averages_from_history(points, end)
    assert set(avgs) == {date(2026, 10, 7), date(2026, 10, 8)}
    # The 7th: 1.0 until 04:15, then 2.0
    assert avgs[date(2026, 10, 7)] == pytest.approx((4.25 * 1.0 + 19.75 * 2.0) / 24)
    assert avgs[date(2026, 10, 8)] == pytest.approx(2.0)


def test_statistics_average_per_day_with_hour_weight():
    rows = [(datetime(2026, 10, 1, h), 1.0 if h < 12 else 3.0) for h in range(24)]
    rows += [(datetime(2026, 10, 2, h), 0.5) for h in range(23)]   # DST-short
    assert daily_averages_from_statistics(rows) == {
        date(2026, 10, 1): (pytest.approx(2.0), 24),
        date(2026, 10, 2): (pytest.approx(0.5), 23),
    }


def test_statistics_skip_partial_days():
    rows = [(datetime(2026, 10, 1, h), 1.0) for h in range(20)]
    assert daily_averages_from_statistics(rows) == {}


def test_history_stops_at_end():
    day = date(2026, 10, 15)
    start = datetime.combine(day, datetime.min.time())
    points = [(start, 1.0)]
    # end = start of today → only full past days count
    assert daily_averages_from_history(points, start + timedelta(hours=12)) == {}


# ------------------------------------------------------------------
# Which monthly average the tariff mode uses
# ------------------------------------------------------------------

PARAMS = {"buy_price_mode": "monthly_average", "grid_tariff_peak_sek": 0,
          "grid_tariff_offpeak_sek": 0}


def test_rolling_average_beats_today_tomorrow_estimate():
    p = PriceAnalyzer(PARAMS)
    p.buy_price_rolling_avg = 0.70
    assert p.buy_energy_price(5.0, [5.0] * 48) == pytest.approx(0.70)


def test_fixed_monthly_average_beats_rolling():
    p = PriceAnalyzer({**PARAMS, "buy_price_monthly_avg_sek": 0.90})
    p.buy_price_rolling_avg = 0.70
    assert p.buy_energy_price(5.0, [5.0] * 48) == pytest.approx(0.90)


def test_floor_applies_to_rolling_average():
    p = PriceAnalyzer({**PARAMS, "buy_price_floor_sek": 0.49})
    p.buy_price_rolling_avg = 0.30
    assert p.buy_energy_price(5.0, []) == pytest.approx(0.49)


def test_zero_floor_means_no_floor():
    p = PriceAnalyzer({"buy_price_floor_sek": 0.0})
    assert p.buy_energy_price(-0.2, []) == pytest.approx(-0.2)


def test_spot_mode_ignores_rolling_average():
    p = PriceAnalyzer({})
    p.buy_price_rolling_avg = 0.70
    assert p.buy_energy_price(1.5, []) == pytest.approx(1.5)


def test_optimizer_passes_rolling_average_to_price_window():
    opt = Optimizer(dict(PARAMS), {"sungrow": {}})
    opt.buy_price_rolling_avg = 0.65
    assert opt.monthly_average({"today": [3.0] * 24}) == pytest.approx(0.65)
    assert opt.buy_energy_price(3.0, {"today": [3.0] * 24}) == pytest.approx(0.65)


# ------------------------------------------------------------------
# Options (UI) → optimizer
# ------------------------------------------------------------------

def test_options_set_tariff_mode_and_prices():
    opt = Optimizer({}, {"sungrow": {}})
    _apply_options(SimpleNamespace(optimizer=opt), {
        "buy_price_mode": "monthly_average",
        "buy_price_floor_sek": 0.49,
        "buy_price_monthly_avg_sek": 0.0,
    })
    opt.buy_price_rolling_avg = 0.30
    assert opt.buy_energy_price(-1.0, {}) == pytest.approx(0.49)
    opt.buy_price_rolling_avg = 0.80
    assert opt.buy_energy_price(-1.0, {}) == pytest.approx(0.80)


def test_options_peak_hours_reach_the_price_window():
    """Peak start/end from the options used to be stored but never applied."""
    opt = Optimizer({"grid_tariff_peak_sek": 1.0, "grid_tariff_offpeak_sek": 0.0},
                    {"sungrow": {}})
    _apply_options(SimpleNamespace(optimizer=opt), {
        "grid_tariff_peak_start": 8, "grid_tariff_peak_end": 20,
    })
    opt._sync_prices()
    assert opt._prices.get_grid_tariff(7) == 0.0
    assert opt._prices.get_grid_tariff(8) == 1.0
    assert opt._prices.get_grid_tariff(20) == 0.0


# ------------------------------------------------------------------
# Break-even sensor maths
# ------------------------------------------------------------------

def test_break_even_matches_the_lp_condition():
    from custom_components.home_energy_management.price_analysis import (
        grid_charge_break_even,
    )
    rt = 0.81
    be = grid_charge_break_even(1.30, 0.831, rt)
    assert be == pytest.approx((0.81 * 1.30 - 0.831) / 0.19)   # ≈ 1.17
    # At the break-even price, off-peak cost equals delivered peak value.
    assert be + 0.831 == pytest.approx(rt * (be + 1.30))
    assert grid_charge_break_even(1.40, 0.831, rt) == pytest.approx(1.595, abs=1e-3)
    assert grid_charge_break_even(1.0, 0.5, 1.0) is None


def test_month_days_lists_this_month_in_order():
    t = MonthlyPriceTracker()
    t.record_day(date(2026, 10, 9), [0.4])
    t.record_day(date(2026, 9, 30), [9.0])
    t.record_day(date(2026, 10, 7), [1.5])
    assert t.month_days(TODAY) == {"2026-10-07": 1.5, "2026-10-09": 0.4}


def test_round_trip_efficiency_from_battery_params():
    opt = Optimizer({"battery_charge_efficiency": 0.9,
                     "battery_discharge_efficiency": 0.95}, {"sungrow": {}})
    assert opt.round_trip_efficiency == pytest.approx(0.855)
