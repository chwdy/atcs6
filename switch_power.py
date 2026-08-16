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
- SwitchPowerController: Owns the throw output and the switch's rail feed
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from network import RailwayNetwork


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


class SwitchPowerController:
    """The switch-side counterpart of `BlockPowerController`.

    It covers a switch's two separate electricals (design_new.md "Power",
    Switch power-controller role):

    - the `throw voltage` driven onto the point motor, through the
      `SwitchOutput` it holds. It is the only caller of that interface —
      including the safe-state call — so `SwitchManager` requests a throw
      through this controller and never touches the interface itself.
    - the block feed its rails run on. A switch has no track channel of its
      own: its rails are wired to the block at its main entrance and carry that
      block's `track voltage`. That wiring is the `switch_track_source` binding
      in Hardware Setup, and this controller is that map's only reader.

    Why the switch has its own controller: `BlockPowerController` takes a
    voltage number and applies it; a switch is the other shape. Its one output
    is an alignment, not a voltage, and its one voltage is sourced from a block
    rather than commanded. Holding both facts here gives every switch-side
    electrical question a single answer site, and leaves `BlockPowerController`
    owning exactly one thing: the voltage on a block.
    """

    def __init__(
        self,
        network: Optional["RailwayNetwork"] = None,
        output_interface: Optional[SwitchOutput] = None,
        *,
        sim_controller: Optional[Any] = None,
    ) -> None:
        self.network = network
        self.output_interface: SwitchOutput = (
            output_interface if output_interface is not None else SimulatedSwitchDriver()
        )
        self._sim_controller = sim_controller


    # ------------------------------------------------------------------
    # Throw output
    # ------------------------------------------------------------------

    def set_throw(self, switch_id: str, target_state: int) -> bool:
        """Command the point motor toward ``target_state`` (0=MAIN, 1=DIVERGE).

        Returns True when the command reached the device and False when it did
        not. True is not a claim that the points seated — a solenoid point
        motor reports nothing back.
        """
        return bool(self.output_interface.set_throw(switch_id, int(target_state)))

    def get_last_throw(self, switch_id: str) -> Optional[int]:
        """The last target alignment this controller commanded, or None."""
        return self.output_interface.get_last_throw(switch_id)

    # ------------------------------------------------------------------
    # Rail feed
    # ------------------------------------------------------------------

    def feed_block_id(self, switch_id: str) -> Optional[str]:
        """The block whose feed powers this switch's rails, per the
        `switch_track_source` binding. None while an incomplete setup is being
        edited or commissioned; that state is an activation blocker and cannot
        occur during Virtual Hardware or Real Hardware operation."""
        try:
            import hardware_config
        except ImportError:  # pragma: no cover - defensive import guard
            return None
        cfg = hardware_config.get_hardware_config()
        return cfg.switch_track_source.get(switch_id)

    def get_switch_voltage(self, switch_id: str) -> Optional[float]:
        """The voltage on a switch's rails, read from its feed block.

        A UI/diagnostic read: the per-switch hardware view shows which block
        feeds the switch and whether those rails are live. No control, routing,
        or physics decision reads it. This controller drives nothing onto the
        rails — the bond is physical, so there is no output to write.
        """
        source_block_id = self.feed_block_id(switch_id)
        if source_block_id is None:
            return None
        power_controller = getattr(self._sim_controller, "power_controller", None)
        if power_controller is None and self.network is not None:
            power_controller = getattr(self.network, "power_controller", None)
        if power_controller is None:
            return None
        return power_controller.get_block_voltage(source_block_id)

    # ------------------------------------------------------------------
    # Safe state
    # ------------------------------------------------------------------

    def de_energize_device_outputs(self) -> None:
        """De-energize every point motor on the device, recording nothing.

        This is the switch half of the `OutputSafety.enter_safe_state()`
        sequence, and it runs first: points are dropped before block voltages
        (design_new.md "Power", Safe state / fail-safe). It commands the device
        transport directly and does not touch `Switch.position` or any other
        recorded model field — the commanded alignment stays the authority;
        this only stops asserting drive — so the sequence is safe to call from
        any thread, including the Real Hardware `OutputWatchdog` daemon.
        """
        self.output_interface.enter_safe_state()

    def enter_safe_state(self) -> None:
        """De-energize every point motor (design_new.md "Power", Safe state /
        fail-safe). As the only caller of the output interface, the controller
        delegates the de-energizing to it."""
        self.de_energize_device_outputs()


class SwitchOutputOwner:
    """Mixin for the two holders of the switch throw output — the runtime
    session and the network.

    `switch_output_interface` and `switch_power_controller` name one wiring from
    two sides, and code assigns the interface (a session rebuild, a bench test
    swapping a driver in). Routing the assignment through here retargets the
    controller with it, so the pair cannot drift into a state where the manager
    drives a stale interface the holder has already replaced.
    """

    @property
    def switch_output_interface(self) -> Optional[SwitchOutput]:
        return getattr(self, "_switch_output_interface", None)

    @switch_output_interface.setter
    def switch_output_interface(self, iface: Optional[SwitchOutput]) -> None:
        self._switch_output_interface = iface
        controller = getattr(self, "switch_power_controller", None)
        if controller is not None and iface is not None:
            controller.output_interface = iface
