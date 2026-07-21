"""SignalRegistry — Signal CRUD pulled out of `RailwayNetwork`.

Owns signal creation / lookup / update / removal / block-connection and the
`signal_ids` bookkeeping on tracks. The `signals` dict lives on
`RailwayNetwork`; the registry reads / writes it through the back-reference
`self._n`, and signal STATE computation stays on the network's
`SignalCalculator` forwarders (`_calculate_signal_state`,
`is_signal_on_entry_tile`, `_can_reach_block`, `get_reachable_blocks`).
`RailwayNetwork.add_signal` / `get_signal` / etc. become thin forwarders so
external callers (editor, scenario setup, REST API, tests) keep working.
"""
from __future__ import annotations

import uuid
from typing import Optional, Union, TYPE_CHECKING

from railway_signal import Signal, SignalType, SignalState
from signal_validation import validate_signal_placement

if TYPE_CHECKING:
    from network import RailwayNetwork


class SignalRegistry:
    """Owns Signal CRUD for one `RailwayNetwork`."""

    def __init__(self, network: "RailwayNetwork") -> None:
        self._n = network

    def add_signal(
        self,
        x: int,
        y: int,
        detection_direction: str,
        signal_type: Union[SignalType, str] = SignalType.BLOCK,
        name: Optional[str] = None,
        auto_connect: bool = True,
        signal_id: Optional[str] = None,
    ) -> Signal:
        """
        Create and add a signal at the specified position.

        Args:
            x, y: Grid coordinates for signal placement
            detection_direction: Direction trains travel TO (NORTH, SOUTH, EAST, WEST)
            signal_type: Type of signal (BLOCK, SWITCH, etc.)
            name: Optional human-readable name
            auto_connect: If True, automatically find and connect to block ahead
            signal_id: Optional specific ID, auto-generated if not provided

        Returns:
            Created Signal instance

        Raises:
            ValueError: If position invalid, direction invalid, or signal already exists
        """
        # Convert string signal_type to enum if needed
        if isinstance(signal_type, str):
            signal_type = SignalType(signal_type.lower())

        # Check if track exists
        track = self._n.get_track(x, y)
        if not track:
            raise ValueError(f"No track at position ({x}, {y})")

        # Check if tile is part of a station (signals not allowed on platform tiles)
        if track.station_id is not None:
            raise ValueError(
                f"Cannot place signal on station tile at ({x}, {y}). "
                f"Tile belongs to station {track.station_id}"
            )

        # Validate signal placement
        is_valid, error_msg = validate_signal_placement(
            track.track_bits, detection_direction.upper()
        )
        if not is_valid:
            raise ValueError(error_msg)

        # Check for existing signal in this detection direction
        existing = self.get_signal_at_direction(x, y, detection_direction.upper())
        if existing:
            raise ValueError(
                f"Signal already exists at ({x}, {y}) with detection direction {detection_direction}: {existing.signal_id}"
            )

        # Determine max signals based on track type:
        # - Diagonal tracks (NE=4, NW=8, SE=16, SW=32): 1 signal max (single position at center)
        # - Straight tracks (NS=1, EW=2): 2 signals max (two positions)
        is_diagonal = track.track_bits in (4, 8, 16, 32)
        max_signals = 1 if is_diagonal else 2

        if len(track.signal_ids) >= max_signals:
            if is_diagonal:
                raise ValueError(
                    f"Diagonal track at ({x}, {y}) can only have 1 signal (center position)"
                )
            else:
                raise ValueError(
                    f"Cannot add more than 2 signals per tile at ({x}, {y})"
                )

        # Generate signal ID if not provided
        if signal_id is None:
            signal_id = f"SIG_{x}_{y}_{uuid.uuid4().hex[:6]}"

        # NOTE: Signals CAN be placed on tiles that belong to blocks.
        # The signal tile remains in its block. The signal can monitor:
        # - Any block AHEAD (normal case)
        # - Its OWN block, but ONLY if positioned on the entry tile
        # (The entry tile validation is enforced in get_reachable_blocks and connect_signal_to_block)

        # Create signal
        signal = Signal(
            signal_id=signal_id,
            position=(x, y),
            detection_direction=detection_direction.upper(),
            signal_type=signal_type,
            state=SignalState.RED,
            name=name,
        )

        # Store signal first (required for get_reachable_blocks to work)
        self._n.signals[signal_id] = signal

        # Auto-connect to block if requested using proper pathfinding
        if auto_connect and signal_type == SignalType.BLOCK:
            # Use the same pathfinding logic as the UI dropdown
            reachable_blocks = self._n.get_reachable_blocks(signal_id)
            if reachable_blocks:
                # Connect to the nearest reachable block (first in sorted list)
                signal.monitored_block_id = reachable_blocks[0]

        # Calculate signal state based on monitored block (if connected)
        if signal.monitored_block_id and signal_type == SignalType.BLOCK:
            signal.state = self._n._calculate_signal_state(signal)

        # Add signal ID to track's signal_ids list (max 2 per tile)
        if signal.signal_id not in track.signal_ids:
            track.signal_ids.append(signal.signal_id)

        return signal

    def get_signal(self, signal_id: str) -> Optional[Signal]:
        return self._n.signals.get(signal_id)

    def get_signal_at(self, x: int, y: int) -> Optional[Signal]:
        """Get first signal at the specified position."""
        for signal in self._n.signals.values():
            if signal.position == (x, y):
                return signal
        return None

    def get_signal_at_direction(
        self, x: int, y: int, detection_direction: str
    ) -> Optional[Signal]:
        """Get signal at position with a specific detection direction."""
        for signal in self._n.signals.values():
            if (
                signal.position == (x, y)
                and signal.detection_direction == detection_direction.upper()
            ):
                return signal
        return None

    def remove_signal(self, signal_id: str) -> bool:
        """
        Remove signal by ID.

        Returns:
            True if removed, False if not found
        """
        signal = self._n.signals.get(signal_id)
        if not signal:
            return False

        # Remove signal ID from track's signal_ids list
        track = self._n.get_track(*signal.position)
        if track and signal_id in track.signal_ids:
            track.signal_ids.remove(signal_id)

        del self._n.signals[signal_id]
        return True

    def remove_signal_at(self, x: int, y: int) -> bool:
        """
        Remove signal at position.

        Returns:
            True if removed, False if not found
        """
        signal = self.get_signal_at(x, y)
        if signal:
            return self.remove_signal(signal.signal_id)
        return False

    def update_signal(self, signal_id: str, **kwargs) -> Optional[Signal]:
        """
        Update signal properties.

        Allowed updates:
            - state: SignalState or string
            - name: str
            - monitored_block_id: str
            - monitored_switch_id: str
            - monitored_signal_id: str

        Args:
            signal_id: Signal to update
            **kwargs: Properties to update

        Returns:
            Updated Signal or None if not found

        Raises:
            ValueError: If trying to change immutable properties
        """
        signal = self._n.signals.get(signal_id)
        if not signal:
            return None

        # Prevent changing immutable properties
        immutable = {"signal_id", "position", "direction"}
        for key in kwargs:
            if key in immutable:
                raise ValueError(f"Cannot change immutable property: {key}")

        # Update allowed properties
        if "state" in kwargs:
            state_val = kwargs["state"]
            if isinstance(state_val, str):
                state_val = SignalState(state_val.lower())
            signal.state = state_val

        if "name" in kwargs:
            signal.name = kwargs["name"]

        if "monitored_block_id" in kwargs:
            new_block_id = kwargs["monitored_block_id"]

            # Validate: If monitoring own block, signal MUST be on entry tile
            if new_block_id:
                signal_track = self._n.get_track(*signal.position)
                if signal_track and signal_track.block_id == new_block_id:
                    if not self._n.is_signal_on_entry_tile(signal):
                        raise ValueError(
                            f"Signal cannot monitor block '{new_block_id}' from the middle. "
                            f"Signal at {signal.position} must be on the entry tile of the block to monitor it."
                        )

            signal.monitored_block_id = new_block_id

            # Recalculate signal state based on new monitored block
            if signal.signal_type == SignalType.BLOCK:
                if signal.monitored_block_id:
                    # Connected to a block - calculate state based on block occupancy
                    new_state = self._n._calculate_signal_state(signal)
                else:
                    # Disconnected - show RED (no block to monitor)
                    new_state = SignalState.RED
                signal.state = new_state

        if "monitored_switch_id" in kwargs:
            signal.monitored_switch_id = kwargs["monitored_switch_id"]

        if "monitored_signal_id" in kwargs:
            signal.monitored_signal_id = kwargs["monitored_signal_id"]

        return signal

    def connect_signal_to_block(self, signal_id: str, block_id: str) -> bool:
        """
        Manually connect a signal to monitor a specific block.

        Args:
            signal_id: Signal to connect
            block_id: Block to monitor

        Returns:
            True if successful

        Raises:
            ValueError: If signal/block not found, or no valid path exists, or
                       signal is in the middle of its own block (not on entry tile)
        """
        signal = self._n.signals.get(signal_id)
        if not signal:
            raise ValueError(f"Signal '{signal_id}' not found")

        if block_id not in self._n.blocks:
            raise ValueError(f"Block '{block_id}' not found")

        # Get the signal's own block (the block at the signal's position)
        signal_track = self._n.get_track(*signal.position)
        signal_own_block = signal_track.block_id if signal_track else None

        # Check if trying to monitor own block
        if signal_own_block and block_id == signal_own_block:
            # Can only monitor own block if on entry tile
            if not self._n.is_signal_on_entry_tile(signal):
                raise ValueError(
                    f"Signal cannot monitor block '{block_id}' from the middle. "
                    f"Signal at {signal.position} must be on the entry tile of the block to monitor it."
                )
            # Signal is on entry tile - allowed to monitor own block
            # Skip the path validation since we're already in the block
        else:
            # Validate that a path exists from signal to target block
            if not self._n._can_reach_block(signal, block_id):
                raise ValueError(
                    f"No valid path exists from signal at {signal.position} "
                    f"(facing {signal.approach_direction}, trains travel {signal.detection_direction}) to block '{block_id}'. "
                    f"Check track connectivity and switch geometry."
                )

        signal.monitored_block_id = block_id

        # Recalculate signal state based on new monitored block
        if signal.signal_type == SignalType.BLOCK:
            signal.state = self._n._calculate_signal_state(signal)

        if signal_track:
            signal_track.signal_block_id = block_id

        return True
