"""The Setup tools that can hold the outputs, closed together.

design_new.md "Hardware setup", Setup: inside Setup the commissioning session
and the occupancy bench each take the outputs while they drive them. Every way
they end — a close, a timeout, Emergency stop in Setup, or an activation taking
the outputs back — completes the safe-state sequence. Both closes are
idempotent, so closing a tool that is not open does nothing.
"""

from __future__ import annotations


def close_setup_tools() -> None:
    """Close the commissioning session and the occupancy bench, if open."""
    from hardware import commission, occupancy_bench

    commission.get_session().close()
    occupancy_bench.get_session().close()
