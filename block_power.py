"""
Block Power Controller

Definition:
This module provides the BlockPowerController class which manages DC voltage output
to track blocks. In DC analog control, each block is electrically isolated and
receives independent voltage based on the trains occupying it.

The module includes:
- OutputInterface: Abstract base for hardware voltage output
- SimulatedOutputInterface: In-memory implementation for simulation
- BlockPowerController: Main controller for voltage management
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, TYPE_CHECKING

MAX_TRACK_VOLTAGE = 12.0
MIN_TRACK_VOLTAGE = 0.0
# Lighting voltage is a per-train property: it lives on the train's
# `SpeedProfile.lighting_voltage` and is emitted by `TrainPhysics.
# compute_track_voltage` for READY / RUNNING_AT_STATION. The block power
# controller does not hold its own lighting-voltage default.

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train import Train
    from speed_profile import SpeedProfile, SpeedProfileRegistry


class OutputInterface(ABC):
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
        """
        Read actual voltage for a block.

        For simulation, this returns the stored value.
        For hardware, this could read from sensors.

        Args:
            block_id: Block identifier

        Returns:
            Current voltage for the block
        """
        pass

    @abstractmethod
    def get_all_voltages(self) -> Dict[str, float]:
        """
        Get all block voltages.

        Returns:
            Dictionary of block_id -> voltage
        """
        pass


class SimulatedOutputInterface(OutputInterface):
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
        """Get stored voltage value."""
        return self._voltages.get(block_id, 0.0)

    def get_all_voltages(self) -> Dict[str, float]:
        """Get all stored voltages."""
        return dict(self._voltages)

    def clear(self) -> None:
        """Clear all stored voltages."""
        self._voltages.clear()


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
        min_voltage: Minimum voltage (default 0V)
    """

    network: Optional["RailwayNetwork"] = None
    output_interface: OutputInterface = field(default_factory=SimulatedOutputInterface)

    # Voltage limits
    max_voltage: float = MAX_TRACK_VOLTAGE
    min_voltage: float = -MAX_TRACK_VOLTAGE  # Allow negative for opposite polarity

    # SimulationController back-reference, set by bootstrap so this
    # controller can fetch the live `TrainController` (for the operational
    # state machine and cab) and the live `TrainPhysics` (canonical home of
    # `track_voltage` per design_new.md "Power-controller role"). This
    # controller does not read controller-owned manual-mode state directly —
    # it asks `TrainPhysics.compute_track_voltage(...)` for the scalar.
    _sim_controller: Optional[Any] = None

    def set_network(self, network: "RailwayNetwork") -> None:
        """Set the network reference."""
        self.network = network

    def update_for_train(
        self, train: "Train", speed_profile: Optional["SpeedProfile"] = None
    ) -> None:
        """
        Update voltage for all blocks occupied by a train.

        Determines voltage based on train state:
        - OFF: 0V
        - READY/RUNNING_AT_STATION: lighting_voltage
        - RUNNING_MOVING: voltage based on target speed

        Args:
            train: Train to update voltage for
            speed_profile: Optional speed profile (uses default if None)
        """
        # Get blocks occupied by train
        occupied_blocks = self._get_train_occupied_blocks(train)

        if not occupied_blocks:
            return

        # All occupied blocks take the same voltage, resolved by TrainPhysics
        # (the canonical owner of `track_voltage`).
        voltage = self._calculate_voltage_for_train(train, speed_profile)
        for block_id in occupied_blocks:
            self.set_block_voltage(block_id, voltage)

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

        Args:
            block_id: Block identifier
            voltage: Voltage to set (will be clamped to limits)

        Returns:
            True if successful
        """
        # Clamp voltage to limits
        clamped = max(self.min_voltage, min(self.max_voltage, voltage))

        # Update block's stored voltage if network available
        if self.network:
            block = self.network.blocks.get(block_id)
            if block:
                block.current_voltage = clamped

        # Output to interface
        return self.output_interface.set_voltage(block_id, clamped)

    def get_block_voltage(self, block_id: str) -> float:
        """
        Get current voltage for a block.

        Args:
            block_id: Block identifier

        Returns:
            Current voltage
        """
        return self.output_interface.get_actual_voltage(block_id)

    def get_all_voltages(self) -> Dict[str, float]:
        """
        Get all block voltages.

        Returns:
            Dictionary of block_id -> voltage
        """
        return self.output_interface.get_all_voltages()

    def emergency_stop_all(self) -> None:
        """Set all blocks to 0V immediately."""
        if self.network:
            for block_id in self.network.blocks:
                self.set_block_voltage(block_id, 0.0)

        # Also clear any voltages in the output interface
        if isinstance(self.output_interface, SimulatedOutputInterface):
            for block_id in list(self.output_interface._voltages.keys()):
                self.set_block_voltage(block_id, 0.0)

    def release_block(self, block_id: str) -> None:
        """
        Set block to 0V (no train occupying).

        Args:
            block_id: Block to release
        """
        self.set_block_voltage(block_id, 0.0)

    def deenergize_unheld_blocks(self) -> None:
        """Drop every block that no train holds back to 0V (full reconciliation).

        A block is *held* when a train physically occupies it (``occupied_by``)
        or has reserved it as future authority (``reserved_for_train``). Per-train
        voltage emission (TrainPhysics) only drives a train's own held blocks, so
        a block a train has vacated and released would otherwise keep its last
        commanded voltage forever.

        The per-tick path is event-driven: `BlockManager` queues each block as
        its holder clears and `SimulationController` drains
        `BlockManager.take_blocks_to_deenergize()` to drive those to 0V. This
        O(all-blocks) sweep is the fallback for a runtime with no BlockManager
        wired, plus an admin/test reconciliation primitive.
        """
        if not self.network:
            return
        for block_id, block in self.network.blocks.items():
            held = (
                getattr(block, "occupied_by", None) is not None
                or getattr(block, "reserved_for_train", None) is not None
            )
            if not held and float(getattr(block, "current_voltage", 0.0)) != 0.0:
                self.set_block_voltage(block_id, 0.0)

    def update_all_trains(
        self, speed_profiles: Optional["SpeedProfileRegistry"] = None
    ) -> None:
        """
        Update voltages for all trains in the network.

        Args:
            speed_profiles: The shared ``SpeedProfileRegistry`` (or any
                profile_id -> SpeedProfile mapping). Looked up with ``.get()``,
                so both a registry and a plain dict work.
        """
        if not self.network:
            return

        for train in self.network.trains.values():
            # Get train's speed profile from the shared store, if assigned.
            profile = None
            if train.speed_profile_id and speed_profiles is not None:
                profile = speed_profiles.get(train.speed_profile_id)

            self.update_for_train(train, profile)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize current voltage state."""
        return {
            "voltages": self.get_all_voltages(),
            "max_voltage": self.max_voltage,
            "min_voltage": self.min_voltage,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BlockPowerController":
        """Reconstruct from dictionary."""
        controller = cls(
            max_voltage=data.get("max_voltage", MAX_TRACK_VOLTAGE),
            min_voltage=data.get("min_voltage", MIN_TRACK_VOLTAGE),
        )

        # Restore voltages
        for block_id, voltage in data.get("voltages", {}).items():
            controller.output_interface.set_voltage(block_id, voltage)

        return controller
