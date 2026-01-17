"""
Train Onboard Computer - Central authority for train control state

Definition:
This module provides the OnboardComputer class which acts as the single authority
for train operational state and control decisions. It is the "cab computer" that:

- Controls power on/off and emergency stop
- Stores the current control mode (ATO vs Manual)
- Knows which cab is active and the travel direction
- Manages the throttle/brake handle position
- Validates whether commands should execute
- Stores and manages destination/path/schedule information
- Reports current operational state to displays

Architecture:
    SimulationController (Orchestrator / Clock)
            ↓
        OnboardComputer (this class - control state)
            ↓
    ┌───────┴───────┬───────────────┐
    Train       BlockManager     TrainATC
  (Data)       (Block State)   (Speed Limits)

Design Principles:
1. Single Authority: OnboardComputer owns all train control state
2. Command Pattern: UI sends commands; OnboardComputer validates and executes
3. Read-Only UI: Control panels read from OnboardComputer, never compute locally
4. Separation: Handles control logic; SimulationController handles time/physics
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from enums import (
    BRAKE_MULTIPLIERS,
    OPPOSITE_DIRECTIONS,
    POWER_MULTIPLIERS,
    CommandResult,
    HandlePosition,
    TrainState,
)

if TYPE_CHECKING:
    from network import RailwayNetwork
    from train import Train


@dataclass
class OnboardComputer:
    """
    Onboard computer for a train - manages control state and validates commands.

    OnboardComputer is the single authority for:
    - Power state and emergency stop
    - Active cab selection
    - Throttle/brake handle position
    - Control mode (ATO/Manual)
    - Travel direction
    - ATO destination, path, and schedule
    - Command validation

    It does NOT own:
    - Train position (owned by Train entity)
    - Block state (owned by BlockManager)
    - Simulation time (owned by SimulationController)
    """

    train_id: str
    network: Optional["RailwayNetwork"] = None

    # Power state
    power_on: bool = False
    emergency_stop_active: bool = False

    # Onboard computer state
    active_cab: Optional[str] = None  # 'A', 'B', or None
    control_mode: str = "ato"  # 'ato' or 'manual'
    handle_position: HandlePosition = field(default=HandlePosition.N)
    travel_direction: str = "forward"  # 'forward' or 'reverse'

    # ATO state
    ato_destination_id: Optional[str] = None
    ato_path_id: Optional[str] = None
    ato_schedule_id: Optional[str] = None
    ato_running: bool = False

    # ─────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────

    def _get_train(self) -> Optional["Train"]:
        """Get the Train entity this onboard computer controls."""
        if self.network is None:
            return None
        return self.network.trains.get(self.train_id)

    # ─────────────────────────────────────────────────────────────────
    # Power Commands
    # ─────────────────────────────────────────────────────────────────

    def power_on_cmd(self) -> CommandResult:
        """
        Turn on train power.

        Rules:
        - Train must be in OFF state
        - Transitions to IDLE state
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if self.power_on:
            return CommandResult(False, "Power already on")

        self.power_on = True
        train.state = TrainState.IDLE
        self.handle_position = HandlePosition.N

        return CommandResult(True, "Power on")

    def power_off_cmd(self) -> CommandResult:
        """
        Turn off train power.

        Rules:
        - Train must be stopped
        - Transitions to OFF state
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not train.is_stopped():
            return CommandResult(False, "Cannot power off while moving")

        self.power_on = False
        self.active_cab = None
        self.ato_running = False
        train.state = TrainState.OFF

        return CommandResult(True, "Power off")

    def emergency_stop_cmd(self) -> CommandResult:
        """
        Activate emergency stop.

        Always allowed - safety override.
        Sets handle to EB, triggers maximum braking.
        """
        train = self._get_train()

        self.emergency_stop_active = True
        self.handle_position = HandlePosition.EB
        self.ato_running = False

        if train:
            train.emergency_brake = True

        return CommandResult(True, "Emergency stop activated")

    def release_emergency_cmd(self) -> CommandResult:
        """
        Release emergency stop.

        Rules:
        - Train must be stopped
        - Handle must be in N or B position
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not train.is_stopped():
            return CommandResult(False, "Cannot release emergency while moving")

        brake_positions = [
            HandlePosition.N,
            HandlePosition.B1,
            HandlePosition.B2,
            HandlePosition.B3,
            HandlePosition.B4,
            HandlePosition.B5,
            HandlePosition.B6,
            HandlePosition.B7,
            HandlePosition.B8,
        ]
        if self.handle_position not in brake_positions:
            return CommandResult(
                False, "Handle must be in N or B before releasing emergency"
            )

        self.emergency_stop_active = False
        train.emergency_brake = False
        self.handle_position = HandlePosition.N

        return CommandResult(True, "Emergency stop released")

    # ─────────────────────────────────────────────────────────────────
    # Handle Commands
    # ─────────────────────────────────────────────────────────────────

    def set_handle(self, position: HandlePosition) -> CommandResult:
        """
        Set throttle/brake handle position.

        Rules:
        - Must be powered on
        - In manual mode: directly controls acceleration/braking
        - In ATO mode: handle is controlled by computer (read-only to user)
        - EB always allowed (safety override)
        """
        if not self.power_on:
            return CommandResult(False, "Power is off")

        if position == HandlePosition.EB:
            # EB is always allowed
            return self.emergency_stop_cmd()

        if self.emergency_stop_active:
            return CommandResult(False, "Release emergency stop first")

        if self.control_mode == "ato" and self.ato_running:
            return CommandResult(False, "Handle controlled by ATO while running")

        self.handle_position = position
        return CommandResult(True, f"Handle set to {position.value}")

    # ─────────────────────────────────────────────────────────────────
    # Cab Commands
    # ─────────────────────────────────────────────────────────────────

    def set_cab(self, cab: Optional[str]) -> CommandResult:
        """
        Set active cab. Flips facing_direction when switching cabs.

        Rules:
        - Train must be stopped (speed = 0)
        - Setting cab to 'A' or 'B' transitions to STANDBY
        - Setting cab to None transitions to IDLE
        - Switching from A to B (or vice versa) flips facing_direction
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not train.is_stopped():
            return CommandResult(False, "Cannot switch cab while moving")

        old_cab = self.active_cab

        # If switching between A and B, flip facing direction
        if old_cab in ("A", "B") and cab in ("A", "B") and old_cab != cab:
            if train.facing_direction:
                train.facing_direction = train.facing_direction.opposite()

        self.active_cab = cab

        # Update train state
        if cab is None:
            train.state = TrainState.IDLE
            train.active_cab = None
        else:
            train.state = TrainState.STANDBY
            train.active_cab = cab

        return CommandResult(True, f"Cab set to {cab}")

    def set_mode(self, mode: str) -> CommandResult:
        """
        Set control mode (ATO or Manual).

        Rules:
        - Train must be stopped
        - 'ato' → TrainState stays STANDBY (or IDLE if no cab)
        - 'manual' → TrainState becomes MANUAL
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not train.is_stopped():
            return CommandResult(False, "Cannot switch mode while moving")

        if mode not in ("ato", "manual"):
            return CommandResult(False, f"Invalid mode: {mode}")

        self.control_mode = mode

        if mode == "manual":
            train.state = TrainState.MANUAL
            self.ato_running = False
        else:
            # Return to STANDBY or IDLE based on cab
            train.state = TrainState.STANDBY if self.active_cab else TrainState.IDLE

        return CommandResult(True, f"Mode set to {mode}")

    def ato_go(self) -> CommandResult:
        """
        Start ATO movement toward destination.

        Rules:
        - Must be in ATO mode
        - Active cab must be set
        - Train must be in STANDBY state
        - Path must be assigned (or will be requested)
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if self.control_mode != "ato":
            return CommandResult(False, "Not in ATO mode")

        if self.active_cab is None:
            return CommandResult(False, "No cab selected")

        if train.state != TrainState.STANDBY:
            return CommandResult(
                False, f"Train not in STANDBY (is {train.state.value})"
            )

        train.state = TrainState.RUNNING_MOVING
        self.ato_running = True

        return CommandResult(True, "ATO started")

    def ato_stop(self) -> CommandResult:
        """
        Stop ATO movement (service stop, not emergency).

        Train will decelerate to stop using the SpeedProfile's
        deceleration_rate, then return to STANDBY.

        Note: This does NOT set target_speed directly. Instead,
        it sets ato_running = False and the ATO controller will
        apply braking via the handle.
        """
        if not self.ato_running:
            return CommandResult(False, "ATO not running")

        self.ato_running = False
        # ATO controller will see ato_running=False and apply braking
        # State will transition to RUNNING_WAITING then STANDBY when stopped

        return CommandResult(True, "ATO stopping")

    def set_travel_direction(self, direction: str) -> CommandResult:
        """
        Set travel direction (forward/reverse) in manual mode.

        Rules:
        - Must be in manual mode
        - Train must be stopped
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if self.control_mode != "manual":
            return CommandResult(False, "Not in manual mode")

        if not train.is_stopped():
            return CommandResult(False, "Cannot change direction while moving")

        if direction not in ("forward", "reverse"):
            return CommandResult(False, f"Invalid direction: {direction}")

        self.travel_direction = direction
        train.travel_direction = direction

        return CommandResult(True, f"Travel direction set to {direction}")

    # ─────────────────────────────────────────────────────────────────
    # Debug Commands (for testing only)
    # ─────────────────────────────────────────────────────────────────

    def set_target_speed_debug(self, speed: float) -> CommandResult:
        """
        DEBUG ONLY: Set target speed directly.

        This bypasses the handle model and sets a target speed.
        The simulation will automatically apply appropriate
        acceleration/deceleration to reach this speed.

        Warning: This is for debugging only. Production code
        should use set_handle() instead.
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        train.target_speed = max(0, min(speed, train.max_speed))

        return CommandResult(True, data={"target_speed": train.target_speed})

    # ─────────────────────────────────────────────────────────────────
    # Read-only state (for UI)
    # ─────────────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Convert onboard computer state to dictionary for JSON serialization."""
        train = self._get_train()

        return {
            "train_id": self.train_id,
            "power_on": self.power_on,
            "emergency_stop_active": self.emergency_stop_active,
            "active_cab": self.active_cab,
            "control_mode": self.control_mode,
            "handle_position": self.handle_position.value,
            "travel_direction": self.travel_direction,
            "ato_destination_id": self.ato_destination_id,
            "ato_path_id": self.ato_path_id,
            "ato_schedule_id": self.ato_schedule_id,
            "ato_running": self.ato_running,
            # Include some train state for convenience
            "train_state": train.state.value if train else None,
            "current_speed": train.current_speed if train else 0.0,
            "target_speed": train.target_speed if train else 0.0,
            "is_stopped": train.is_stopped() if train else True,
        }

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], network: "RailwayNetwork"
    ) -> "OnboardComputer":
        """Reconstruct from dictionary after deserialization."""
        handle_str = data.get("handle_position", "N")
        try:
            handle = HandlePosition(handle_str)
        except ValueError:
            handle = HandlePosition.N

        return cls(
            train_id=data["train_id"],
            network=network,
            power_on=data.get("power_on", False),
            emergency_stop_active=data.get("emergency_stop_active", False),
            active_cab=data.get("active_cab"),
            control_mode=data.get("control_mode", "ato"),
            handle_position=handle,
            travel_direction=data.get("travel_direction", "forward"),
            ato_destination_id=data.get("ato_destination_id"),
            ato_path_id=data.get("ato_path_id"),
            ato_schedule_id=data.get("ato_schedule_id"),
            ato_running=data.get("ato_running", False),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Speed Calculation Functions
# ─────────────────────────────────────────────────────────────────────────────


def get_acceleration_for_handle(
    handle: HandlePosition,
    acceleration_rate: float,
    deceleration_rate: float,
    emergency_brake_rate: float,
) -> float:
    """
    Get acceleration/deceleration rate for current handle position.

    Args:
        handle: Current handle position
        acceleration_rate: Maximum acceleration rate (km/h/s)
        deceleration_rate: Maximum service deceleration rate (km/h/s)
        emergency_brake_rate: Emergency braking rate (km/h/s)

    Returns:
        Positive value for acceleration (km/h/s)
        Negative value for deceleration (km/h/s)
        Zero for neutral (coast)
    """
    # Power notches (acceleration)
    if handle in POWER_MULTIPLIERS:
        return acceleration_rate * POWER_MULTIPLIERS[handle]

    # Neutral (coast)
    if handle == HandlePosition.N:
        return 0.0

    # Brake notches (deceleration)
    if handle in BRAKE_MULTIPLIERS:
        return -deceleration_rate * BRAKE_MULTIPLIERS[handle]

    # Emergency brake
    if handle == HandlePosition.EB:
        return -emergency_brake_rate

    return 0.0


def update_speed_from_handle(
    current_speed: float,
    handle: HandlePosition,
    acceleration_rate: float,
    deceleration_rate: float,
    emergency_brake_rate: float,
    delta_seconds: float,
    max_speed: float,
    atc_limit: float,
) -> float:
    """
    Calculate new speed based on handle position and time elapsed.

    Args:
        current_speed: Current speed in km/h
        handle: Current handle position
        acceleration_rate: Maximum acceleration rate (km/h/s)
        deceleration_rate: Maximum service deceleration rate (km/h/s)
        emergency_brake_rate: Emergency braking rate (km/h/s)
        delta_seconds: Time since last update
        max_speed: Train's maximum speed
        atc_limit: ATC-enforced speed limit

    Returns:
        New speed in km/h
    """
    acceleration = get_acceleration_for_handle(
        handle, acceleration_rate, deceleration_rate, emergency_brake_rate
    )

    # Calculate effective speed limit (min of max_speed and atc_limit)
    effective_limit = min(max_speed, atc_limit)

    # Apply acceleration/deceleration
    new_speed = current_speed + (acceleration * delta_seconds)

    # Clamp to valid range
    new_speed = max(0.0, min(new_speed, effective_limit))

    # If accelerating and at/above limit, don't accelerate further
    if acceleration > 0 and current_speed >= effective_limit:
        new_speed = current_speed

    return new_speed
