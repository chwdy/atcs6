"""The Setup tools that can hold the outputs, closed together.

design_v2.md "Hardware interface", Who may write the outputs: inside Setup the
commissioning session and the occupancy bench each own the outputs from their
open until their close completes. Every way they end — a close, a timeout, a
failure, or an activation closing them first — completes the safe-state
sequence and then gives the outputs back. Both closes are idempotent, so
closing a tool that is not open does nothing.
"""

from __future__ import annotations

from typing import Any, List, Optional


# The errors a failed device write raises: the transport's own (`OSError`,
# `ConnectionError`, a chip's no-ACK `IOError`) and a lost or closed bridge
# (`RuntimeError`, e.g. `Ch347LinkLost`). A write the device handle refuses is
# not among them (`OutputNotOwned`, checked first): it reached no chip.
WRITE_FAILURES = (OSError, RuntimeError)


class SetupToolWriteFailed(RuntimeError):
    """An open Setup tool's own write failed, so the tool ended (design_v2.md
    "Hardware interface", A failure the tool did not cause leaves it open: a
    failure of the tool's own write ends the tool). By the time this is raised
    the tool has completed its own safe-state sequence and given the outputs
    back. `close_failures` lists the zeroing writes of that close that failed.
    The caller reports the failure through `report_tool_write_failure(...)`.
    """

    def __init__(self, tool: str, cause: BaseException, close_failures: List[str]) -> None:
        self.tool = tool
        self.cause = cause
        self.close_failures = list(close_failures)
        super().__init__(f"{tool}: output write failed, so the tool closed: {cause}")


def is_write_failure(exc: BaseException) -> bool:
    """Whether `exc` from a tool's own write is a failed write that ends the
    tool, rather than a write the device handle refused."""
    from hardware.device_handle import OutputNotOwned

    return isinstance(exc, WRITE_FAILURES) and not isinstance(exc, OutputNotOwned)


def report_tool_write_failure(sim_controller: Optional[Any], exc: SetupToolWriteFailed) -> None:
    """Report an open tool's failed write as a hardware I/O failure
    (design_v2.md "Safe state, watchdog and hardware failure", Hardware I/O
    failure is fatal to the session: a failed output write). The caller holds
    `AppState.simulation_lock`."""
    if sim_controller is None:
        return
    from runtime_payloads import HardwareFailure

    message = f"{exc.tool} output write failed: {exc.cause}"
    if exc.close_failures:
        message += (
            "; could not zero every output while closing it: "
            + "; ".join(exc.close_failures)
        )
    sim_controller.enter_hardware_failure(
        HardwareFailure(
            component="setup_tool",
            operation="write",
            resource_id=None,
            message=message,
        )
    )


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
