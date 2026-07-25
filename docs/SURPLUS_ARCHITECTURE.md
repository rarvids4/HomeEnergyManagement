# Solar Surplus EV Charging — Architecture

> Canonical reference for the **surplus controller** that runs independently
> of the LP optimiser. The 5 rules below are the contract; the
> [surplus_controller.py](../custom_components/home_energy_management/surplus_controller.py)
> module is the implementation.

---

## 1. Why a separate controller?

The LP optimiser plans **battery mode** and **scheduled EV charging** on a
15-minute cycle from price forecasts. It cannot react to the second-by-second
fluctuations of solar production.

Solar surplus is fundamentally a **real-time control** problem:

- Whether to start charging depends on **right now's** export power.
- The charger current must continuously track **right now's** available solar.
- Stopping must happen quickly when the sun goes behind a cloud.

These belong in a **state machine** that ticks every ~10 s on live grid
sensors — not in an LP that runs every 15 min on price forecasts.

```
┌─────────────────────────────────────────────────────────────────┐
│                      Coordinator (every ~10 s)                  │
│                                                                 │
│   ┌──────────────────┐         ┌────────────────────────────┐   │
│   │ LP Optimizer     │         │ SurplusController          │   │
│   │ (every 15 min)   │         │ (every fast-loop tick)     │   │
│   │                  │         │                            │   │
│   │ - battery mode   │         │ - EV on/off                │   │
│   │ - scheduled EV   │         │ - dynamic current          │   │
│   │ - export limit   │         │ - state machine            │   │
│   └────────┬─────────┘         └─────────────┬──────────────┘   │
│            │                                 │                  │
│            └────────────► ActionBuilder ◄────┘                  │
│                                  │                              │
│                                  ▼                              │
│                         HA service calls                        │
└─────────────────────────────────────────────────────────────────┘
```

The two paths never reach into each other's state. ActionBuilder receives a
`surplus_active: bool` flag from the coordinator and skips its EV branch
whenever the surplus controller is active (so it never fights the controller).

---

## 2. State machine

```
INACTIVE
    │  export ≥ threshold_w (sustained for activation_delay_s)
    │  OR price < 0  (immediate, no debounce)
    ▼
DEBOUNCING ──activation_delay_s elapsed──► ACTIVE
    │                                          │
    │  export < threshold_w (reset)            │  net_available < min_viable
    ▼                                          ▼
INACTIVE                                  STOPPING
                                               │  deficit_timeout_s elapsed
                                               ▼
                                          INACTIVE  (all surplus EVs stopped)
                                               ↑
                                     (surplus recovered → ACTIVE)
```

| State | Meaning | Emits |
|-------|---------|-------|
| `INACTIVE` | No surplus charging in progress | nothing |
| `DEBOUNCING` | Export above threshold, waiting for it to be sustained | nothing |
| `ACTIVE` | Surplus charging is live | `switch.turn_on`, `set_dynamic_limit` |
| `STOPPING` | Deficit detected, holding at min current before cleanup | `set_dynamic_limit` (min) |

---

## 3. The five rules

### Rule 1 — ACTIVATION
> When I am exporting to the grid above **X watts** (`threshold_w`) for **Y seconds** (`activation_delay_s`), surplus charging is set.

Implementation:
- `INACTIVE`: export ≥ `threshold_w` → enter `DEBOUNCING`, start timer.
- `DEBOUNCING`: brief sub-threshold dips are **tolerated** so cloud-edge
  flicker doesn’t restart the timer forever. The timer is only reset
  (back to `INACTIVE`) when export drops **below** `threshold_w − safety_margin_w`
  (hysteresis). Timer reaches `Y` → enter `ACTIVE`.
- Negative spot price short-circuits the debounce and activates immediately.

### Rule 2 — CHARGING (OR condition)
> When surplus is set, the cars start to charge (both are activated).

Implementation:
- On entering `ACTIVE`, **every** connected EV below its target SoC is started simultaneously.
- An EV that connects later while `ACTIVE` is added on the next tick.
- Each EV is independent — a vehicle at target SoC is skipped without affecting the others.

### Rule 3 — CURRENT CONTROLLER
> When cars are charging due to surplus (OR condition), a special current controller is live, ensuring that the power from and to the grid is almost zero.

Implementation — **PV feed-forward setpoint plus a closed-loop grid trim**:

```text
non_ev_load_w     = max(0, house_load_w − Σ(ev_power_w))
solar_surplus_w   = pv_power_w − non_ev_load_w          # feed-forward
total_available_w = solar_surplus_w − safety_margin_w − grid_trim_w
per_charger_w     = total_available_w / n_active_chargers
amps_per_charger  = floor(per_charger_w / (voltage × phases))
amps_per_charger  = clamp(amps_per_charger, min_current, max_current)
```

The **feed-forward** term is battery-independent on purpose: computing the
surplus from PV minus non-EV load (rather than from grid balance) stops
battery *discharge* from masquerading as sunshine, which would let the cars
drain the battery the LP is saving for an expensive hour.

The **trim** is the other half, and the reason the cars can't sit on the grid:

```text
error_w  = grid_import_w − grid_export_w                # >0 = importing
trim_w   = clamp(trim_w + gain × error_w, 0, solar_surplus_w)
```

Feed-forward alone is open-loop — it is only as accurate as its constants
(`voltage × phases`, gross-DC vs AC PV, inverter loss) and its sensors, and
it cannot see anything else competing for the same solar (e.g. the battery
charging). Every such error lands on the grid, and no amount of feed-forward
notices. Measured net grid import *is* the error signal, so it is integrated
back out of the setpoint. Guards:

- Accumulates only while chargers are actually under control, so ordinary
  house import can't wind it up against the next activation.
- Anti-windup: frozen once every active charger sits at `min_current`, where
  the remaining import is irreducible (a 3-phase charger's 6 A floor is
  ~4.1 kW) — Rule 4/5 resolve that case instead. It still decays on export.
- Bypassed while the spot price is negative, where import is deliberate.
- Cleared on deactivation, so each activation starts from clean feed-forward.

Solar is split equally among active chargers. Each is clamped to its hardware
range. **The controller fires every fast-loop tick (at most every 10 s,
clamped in [coordinator.py](../custom_components/home_energy_management/coordinator.py)),
so as solar wobbles the limit follows in real time — the cars never draw
from the grid.**

> ⚠️ `voltage × phases` is the **watts-per-amp** scaling that converts
> available power into a current setpoint — 230 × 3 = 690 W/A, 230 × 1 =
> 230 W/A. Get it wrong and every watt of surplus becomes N watts of
> charging, with the difference imported. Verify it against what the
> charger *actually does* while charging (for Easee: the status sensor's
> `state_outputPhase` / `config_phaseMode` attributes), never from the
> model name. Incident 24 Jul 2026: a 3-phase Easee configured as
> `phases: 1` drew 12.8 kW on ~4.4 kW of surplus for ten minutes.

> The integration **never** delegates surplus control to the charger
> hardware (Easee "smart" / "surplus" mode) or any smart-meter surplus
> switch. It re-issues the current setpoint itself on every tick.

### Rule 4 — DEACTIVATION TRIGGER
> If the power is still drawn from the grid even with very low charging power set to the cars, the surplus mode is deactivated after **Z seconds** (`deficit_timeout_s`).

Implementation:
- In `ACTIVE`: when `total_available_w < min_viable_w`, all active chargers
  are clamped to `min_current` and the controller transitions to `STOPPING`,
  starting the deficit timer.
- In `STOPPING`: if surplus recovers within `Z` seconds → back to `ACTIVE`.
- If `Z` elapses with the deficit unresolved → Rule 5 fires.

`min_viable_w` is the larger of `threshold_w` and `min_current × voltage × phases`
across all configured chargers.

### Rule 5 — CLEANUP
> When surplus is deactivated, charging due to surplus stops.

Implementation:
- On `STOPPING → INACTIVE`, **every** charger that was started by the
  surplus controller receives a `switch.turn_off` (or its configured
  `stop_charging` service).
- The controller forgets its active set; the next activation re-evaluates
  all connected EVs from scratch.

---

## 4. Configuration

All values live in the parameters block of `variable_mapping.local.yaml`
(or `variable_mapping.yaml` template). Defaults are in
[const.py](../custom_components/home_energy_management/const.py).

| Parameter | Default | Rule | Meaning |
|-----------|--------:|------|---------|
| `solar_surplus_threshold_w` | `2000` | 1 | X — minimum sustained export before activation |
| `surplus_activation_delay_seconds` | `60`   | 1 | Y — debounce window before activation |
| `surplus_deficit_timeout_seconds`  | `60`   | 4 | Z — grace before cleanup |
| `surplus_safety_margin_w`          | `200`  | 3 | Headroom kept toward export |
| `surplus_grid_trim_gain`           | `0.5`  | 3 | Fraction of measured net grid import corrected per tick (`1.0` = one tick, may overshoot with sensor lag) |
| `surplus_grid_trim_deadband_w`     | `100`  | 3 | Net grid flow ignored by the trim so meter noise doesn't jitter the current |
| `surplus_grid_import_grace_seconds`| `60`   | 1+4 | Backward-compat default for both Y and Z if not explicitly set |

---

## 5. Files (separation of concerns)

| File | Responsibility |
|------|----------------|
| [surplus_controller.py](../custom_components/home_energy_management/surplus_controller.py) | The state machine. **Owns** all surplus logic. |
| [coordinator.py](../custom_components/home_energy_management/coordinator.py) | Builds the controller, ticks it on every fast loop, executes returned service calls, exposes `surplus_active` to the LP path. |
| [optimizer.py](../custom_components/home_energy_management/optimizer.py) | LP only. Receives `surplus_active` and forwards to ActionBuilder so the LP doesn't fight the controller. |
| [action_builder.py](../custom_components/home_energy_management/action_builder.py) | LP path only. Skips EV start/stop when `surplus_active=True`. Owns inverter mode + export limit. |
| [sensor.py](../custom_components/home_energy_management/sensor.py) | `SurplusChargingSensor` reads `controller.status()` for diagnostics. |

---

## 6. Tests

| File | Coverage |
|------|----------|
| [tests/test_optimizer.py](../tests/test_optimizer.py) | SurplusController activation on negative price + solar surplus; LP path no longer touches surplus. |
| [tests/test_solar_timeseries.py](../tests/test_solar_timeseries.py) | Hardware-shaped (Sungrow + Easee) end-to-end ticks, dynamic current sweep, full 2 h scenario. |
| [tests/test_timeseries_integration.py](../tests/test_timeseries_integration.py) | Generic time-series — works for any user's hardware. |

Run with: `pytest`
