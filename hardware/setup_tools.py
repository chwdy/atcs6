"""The Setup tools that can hold the outputs, closed together.

design_new.md "Hardware setup", Setup: inside Setup the commissioning session
and the occupancy bench each take the outputs while they drive them. Every way
they end — a close, a timeout, Emergency stop in Setup, or an activation taking
the outputs back — completes the safe-state sequence. Both closes are
idempotent, so closing a tool that is not open does nothing.
"""

from __future__ import annotations

from typing import Any, List, Optional


def close_setup_tools() -> List[str]:
    """Close the commissioning session and the occupancy bench, if open.

    Each close zeroes the outputs of the chips that tool drives itself.
    Returns one message per chip whose zeroing failed, empty when every write
    landed; the caller reports a failure through `report_close_failures(...)`.
    """
    from hardware import commission, occupancy_bench

    errors = list(commission.get_session().close())
    errors.extend(occupancy_bench.get_session().close())
    return errors


def report_close_failures(sim_controller: Optional[Any], errors: List[str]) -> None:
    """Report a Setup tool's failed closing writes as a hardware I/O failure
    (design_new.md "Power", Hardware setup, Closing a Setup tool restores the
    safe state; Hardware I/O failure is fatal to the physical session). The
    caller holds `AppState.simulation_lock`. No-op when nothing failed."""
    if not errors or sim_controller is None:
        return
    from runtime_payloads import HardwareFailure

    sim_controller.enter_hardware_failure(
        HardwareFailure(
            component="setup_tool",
            operation="close",
            resource_id=None,
            message=(
                "Could not zero every output while closing a Setup tool: "
                + "; ".join(errors)
            ),
        )
    )
