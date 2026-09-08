"""
Block Power Controller

Definition:
This module provides the BlockPowerController class which manages DC voltage output
to track blocks. In DC analog control, each block is electrically isolated and
receives independent voltage based on the trains occupying it.

The module includes:
- TrackOutput: Abstract base for hardware voltage output
- SimulatedTrackDriver: In-memory implementation for simulation
- BlockPowerController: Main controller for voltage management
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set, TYPE_CHECKING

logger = logging.getLogger(__name__)

MAX_TRACK_VOLTAGE = 12.0
# Lighting voltage is a per-train property: it lives on the train's
# `SpeedProfile.lighting_voltage` and is emitted by `TrainPhysics.
#`compute_track_voltage` for READY. The block power
# controller does not hold its own lighting-voltage default.

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train import Train
    from speed_profile import SpeedProfile, SpeedProfileRegistry


class TrackOutput(ABC):
    """
    Abstract interface for hardware voltage output.

    Implementations of this interface handle the actual voltage output,
    whether to simulated storage or real hardware (Serial/I2C/GPIO).
    """

    @abstractmethod
    def set_voltage(self, block_id: str, voltage: float) -> bool:
        """
        Output voltage to a block.

        Args:
            block_id: Block identifier
            voltage: Voltage to output (0-12V typically)

        Returns:
            True if successful, False otherwise
        """
        pass

    @abstractmethod
    def get_actual_voltage(self, block_id: str) -> float:
        """The last voltage this driver was commanded to output for a block.

        This is the app's commanded value (its own output, read back), which the
        occupancy drive-zone logic reads to decide whether a block is being
        driven. It is NOT the measured rail voltage — that is
        `get_readback_voltage(...)`.
        """
        pass

    def get_readback_voltage(self, block_id: str) -> Optional[float]:
        """Read back the actual voltage on a block (design_new.md "Power", the
        block-voltage read surface).

        Simulation returns the last value accepted by the happy-path model (so
        the default here is `get_actual_voltage`). Virtual Hardware returns the
        fake device's readback, and Real Hardware reads the block's bound
        `block_occupancy_current` channel bus voltage where that chip provides
        one — neither hardware-backed environment silently substitutes the
        commanded value. Returns `None` where the rig has no measurement
        facility, and raises on a failed readback transaction (the
        `BlockPowerController.get_block_voltage(...)` consumer turns that into
        `None` plus one warning event).
        """
        return self.get_actual_voltage(block_id)

    @abstractmethod
    def get_all_voltages(self) -> Dict[str, float]:
        """
        Get all block voltages.

        Returns:
            Dictionary of block_id -> voltage
        """
        pass

    @abstractmethod
    def enter_safe_state(self) -> None:
        """Drive the safe state: every block track voltage to 0.

        Per design_new.md "Power" (Safe state / fail-safe) the runtime issues
        this when it stops driving outputs (simulation stop or process
        shutdown). The simulated interface zeroes its stored outputs; real
        hardware de-energizes the track.
        """
        pass


class SimulatedTrackDriver(TrackOutput):
    """
    Simulated output interface - stores voltages in memory.

    Used for simulation mode when no hardware is connected.
    """

    def __init__(self):
        self._voltages: Dict[str, float] = {}

    def set_voltage(self, block_id: str, voltage: float) -> bool:
        """Store voltage value in memory."""
        self._voltages[block_id] = voltage
        return True

    def get_actual_voltage(self, block_id: str) -> float:
        """Simulation: the last value accepted by the happy-path model — both the
        commanded value and the readback, since the model always agrees."""
        return self._voltages.get(block_id, 0.0)

    def get_all_voltages(self) -> Dict[str, float]:
        """Get all stored voltages."""
        return dict(self._voltages)

    def clear(self) -> None:
        """Clear all stored voltages."""
        self._voltages.clear()

    def enter_safe_state(self) -> None:
        """Safe state: zero every stored block voltage (design_new.md "Power",
        Safe state / fail-safe)."""
        for block_id in self._voltages:
            self._voltages[block_id] = 0.0


@dataclass
class BlockPowerController:
    """
    Manages DC voltage output to track blocks.

    The controller determines the appropriate voltage for each block
    based on:
    - Which trains occupy the block
    - Train state (OFF, READY, RUNNING_*, etc.)
    - Train speed profile and target speed

    Attributes:
        network: Reference to the railway network
        output_interface: Interface for actual voltage output
        max_voltage: Maximum allowable voltage (default 12V)
        min_voltage: Most negative commandable voltage (default -12V)
    """

    network: Optional["RailwayNetwork"] = None
    output_interface: TrackOutput = field(default_factory=SimulatedTrackDriver)

    # Voltage limits
    max_voltage: float = MAX_TRACK_VOLTAGE
    # A commanded block voltage is signed: the magnitude is `track_voltage`
    # and the sign is the block-relative direction the train runs through that
    # block (design_new.md "Power", A block voltage carries a direction), so
    # the range is symmetric. The sign is not a voltage on the rails — a
    # block's rails are two 0-100% PWM channels into an H-bridge, and which
    # channel a sign reaches is wiring the driver resolves from the binding
    # below this layer (design_new.md "Power", The rail binding bonds tile
    # order to rail polarity).
    min_voltage: float = -MAX_TRACK_VOLTAGE

    # SimulationController back-reference, set by bootstrap so this
    # controller can fetch the live `TrainController` (for the operational
    # state machine and cab) and the live `TrainPhysics` (canonical home of
    # `track_voltage` per design_new.md "Power-controller role"). This
    # controller does not read controller-owned manual-mode state directly —
    # it asks `TrainPhysics.compute_track_voltage(...)` for the scalar.
    _sim_controller: Optional[Any] = None

    # Blocks whose voltage write the driver has already refused. Reporting is
    # once-per-block: a train sits on a block for thousands of ticks, and the
    # fault is a static wiring/configuration one, not a per-tick event.
    _unreachable_blocks: Set[str] = field(default_factory=set)

    def zero_device_outputs(self) -> None:
        """Command every block voltage to `0` on the device, recording nothing.

        This is the block half of the `OutputSafety.enter_safe_state()` sequence
        (design_new.md "Power", Safe state / fail-safe). It commands the device
        transport directly and does not touch `Block.current_voltage` or any
        other recorded model field, which is what makes the sequence safe to
        call from any thread — including the Real Hardware `OutputWatchdog`
        daemon. Reconciling the recorded fields is a separate, later step owned
        by the runtime thread: `SimulationController.enter_output_safe_state()`.
        """
        self.output_interface.enter_safe_state()

    def reconcile_recorded_voltages(self) -> None:
        """Bring `Block.current_voltage` into line with a rail already at `0`.

        The runtime-thread half of a safe-state entry. Kept apart from
        `zero_device_outputs()` so the device write stays non-recording.
        """
        if self.network is not None:
            for block in self.network.blocks.values():
                block.current_voltage = 0.0

    def enter_safe_state(self) -> None:
        """Zero the block outputs and reconcile the recorded voltages.

        The runtime-thread convenience pairing of the two halves above. Callers
        that own the outputs reach it through
        `SimulationController.enter_output_safe_state()`, which also advances
        the safe-state generation.
        """
        try:
            self.zero_device_outputs()
        finally:
            self.reconcile_recorded_voltages()

    def _calculate_voltage_for_train(
        self, train: "Train", speed_profile: Optional["SpeedProfile"] = None
    ) -> float:
        """Resolve the train's current `track_voltage` from `TrainPhysics`.

        Per design_new.md "Power-controller role", `TrainPhysics` owns
        `track_voltage` and `BlockPowerController` is the software-to-
        hardware emission layer. This method asks
        `TrainPhysics.compute_track_voltage(...)` for the scalar; it does
        not branch on `control_mode`, `manual_mode_type`, or any other
        manual-mode policy.
        """
        if self._sim_controller is None:
            return 0.0

        controller = self._sim_controller.get_or_create_controller(train.train_id)
        physics = self._sim_controller.get_or_create_physics(train.train_id)
        if physics is None:
            return 0.0
        return physics.compute_track_voltage(controller, speed_profile)

    def _get_train_occupied_blocks(self, train: "Train") -> list:
        """Get list of block IDs occupied by train.

        Per design_new.md, physical occupancy is read from the controller-owned
        canonical span (`TrainController.physics_sync.current_span.occupied_blocks`,
        exposed via `get_current_span()`); authority-future blocks live on the
        dispatcher's active path.
        """
        if self._sim_controller is not None:
            getter = getattr(self._sim_controller, "get_controller", None)
            ctrl = getter(train.train_id) if callable(getter) else None
            if ctrl is not None:
                span = ctrl.get_current_span()
                if span is not None:
                    return list(span.occupied_blocks)
        return []

    def set_block_voltage(self, block_id: str, voltage: float) -> bool:
        """
        Set voltage for a specific block.

        A real driver refuses the write for a block it cannot reach — one with
        no `block_track` binding, or a binding naming a chip the session did not
        build. `Block.current_voltage` records what actually reached the rail,
        so a refused write leaves it at `0`: the model must not claim a block is
        energized when the rail is dead, or the operator sees a train the app
        believes is running and a locomotive that never moves.

        The first refusal per block is reported through `NotificationCenter`,
        because a silently dead rail is the hardest possible fault to diagnose
        from the layout. It is reported once, not every tick — a train sits on a
        block for thousands of ticks and the stream must stay readable.

        Args:
            block_id: Block identifier
            voltage: Voltage to set (will be clamped to limits)

        Returns:
            True when the write landed, False when the driver refused it.
        """
        # Clamp voltage to limits
        clamped = max(self.min_voltage, min(self.max_voltage, voltage))

        failure_message: Optional[str] = None
        try:
            landed = self.output_interface.set_voltage(block_id, clamped)
            if not landed:
                failure_message = "output driver refused the write"
        except Exception as exc:  # noqa: BLE001 - device errors enter one fatal path
            landed = False
            failure_message = f"output write raised {type(exc).__name__}: {exc}"

        if self.network:
            block = self.network.blocks.get(block_id)
            if block:
                block.current_voltage = clamped if landed else 0.0

        if not landed and block_id not in self._unreachable_blocks:
            self._unreachable_blocks.add(block_id)
            self._emit_unreachable(block_id, clamped)

        if not landed:
            self._latch_hardware_failure(block_id, clamped, failure_message or "write failed")

        return landed

    def _latch_hardware_failure(
        self, block_id: str, voltage: float, message: str
    ) -> None:
        controller = self._sim_controller
        enter = getattr(controller, "enter_hardware_failure", None)
        if not callable(enter):
            return
        from runtime_payloads import HardwareFailure

        enter(
            HardwareFailure(
                component="track_output",
                operation="set_voltage",
                resource_id=block_id,
                message=(
                    f"Could not command {voltage:.2f}V on block {block_id}: {message}"
                ),
            )
        )

    def _emit_unreachable(self, block_id: str, voltage: float) -> None:
        """Report a block the output driver cannot drive (once per block)."""
        center = None
        if self._sim_controller is not None:
            center = getattr(self._sim_controller, "notification_center", None)
        if center is None:
            logger.warning(
                "Track voltage %.2fV for block %s was refused by the output "
                "driver: no track binding, or its chip is not configured. "
                "Nothing will move on this block.",
                voltage,
                block_id,
            )
            return
        from notification_center import Kind, block_ref
        from runtime_payloads import ErrorRecord

        try:
            center.emit(
                source="BlockPowerController",
                operation="set_block_voltage",
                kind=Kind.WARNING,
                message=(
                    f"block {block_id} was commanded {voltage:.2f}V but the "
                    f"output driver refused it: it has no track binding, or "
                    f"the chip that binding names is not configured. Nothing "
                    f"will move on this block until that is fixed in "
                    f"Hardware Setup."
                ),
                entities=[block_ref(block_id)],
                error=ErrorRecord(
                    "the output driver refused the write",
                    "block_track_output_unreachable",
                ),
                event_time=time.time(),
                payload={
                    "block_id": block_id,
                    "commanded_voltage": round(float(voltage), 3),
                },
            )
        except Exception:
            # A reporting failure must not mask the underlying output fault.
            logger.warning(
                "Track voltage for block %s was refused by the output driver",
                block_id,
            )

    def get_block_voltage(self, block_id: str) -> Optional[float]:
        """Diagnostic block-voltage read (design_new.md "Power", the block-voltage
        read surface).

        Reads the actual voltage back from the output driver: the model value in
        Simulation, the fake device's readback in Virtual Hardware, and the bus
        voltage on the block's bound `block_occupancy_current` channel in Real
        Hardware — never the commanded value silently substituted for a failed
        read. Returns `None` where the rig has no measurement facility, and
        `None` plus one `kind="warning"` event on a failed readback transaction.
        This is a diagnostic read; it never enters the hardware-failure
        lifecycle.
        """
        try:
            return self.output_interface.get_readback_voltage(block_id)
        except Exception:
            self._emit_readback_failure(block_id)
            return None

    def _emit_readback_failure(self, block_id: str) -> None:
        """Report a failed block-voltage readback transaction (design_new.md
        "Power", the block-voltage read surface). Diagnostic only — it never
        enters the hardware-failure lifecycle."""
        center = None
        if self._sim_controller is not None:
            center = getattr(self._sim_controller, "notification_center", None)
        if center is None:
            logger.warning(
                "Block-voltage readback for block %s failed", block_id
            )
            return
        try:
            from notification_center import Kind, block_ref
            from runtime_payloads import ErrorRecord

            center.emit(
                source="BlockPowerController",
                operation="get_block_voltage",
                kind=Kind.WARNING,
                message=(
                    f"block {block_id} voltage readback failed: the bound "
                    f"current-sense channel did not answer. No voltage is "
                    f"reported for it."
                ),
                entities=[block_ref(block_id)],
                error=ErrorRecord(
                    "block-voltage readback transaction failed",
                    "block_voltage_readback_failed",
                ),
                event_time=time.time(),
                payload={"block_id": block_id},
            )
        except Exception:
            logger.warning(
                "Block-voltage readback for block %s failed", block_id
            )

    def get_all_voltages(self) -> Dict[str, float]:
        """
        Get all block voltages.

        Returns:
            Dictionary of block_id -> voltage
        """
        return self.output_interface.get_all_voltages()

    def release_block(self, block_id: str) -> None:
        """
        Set block to 0V (no train occupying).

        Args:
            block_id: Block to release
        """
        self.set_block_voltage(block_id, 0.0)

    def deenergize_unheld_blocks(
        self, skip: Optional[Set[str]] = None
    ) -> None:
        """Drop every block no train is standing on back to 0V (full
        reconciliation).

        A block is *held* when a train physically occupies it (``occupied_by``).
        A reservation is not a hold: a reserved block the train has not reached
        yet carries no voltage, because only the *next* block of a corridor is
        pre-energized (design_new.md "Power", Only the next reserved block is
        pre-energized). Per-tick voltage emission (TrainPhysics) drives only a
        train's own energized set, so a block it has vacated, released, or run
        past would otherwise keep its last commanded voltage forever.

        ``skip`` names blocks to leave alone whatever this test says, and is how
        the calculated half of the energized set reaches this sweep: the runtime
        passes each train's calculated span, its ``pending-entry block``, and its
        one next reserved block — none of which this scan can see from the block
        fields alone, and all of which must stay live under or in front of a
        locomotive (design_new.md "Power", Block power-controller role). Called
        bare, with no ``skip``, it reconciles against physical occupancy only.

        An unidentified train is excluded from that test: it commands no voltage, so a
        block held only by an unidentified train is driven to ``0`` (design_new.md "Unidentified train
        trains", Never energized).

        The per-tick path is event-driven: `BlockManager` queues each block as
        its holder clears, `TrainPhysics` reports each block that left its
        energized set, and `SimulationController` drains both to 0V. This
        O(all-blocks) sweep is the fallback for a runtime with no BlockManager
        wired, plus an admin/test reconciliation primitive.
        """
        if not self.network:
            return
        exempt = skip or set()
        trains = getattr(self.network, "trains", {}) or {}
        for block_id, block in self.network.blocks.items():
            if block_id in exempt:
                continue
            occupant = getattr(block, "occupied_by", None)
            occupant_train = trains.get(occupant) if occupant else None
            held = occupant is not None and not getattr(
                occupant_train, "is_unidentified", False
            )
            if not held and float(getattr(block, "current_voltage", 0.0)) != 0.0:
                self.set_block_voltage(block_id, 0.0)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize current voltage state."""
        return {
            "voltages": self.get_all_voltages(),
            "max_voltage": self.max_voltage,
            "min_voltage": self.min_voltage,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BlockPowerController":
        """Reconstruct from dictionary.

        Voltage limits are only overridden when present in ``data``;
        absent keys fall back to the dataclass field defaults
        (``max_voltage`` = ``MAX_TRACK_VOLTAGE``, ``min_voltage`` =
        ``-MAX_TRACK_VOLTAGE``).
        Keeping the defaults in one place — the fields themselves — means a
        hand-written dict that omits either limit gets the same range a
        freshly constructed controller has.
        """
        kwargs: Dict[str, Any] = {}
        if "max_voltage" in data:
            kwargs["max_voltage"] = data["max_voltage"]
        if "min_voltage" in data:
            kwargs["min_voltage"] = data["min_voltage"]
        controller = cls(**kwargs)

        # Restore voltages
        for block_id, voltage in data.get("voltages", {}).items():
            controller.output_interface.set_voltage(block_id, voltage)

        return controller
