"""The session state: the one answer to "is the layout live?".

design_new.md "Session state": the session reads Setup, Live, Paused, or Halted
with a reason, derived on every read from six stored flags in this order:

1. Halted, `runtime_failure`          `SimulationController.failed_runtime`
2. Setup                              `AppState.setup_active`
3. Halted, `hardware_failure`         `hardware_failure` and no environment active
4. Halted, `not_activated`            no environment active
5. Halted, `layout_emergency_cut_out` `SimulationController.layout_cut_out_active`
6. Paused                             `AppState.simulation_running` is False
7. Live                               no row above holds

No command, stage, or event writes the session state; each writes its own flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from enums import HaltReason, SessionStateValue


@dataclass(frozen=True)
class SessionState:
    """`{value, halt_reason}`. `halt_reason` is set only while Halted."""

    value: SessionStateValue
    halt_reason: Optional[HaltReason] = None

    @property
    def is_live(self) -> bool:
        """The session is running: trains may be driven and the whole tick runs."""
        return self.value is SessionStateValue.LIVE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value.value,
            "halt_reason": self.halt_reason.value if self.halt_reason else None,
        }


LIVE = SessionState(SessionStateValue.LIVE)


def derive_session_state(
    sim_controller, *, setup_active: bool, simulation_running: bool
) -> SessionState:
    """The session state for these flags, per the precedence table above."""
    if getattr(sim_controller, "failed_runtime", False):
        return SessionState(SessionStateValue.HALTED, HaltReason.RUNTIME_FAILURE)
    if setup_active:
        return SessionState(SessionStateValue.SETUP)
    environment_active = bool(getattr(sim_controller, "environment_active", True))
    if not environment_active:
        if getattr(sim_controller, "hardware_failure", False):
            return SessionState(SessionStateValue.HALTED, HaltReason.HARDWARE_FAILURE)
        return SessionState(SessionStateValue.HALTED, HaltReason.NOT_ACTIVATED)
    if getattr(sim_controller, "layout_cut_out_active", False):
        return SessionState(
            SessionStateValue.HALTED, HaltReason.LAYOUT_EMERGENCY_CUT_OUT
        )
    if not simulation_running:
        return SessionState(SessionStateValue.PAUSED)
    return LIVE
