"""Battery strategy — LP-based optimal charge/discharge scheduling.

Uses a pure-Python simplex LP solver (no scipy / numpy) to minimise
total electricity cost over the planning horizon.  Falls back to a
heuristic scheduler if the LP fails.

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

**Decision variables** (per hour *h* = 0 … H−1, 5 × H total):

  charge[h]     kWh charged into the battery     ≥ 0
  discharge[h]  kWh discharged from the battery   ≥ 0
  grid_buy[h]   kWh purchased from the grid       ≥ 0
  grid_sell[h]  kWh sold back to the grid         ≥ 0
  curtail[h]    kWh of solar discarded            ≥ 0, ≤ solar[h]

**Objective** — minimise total energy cost:

  min  Σ_h  buy_price[h] × grid_buy[h]
           − sell_price[h] × grid_sell[h]

============================================================
CONSTRAINTS
============================================================

C1  Energy balance  (equality, one per hour)
    Every kWh consumed, stored, sold, or wasted must come from
    somewhere.  Curtailment is solar that the inverter discards.
    solar[h] + discharge[h] + grid_buy[h]
      = consumption[h] + charge[h] + grid_sell[h] + curtail[h]

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
  discharge > ε (neg ahead)  → PRE_DISCHARGE   (force-discharge to make room)
  discharge > ε              → DISCHARGE_BATTERY (self-consumption on inverter)
  otherwise                  → SELF_CONSUMPTION  (grid-neutral)

Note: DISCHARGE_BATTERY is a *planning label* indicating the LP
expects stored energy to cover consumption.  The action builder maps
it to **self-consumption mode** on the inverter — the battery never
force-discharges or exports to the grid.  Only PRE_DISCHARGE triggers
actual force-discharge.
"""

from __future__ import annotations

import logging
from typing import Any

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
    DEFAULT_TERMINAL_SOC_WEIGHT,
    OUTPUT_SUNGROW,
)
from .price_analysis import PriceWindow

_LOGGER = logging.getLogger(__name__)

# Solver threshold — values below this are treated as zero
_LP_EPS = 0.01  # kWh

# Look-ahead for labelling PRE_DISCHARGE (hours)
_PRE_DISCHARGE_LOOKAHEAD = 4

# Minimum SoC (%) to allow pre-discharge (force-discharge).
# Pre-discharge is aggressive — it actively drains the battery to make
# room for free solar during upcoming negative-price hours.  Require
# sufficient stored energy so we don't micro-cycle a nearly-empty battery.
_PRE_DISCHARGE_MIN_SOC = 20  # %

# Minimum predicted solar (kWh) during negative-price hours to justify
# pre-discharge.  Without meaningful solar, there's less value in
# emptying the battery — self-consumption is safer.
_PRE_DISCHARGE_MIN_SOLAR = 0.5  # kWh total across lookahead hours


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

        # Terminal SoC value weight (0=off, 1=default)
        self.terminal_soc_weight: float = params.get(
            "terminal_soc_weight", DEFAULT_TERMINAL_SOC_WEIGHT
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
            # absorb naturally.  EVs will also be started by the action
            # builder to minimise grid export.  Only when:
            #   • grid export exceeds the surplus threshold
            #   • battery is not full
            #   • spot price is non-negative (neg prices take priority)
            elif (
                grid_export_power >= self.solar_surplus_threshold
                and battery_soc < self.max_soc
                and pw.spot[0] >= 0
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

        Variable layout  (5 × H total):
          x = [ charge₀…H₋₁ | discharge₀…H₋₁ | buy₀…H₋₁ | sell₀…H₋₁ | curtail₀…H₋₁ ]
              idx 0…H-1       H…2H-1            2H…3H-1    3H…4H-1      4H…5H-1

        The curtailment variable models solar production that the
        inverter discards when the battery is full and grid export
        is uneconomical (e.g. negative spot prices).  It carries
        zero cost in the objective so the LP will only curtail as
        a last resort.

        Uses the pure-Python simplex solver from ``lp_solver.py``
        — no scipy or numpy dependency.
        """
        from .lp_solver import linprog

        H = len(pw.effective)
        n = 5 * H

        eta_c = self.charge_efficiency
        eta_d = self.discharge_efficiency
        cap = self.battery_capacity

        # ── Objective vector (minimise c @ x) ──────────────────────
        # Buying from grid: full cost = spot (incl VAT) + grid tariff
        # Selling to grid: revenue = spot price only (no grid fee back)
        # The sell_price_factor accounts for retailer margin, VAT
        # adjustments, etc.  Curtailment has zero cost.
        #
        # Time-preference epsilon: when prices are equal the LP may
        # pick any hour.  A tiny penalty increasing with h makes the
        # LP prefer to act sooner (charge earlier, discharge earlier),
        # which is operationally better — earlier action means more
        # flexibility if forecasts change.
        _TIME_EPS = 1e-6  # SEK per hour — negligible vs real prices
        c = [0.0] * n
        for h in range(H):
            c[h] = _TIME_EPS * h                                     # tiny charge-later penalty
            c[H + h] = _TIME_EPS * h                                 # tiny discharge-later penalty
            c[2 * H + h] = pw.effective[h]                           # grid_buy cost
            c[3 * H + h] = -(pw.spot[h] * self.sell_price_factor)    # grid_sell revenue

        # ── Terminal SoC value ─────────────────────────────────────
        # In a rolling 24 h horizon the LP has no incentive to keep
        # energy stored at the last hour — it sells all solar and
        # depletes the battery.  The terminal value tells the LP that
        # stored energy has future worth beyond the horizon.
        #
        # terminal_val = Q1(effective) × η_d × weight
        #
        # Using the lower quartile naturally separates cheap / expensive
        # hours:
        #   • discharge penalty < expensive price → evening discharge OK
        #   • discharge penalty ≥ cheap price → battery preserved overnight
        #   • charge reward >> sell revenue → solar charges battery
        if self.terminal_soc_weight > 0 and H > 0:
            sorted_eff = sorted(pw.effective)
            q1_idx = max(0, len(sorted_eff) // 4)
            terminal_base = sorted_eff[q1_idx]
            terminal_val = self.terminal_soc_weight * terminal_base * eta_d

            _LOGGER.debug(
                "LP terminal SoC value: base=%.3f (Q1 effective), "
                "terminal_val=%.3f SEK/kWh stored",
                terminal_base, terminal_val,
            )

            for h in range(H):
                # Charging stores η_c kWh per kWh charged → reward
                c[h] += -terminal_val * eta_c
                # Discharging removes 1/η_d kWh per kWh delivered → penalty
                c[H + h] += terminal_val / eta_d

        # ── C1: Energy balance (equality) ──────────────────────────
        #   supply = demand + waste
        #   solar[h] + discharge[h] + grid_buy[h]
        #     = consumption[h] + charge[h] + grid_sell[h] + curtail[h]
        #
        #   Rearranged: −charge + discharge + buy − sell − curtail
        #               = consumption − solar
        A_eq = [[0.0] * n for _ in range(H)]
        b_eq = [0.0] * H
        for h in range(H):
            A_eq[h][h] = -1.0             # charge[h]
            A_eq[h][H + h] = 1.0          # discharge[h]
            A_eq[h][2 * H + h] = 1.0      # grid_buy[h]
            A_eq[h][3 * H + h] = -1.0     # grid_sell[h]
            A_eq[h][4 * H + h] = -1.0     # curtail[h]  (reduces available solar)
            b_eq[h] = consumption[h] - solar[h]

        # ── C2 & C3: SoC bounds (inequality, A_ub @ x ≤ b_ub) ─────
        A_ub = [[0.0] * n for _ in range(2 * H)]
        b_ub = [0.0] * (2 * H)

        for h in range(H):
            # C2 — lower bound:  −cumulative ≤ (soc₀ − min_soc)/100 × cap
            for i in range(h + 1):
                A_ub[h][i] = -eta_c             # −η_c × charge[i]
                A_ub[h][H + i] = 1.0 / eta_d    # +discharge[i]/η_d
            b_ub[h] = (initial_soc - self.min_soc) / 100.0 * cap

            # C3 — upper bound:  +cumulative ≤ (max_soc − soc₀)/100 × cap
            row = H + h
            for i in range(h + 1):
                A_ub[row][i] = eta_c              # +η_c × charge[i]
                A_ub[row][H + i] = -1.0 / eta_d   # −discharge[i]/η_d
            b_ub[row] = (self.max_soc - initial_soc) / 100.0 * cap

        # ── C4–C8: Variable bounds ─────────────────────────────────
        bounds: list[tuple[float, float | None]] = []

        # C4: charge bounds
        for h in range(H):
            bounds.append((0.0, self.max_charge_kw))

        # C5: discharge bounds
        for h in range(H):
            bounds.append((0.0, self.max_discharge_kw))

        # C6: grid_buy ≥ 0  (unbounded above)
        for h in range(H):
            bounds.append((0.0, None))

        # C7: grid_sell — only excess solar can be sold to grid.
        #   The battery NEVER exports to grid; discharge only offsets
        #   house consumption (self-consumption mode).  So the maximum
        #   that can ever be sold in an hour is the solar production.
        #   Block all export during negative spot prices.
        for h in range(H):
            if pw.spot[h] < 0:
                bounds.append((0.0, 0.0))
            else:
                bounds.append((0.0, max(0.0, solar[h])))

        # C8: curtailment — cannot curtail more solar than produced
        for h in range(H):
            bounds.append((0.0, max(0.0, solar[h])))

        # ── Solve ─────────────────────────────────────────────────
        result = linprog(
            c, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq, bounds=bounds,
        )

        if not result.success:
            raise RuntimeError(f"LP solver: {result.message}")

        x = result.x

        # ── Post-process: cancel simultaneous charge+discharge ────
        # The simplex can produce degenerate solutions where both
        # charge[h] and discharge[h] are non-zero.  This is never
        # physically useful (wastes efficiency both ways).  Reduce
        # both by the smaller value to get the net effect.
        for h in range(H):
            ch_raw = x[h]
            dis_raw = x[H + h]
            if ch_raw > _LP_EPS and dis_raw > _LP_EPS:
                cancel = min(ch_raw, dis_raw)
                x[h] -= cancel
                x[H + h] -= cancel

        # ── Build hourly plan from solution ───────────────────────
        plan: list[dict[str, Any]] = []
        sim_soc = initial_soc
        total_curtailed = 0.0

        for h in range(H):
            ch = float(x[h])
            dis = float(x[H + h])
            buy = float(x[2 * H + h])
            sell = float(x[3 * H + h])
            curtail = float(x[4 * H + h])
            total_curtailed += curtail

            sim_soc += (eta_c * ch - dis / eta_d) / cap * 100.0
            hour_of_day = (pw.current_hour + h) % 24
            spot = pw.spot[h] if h < len(pw.spot) else 0.0

            action, reason = self._classify_lp_hour(
                h, H, ch, dis, spot, pw.effective[h], sim_soc,
                pw.spot, solar, consumption,
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
                "lp_curtailed_kwh": round(curtail, 3),
                "lp_soc_after": round(sim_soc, 1),
            })

        if total_curtailed > 0.1:
            _LOGGER.info(
                "LP curtailed %.1f kWh of solar (battery full / neg prices)",
                total_curtailed,
            )

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
        all_solar: list[float] | None = None,
        all_consumption: list[float] | None = None,
    ) -> tuple[str, str]:
        """Map LP quantities for a single hour to an action label."""
        if all_solar is None:
            all_solar = []
        if all_consumption is None:
            all_consumption = []

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

        # Use net battery flow — handles numerical noise where both
        # charge and discharge are slightly non-zero.
        net = charge_kwh - discharge_kwh

        # Net charging
        if net > _LP_EPS:
            # When solar surplus covers the planned charge, classify as
            # self_consumption — the inverter absorbs solar naturally
            # without forced grid charging.
            solar_h = all_solar[h] if h < len(all_solar) else 0.0
            cons_h = all_consumption[h] if h < len(all_consumption) else 0.0
            solar_surplus = max(0.0, solar_h - cons_h)
            if solar_surplus > _LP_EPS and net <= solar_surplus + 0.1:
                return (
                    ACTION_SELF_CONSUMPTION,
                    f"LP: solar → battery {net:.2f} kWh "
                    f"at {effective:.2f} (SoC → {soc_after:.0f}%)",
                )
            return (
                ACTION_CHARGE_BATTERY,
                f"LP: charge {charge_kwh:.2f} kWh at {effective:.2f} "
                f"(SoC → {soc_after:.0f}%)",
            )

        # Net discharging — check for pre-discharge label
        if net < -_LP_EPS:
            lookahead_spot = all_spot[h + 1 : h + 1 + _PRE_DISCHARGE_LOOKAHEAD]
            has_neg_ahead = any(p < 0 for p in lookahead_spot)

            if has_neg_ahead:
                # Pre-discharge conditions (force-discharge to make room):
                #   1. Negative prices in next 4 hours  (checked above)
                #   2. SoC after discharge stays ≥ 20 %  (protect battery)
                #   3. Predicted solar during those neg-price hours
                #      exceeds threshold (otherwise no free energy to absorb)
                lookahead_solar = all_solar[h + 1 : h + 1 + _PRE_DISCHARGE_LOOKAHEAD]
                neg_hour_solar = sum(
                    s for p, s in zip(lookahead_spot, lookahead_solar)
                    if p < 0
                )
                if (
                    soc_after >= _PRE_DISCHARGE_MIN_SOC
                    and neg_hour_solar >= _PRE_DISCHARGE_MIN_SOLAR
                ):
                    return (
                        ACTION_PRE_DISCHARGE,
                        f"LP: pre-discharge {discharge_kwh:.2f} kWh at "
                        f"{effective:.2f} — negative prices ahead, "
                        f"solar {neg_hour_solar:.1f} kWh predicted "
                        f"(SoC → {soc_after:.0f}%)",
                    )
                # Conditions not met — fall through to self-consumption.
                # The inverter handles discharge passively in SC mode.

            return (
                ACTION_SELF_CONSUMPTION,
                f"LP: self-consumption (discharge {discharge_kwh:.2f} kWh) "
                f"at {effective:.2f} (SoC → {soc_after:.0f}%)",
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
    # HEURISTIC FALLBACK  (used when LP solver fails)
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
                ACTION_SELF_CONSUMPTION,
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
                ACTION_SELF_CONSUMPTION,
                f"Cheap price ({price:.2f}), charging battery "
                f"(SoC {battery_soc:.0f}%)",
            )

        # Only discharge if we have meaningful energy above the floor.
        # At least 10 % above min_soc ensures we don't micro-cycle
        # a nearly-empty battery for negligible savings.
        discharge_floor = self.min_soc + 10
        if (
            self.enable_battery
            and price >= expensive_threshold
            and battery_soc > discharge_floor
        ):
            return (
                ACTION_DISCHARGE_BATTERY,
                f"Expensive price ({price:.2f}), discharging battery "
                f"(SoC {battery_soc:.0f}%)",
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
