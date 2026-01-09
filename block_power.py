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

from train_config import (
    MAX_TRACK_VOLTAGE,
    MIN_TRACK_VOLTAGE,
    DEFAULT_LIGHTING_VOLTAGE,
)
from enums import TrainState

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train import Train
    from speed_profile import SpeedProfile


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
    - Train state (OFF, IDLE, RUNNING, etc.)
    - Train speed profile and target speed

    Attributes:
        network: Reference to the railway network
        output_interface: Interface for actual voltage output
        max_voltage: Maximum allowable voltage (default 12V)
        min_voltage: Minimum voltage (default 0V)
        default_lighting_voltage: Voltage for lighting states
    """

    network: Optional["RailwayNetwork"] = None
    output_interface: OutputInterface = field(default_factory=SimulatedOutputInterface)

    # Voltage limits
    max_voltage: float = MAX_TRACK_VOLTAGE
    min_voltage: float = -MAX_TRACK_VOLTAGE  # Allow negative for opposite polarity
    default_lighting_voltage: float = DEFAULT_LIGHTING_VOLTAGE

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
        - IDLE/STANDBY/RUNNING_WAITING/RUNNING_AT_STATION: lighting_voltage
          (IDLE with ≥2 blocks may use opposite polarity for red lights both ends)
        - RUNNING_MOVING: voltage based on target speed
        - MANUAL: voltage based on manual_target_speed

        Args:
            train: Train to update voltage for
            speed_profile: Optional speed profile (uses default if None)
        """
        # Get blocks occupied by train
        occupied_blocks = self._get_train_occupied_blocks(train)

        if not occupied_blocks:
            return

        # Check if we should use opposite polarity for IDLE state
        if (train.state == TrainState.IDLE and
            len(occupied_blocks) >= 2 and
            train.is_opposite_polarity_safe(self.network)):
            # Opposite polarity: first block gets positive, last gets negative
            # This shows red lights on both ends
            lighting_voltage = self.default_lighting_voltage
            if speed_profile:
                lighting_voltage = speed_profile.lighting_voltage

            # Set first block (head) to positive
            self.set_block_voltage(occupied_blocks[0], lighting_voltage)

            # Set last block (tail) to negative
            self.set_block_voltage(occupied_blocks[-1], -lighting_voltage)

            # Middle blocks (if any) get 0V
            for block_id in occupied_blocks[1:-1]:
                self.set_block_voltage(block_id, 0.0)
        else:
            # Normal operation: all blocks get same voltage
            voltage = self._calculate_voltage_for_train(train, speed_profile)

            for block_id in occupied_blocks:
                self.set_block_voltage(block_id, voltage)

    def _calculate_voltage_for_train(
        self, train: "Train", speed_profile: Optional["SpeedProfile"] = None
    ) -> float:
        """
        Calculate voltage for a train based on its state.

        Args:
            train: Train to calculate voltage for
            speed_profile: Optional speed profile

        Returns:
            Voltage to output to train's blocks
        """
        # OFF state = 0V
        if train.state == TrainState.OFF:
            return 0.0

        # Get lighting voltage from profile or use default
        lighting_voltage = self.default_lighting_voltage
        if speed_profile:
            lighting_voltage = speed_profile.lighting_voltage

        # States that use lighting voltage
        if train.state in (
            TrainState.IDLE,
            TrainState.STANDBY,
            TrainState.RUNNING_WAITING,
            TrainState.RUNNING_AT_STATION,
        ):
            return lighting_voltage

        # RUNNING_MOVING: use target speed
        if train.state == TrainState.RUNNING_MOVING:
            if speed_profile:
                return speed_profile.speed_to_voltage(train.target_speed)
            else:
                # No profile - linear approximation (12V = 120 km/h)
                return self._linear_speed_to_voltage(train.target_speed)

        # MANUAL: use manual target speed
        if train.state == TrainState.MANUAL:
            if speed_profile:
                return speed_profile.speed_to_voltage(train.manual_target_speed)
            else:
                return self._linear_speed_to_voltage(train.manual_target_speed)

        # Default fallback
        return 0.0

    def _linear_speed_to_voltage(self, speed_kmh: float) -> float:
        """Simple linear speed to voltage conversion (fallback)."""
        return min(self.max_voltage, max(0.0, speed_kmh / 10.0))

    def _get_train_occupied_blocks(self, train: "Train") -> list:
        """Get list of block IDs occupied by train."""
        # Use train's new get_occupied_blocks method (Phase 6)
        if hasattr(train, "get_occupied_blocks"):
            return train.get_occupied_blocks(self.network)

        # Fallback for old code: use reserved_blocks
        if hasattr(train, "reserved_blocks") and train.reserved_blocks:
            return train.reserved_blocks

        # Last fallback: get block from current position
        if self.network and train.current_pos:
            track = self.network.tracks.get(train.current_pos)
            if track and track.block_section_id:
                return [track.block_section_id]

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

    def update_all_trains(
        self, speed_profiles: Optional[Dict[str, "SpeedProfile"]] = None
    ) -> None:
        """
        Update voltages for all trains in the network.

        Args:
            speed_profiles: Dictionary of profile_id -> SpeedProfile
        """
        if not self.network:
            return

        speed_profiles = speed_profiles or {}

        for train in self.network.trains.values():
            # Get train's speed profile
            profile = None
            if train.speed_profile_id and train.speed_profile_id in speed_profiles:
                profile = speed_profiles[train.speed_profile_id]

            self.update_for_train(train, profile)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize current voltage state."""
        return {
            "voltages": self.get_all_voltages(),
            "max_voltage": self.max_voltage,
            "min_voltage": self.min_voltage,
            "default_lighting_voltage": self.default_lighting_voltage,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BlockPowerController":
        """Reconstruct from dictionary."""
        controller = cls(
            max_voltage=data.get("max_voltage", MAX_TRACK_VOLTAGE),
            min_voltage=data.get("min_voltage", MIN_TRACK_VOLTAGE),
            default_lighting_voltage=data.get(
                "default_lighting_voltage", DEFAULT_LIGHTING_VOLTAGE
            ),
        )

        # Restore voltages
        for block_id, voltage in data.get("voltages", {}).items():
            controller.output_interface.set_voltage(block_id, voltage)

        return controller
