"""The Setup tools that can hold the outputs, closed together.

design_v2.md "Setup tools": inside Setup the commissioning session and the
occupancy bench each take the outputs while they drive them. Every way they
end — a close, a timeout, a failure, or an activation taking the outputs back —
completes the safe-state sequence. Both closes are
idempotent, so closing a tool that is not open does nothing.
"""

from __future__ import annotations

from typing import Any, List, Optional


def close_setup_tools() -> List[str]:
    """Close the commissioning session and the occupancy bench, if open.

    Each close zeroes the outputs of the chips that tool drives itself.
    Returns one message per chip whose zeroing failed, empty when every write
    landed, plus any a timed-out close left unreported; the caller reports a
    failure through `report_close_failures(...)`.
    """
    from hardware import commission, occupancy_bench

    errors = list(commission.get_session().close())
    errors.extend(occupancy_bench.get_session().close())
    return errors


def take_timed_out_close_failures() -> List[str]:
    """The failed zeroing writes of any Setup tool close an idle timeout ran
    since the last call, each returned once. A timeout is noticed inside
    whichever request touched the tool, which cannot report it; the caller
    reports these through `report_close_failures(...)`."""
    from hardware import commission, occupancy_bench

    errors = list(commission.get_session().take_close_failures())
    errors.extend(occupancy_bench.get_session().take_close_failures())
    return errors


def any_setup_tool_open() -> bool:
    """Whether the commissioning session or the occupancy bench is open. A
    cheap read with no lock, so the runtime loop can ask on every iteration."""
    from hardware import commission, occupancy_bench

    return (
        commission.get_session().is_holding_outputs()
        or occupancy_bench.get_session().is_holding_outputs()
    )


def close_idle_setup_tools() -> List[str]:
    """Close every Setup tool whose idle timeout has passed (design_v2.md
    "Setup tools", A timeout closes the tool on time).
    The runtime loop calls this each iteration, so a tool no request reaches
    still closes on time. Returns the failed zeroing writes for the caller to
    report through `report_close_failures(...)`."""
    from hardware import commission, occupancy_bench

    errors = list(commission.get_session().close_if_idle())
    errors.extend(occupancy_bench.get_session().close_if_idle())
    return errors


def report_close_failures(sim_controller: Optional[Any], errors: List[str]) -> None:
    """Report a Setup tool's failed closing writes as a hardware I/O failure
    (design_v2.md "Setup tools", A failed closing write is a hardware I/O
    failure). The
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
