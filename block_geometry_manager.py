"""
BlockGeometryManager — owns Block topology / geometry mutation pulled out of
`RailwayNetwork`.

Per design_new.md, `Block` is a data container; `BlockManager` is the sole
mutator for runtime state (`occupied_by`, `_is_caution`, `reservation`);
this module is the sole writer of geometry / topology fields
(`length_cm`, `speed_limit_kmh`, `ordered_tiles`) and of
`track.block_section_id` for block lifecycle. The `RailwayNetwork` keeps
the doc-canonical method names as forwarders so existing callers (UI,
scenario setup, tests) continue to work without change.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, List, Optional, Set, Tuple

from block import Block
from railway_signal import SignalState

if TYPE_CHECKING:
    from network import RailwayNetwork
    from track import TrackSegment
    from railway_signal import Signal


class BlockGeometryManager:
    """Owns block topology / geometry mutation for one `RailwayNetwork`."""

    def __init__(self, network: "RailwayNetwork") -> None:
        self._n = network

    def create_block(self, block_id: str, name: str = "") -> Block:
        """
        Create a new empty block.

        Args:
            block_id: Unique identifier for the block
            name: Optional human-readable name

        Returns:
            Created Block instance

        Raises:
            ValueError: If block_id already exists
        """
        if block_id in self._n.blocks:
            raise ValueError(f"Block {block_id} already exists")
        block = Block(block_id=block_id, name=name)
        self._n.blocks[block_id] = block
        self._n._ensure_resource_managers()
        return block

    def create_block_with_tracks(
        self,
        block_id: str,
        positions: List[Tuple[int, int]],
        name: str = "",
        length_cm: float = 0.0,
        speed_limit_kmh: Optional[float] = None,
        validate: bool = True,
    ) -> Block:
        """
        Create a new block and assign tracks to it with full validation.

        This is the RECOMMENDED way to create blocks. Validates:
        - Block ID uniqueness
        - Block properties are valid
        - All positions have valid tracks
        - No tracks are switches, signals, or stations
        - No tracks are already in other blocks
        - All tracks form a continuous path

        Args:
            block_id: Unique block identifier
            positions: List of (x, y) track positions
            name: Human-readable block name
            length_cm: Physical track length
            speed_limit_kmh: Optional speed limit
            validate: If True, perform full validation (recommended)

        Returns:
            Created Block instance

        Raises:
            ValueError: If validation fails

        Example:
            # Create a block with validated tracks
            block = network.create_block_with_tracks(
                block_id="BLOCK_MAIN_1",
                positions=[(0,0), (1,0), (2,0)],
                name="Main Line Section 1",
                length_cm=150.0,
                speed_limit_kmh=80.0
            )
        """
        from block_validation import (
            validate_block_properties,
            validate_track_for_block,
            validate_block_assignment_continuity,
            validate_station_boundary,
        )

        # Validate block properties
        if validate:
            is_valid, errors = validate_block_properties(
                block_id, name, length_cm, speed_limit_kmh
            )
            if not is_valid:
                raise ValueError(f"Invalid block properties: {', '.join(errors)}")

            # Validate station boundaries - blocks cannot cross station boundaries
            valid, error = validate_station_boundary(positions, self._n)
            if not valid:
                raise ValueError(f"Station boundary violation: {error}")

        # Check block doesn't already exist
        if block_id in self._n.blocks:
            raise ValueError(f"Block {block_id} already exists")

        # Validate all tracks
        if validate:
            for x, y in positions:
                track = self._n.get_track(x, y)
                is_valid, error = validate_track_for_block(track, self._n)
                if not is_valid:
                    raise ValueError(f"Track at ({x}, {y}): {error}")

                # Check not already in another block
                if track.block_section_id:
                    raise ValueError(
                        f"Track at ({x}, {y}) already in block {track.block_section_id}"
                    )

            # Validate continuity
            is_continuous, errors = validate_block_assignment_continuity(
                [], positions, self._n
            )
            if not is_continuous:
                raise ValueError(f"Block tracks not continuous: {', '.join(errors)}")

        # Create block
        block = self.create_block(block_id, name)
        block.length_cm = length_cm
        if speed_limit_kmh:
            block.speed_limit_kmh = speed_limit_kmh

        # Assign tracks
        for x, y in positions:
            track = self._n.get_track(x, y)
            if track:
                track.block_section_id = block_id

        return block

    def extend_block(
        self,
        block_id: str,
        new_positions: List[Tuple[int, int]],
        force: bool = False,
        force_overwrite: bool = False,
    ) -> Block:
        """
        Extend an existing block with new track positions.

        Validates:
        - Block exists
        - All new tracks are valid for blocks (unless force=True for signals)
        - New tracks are adjacent to existing block
        - Extended block remains continuous

        Args:
            block_id: ID of block to extend
            new_positions: List of (x, y) positions to add
            force: If True, removes signals from tiles to allow block extension.
                   This allows extending a block past a signal boundary.
            force_overwrite: If True, allows overwriting tracks that belong to
                   other blocks (reassigns them to this block).

        Returns:
            Updated Block instance

        Raises:
            ValueError: If validation fails
        """
        from block_validation import (
            validate_track_for_block,
            validate_block_assignment_continuity,
            get_adjacent_positions,
            is_crossing,
        )

        if block_id not in self._n.blocks:
            raise ValueError(f"Block {block_id} not found")

        block = self._n.blocks[block_id]
        existing_positions = self._n.get_block_positions(block_id)

        # Track signals that need to be removed when force=True
        signals_to_remove = []
        # Track blocks that will be affected by force_overwrite
        overwritten_blocks = set()

        # Validate each new position
        for x, y in new_positions:
            track = self._n.get_track(x, y)
            if not track:
                raise ValueError(f"No track at position ({x}, {y})")

            # Check for crossings (never allowed, even with force)
            if is_crossing(track.track_bits):
                raise ValueError(
                    f"Track at ({x}, {y}): Cannot add crossing to block - crossings isolate paths"
                )

            # Check for switches (never allowed, even with force)
            if track.is_switch:
                raise ValueError(
                    f"Track at ({x}, {y}): Cannot add switch to block - switches are block boundaries"
                )

            # Check for signals - can be overridden with force
            # When force=True, we clear the signal's monitored block (shows warning triangle)
            # but we do NOT remove the signal itself
            # Only invalidate signals that monitor the block being extended (block_id)
            if track.signal_ids:
                if force:
                    # Collect signals to invalidate (clear monitored block)
                    # Only invalidate if the signal monitors the block we're extending
                    for signal_id in track.signal_ids:
                        signal = self._n.signals.get(signal_id)
                        if signal and signal.monitored_block_id == block_id:
                            signals_to_remove.append((signal_id, x, y))
                else:
                    raise ValueError(
                        f"Track at ({x}, {y}): Has signal(s) - use force=true to extend past signal"
                    )

            if track.block_section_id and track.block_section_id != block_id:
                if force_overwrite:
                    # Track the block that will lose this track
                    overwritten_blocks.add(track.block_section_id)
                else:
                    raise ValueError(
                        f"Track at ({x}, {y}) already in block {track.block_section_id}"
                    )

        # Check adjacency
        is_adjacent = False
        for new_x, new_y in new_positions:
            for old_x, old_y in existing_positions:
                if abs(new_x - old_x) + abs(new_y - old_y) == 1:
                    is_adjacent = True
                    break
            if is_adjacent:
                break

        if not is_adjacent and len(existing_positions) > 0:
            raise ValueError("New positions must be adjacent to existing block")

        # Validate station boundaries - block cannot cross station boundaries
        from block_validation import validate_station_boundary

        all_positions = existing_positions + new_positions
        valid, error = validate_station_boundary(all_positions, self._n)
        if not valid:
            raise ValueError(f"Station boundary violation: {error}")

        # Invalidate signals in force mode - clear their monitored block
        # This causes them to show an orange warning triangle (signal needs reconfiguration)
        invalidated_signals = []
        for signal_id, x, y in signals_to_remove:
            if signal_id in self._n.signals:
                signal = self._n.signals[signal_id]
                # detection_direction can be either a string or an enum, handle both
                detection_direction = signal.detection_direction
                if hasattr(detection_direction, "name"):
                    detection_direction = detection_direction.name
                invalidated_signals.append(
                    {
                        "signal_id": signal_id,
                        "position": [x, y],
                        "detection_direction": detection_direction,
                        "approach_direction": signal.approach_direction,
                        "previous_block": signal.monitored_block_id,
                    }
                )
                # Clear the monitored block - signal will show warning triangle
                signal.monitored_block_id = None
                # Set signal to RED since it's no longer monitoring a block
                signal.state = SignalState.RED

        # Validate continuity
        is_continuous, errors = validate_block_assignment_continuity(
            existing_positions, new_positions, self._n
        )
        if not is_continuous:
            raise ValueError(f"Extension would break continuity: {', '.join(errors)}")

        # Assign new tracks
        for x, y in new_positions:
            track = self._n.get_track(x, y)
            if track:
                track.block_section_id = block_id

        # Store invalidated signals info on block for API response
        block._invalidated_signals = invalidated_signals

        return block

    def delete_block(self, block_id: str) -> bool:
        """
        Delete block and clear references from all tracks.
        Also disconnects signals monitoring this block.

        Args:
            block_id: ID of block to delete

        Returns:
            True if deleted, False if not found
        """
        if block_id not in self._n.blocks:
            return False

        # Clear block_section_id from all tracks in this block
        for track in self._n.tracks.values():
            if track.block_section_id == block_id:
                track.block_section_id = None

        # Disconnect signals monitoring this block (don't delete them)
        for signal in self._n.signals.values():
            if signal.monitored_block_id == block_id:
                signal.monitored_block_id = None

        del self._n.blocks[block_id]
        return True

    def assign_track_to_block(
        self, x: int, y: int, block_id: Optional[str], validate_continuity: bool = True
    ) -> None:
        """
        Assign a track to a block, validating block membership and continuity.

        Args:
            x, y: Track position
            block_id: Block ID to assign (None to unassign)
            validate_continuity: If True, check that assignment maintains continuity

        Raises:
            ValueError: If validation fails with detailed error message
        """
        from block_validation import (
            validate_track_for_block,
            validate_block_assignment_continuity,
        )

        track = self._n.get_track(x, y)
        if not track:
            raise ValueError(f"No track at position ({x}, {y})")

        # If assigning to a block
        if block_id:
            # Check block exists
            if block_id not in self._n.blocks:
                raise ValueError(f"Block {block_id} does not exist")

            # Validate track can be in a block
            is_valid, error = validate_track_for_block(track, self._n)
            if not is_valid:
                raise ValueError(f"Cannot assign track at ({x}, {y}) to block: {error}")

            # Check if already assigned to a different block
            if track.block_section_id and track.block_section_id != block_id:
                raise ValueError(
                    f"Track at ({x}, {y}) already assigned to block {track.block_section_id}. "
                    f"Unassign first or use reassign method."
                )

            # Validate continuity if requested
            if validate_continuity:
                existing_positions = self._n.get_block_positions(block_id)
                if len(existing_positions) > 0:
                    is_continuous, errors = validate_block_assignment_continuity(
                        existing_positions, [(x, y)], self._n
                    )
                    if not is_continuous:
                        raise ValueError(
                            f"Adding track at ({x}, {y}) would create discontinuous block: "
                            f"{', '.join(errors)}"
                        )

        # Assignment is valid - update
        track.block_section_id = block_id

    def validate_block_continuity(self, block_id: str) -> Tuple[bool, List[str]]:
        """
        Check if block tracks form a connected graph.

        Uses BFS to verify all tracks in the block are reachable from
        any starting track via edge connections.

        Args:
            block_id: Block ID to validate

        Returns:
            Tuple of (is_valid, list of error messages)
        """
        tracks = self._n.get_block_tracks(block_id)
        if len(tracks) <= 1:
            return True, []

        positions = set((t.x, t.y) for t in tracks)
        visited = set()
        errors = []

        # BFS from first track
        start = tracks[0]
        to_visit = [(start.x, start.y)]

        while to_visit:
            pos = to_visit.pop()
            if pos in visited:
                continue
            visited.add(pos)

            track = self._n.get_track(*pos)
            if not track:
                continue

            # Get connected neighbors via edges
            for neighbor_pos in self._n._get_block_connected_neighbors(track):
                if neighbor_pos in positions and neighbor_pos not in visited:
                    to_visit.append(neighbor_pos)

        if len(visited) != len(positions):
            unvisited = positions - visited
            errors.append(f"Disconnected tracks at: {list(unvisited)}")
            return False, errors

        return True, []

    def remove_tile_from_block(self, x: int, y: int) -> List[str]:
        """
        Remove a tile from its block and handle connectivity check.

        This is the core lifecycle method for block management. When a tile
        is removed from a block (due to station/signal placement, or track deletion),
        this method:
        1. Clears the tile's block_section_id
        2. Checks if remaining block tiles are still connected
        3. If disconnected, splits into new child blocks

        Args:
            x, y: Tile position to remove from its block

        Returns:
            List of affected block IDs (original + any new child blocks created)
        """
        track = self._n.get_track(x, y)
        if not track or not track.block_section_id:
            return []

        block_id = track.block_section_id
        track.block_section_id = None

        # Check and potentially split the block
        return self._check_block_connectivity(block_id)

    def remove_tiles_from_block(self, positions: List[Tuple[int, int]]) -> List[str]:
        """
        Remove multiple tiles from their block(s) and handle connectivity.

        This is a batch version of remove_tile_from_block for efficiency.
        All tiles should belong to the same block for proper splitting.

        Args:
            positions: List of (x, y) positions to remove

        Returns:
            List of affected block IDs
        """
        if not positions:
            return []

        # Group positions by block
        blocks_affected = {}
        for x, y in positions:
            track = self._n.get_track(x, y)
            if track and track.block_section_id:
                block_id = track.block_section_id
                if block_id not in blocks_affected:
                    blocks_affected[block_id] = []
                blocks_affected[block_id].append((x, y))
                track.block_section_id = None

        # Check connectivity for each affected block
        all_affected = []
        for block_id in blocks_affected:
            affected = self._check_block_connectivity(block_id)
            all_affected.extend(affected)

        return all_affected

    def _check_block_connectivity(self, block_id: str) -> List[str]:
        """
        Check block connectivity and split if disconnected.

        After tiles are removed from a block, this method:
        1. Gets remaining tile positions
        2. Finds connected components
        3. If multiple components, deletes original and creates child blocks
        4. Remaps orphaned signals to nearest reachable child block

        Args:
            block_id: Block ID to check

        Returns:
            List of affected block IDs (may include new child block IDs)
        """
        block = self._n.blocks.get(block_id)
        if not block:
            return []

        remaining_positions = self._n.get_block_positions(block_id)

        # Case 1: Block is now empty
        if not remaining_positions:
            self._disconnect_signals_for_block(block_id)
            self.delete_block(block_id)
            return [block_id]

        # Find connected components
        components = self._find_connected_components(remaining_positions)

        # Case 2: Single component - block is still connected
        if len(components) == 1:
            return [block_id]

        # Case 3: Multiple components - split into child blocks
        # Get signals monitoring this block before deletion
        orphaned_signals = [
            signal
            for signal in self._n.signals.values()
            if signal.monitored_block_id == block_id
        ]

        # Create child blocks
        new_block_ids = self._create_child_blocks(block, components)

        # Delete original block (but don't disconnect signals yet - we'll remap them)
        if block_id in self._n.blocks:
            del self._n.blocks[block_id]

        # Remap orphaned signals to nearest reachable child block
        self._remap_orphaned_signals(orphaned_signals, new_block_ids)

        return [block_id] + new_block_ids

    def _disconnect_signals_for_block(self, block_id: str) -> None:
        """
        Disconnect all signals monitoring the specified block.

        Sets monitored_block_id = None for all signals monitoring this block.

        Args:
            block_id: Block ID to disconnect signals from
        """
        for signal in self._n.signals.values():
            if signal.monitored_block_id == block_id:
                signal.monitored_block_id = None

    # === Unified Block Lifecycle Management ===

    def _create_child_blocks(
        self, parent_block: Block, components: List[List[Tuple[int, int]]]
    ) -> List[str]:
        """
        Create child blocks from connected components, inheriting parent properties.

        Child blocks inherit:
        - name: "{parent.name}_1", "{parent.name}_2", etc.
        - length_cm: Calculated based on tile count × length_per_tile_cm from parent
        - min_radius_cm, speed_limit_kmh (copied as-is)
        - state: Reset to FREE (original occupancy doesn't transfer cleanly)

        Args:
            parent_block: Original block being split
            components: List of connected components (each is list of positions)

        Returns:
            List of new block IDs
        """
        new_block_ids = []
        base_name = parent_block.name if parent_block.name else parent_block.block_id

        # Calculate length per tile from parent block
        # Check if parent has stored length_per_tile_cm (from station split)
        length_per_tile_cm = getattr(parent_block, "_length_per_tile_cm", None)

        if length_per_tile_cm is None:
            if (
                not hasattr(parent_block, "length_cm")
                or parent_block.length_cm is None
                or parent_block.length_cm <= 0
            ):
                parent_block.length_cm = 0.0
            parent_positions = self._n.get_block_positions(parent_block.block_id)
            if parent_positions and parent_block.length_cm > 0:
                length_per_tile_cm = parent_block.length_cm / len(parent_positions)
            else:
                total_tiles = sum(len(comp) for comp in components)
                if total_tiles > 0 and parent_block.length_cm > 0:
                    length_per_tile_cm = parent_block.length_cm / total_tiles
                else:
                    length_per_tile_cm = 0.0

        for i, component_positions in enumerate(components, start=1):
            # Generate child block ID and name
            child_id = f"{parent_block.block_id}_{i}"
            child_name = f"{base_name}_{i}"

            # Calculate child block length based on tile count
            child_length_cm = (
                len(component_positions) * length_per_tile_cm
                if length_per_tile_cm > 0
                else 0.0
            )

            # Create child block with inherited properties
            # v1: Block.state is a derived @property; do not pass it to the
            # constructor. occupied_by defaults to None which yields FREE.
            child_block = Block(
                block_id=child_id,
                name=child_name,
                length_cm=child_length_cm,  # Calculated based on tile count
                min_radius_cm=parent_block.min_radius_cm,
                speed_limit_kmh=parent_block.speed_limit_kmh,
                occupied_by=None,
            )

            self._n.blocks[child_id] = child_block
            self._n._ensure_resource_managers()

            for x, y in component_positions:
                track = self._n.get_track(x, y)
                if track:
                    track.block_section_id = child_id

            new_block_ids.append(child_id)

        return new_block_ids

    def _find_connected_components(
        self, positions: List[Tuple[int, int]]
    ) -> List[List[Tuple[int, int]]]:
        """
        Find connected components among tile positions using track connectivity.

        Two tiles are connected if:
        1. They are adjacent (Manhattan distance = 1)
        2. Their track_bits allow connection (one has edge pointing to other)

        Uses BFS to group tiles into connected components.

        Args:
            positions: List of (x, y) tile positions

        Returns:
            List of components, where each component is a list of positions
        """
        if not positions:
            return []

        position_set = set(positions)
        visited = set()
        components = []

        for start_pos in positions:
            if start_pos in visited:
                continue

            # BFS to find all connected tiles from this starting point
            component = []
            queue = [start_pos]
            visited.add(start_pos)

            while queue:
                current = queue.pop(0)
                component.append(current)

                # Find connected neighbors within the position set
                for neighbor in self._get_track_connected_neighbors(current):
                    if neighbor in position_set and neighbor not in visited:
                        visited.add(neighbor)
                        queue.append(neighbor)

            components.append(component)

        return components

    def _get_track_connected_neighbors(
        self, pos: Tuple[int, int]
    ) -> List[Tuple[int, int]]:
        """
        Get positions of tracks connected via edges (track_bits connectivity).

        A neighbor is connected if:
        1. This track has an edge pointing toward the neighbor
        2. The neighbor has an edge pointing back

        Args:
            pos: Position to check neighbors for

        Returns:
            List of connected neighbor positions
        """
        track = self._n.get_track(*pos)
        if not track:
            return []

        neighbors = []
        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}
        opposite_edge = {"N": "S", "S": "N", "E": "W", "W": "E"}

        for edge in track.edges:
            if edge not in edge_offsets:
                continue
            dx, dy = edge_offsets[edge]
            neighbor_pos = (pos[0] + dx, pos[1] + dy)
            neighbor = self._n.get_track(*neighbor_pos)
            if neighbor:
                opp = opposite_edge[edge]
                if opp in neighbor.edges:
                    neighbors.append(neighbor_pos)

        return neighbors

    def _remap_orphaned_signals(
        self, orphaned_signals: List[Signal], new_block_ids: List[str]
    ) -> None:
        """
        Remap signals that monitored a deleted block to nearest reachable child block.

        For each orphaned signal, uses _can_reach_block to find which new block
        is reachable, and updates monitored_block_id accordingly.

        Args:
            orphaned_signals: List of signals that need remapping
            new_block_ids: List of child block IDs to consider
        """
        for signal in orphaned_signals:
            # Try to find a reachable block among the new child blocks
            found_block = None
            for block_id in new_block_ids:
                if self._n._can_reach_block(signal, block_id):
                    found_block = block_id
                    break

            # Update signal
            signal.monitored_block_id = found_block

            # Recalculate signal state based on new monitored block
            if signal.signal_type == SignalType.BLOCK:
                if signal.monitored_block_id:
                    signal.state = self._n._calculate_signal_state(signal)
                else:
                    signal.state = SignalState.RED

    # === Centralized Switch Management ===

