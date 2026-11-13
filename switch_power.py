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
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

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
    ) -> None:
        self.network = network
        self.output_interface: SwitchOutput = (
            output_interface if output_interface is not None else SimulatedSwitchDriver()
        )
        # Named dependencies, handed in by `wire_dependencies(...)`. An editor
        # or unit setup with none wired reports nothing for an unissued throw
        # and reads the feed block's voltage through `network.power_controller`.
        self._notification_center: Optional[Any] = None
        self._enter_hardware_failure: Optional[Callable[[Any], None]] = None
        self._power_controller: Optional[Any] = None

    def wire_dependencies(
        self,
        *,
        notification_center: Optional[Any] = None,
        enter_hardware_failure: Optional[Callable[[Any], None]] = None,
        power_controller: Optional[Any] = None,
    ) -> None:
        """Inject the named dependencies this controller needs: the event-log
        NotificationCenter and `SimulationController.enter_hardware_failure`,
        which an unissued throw reports to (design_new.md "Power", An unissued
        throw is fatal to a hardware-backed session), and the
        BlockPowerController whose block feeds a switch's rails. Called by
        SimulationController each time it builds the power controllers; this
        controller holds no general SimulationController reference of its own."""
        self._notification_center = notification_center
        self._enter_hardware_failure = enter_hardware_failure
        self._power_controller = power_controller


    # ------------------------------------------------------------------
    # Throw output
    # ------------------------------------------------------------------

    def set_throw(self, switch_id: str, target_state: int) -> bool:
        """Command the point motor toward ``target_state`` (0=MAIN, 1=DIVERGE).

        Returns True when the command reached the device and False when it did
        not. True is not a claim that the points seated — a solenoid point
        motor reports nothing back.

        A refusal or an exception after activation means the device contract
        failed at runtime (design_new.md "Power", An unissued throw is fatal to
        a hardware-backed session). This controller is the only reporter of it:
        it reports the failed command through `NotificationCenter` and the
        failure to `SimulationController.enter_hardware_failure(...)`, which
        drives the safe state itself — the same way `BlockPowerController`
        reports a refused voltage write. `SwitchManager` only sees the False
        return and rolls back its software locks.
        """
        failure: Optional[str] = None
        try:
            issued = bool(self.output_interface.set_throw(switch_id, int(target_state)))
            if not issued:
                failure = "output driver refused the throw"
        except Exception as exc:  # noqa: BLE001 - device errors enter one fatal path
            issued = False
            failure = f"output throw raised {type(exc).__name__}: {exc}"
        if not issued:
            self._report_unissued_throw(
                switch_id, int(target_state), failure or "throw failed"
            )
        return issued

    def _report_unissued_throw(
        self, switch_id: str, target_state: int, reason: str
    ) -> None:
        """Report an unissued throw: one warning event, then the hardware
        failure latch. No-op when neither is wired (editor/unit setups)."""
        center = self._notification_center
        if center is not None:
            from notification_center import Kind, switch_ref
            from runtime_payloads import ErrorRecord

            try:
                center.emit(
                    source="SwitchPowerController",
                    operation="set_throw",
                    kind=Kind.WARNING,
                    message=f"switch {switch_id} throw not issued: {reason}",
                    entities=[switch_ref(switch_id)],
                    error=ErrorRecord(reason, "throw_not_issued"),
                )
            except Exception:
                pass
        enter = self._enter_hardware_failure
        if enter is not None:
            from runtime_payloads import HardwareFailure

            enter(
                HardwareFailure(
                    component="switch_output",
                    operation="set_throw",
                    resource_id=switch_id,
                    message=(
                        f"Could not command switch {switch_id} to "
                        f"{target_state}: {reason}"
                    ),
                )
            )

    def get_last_throw(self, switch_id: str) -> Optional[int]:
        """The last target alignment this controller commanded, or None."""
        return self.output_interface.get_last_throw(switch_id)

    # ------------------------------------------------------------------
    # Rail feed
    # ------------------------------------------------------------------


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
