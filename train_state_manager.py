"""
Train State Manager

Definition:
This module provides the TrainStateManager class which validates and manages
train state transitions according to the state machine specification.

The state machine ensures trains transition safely between states:
- OFF: No power, 0V
- IDLE: Powered, lighting voltage, no destination
- STANDBY: Ready to depart, direction set
- RUNNING_MOVING: Actively moving with speed-based voltage
- RUNNING_WAITING: Stopped, waiting for path/signal
- RUNNING_AT_STATION: Dwelling at station
- MANUAL: User-controlled speed with ATC protection
"""

from typing import Optional, TYPE_CHECKING
from dataclasses import dataclass

from enums import TrainState, Direction
from train_config import STOPPED_SPEED_THRESHOLD_KMH

if TYPE_CHECKING:
    from train import Train


@dataclass
class StateTransitionResult:
    """
    Result of a state transition attempt.

    Attributes:
        success: Whether the transition was successful
        error: Error message if transition failed
        old_state: Previous state
        new_state: Current state after transition attempt
    """
    success: bool
    error: Optional[str] = None
    old_state: Optional[TrainState] = None
    new_state: Optional[TrainState] = None


class TrainStateManager:
    """
    Manages train state transitions and validates state machine rules.

    The state manager ensures trains can only transition between valid states
    according to the state machine specification. It also checks preconditions
    like train being stopped, having a direction set, etc.
    """

    # Valid state transitions: from_state -> [to_state, ...]
    VALID_TRANSITIONS = {
        TrainState.OFF: [TrainState.IDLE],
        TrainState.IDLE: [TrainState.OFF, TrainState.STANDBY, TrainState.MANUAL],
        TrainState.STANDBY: [TrainState.IDLE, TrainState.RUNNING_MOVING, TrainState.MANUAL],
        TrainState.RUNNING_MOVING: [TrainState.RUNNING_WAITING, TrainState.RUNNING_AT_STATION],
        TrainState.RUNNING_WAITING: [TrainState.RUNNING_MOVING, TrainState.STANDBY, TrainState.IDLE],
        TrainState.RUNNING_AT_STATION: [TrainState.STANDBY, TrainState.IDLE],
        TrainState.MANUAL: [TrainState.IDLE, TrainState.STANDBY],
    }

    @staticmethod
    def can_transition(train: "Train", to_state: TrainState) -> StateTransitionResult:
        """
        Check if a train can transition to a new state.

        Validates:
        1. Transition is in the valid transitions map
        2. Preconditions for the transition are met

        Args:
            train: Train to check
            to_state: Desired target state

        Returns:
            StateTransitionResult with success and error info
        """
        from_state = train.state

        # Same state is always valid (no-op)
        if from_state == to_state:
            return StateTransitionResult(
                success=True,
                old_state=from_state,
                new_state=to_state
            )

        # Check if transition is in valid transitions
        valid_targets = TrainStateManager.VALID_TRANSITIONS.get(from_state, [])
        if to_state not in valid_targets:
            return StateTransitionResult(
                success=False,
                error=f"Invalid transition: {from_state.value} -> {to_state.value}",
                old_state=from_state,
                new_state=from_state
            )

        # Check preconditions for specific transitions

        # OFF -> IDLE: no preconditions
        if from_state == TrainState.OFF and to_state == TrainState.IDLE:
            return StateTransitionResult(success=True, old_state=from_state, new_state=to_state)

        # Any -> OFF: must be stopped
        if to_state == TrainState.OFF:
            if not train.is_stopped():
                return StateTransitionResult(
                    success=False,
                    error="Cannot power off while train is moving",
                    old_state=from_state,
                    new_state=from_state
                )
            return StateTransitionResult(success=True, old_state=from_state, new_state=to_state)

        # IDLE -> STANDBY: no additional preconditions (direction will be set)
        if from_state == TrainState.IDLE and to_state == TrainState.STANDBY:
            return StateTransitionResult(success=True, old_state=from_state, new_state=to_state)

        # IDLE/STANDBY -> MANUAL: must be stopped
        if to_state == TrainState.MANUAL:
            if from_state == TrainState.OFF:
                return StateTransitionResult(
                    success=False,
                    error="Cannot enable manual mode from OFF state",
                    old_state=from_state,
                    new_state=from_state
                )
            if not train.is_stopped():
                return StateTransitionResult(
                    success=False,
                    error="Cannot enable manual mode while train is moving",
                    old_state=from_state,
                    new_state=from_state
                )
            return StateTransitionResult(success=True, old_state=from_state, new_state=to_state)

        # MANUAL -> IDLE/STANDBY: must be stopped
        if from_state == TrainState.MANUAL:
            if not train.is_stopped():
                return StateTransitionResult(
                    success=False,
                    error="Cannot disable manual mode while train is moving",
                    old_state=from_state,
                    new_state=from_state
                )
            return StateTransitionResult(success=True, old_state=from_state, new_state=to_state)

        # STANDBY -> RUNNING_MOVING: should have direction and path
        if from_state == TrainState.STANDBY and to_state == TrainState.RUNNING_MOVING:
            if train.facing_direction is None:
                return StateTransitionResult(
                    success=False,
                    error="Cannot start moving without facing direction",
                    old_state=from_state,
                    new_state=from_state
                )
            # Path check is optional - train can start moving and request path
            return StateTransitionResult(success=True, old_state=from_state, new_state=to_state)

        # All other valid transitions have no additional preconditions
        return StateTransitionResult(success=True, old_state=from_state, new_state=to_state)

    @staticmethod
    def transition(train: "Train", to_state: TrainState) -> StateTransitionResult:
        """
        Transition a train to a new state if valid.

        Checks if transition is valid, then updates the train's state.

        Args:
            train: Train to transition
            to_state: Target state

        Returns:
            StateTransitionResult indicating success or failure
        """
        result = TrainStateManager.can_transition(train, to_state)

        if result.success:
            train.state = to_state
            result.new_state = to_state

        return result

    @staticmethod
    def get_voltage_for_state(train: "Train", speed_profile_lighting_voltage: float = 2.5) -> float:
        """
        Get the appropriate voltage for a train's current state.

        This is used by BlockPowerController to determine voltage output.

        Args:
            train: Train to get voltage for
            speed_profile_lighting_voltage: Lighting voltage from speed profile

        Returns:
            Voltage in volts (0-12V)
        """
        state = train.state

        if state == TrainState.OFF:
            return 0.0

        if state in (TrainState.IDLE, TrainState.STANDBY,
                     TrainState.RUNNING_WAITING, TrainState.RUNNING_AT_STATION):
            return speed_profile_lighting_voltage

        # RUNNING_MOVING and MANUAL use speed-based voltage
        # (actual conversion done by BlockPowerController with SpeedProfile)
        # Return a sentinel value to indicate speed-based voltage needed
        return -1.0  # Sentinel for "use speed profile"

    @staticmethod
    def is_state_moving(state: TrainState) -> bool:
        """
        Check if a state represents the train actively moving.

        Args:
            state: State to check

        Returns:
            True if train should be moving in this state
        """
        return state in (TrainState.RUNNING_MOVING, TrainState.MANUAL)

    @staticmethod
    def is_state_powered(state: TrainState) -> bool:
        """
        Check if a state has power applied (not OFF).

        Args:
            state: State to check

        Returns:
            True if state has power
        """
        return state != TrainState.OFF

    @staticmethod
    def get_state_description(state: TrainState) -> str:
        """
        Get human-readable description of a state.

        Args:
            state: State to describe

        Returns:
            Description string
        """
        descriptions = {
            TrainState.OFF: "Off - No power, no lights",
            TrainState.IDLE: "Idle - Powered, lighting only, no destination",
            TrainState.STANDBY: "Standby - Ready to depart, direction set",
            TrainState.RUNNING_MOVING: "Running (Moving) - Actively moving with speed control",
            TrainState.RUNNING_WAITING: "Running (Waiting) - Stopped, waiting for path or signal",
            TrainState.RUNNING_AT_STATION: "Running (At Station) - Dwelling at station",
            TrainState.MANUAL: "Manual - User-controlled speed with ATC protection",
        }
        return descriptions.get(state, f"Unknown state: {state.value}")
