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

Implementation:

```text
total_available_w = export_w − import_w + Σ(ev_power_w) − safety_margin_w
per_charger_w     = total_available_w / n_active_chargers
amps_per_charger  = floor(per_charger_w / (voltage × phases))
amps_per_charger  = clamp(amps_per_charger, min_current, max_current)
```

Solar is split equally among active chargers. Each is clamped to its hardware
range. **The controller fires every fast-loop tick (at most every 10 s,
clamped in [coordinator.py](../custom_components/home_energy_management/coordinator.py)),
so as solar wobbles the limit follows in real time — the cars never draw
from the grid.**

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
