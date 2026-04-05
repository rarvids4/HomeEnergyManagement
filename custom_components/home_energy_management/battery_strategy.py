"""Battery strategy — LP-based optimal charge/discharge scheduling.

Replaces the old heuristic (price-percentile) scheduler with a proper
Linear Program that minimises total electricity cost over the planning
horizon.  Falls back to the heuristic when *scipy* is unavailable.

============================================================
PRICING MODEL
============================================================

All prices used by the LP include the full cost to the consumer:

  buy_price[h]  = Nordpool spot (incl. VAT)
                + grid transfer tariff (incl. VAT)
                = pw.effective[h]

  sell_price[h] = Nordpool spot (incl. VAT) × sell_factor
                  (no tariff earned back when selling)

The Nordpool HA integration delivers spot prices already including
25 % VAT.  The grid tariffs (peak / off-peak) configured by the user
must also include VAT.

============================================================
BATTERY EFFICIENCY
============================================================

Round-trip efficiency ≈ 85 % (typical home lithium batteries).
Modelled as two independent one-way efficiencies:

  η_charge    = √0.85 ≈ 0.922   (grid → battery)
  η_discharge = √0.85 ≈ 0.922   (battery → house/grid)

Charging 1 kWh from the grid stores only 0.922 kWh in the battery.
Discharging 1 kWh of stored energy delivers only 0.922 kWh.

============================================================
LP FORMULATION
============================================================

**Decision variables** (per hour *h* = 0 … H−1, 4 × H total):

  charge[h]     kWh charged into the battery     ≥ 0
  discharge[h]  kWh discharged from the battery   ≥ 0
  grid_buy[h]   kWh purchased from the grid       ≥ 0
  grid_sell[h]  kWh sold back to the grid         ≥ 0

**Objective** — minimise total energy cost:

  min  Σ_h  buy_price[h] × grid_buy[h]
           − sell_price[h] × grid_sell[h]

============================================================
CONSTRAINTS
============================================================

C1  Energy balance  (equality, one per hour)
    Every kWh consumed or stored must come from somewhere.
    solar[h] + discharge[h] + grid_buy[h]
      = consumption[h] + charge[h] + grid_sell[h]

C2  SoC lower bound  (inequality, one per hour)
    The battery must never drop below max(min_soc, 6 %).
    The 6 % hard floor protects battery health regardless
    of the user's configuration.
    soc₀ + Σ_{i≤h}(η_c·charge[i] − discharge[i]/η_d)/cap×100
      ≥ max(min_soc, 6)

C3  SoC upper bound  (inequality, one per hour)
    The battery must never exceed *max_soc*.
    soc₀ + Σ_{i≤h}(η_c·charge[i] − discharge[i]/η_d)/cap×100  ≤  max_soc

C4  Charge power limit  (bound, per hour)
    0 ≤ charge[h] ≤ max_charge_kW × 1 h

C5  Discharge power limit  (bound, per hour)
    0 ≤ discharge[h] ≤ max_discharge_kW × 1 h

C6  Non-negativity  (bounds)
    grid_buy[h] ≥ 0,  grid_sell[h] ≥ 0

C7  Negative-price export block  (conditional bound)
    When spot[h] < 0:  grid_sell[h] = 0
    Uses the *spot* price (not effective) to decide — avoids
    *paying* to export during negative-price hours.

============================================================
GRID NEUTRALITY
============================================================

When the LP solution has charge[h] ≈ 0 and discharge[h] ≈ 0, the
battery does not interact with the grid.  The energy balance reduces
to:  grid_buy[h] = consumption[h] − solar[h] + grid_sell[h]
The hour is classified as SELF_CONSUMPTION and the inverter runs in
self-consumption mode — grid-neutral from the battery's perspective.

============================================================
ACTION MAPPING  (LP solution → HA action labels)
============================================================

  spot < 0 and charge > 0    → MAXIMIZE_LOAD
  spot < 0                   → MAXIMIZE_LOAD
  charge > ε                 → CHARGE_BATTERY
  discharge > ε (neg ahead)  → PRE_DISCHARGE
  discharge > ε              → DISCHARGE_BATTERY
  otherwise                  → SELF_CONSUMPTION (grid-neutral)
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from .const import (
    ACTION_CHARGE_BATTERY,
    ACTION_DISCHARGE_BATTERY,
    ACTION_MAXIMIZE_LOAD,
    ACTION_PRE_DISCHARGE,
    ACTION_SELF_CONSUMPTION,
    BATTERY_SOC_HARD_FLOOR,
    DEFAULT_BATTERY_CAPACITY,
    DEFAULT_BATTERY_CHARGE_EFFICIENCY,
    DEFAULT_BATTERY_DISCHARGE_EFFICIENCY,
    DEFAULT_BATTERY_MAX_CHARGE_POWER_W,
    DEFAULT_BATTERY_MAX_DISCHARGE_POWER_W,
    DEFAULT_MAX_SOC,
    DEFAULT_MIN_PRICE_SPREAD,
    DEFAULT_MIN_SOC,
    DEFAULT_SELL_PRICE_FACTOR,
    DEFAULT_SOLAR_SURPLUS_THRESHOLD,
    OUTPUT_SUNGROW,
)
from .price_analysis import PriceWindow

_LOGGER = logging.getLogger(__name__)

# LP variable threshold — values below this are treated as zero
_LP_EPS = 0.01  # kWh

# Look-ahead for labelling PRE_DISCHARGE (hours)
_PRE_DISCHARGE_LOOKAHEAD = 4

# --------------- scipy (optional) ---------------
try:
    from scipy.optimize import linprog as _scipy_linprog

    _HAS_SCIPY = True
except ImportError:  # pragma: no cover
    _HAS_SCIPY = False
    _LOGGER.warning(
        "scipy not installed — LP battery optimisation disabled; "
        "falling back to heuristic scheduler"
    )


class BatteryStrategy:
    """LP-based battery charge/discharge optimiser.

    Parameters are read from the user's *variable_mapping* YAML.
    The class exposes a single public method :meth:`plan_battery`
    whose signature is unchanged from the heuristic version so that
    :class:`Optimizer` can call it without modification.
    """

    def __init__(self, params: dict[str, Any], outputs: dict[str, Any]) -> None:
        self.enable_battery = params.get("enable_battery_control", True)
        self.min_price_spread = params.get(
            "min_price_spread", DEFAULT_MIN_PRICE_SPREAD
        )

        sg_out = outputs.get(OUTPUT_SUNGROW, {})
        # Enforce the hard SoC floor — never below BATTERY_SOC_HARD_FLOOR
        configured_min_soc = sg_out.get("min_soc", DEFAULT_MIN_SOC)
        self.min_soc: float = max(configured_min_soc, BATTERY_SOC_HARD_FLOOR)
        self.max_soc: float = sg_out.get("max_soc", DEFAULT_MAX_SOC)
        self.battery_capacity: float = sg_out.get(
            "capacity_kwh", DEFAULT_BATTERY_CAPACITY
        )

        # Efficiencies (0–1)
        self.charge_efficiency: float = params.get(
            "battery_charge_efficiency", DEFAULT_BATTERY_CHARGE_EFFICIENCY
        )
        self.discharge_efficiency: float = params.get(
            "battery_discharge_efficiency", DEFAULT_BATTERY_DISCHARGE_EFFICIENCY
        )

        # Power limits (kW, derived from watts)
        charge_w = sg_out.get("set_forced_power", {}).get(
            "max", DEFAULT_BATTERY_MAX_CHARGE_POWER_W
        )
        self.max_charge_kw: float = (
            params.get("battery_max_charge_power_w", charge_w) / 1000.0
        )

        discharge_w = sg_out.get("set_discharge_power", {}).get(
            "max", DEFAULT_BATTERY_MAX_DISCHARGE_POWER_W
        )
        self.max_discharge_kw: float = (
            params.get("battery_max_discharge_power_w", discharge_w) / 1000.0
        )

        # Sell-price factor
        self.sell_price_factor: float = params.get(
            "sell_price_factor", DEFAULT_SELL_PRICE_FACTOR
        )

        self.solar_surplus_threshold = params.get(
            "solar_surplus_threshold_w", DEFAULT_SOLAR_SURPLUS_THRESHOLD
        )

    # ==================================================================
    # Public API
    # ==================================================================

    def plan_battery(
        self,
        pw: PriceWindow,
        predicted_consumption: list[float],
        battery_soc: float,
        grid_export_power: float = 0.0,
        predicted_solar: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        """Produce an hour-by-hour battery action plan.

        Parameters
        ----------
        pw : PriceWindow
            Pre-computed price window (effective + spot prices).
        predicted_consumption : list[float]
            Predicted house consumption per hour (kWh).
        battery_soc : float
            Current battery state of charge (%).
        grid_export_power : float
            Current grid export in Watts (real-time, hour-0 only).
        predicted_solar : list[float] | None
            Predicted solar production per hour (kWh).  ``None`` → 0.

        Returns
        -------
        list[dict]
            Hourly plan entries with action, reason, price, etc.
        """
        if not self.enable_battery or pw.is_empty:
            return self._passive_plan(pw, predicted_consumption)

        H = len(pw.effective)
        consumption = _pad(predicted_consumption, H)
        solar = _pad(predicted_solar or [], H)

        # Real-time solar surplus → use as hour-0 solar estimate
        if grid_export_power > 0 and solar[0] == 0:
            solar[0] = grid_export_power / 1000.0  # W → kWh

        # ── Check min_price_spread ────────────────────────────────
        # If the effective price spread is too small, battery arbitrage
        # yields negligible savings and isn't worth the wear.  Skip LP
        # and return a passive self-consumption plan.
        price_spread = max(pw.effective) - min(pw.effective)
        has_negative_spot = any(p < 0 for p in pw.spot)

        if price_spread < self.min_price_spread and not has_negative_spot:
            _LOGGER.debug(
                "Price spread %.3f < min_price_spread %.3f — "
                "defaulting to self-consumption",
                price_spread,
                self.min_price_spread,
            )
            return self._passive_plan(pw, predicted_consumption)

        plan: list[dict[str, Any]] | None = None

        if _HAS_SCIPY:
            try:
                plan = self._solve_lp(pw, consumption, solar, battery_soc)
            except Exception as exc:
                _LOGGER.warning(
                    "LP solver failed (%s), falling back to heuristic", exc
                )

        if plan is None:
            plan = self._heuristic_plan(
                pw, consumption, solar, battery_soc, grid_export_power
            )

        # ==============================================================
        # POST-PLAN GUARDRAILS  (override LP or heuristic for safety)
        # These run regardless of which planner produced the schedule.
        # ==============================================================
        if plan:
            # ── G1: Negative spot price → MAXIMIZE_LOAD ──────────────
            # When raw spot is negative we are PAID to consume.
            # Never discharge or export — absorb as much as possible.
            if pw.spot[0] < 0:
                if plan[0]["action"] in (
                    ACTION_DISCHARGE_BATTERY,
                    ACTION_PRE_DISCHARGE,
                    ACTION_SELF_CONSUMPTION,
                ):
                    plan[0]["action"] = ACTION_MAXIMIZE_LOAD
                    plan[0]["reason"] = (
                        f"GUARDRAIL: Negative spot ({pw.spot[0]:.3f}) — "
                        f"maximize load, block grid export"
                    )

            # ── G2: SoC at or below hard floor → never discharge ─────
            # Protects the battery from deep discharge regardless of
            # what the optimizer decided.
            elif battery_soc <= BATTERY_SOC_HARD_FLOOR:
                if plan[0]["action"] in (
                    ACTION_DISCHARGE_BATTERY,
                    ACTION_PRE_DISCHARGE,
                ):
                    # If spot is cheap, charge; otherwise self-consume
                    if pw.effective[0] <= pw.avg:
                        plan[0]["action"] = ACTION_CHARGE_BATTERY
                        plan[0]["reason"] = (
                            f"GUARDRAIL: SoC {battery_soc:.0f}% ≤ hard floor "
                            f"{BATTERY_SOC_HARD_FLOOR}% — charging at "
                            f"{pw.effective[0]:.2f} (below avg {pw.avg:.2f})"
                        )
                    else:
                        plan[0]["action"] = ACTION_SELF_CONSUMPTION
                        plan[0]["reason"] = (
                            f"GUARDRAIL: SoC {battery_soc:.0f}% ≤ hard floor "
                            f"{BATTERY_SOC_HARD_FLOOR}% — self-consumption "
                            f"(price {pw.effective[0]:.2f} above avg)"
                        )

            # ── G3: Solar surplus → self-consumption ─────────────────
            # When real-time solar surplus is detected, let the inverter
            # absorb naturally.  Only when:
            #   • grid export exceeds the surplus threshold
            #   • battery is not full
            #   • spot price is non-negative (neg prices take priority)
            #   • planner did NOT schedule discharge (selling stored
            #     energy at high prices is more valuable)
            elif (
                grid_export_power >= self.solar_surplus_threshold
                and battery_soc < self.max_soc
                and pw.spot[0] >= 0
                and plan[0]["action"] != ACTION_DISCHARGE_BATTERY
            ):
                plan[0]["action"] = ACTION_SELF_CONSUMPTION
                plan[0]["reason"] = (
                    f"GUARDRAIL: Solar surplus ({grid_export_power:.0f} W) — "
                    f"self-consumption to absorb surplus"
                )

        return plan

    # ==================================================================
    # LP SOLVER
    # ==================================================================

    def _solve_lp(
        self,
        pw: PriceWindow,
        consumption: list[float],
        solar: list[float],
        initial_soc: float,
    ) -> list[dict[str, Any]]:
        """Build and solve the LP; return the hourly plan.

        Variable layout  (4 × H total):
          x = [ charge₀…H₋₁ | discharge₀…H₋₁ | buy₀…H₋₁ | sell₀…H₋₁ ]
              idx 0…H-1       H…2H-1            2H…3H-1    3H…4H-1
        """
        H = len(pw.effective)
        n = 4 * H

        eta_c = self.charge_efficiency
        eta_d = self.discharge_efficiency
        cap = self.battery_capacity

        # ── Objective vector (minimise c @ x) ──────────────────────
        c = np.zeros(n)
        for h in range(H):
            c[2 * H + h] = pw.effective[h]                          # grid_buy cost
            c[3 * H + h] = -(pw.spot[h] * self.sell_price_factor)   # grid_sell revenue

        # ── C1: Energy balance (equality) ──────────────────────────
        #   −charge[h] + discharge[h] + buy[h] − sell[h]
        #     = consumption[h] − solar[h]
        A_eq = np.zeros((H, n))
        b_eq = np.zeros(H)
        for h in range(H):
            A_eq[h, h] = -1.0            # charge[h]
            A_eq[h, H + h] = 1.0         # discharge[h]
            A_eq[h, 2 * H + h] = 1.0     # grid_buy[h]
            A_eq[h, 3 * H + h] = -1.0    # grid_sell[h]
            b_eq[h] = consumption[h] - solar[h]

        # ── C2 & C3: SoC bounds (inequality, A_ub @ x ≤ b_ub) ────
        A_ub = np.zeros((2 * H, n))
        b_ub = np.zeros(2 * H)

        # Cumulative factor: soc[h] = soc₀ + Σ_{i≤h}(η_c·charge[i]
        #   − discharge[i]/η_d) / cap × 100
        for h in range(H):
            # C2 — lower bound:  −cumulative ≤ (soc₀ − min_soc)/100 × cap
            for i in range(h + 1):
                A_ub[h, i] = -eta_c             # −η_c × charge[i]
                A_ub[h, H + i] = 1.0 / eta_d   # +discharge[i]/η_d
            b_ub[h] = (initial_soc - self.min_soc) / 100.0 * cap

            # C3 — upper bound:  +cumulative ≤ (max_soc − soc₀)/100 × cap
            row = H + h
            for i in range(h + 1):
                A_ub[row, i] = eta_c             # +η_c × charge[i]
                A_ub[row, H + i] = -1.0 / eta_d  # −discharge[i]/η_d
            b_ub[row] = (self.max_soc - initial_soc) / 100.0 * cap

        # ── C4–C7: Variable bounds ────────────────────────────────
        bounds: list[tuple[float, float | None]] = []

        # C4: charge bounds
        for h in range(H):
            bounds.append((0.0, self.max_charge_kw))

        # C5: discharge bounds
        for h in range(H):
            bounds.append((0.0, self.max_discharge_kw))

        # C6: grid_buy ≥ 0
        for h in range(H):
            bounds.append((0.0, None))

        # C7: grid_sell — block export during negative spot prices
        for h in range(H):
            if pw.spot[h] < 0:
                bounds.append((0.0, 0.0))
            else:
                bounds.append((0.0, None))

        # ── Solve ─────────────────────────────────────────────────
        # Use HiGHS (scipy ≥ 1.9) if available, else revised simplex
        try:
            result = _scipy_linprog(
                c,
                A_ub=A_ub,
                b_ub=b_ub,
                A_eq=A_eq,
                b_eq=b_eq,
                bounds=bounds,
                method="highs",
            )
        except (ValueError, TypeError):
            result = _scipy_linprog(
                c,
                A_ub=A_ub,
                b_ub=b_ub,
                A_eq=A_eq,
                b_eq=b_eq,
                bounds=bounds,
                method="revised simplex",
            )

        if not result.success:
            raise RuntimeError(f"LP infeasible: {result.message}")

        x = result.x

        # ── Build hourly plan from solution ───────────────────────
        plan: list[dict[str, Any]] = []
        sim_soc = initial_soc

        for h in range(H):
            ch = float(x[h])
            dis = float(x[H + h])
            buy = float(x[2 * H + h])
            sell = float(x[3 * H + h])

            sim_soc += (eta_c * ch - dis / eta_d) / cap * 100.0
            hour_of_day = (pw.current_hour + h) % 24
            spot = pw.spot[h] if h < len(pw.spot) else 0.0

            action, reason = self._classify_lp_hour(
                h, H, ch, dis, spot, pw.effective[h], sim_soc, pw.spot
            )

            plan.append({
                "hour": hour_of_day,
                "action": action,
                "reason": reason,
                "price": round(pw.effective[h], 4),
                "spot_price": round(spot, 4),
                "predicted_consumption_kwh": round(consumption[h], 2),
                "predicted_solar_kwh": round(solar[h], 2),
                "lp_charge_kwh": round(ch, 3),
                "lp_discharge_kwh": round(dis, 3),
                "lp_grid_buy_kwh": round(buy, 3),
                "lp_grid_sell_kwh": round(sell, 3),
                "lp_soc_after": round(sim_soc, 1),
            })

        self._log_lp_summary(plan, result.fun)
        return plan

    # ------------------------------------------------------------------
    # LP → action classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_lp_hour(
        h: int,
        H: int,
        charge_kwh: float,
        discharge_kwh: float,
        spot: float,
        effective: float,
        soc_after: float,
        all_spot: list[float],
    ) -> tuple[str, str]:
        """Map LP quantities for a single hour to an action label."""

        # Negative spot → MAXIMIZE_LOAD (absorb everything)
        if spot < 0:
            if charge_kwh > _LP_EPS:
                return (
                    ACTION_MAXIMIZE_LOAD,
                    f"Negative spot ({spot:.3f}), charging battery "
                    f"{charge_kwh:.2f} kWh (SoC → {soc_after:.0f}%)",
                )
            return (
                ACTION_MAXIMIZE_LOAD,
                f"Negative spot ({spot:.3f}) — minimize grid export",
            )

        # Charge
        if charge_kwh > _LP_EPS and discharge_kwh <= _LP_EPS:
            return (
                ACTION_CHARGE_BATTERY,
                f"LP: charge {charge_kwh:.2f} kWh at {effective:.2f} "
                f"(SoC → {soc_after:.0f}%)",
            )

        # Discharge — check for pre-discharge label
        if discharge_kwh > _LP_EPS and charge_kwh <= _LP_EPS:
            lookahead = all_spot[h + 1 : h + 1 + _PRE_DISCHARGE_LOOKAHEAD]
            if any(p < 0 for p in lookahead):
                return (
                    ACTION_PRE_DISCHARGE,
                    f"LP: pre-discharge {discharge_kwh:.2f} kWh at "
                    f"{effective:.2f} — negative prices ahead "
                    f"(SoC → {soc_after:.0f}%)",
                )
            return (
                ACTION_DISCHARGE_BATTERY,
                f"LP: discharge {discharge_kwh:.2f} kWh at "
                f"{effective:.2f} (SoC → {soc_after:.0f}%)",
            )

        # Self-consumption (no significant battery action)
        return (
            ACTION_SELF_CONSUMPTION,
            f"LP: self-consumption at {effective:.2f} "
            f"(SoC {soc_after:.0f}%)",
        )

    @staticmethod
    def _log_lp_summary(plan: list[dict[str, Any]], obj_value: float) -> None:
        """Emit a compact info-level log line."""
        charge_h = sum(
            1 for e in plan if e["action"] == ACTION_CHARGE_BATTERY
        )
        discharge_h = sum(
            1 for e in plan
            if e["action"] in (ACTION_DISCHARGE_BATTERY, ACTION_PRE_DISCHARGE)
        )
        neg_h = sum(
            1 for e in plan if e["action"] == ACTION_MAXIMIZE_LOAD
        )
        _LOGGER.info(
            "LP solved: obj=%.4f  charge=%d  discharge=%d  "
            "neg-price=%d  self-consumption=%d hours",
            obj_value,
            charge_h,
            discharge_h,
            neg_h,
            len(plan) - charge_h - discharge_h - neg_h,
        )

    # ==================================================================
    # HEURISTIC FALLBACK  (used when scipy is not installed)
    # ==================================================================

    def _heuristic_plan(
        self,
        pw: PriceWindow,
        consumption: list[float],
        solar: list[float],
        battery_soc: float,
        grid_export_power: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Legacy heuristic: classify each hour by price position."""
        initial_soc = battery_soc
        hourly_plan: list[dict[str, Any]] = []

        for i, price in enumerate(pw.effective):
            hour = (pw.current_hour + i) % 24
            spot = pw.spot[i] if i < len(pw.spot) else price
            cons = consumption[i] if i < len(consumption) else 0.0
            sol = solar[i] if i < len(solar) else 0.0

            upcoming = pw.effective[i + 1 : i + 1 + _PRE_DISCHARGE_LOOKAHEAD]
            upcoming_spot_prices = pw.spot[i + 1 : i + 1 + _PRE_DISCHARGE_LOOKAHEAD]

            action, reason = self._classify_hour(
                price=price,
                avg_price=pw.avg,
                min_price=pw.min,
                max_price=pw.max,
                price_spread=pw.spread,
                battery_soc=battery_soc,
                consumption=cons,
                upcoming_prices=upcoming,
                grid_export_power=grid_export_power if i == 0 else 0.0,
                spot_price=spot,
                upcoming_spot=upcoming_spot_prices,
            )

            battery_soc = self._simulate_soc(battery_soc, action)

            hourly_plan.append({
                "hour": hour,
                "action": action,
                "reason": reason,
                "price": round(price, 4),
                "spot_price": round(spot, 4),
                "predicted_consumption_kwh": round(cons, 2),
                "predicted_solar_kwh": round(sol, 2),
                "soc_after": round(battery_soc, 1),
            })

        hourly_plan = self._limit_discharge_to_capacity(
            hourly_plan, initial_soc, consumption
        )
        return hourly_plan

    # ------------------------------------------------------------------
    # Heuristic helpers
    # ------------------------------------------------------------------

    def _classify_hour(
        self,
        price: float,
        avg_price: float,
        min_price: float,
        max_price: float,
        price_spread: float,
        battery_soc: float,
        consumption: float,
        upcoming_prices: list[float] | None = None,
        grid_export_power: float = 0.0,
        spot_price: float | None = None,
        upcoming_spot: list[float] | None = None,
    ) -> tuple[str, str]:
        """Decide what to do in a given hour (heuristic)."""
        if upcoming_prices is None:
            upcoming_prices = []
        if upcoming_spot is None:
            upcoming_spot = []

        # Use raw spot price for negative-price detection —
        # effective = spot + grid tariff, so effective can be positive
        # even when spot is negative.
        check_price = spot_price if spot_price is not None else price

        if check_price < 0:
            return (
                ACTION_MAXIMIZE_LOAD,
                f"Negative spot ({check_price:.3f}) — absorb surplus, "
                f"no grid export + all EVs ON",
            )

        if price_spread >= self.min_price_spread:
            _expensive_threshold = max_price - price_spread * 0.30
        else:
            _expensive_threshold = float("inf")

        if (
            grid_export_power > 0
            and self.enable_battery
            and battery_soc < self.max_soc
            and price < _expensive_threshold
        ):
            return (
                ACTION_SELF_CONSUMPTION,
                f"Solar surplus ({grid_export_power:.0f} W export) — "
                f"battery absorbing excess solar (SoC {battery_soc:.0f}%)",
            )

        # Check upcoming *spot* prices for negative values (not effective)
        lookahead_for_neg = upcoming_spot if upcoming_spot else upcoming_prices
        has_negative_ahead = any(p < 0 for p in lookahead_for_neg)
        if (
            has_negative_ahead
            and self.enable_battery
            and battery_soc > self.min_soc
            and check_price > 0
        ):
            return (
                ACTION_PRE_DISCHARGE,
                f"Pre-discharging at {price:.2f} — negative spot prices ahead, "
                f"making room in battery (SoC {battery_soc:.0f}%)",
            )

        if price_spread < self.min_price_spread:
            return ACTION_SELF_CONSUMPTION, "Price spread too small to optimise"

        cheap_threshold = min_price + price_spread * 0.30
        expensive_threshold = max_price - price_spread * 0.30

        if (
            self.enable_battery
            and price <= cheap_threshold
            and battery_soc < self.max_soc
        ):
            return (
                ACTION_CHARGE_BATTERY,
                f"Cheap price ({price:.2f}), charging battery "
                f"(SoC {battery_soc:.0f}%)",
            )

        if (
            self.enable_battery
            and price >= expensive_threshold
            and battery_soc > self.min_soc
        ):
            return (
                ACTION_DISCHARGE_BATTERY,
                f"Expensive price ({price:.2f}), discharging battery",
            )

        return (
            ACTION_SELF_CONSUMPTION,
            f"Normal price ({price:.2f}), self-consumption mode",
        )

    def _simulate_soc(self, soc: float, action: str) -> float:
        """Rough SoC change for heuristic planning."""
        delta = (2.0 / self.battery_capacity) * 100
        surplus_delta = (1.0 / self.battery_capacity) * 100

        if action == ACTION_CHARGE_BATTERY:
            return min(self.max_soc, soc + delta)
        if action == ACTION_MAXIMIZE_LOAD:
            return min(self.max_soc, soc + surplus_delta)
        if action in (ACTION_DISCHARGE_BATTERY, ACTION_PRE_DISCHARGE):
            return max(self.min_soc, soc - delta)
        return soc

    def _limit_discharge_to_capacity(
        self,
        hourly_plan: list[dict[str, Any]],
        initial_soc: float,
        consumption: list[float],
    ) -> list[dict[str, Any]]:
        """Limit heuristic discharge hours to available energy."""
        if not self.enable_battery:
            return hourly_plan

        discharge_indices = [
            (i, e["price"])
            for i, e in enumerate(hourly_plan)
            if e["action"] == ACTION_DISCHARGE_BATTERY
        ]
        if not discharge_indices:
            return hourly_plan

        discharge_indices.sort(key=lambda x: x[1], reverse=True)

        available_kwh = (
            (initial_soc - self.min_soc) / 100.0 * self.battery_capacity
        )
        if available_kwh <= 0:
            for idx, _ in discharge_indices:
                hourly_plan[idx]["action"] = ACTION_SELF_CONSUMPTION
                hourly_plan[idx]["reason"] = (
                    f"Battery depleted (SoC {initial_soc:.0f}%) — "
                    "self-consumption"
                )
            return hourly_plan

        if len(discharge_indices) == 1:
            return hourly_plan

        keep: set[int] = set()
        remaining = available_kwh
        for idx, price in discharge_indices:
            need = max(
                consumption[idx] if idx < len(consumption) else 1.0,
                0.5,
            )
            if remaining >= need:
                keep.add(idx)
                remaining -= need
            else:
                hourly_plan[idx]["action"] = ACTION_SELF_CONSUMPTION
                hourly_plan[idx]["reason"] = (
                    f"Battery capacity limited — self-consumption "
                    f"(price {price:.2f}, saving for more expensive hours)"
                )

        return hourly_plan

    # ==================================================================
    # Shared helpers
    # ==================================================================

    def _passive_plan(
        self,
        pw: PriceWindow,
        predicted_consumption: list[float],
    ) -> list[dict[str, Any]]:
        """Self-consumption plan when battery control is disabled."""
        plan: list[dict[str, Any]] = []
        for i, price in enumerate(pw.effective):
            hour = (pw.current_hour + i) % 24
            spot = pw.spot[i] if i < len(pw.spot) else price
            cons = (
                predicted_consumption[i]
                if i < len(predicted_consumption)
                else 0.0
            )
            plan.append({
                "hour": hour,
                "action": ACTION_SELF_CONSUMPTION,
                "reason": "Battery control disabled",
                "price": round(price, 4),
                "spot_price": round(spot, 4),
                "predicted_consumption_kwh": round(cons, 2),
            })
        return plan


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _pad(values: list[float], length: int) -> list[float]:
    """Pad or truncate *values* to exactly *length* entries."""
    if len(values) >= length:
        return list(values[:length])
    return list(values) + [0.0] * (length - len(values))
