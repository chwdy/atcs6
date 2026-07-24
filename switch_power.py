"""
Switch throw output

Definition:
This module defines the software-to-hardware boundary for switch *throw*
output: driving a switch's point motor to a target alignment (MAIN=0 /
DIVERGE=1). It holds no wiring knowledge — how a target alignment reaches the
motor belongs to the driver, and is defined in design_new.md "Power", Hardware
configuration ("A switch throw is a pulse on one of two coils"). This throw
voltage is a separate electrical output from the block ``track_voltage`` emitted
by ``BlockPowerController``; the two are different wires and are never compared
to each other.

A throw is blind. A solenoid point motor reports nothing back — no contact, no
encoder, no sensed position — so ``Switch.position`` is the commanded alignment
and there is nothing downstream that can confirm the points seated. The
interface reports only whether the command was *issued*: ``set_throw`` returns
False when there is no hardware binding for the switch or its chip is missing,
and `SwitchManager` fails the operation on that. It never claims the switch
moved.

The module includes:
- SwitchOutput: Abstract base for hardware throw output
- SimulatedSwitchDriver: In-memory implementation for simulation
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional


class SwitchOutput(ABC):
    """Abstract interface for hardware switch-throw output.

    Implementations drive the point motor, whether to simulated storage or
    real hardware. The target alignment is 0 = MAIN, 1 = DIVERGE; how that
    reaches the motor is the implementation's business (the real driver pulses
    one of two coils — see ``hardware/interfaces.py``).
    """

    @abstractmethod
    def set_throw(self, switch_id: str, target_state: int) -> bool:
        """Drive the switch toward ``target_state`` (0=MAIN, 1=DIVERGE).

        Returns True when the command was issued to the motor, False when it
        could not be (no binding for this switch, missing chip). True is not a
        claim that the points moved — nothing on this hardware can report that.
        """

    @abstractmethod
    def get_last_throw(self, switch_id: str) -> Optional[int]:
        """Return the last target_state commanded for ``switch_id``, or None."""

    @abstractmethod
    def enter_safe_state(self) -> None:
        """Drive the safe state: every switch point motor de-energized.

        Per design_new.md "Power" (Safe state / fail-safe) the runtime issues
        this when it stops driving outputs (simulation stop or process
        shutdown). The simulated interface drops its commanded drive; real
        hardware de-energizes the point motors.
        """


class SimulatedSwitchDriver(SwitchOutput):
    """Simulated throw output — records the last commanded target per switch.

    Used for simulation mode when no hardware is connected. There is no
    separate notion of a "realized" alignment here: simulation models happy
    hardware, so the recorded command is also what the switch reaches.
    """

    def __init__(self) -> None:
        self._throws: Dict[str, int] = {}

    def set_throw(self, switch_id: str, target_state: int) -> bool:
        self._throws[switch_id] = 0 if target_state == 0 else 1
        return True

    def get_last_throw(self, switch_id: str) -> Optional[int]:
        return self._throws.get(switch_id)

    def clear(self) -> None:
        self._throws.clear()

    def enter_safe_state(self) -> None:
        """Safe state: drop every commanded throw (motors de-energized). The
        switch's commanded `Switch.position` remains the alignment authority;
        this only stops asserting drive (design_new.md "Power", Safe state /
        fail-safe)."""
        self._throws.clear()
