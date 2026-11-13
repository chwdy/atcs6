"""The session state: the one answer to "is the layout live?".

design_v2.md "Session state": the session reads Setup, Live, or Halted with a
reason, derived on every read from three stored flags in this order:

1. Setup                      `AppState.setup_active`
2. Halted, `hardware_failure` `hardware_failure`
3. Halted, `not_activated`    no environment active
4. Live                       no row above holds

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


def derive_session_state(sim_controller, *, setup_active: bool) -> SessionState:
    """The session state for these flags, per the precedence table above."""
    if setup_active:
        return SessionState(SessionStateValue.SETUP)
    if getattr(sim_controller, "hardware_failure", False):
        return SessionState(SessionStateValue.HALTED, HaltReason.HARDWARE_FAILURE)
    if not getattr(sim_controller, "environment_active", False):
        return SessionState(SessionStateValue.HALTED, HaltReason.NOT_ACTIVATED)
    return LIVE
