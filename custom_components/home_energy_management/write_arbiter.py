"""WriteArbiter — coordinate multiple writers targeting one control value.

Several parts of the integration may want to set the same inverter
control register (e.g. the LP wants ``battery_max_charge = 2 kW`` but
a future surplus controller might want ``500 W``). To avoid each
component fighting the others, they all *propose* a value via this
arbiter; on every emit cycle the arbiter returns the **minimum** of
all *non-stale* proposals — the safest common denominator.

The cadence at which the arbitrated value is written to the inverter
is driven by whichever caller invokes :meth:`arbitrate` most often
(typically the coordinator at every poll). This matches the user's
spec: *"the writer with the highest write interval sets the writing"*.

Stale proposals (older than ``max_age_s`` seconds) are ignored, so a
crashed or paused source can't pin the cap at a value forever.

Example
-------
::

    arb = WriteArbiter("battery_max_charge_power_w")
    arb.propose("lp", 2000, max_age_s=120)        # LP optimiser
    arb.propose("surplus", 500, max_age_s=30)     # surplus controller
    value = arb.arbitrate()                       # 500 (the minimum)
"""

from __future__ import annotations

import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)


class WriteArbiter:
    """Min-arbiter for multi-source control writes.

    Thread-safety: this class is not thread-safe; the integration's
    coordinator runs on a single asyncio loop so all proposals and
    arbitration calls happen on the same task.
    """

    __slots__ = ("name", "_proposals")

    def __init__(self, name: str) -> None:
        self.name = name
        # source -> (value, monotonic_ts, max_age_s)
        self._proposals: dict[str, tuple[float, float, float]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def propose(
        self,
        source: str,
        value: float,
        *,
        max_age_s: float = 120.0,
    ) -> None:
        """Register *source*'s desired value (in W, or whatever unit).

        Subsequent calls from the same *source* overwrite the previous
        proposal. ``max_age_s`` is the time after which the proposal is
        treated as stale and ignored by :meth:`arbitrate`.
        """
        self._proposals[source] = (float(value), time.monotonic(), float(max_age_s))

    def withdraw(self, source: str) -> None:
        """Remove *source*'s proposal entirely (e.g. component shutdown)."""
        self._proposals.pop(source, None)

    def arbitrate(self) -> float | None:
        """Return ``min(live proposals)`` or ``None`` if all stale/empty."""
        now = time.monotonic()
        live = [
            v for (v, ts, max_age) in self._proposals.values()
            if (now - ts) <= max_age
        ]
        if not live:
            return None
        return min(live)

    def diagnostics(self) -> dict[str, Any]:
        """Snapshot for sensor attributes / debugging."""
        now = time.monotonic()
        sources: dict[str, dict[str, Any]] = {}
        for src, (val, ts, max_age) in self._proposals.items():
            age = now - ts
            sources[src] = {
                "value": val,
                "age_s": round(age, 1),
                "stale": age > max_age,
                "max_age_s": max_age,
            }
        return {
            "name": self.name,
            "arbitrated": self.arbitrate(),
            "sources": sources,
        }
