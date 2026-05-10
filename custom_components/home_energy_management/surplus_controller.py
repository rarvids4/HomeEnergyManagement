"""Solar Surplus EV Charging Controller.

Architecture
============
This module owns the **entire** surplus-EV lifecycle, completely separate from
the LP optimiser.  The LP decides *battery* mode and *LP-scheduled* EV
charging on its 15-minute cycle.  This controller decides *EV on/off and
current* in real time based on live grid-power readings.

State machine
-------------

    INACTIVE
        │  export ≥ threshold_w (sustained for activation_delay_s)
        │  OR price < 0 (immediate, no debounce)
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

Architecture rules  (canonical reference — duplicated in docs/SURPLUS_ARCHITECTURE.md)
--------------------------------------------------------------------------------------
Rule 1 — ACTIVATION
    When net grid export (export_w - import_w) ≥ ``threshold_w`` for at least
    ``activation_delay_s`` seconds continuously, transition INACTIVE → ACTIVE.
    When the spot price is negative, skip the debounce and activate immediately.

Rule 2 — CHARGING  (OR condition)
    On every activation, ALL connected EVs that are below their target SoC
    start charging simultaneously.  Both chargers are enabled regardless of
    which one has the lower SoC.  Any EV that connects while the controller
    is ACTIVE is added to the active set automatically.

Rule 3 — CURRENT CONTROL
    On every fast-loop tick while ACTIVE, each charger's dynamic current limit
    is set so that total EV draw ≈ available solar, keeping net grid ≈ 0:

        available_w = export_w - import_w + Σ(ev_power_w) - safety_margin_w
        amps_per_charger = floor(available_w / n_chargers / (voltage × phases))

    Each charger is clamped to [min_current, max_current].

Rule 4 — DEACTIVATION TRIGGER
    When ``available_w < min_viable_w`` all chargers are clamped to
    ``min_current`` and the STOPPING timer starts.  If surplus recovers within
    ``deficit_timeout_s``, the controller returns to ACTIVE.

Rule 5 — CLEANUP
    When the deficit persists for ``deficit_timeout_s`` the controller
    transitions to INACTIVE and stops ALL surplus-started EVs.
"""

from __future__ import annotations

import enum
import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------

class SurplusState(enum.Enum):
    """States of the surplus EV charging state machine (see module docstring)."""
    INACTIVE    = "inactive"
    DEBOUNCING  = "debouncing"
    ACTIVE      = "active"
    STOPPING    = "stopping"


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class SurplusController:
    """Manages solar surplus EV charging, independent of the LP optimiser.

    Parameters (from mapping ``parameters`` section)
    -------------------------------------------------
    solar_surplus_threshold_w       X — minimum net export to trigger activation (W)
    surplus_activation_delay_s      Y — seconds export must be sustained before arming
    surplus_deficit_timeout_s       Z — seconds deficit must persist before stopping
    surplus_safety_margin_w             safety buffer subtracted from available power (W)

    The old ``surplus_grid_import_grace_seconds`` parameter is accepted as a
    fallback so existing mappings continue to work unchanged.

    Call :meth:`tick` on every fast-loop interval (e.g. every 10 s).  It
    returns a list of HA service-call dicts to be executed immediately by the
    coordinator.
    """

    def __init__(
        self,
        params: dict[str, Any],
        ev_chargers_cfg: list[dict[str, Any]],
        surplus_switch_cfg: dict[str, Any] | None = None,
    ) -> None:
        from .const import (
            DEFAULT_SOLAR_SURPLUS_THRESHOLD,
            DEFAULT_SURPLUS_SAFETY_MARGIN_W,
            DEFAULT_SURPLUS_ACTIVATION_DELAY_S,
            DEFAULT_SURPLUS_DEFICIT_TIMEOUT_S,
        )

        # Parameters
        self.threshold_w: float = params.get(
            "solar_surplus_threshold_w", DEFAULT_SOLAR_SURPLUS_THRESHOLD
        )
        # Y — activation debounce (fall back to old param name for compat)
        _grace = params.get("surplus_grid_import_grace_seconds", DEFAULT_SURPLUS_ACTIVATION_DELAY_S)
        self.activation_delay_s: float = params.get("surplus_activation_delay_s", _grace)
        # Z — deficit timeout (fall back to old param name for compat)
        self.deficit_timeout_s: float = params.get("surplus_deficit_timeout_s", _grace)
        self.safety_margin_w: float = params.get(
            "surplus_safety_margin_w", DEFAULT_SURPLUS_SAFETY_MARGIN_W
        )

        self.ev_chargers_cfg: list[dict[str, Any]] = ev_chargers_cfg
        self._surplus_switch_cfg: dict[str, Any] = surplus_switch_cfg or {}

        # State machine
        self.state: SurplusState = SurplusState.INACTIVE
        self._state_entered: float | None = None

        # Names of EVs currently being managed (started) by this controller
        self._active_charger_names: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """True when surplus EV charging is live (ACTIVE or STOPPING)."""
        return self.state in (SurplusState.ACTIVE, SurplusState.STOPPING)

    @property
    def active_charger_names(self) -> list[str]:
        """Names of chargers currently under surplus control."""
        return list(self._active_charger_names)

    def tick(
        self,
        grid_export_w: float,
        grid_import_w: float,
        ev_vehicles: list[dict[str, Any]],
        ev_connected: bool,
        current_price: float = 0.0,
    ) -> list[dict[str, Any]]:
        """Advance the state machine one tick. Returns HA service calls.

        Called by the coordinator fast loop (every ~10 s).
        The caller is responsible for executing the returned actions.
        """
        price_is_negative = current_price < 0
        # Positive = exporting (net solar surplus)
        net_surplus_w = grid_export_w - grid_import_w

        if self.state is SurplusState.INACTIVE:
            return self._handle_inactive(
                net_surplus_w, price_is_negative,
                ev_vehicles, ev_connected, grid_export_w, grid_import_w,
            )
        if self.state is SurplusState.DEBOUNCING:
            return self._handle_debouncing(
                net_surplus_w, price_is_negative,
                ev_vehicles, ev_connected, grid_export_w, grid_import_w,
            )
        if self.state is SurplusState.ACTIVE:
            return self._handle_active(
                net_surplus_w, price_is_negative,
                ev_vehicles, ev_connected, grid_export_w, grid_import_w,
            )
        if self.state is SurplusState.STOPPING:
            return self._handle_stopping(
                net_surplus_w, price_is_negative,
                ev_vehicles, ev_connected, grid_export_w, grid_import_w,
            )
        return []

    def status(self) -> dict[str, Any]:
        """Return controller status for sensor / diagnostic display."""
        elapsed: float | None = None
        if self._state_entered is not None:
            elapsed = round(time.monotonic() - self._state_entered, 1)
        return {
            "state": self.state.value,
            "active_charger_names": list(self._active_charger_names),
            "is_active": self.is_active,
            "threshold_w": self.threshold_w,
            "activation_delay_s": self.activation_delay_s,
            "deficit_timeout_s": self.deficit_timeout_s,
            "safety_margin_w": self.safety_margin_w,
            "state_elapsed_s": elapsed,
        }

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    def _handle_inactive(
        self, net_surplus_w, price_is_negative,
        ev_vehicles, ev_connected, grid_export_w, grid_import_w,
    ) -> list[dict[str, Any]]:
        # Rule 1: negative price → activate immediately (no debounce)
        if price_is_negative:
            _LOGGER.info("SurplusController: negative price → activating immediately")
            return self._enter_active(ev_vehicles, ev_connected, grid_export_w, grid_import_w)

        # Rule 1: export above threshold → start debounce timer
        if net_surplus_w >= self.threshold_w:
            self.state = SurplusState.DEBOUNCING
            self._state_entered = time.monotonic()
            _LOGGER.info(
                "SurplusController: export %.0f W ≥ threshold %.0f W → debouncing (%ds)",
                net_surplus_w, self.threshold_w, self.activation_delay_s,
            )
        return []

    def _handle_debouncing(
        self, net_surplus_w, price_is_negative,
        ev_vehicles, ev_connected, grid_export_w, grid_import_w,
    ) -> list[dict[str, Any]]:
        # Negative price → skip remaining debounce (Rule 1)
        if price_is_negative:
            _LOGGER.info("SurplusController: negative price during debounce → activating immediately")
            return self._enter_active(ev_vehicles, ev_connected, grid_export_w, grid_import_w)

        # Export dropped — reset timer (Rule 1: must be sustained)
        if net_surplus_w < self.threshold_w:
            _LOGGER.debug(
                "SurplusController: export dropped (%.0f W < %.0f W) → debounce reset",
                net_surplus_w, self.threshold_w,
            )
            self.state = SurplusState.INACTIVE
            self._state_entered = None
            return []

        elapsed = time.monotonic() - (self._state_entered or time.monotonic())
        if elapsed >= self.activation_delay_s:
            _LOGGER.info(
                "SurplusController: debounce %.1fs elapsed → activating", elapsed,
            )
            return self._enter_active(ev_vehicles, ev_connected, grid_export_w, grid_import_w)

        _LOGGER.debug(
            "SurplusController: debouncing %.1fs / %ds (export %.0f W)",
            elapsed, self.activation_delay_s, net_surplus_w,
        )
        return []

    def _handle_active(
        self, net_surplus_w, price_is_negative,
        ev_vehicles, ev_connected, grid_export_w, grid_import_w,
    ) -> list[dict[str, Any]]:
        vehicle_map = {v.get("name", ""): v for v in (ev_vehicles or [])}
        active_cfgs = self._active_charger_configs()

        total_ev_power_w = sum(
            vehicle_map.get(c.get("name", ""), {}).get("power_w", 0.0)
            for c in active_cfgs
        )
        total_available_w = (
            grid_export_w - grid_import_w + total_ev_power_w - self.safety_margin_w
        )
        min_viable_w = self._min_viable_w()

        # Rule 2: arm any newly connected EVs
        new_ev_actions = self._arm_newly_connected(ev_vehicles, ev_connected)

        # Rule 4: deficit → enter STOPPING
        if total_available_w < min_viable_w and not price_is_negative:
            _LOGGER.info(
                "SurplusController: deficit (%.0f W < %.0f W min_viable) → STOPPING",
                total_available_w, min_viable_w,
            )
            self.state = SurplusState.STOPPING
            self._state_entered = time.monotonic()
            active_cfgs = self._active_charger_configs()
            return new_ev_actions + self._set_all_to_min_current(active_cfgs)

        # Rule 3: current controller
        active_cfgs = self._active_charger_configs()
        return new_ev_actions + self._run_current_controller(
            active_cfgs, grid_export_w, grid_import_w, vehicle_map,
        )

    def _handle_stopping(
        self, net_surplus_w, price_is_negative,
        ev_vehicles, ev_connected, grid_export_w, grid_import_w,
    ) -> list[dict[str, Any]]:
        vehicle_map = {v.get("name", ""): v for v in (ev_vehicles or [])}
        active_cfgs = self._active_charger_configs()

        total_ev_power_w = sum(
            vehicle_map.get(c.get("name", ""), {}).get("power_w", 0.0)
            for c in active_cfgs
        )
        total_available_w = (
            grid_export_w - grid_import_w + total_ev_power_w - self.safety_margin_w
        )
        min_viable_w = self._min_viable_w()

        # Rule 4: surplus recovered → back to ACTIVE
        if total_available_w >= min_viable_w or price_is_negative:
            _LOGGER.info(
                "SurplusController: surplus recovered (%.0f W) → ACTIVE",
                total_available_w,
            )
            self.state = SurplusState.ACTIVE
            self._state_entered = time.monotonic()
            return self._run_current_controller(active_cfgs, grid_export_w, grid_import_w, vehicle_map)

        elapsed = time.monotonic() - (self._state_entered or time.monotonic())

        # Rule 5: deficit timeout expired → deactivate
        if elapsed >= self.deficit_timeout_s:
            _LOGGER.info(
                "SurplusController: deficit %.1fs ≥ %ds timeout → deactivating (Rule 5)",
                elapsed, self.deficit_timeout_s,
            )
            return self._enter_inactive(active_cfgs)

        _LOGGER.debug(
            "SurplusController: STOPPING grace %.1fs / %ds — holding at min current",
            elapsed, self.deficit_timeout_s,
        )
        return self._set_all_to_min_current(active_cfgs)

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    def _enter_active(
        self,
        ev_vehicles: list[dict[str, Any]],
        ev_connected: bool,
        grid_export_w: float,
        grid_import_w: float,
    ) -> list[dict[str, Any]]:
        """Rule 2: Activate — start ALL connected EVs below target SoC."""
        self.state = SurplusState.ACTIVE
        self._state_entered = time.monotonic()
        self._active_charger_names = []
        actions: list[dict[str, Any]] = []

        vehicle_map = {v.get("name", ""): v for v in (ev_vehicles or [])}

        for cfg in self.ev_chargers_cfg:
            name = cfg.get("name", "")
            vehicle = vehicle_map.get(name)
            connected = vehicle.get("connected", ev_connected) if vehicle else ev_connected
            if not connected:
                _LOGGER.debug("SurplusController: %s not connected — skipping", name)
                continue
            soc = vehicle.get("vehicle_soc", 0) if vehicle else 0
            target = (vehicle.get("vehicle_target_soc") or 100) if vehicle else 100
            if soc > 0 and soc >= target:
                _LOGGER.info(
                    "SurplusController: %s already at target SoC %.0f%% — skipping",
                    name, soc,
                )
                continue

            actions.extend(_start_charger(cfg))
            self._active_charger_names.append(name)
            _LOGGER.info(
                "SurplusController: activating %s (SoC %.0f%% / target %.0f%%)",
                name, soc, target,
            )

        # Rule 3: set initial current limits
        active_cfgs = self._active_charger_configs()
        vehicle_map_ctrl = {v.get("name", ""): v for v in (ev_vehicles or [])}
        current_actions = self._run_current_controller(
            active_cfgs, grid_export_w, grid_import_w, vehicle_map_ctrl,
        )

        switch_actions: list[dict[str, Any]] = []
        _set_surplus_switch(self._surplus_switch_cfg, switch_actions, active=True)

        return actions + current_actions + switch_actions

    def _enter_inactive(
        self,
        active_cfgs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Rule 5: Deactivate — stop all surplus-started EVs."""
        actions: list[dict[str, Any]] = []
        for cfg in active_cfgs:
            actions.extend(_stop_charger(cfg))
            _LOGGER.info("SurplusController: stopping %s", cfg.get("name", ""))

        self.state = SurplusState.INACTIVE
        self._state_entered = None
        self._active_charger_names = []

        switch_actions: list[dict[str, Any]] = []
        _set_surplus_switch(self._surplus_switch_cfg, switch_actions, active=False)

        return actions + switch_actions

    # ------------------------------------------------------------------
    # Rule 3: Current controller
    # ------------------------------------------------------------------

    def _run_current_controller(
        self,
        active_cfgs: list[dict[str, Any]],
        grid_export_w: float,
        grid_import_w: float,
        vehicle_map: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Adjust each charger's current to keep net grid power ≈ 0.

        Available solar is split equally among active chargers:
            available_w = export - import + Σ(ev_power) - safety_margin
            amps_each   = floor(available_w / n_chargers / (voltage × phases))
        """
        if not active_cfgs:
            return []

        total_ev_power_w = sum(
            vehicle_map.get(c.get("name", ""), {}).get("power_w", 0.0)
            for c in active_cfgs
        )
        total_available_w = (
            grid_export_w - grid_import_w + total_ev_power_w - self.safety_margin_w
        )
        n = len(active_cfgs)
        per_charger_w = total_available_w / n

        actions: list[dict[str, Any]] = []
        for cfg in active_cfgs:
            dyn_cfg = cfg.get("set_dynamic_limit", {})
            service = dyn_cfg.get("service")
            device_id = dyn_cfg.get("device_id")
            if not service or not device_id:
                continue  # switch-only charger — no current modulation

            voltage = dyn_cfg.get("voltage", 230)
            phases = dyn_cfg.get("phases", 3)
            min_current = dyn_cfg.get("min_current", 6)
            max_current = dyn_cfg.get("max_current", 32)

            target_amps = int(per_charger_w / (voltage * phases))
            target_amps = max(min_current, min(max_current, target_amps))

            _LOGGER.debug(
                "SurplusController: %s → %dA "
                "(avail/charger %.0f W, export %.0f W, import %.0f W)",
                cfg.get("name"), target_amps, per_charger_w,
                grid_export_w, grid_import_w,
            )
            actions.append({
                "service": service,
                "device_id": device_id,
                "data": {"current": target_amps},
            })
        return actions

    def _set_all_to_min_current(
        self, active_cfgs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Clamp all active chargers to minimum current (STOPPING state)."""
        actions: list[dict[str, Any]] = []
        for cfg in active_cfgs:
            dyn_cfg = cfg.get("set_dynamic_limit", {})
            service = dyn_cfg.get("service")
            device_id = dyn_cfg.get("device_id")
            min_current = dyn_cfg.get("min_current", 6)
            if service and device_id:
                actions.append({
                    "service": service,
                    "device_id": device_id,
                    "data": {"current": min_current},
                })
        return actions

    # ------------------------------------------------------------------
    # Rule 2: Arm newly connected EVs while ACTIVE
    # ------------------------------------------------------------------

    def _arm_newly_connected(
        self,
        ev_vehicles: list[dict[str, Any]],
        ev_connected: bool,
    ) -> list[dict[str, Any]]:
        """Start any EV that connects after surplus was already active."""
        vehicle_map = {v.get("name", ""): v for v in (ev_vehicles or [])}
        actions: list[dict[str, Any]] = []

        for cfg in self.ev_chargers_cfg:
            name = cfg.get("name", "")
            if name in self._active_charger_names:
                continue
            vehicle = vehicle_map.get(name)
            connected = vehicle.get("connected", ev_connected) if vehicle else ev_connected
            if not connected:
                continue
            soc = vehicle.get("vehicle_soc", 0) if vehicle else 0
            target = (vehicle.get("vehicle_target_soc") or 100) if vehicle else 100
            if soc > 0 and soc >= target:
                continue

            _LOGGER.info("SurplusController: newly connected EV %s — adding to surplus", name)
            actions.extend(_start_charger(cfg))
            self._active_charger_names.append(name)

        return actions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _active_charger_configs(self) -> list[dict[str, Any]]:
        """Return output configs for currently active chargers."""
        return [
            c for c in self.ev_chargers_cfg
            if c.get("name") in self._active_charger_names
        ]

    def _min_viable_w(self) -> float:
        """Minimum power that makes sense to run any charger."""
        min_w = self.threshold_w
        for cfg in self.ev_chargers_cfg:
            dyn_cfg = cfg.get("set_dynamic_limit", {})
            if dyn_cfg.get("service"):
                voltage = dyn_cfg.get("voltage", 230)
                phases = dyn_cfg.get("phases", 3)
                min_current = dyn_cfg.get("min_current", 6)
                min_w = max(min_w, float(min_current * voltage * phases))
        return min_w


# ---------------------------------------------------------------------------
# Module-level helpers (used by coordinator for HA service call execution)
# ---------------------------------------------------------------------------

def _start_charger(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    start = cfg.get("start_charging", {})
    if start.get("service"):
        return [{"service": start["service"], "entity_id": start["entity_id"], "data": {}}]
    return []


def _stop_charger(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    stop = cfg.get("stop_charging", {})
    if stop.get("service"):
        return [{"service": stop["service"], "entity_id": stop["entity_id"], "data": {}}]
    return []


def _set_surplus_switch(
    cfg: dict[str, Any],
    actions: list[dict[str, Any]],
    *,
    active: bool,
) -> None:
    """Append turn_on / turn_off for an optional smart-meter surplus switch."""
    entity_id = cfg.get("entity_id")
    service_on = cfg.get("service_on", "switch.turn_on")
    service_off = cfg.get("service_off", "switch.turn_off")
    service = service_on if active else service_off
    if entity_id and service:
        actions.append({"service": service, "entity_id": entity_id, "data": {}})
