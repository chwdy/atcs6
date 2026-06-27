"""
SignalCalculator — owns the signal-state computation and the BFS-based
reachability / distance walks pulled out of `RailwayNetwork`.

Per design_new.md "Signals" the signal state for every BLOCK-type signal
is derived from `(monitored_block.state, switch_routing,
next_block.state, signal.approaching_train_id)`. This module centralises
those derivations together with the graph walks (`_can_reach_block`,
`_get_distance_to_block`, `_find_next_block_after`, `_find_block_ahead`)
so `RailwayNetwork` stays a registry / topology surface.

`RailwayNetwork` keeps the doc-canonical method names
(`update_signals_from_blocks`, `get_reachable_blocks`,
`is_signal_on_entry_tile`) as thin forwarders that delegate here.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING, List, Optional

from enums import BlockState
from railway_signal import SignalState, SignalType
from signal_validation import get_direction_vector

if TYPE_CHECKING:
    from network import RailwayNetwork
    from railway_signal import Signal  # noqa: F401


class SignalCalculator:
    """Computes signal state and reachability for a `RailwayNetwork`.

    Holds a back-reference to the network; every read goes through
    `self._n.<field>` (`tracks`, `blocks`, `signals`).
    """

    def __init__(self, network: "RailwayNetwork") -> None:
        self._n = network

    # ------------------------------------------------------------------
    # Signal-state computation
    # ------------------------------------------------------------------

    def update_all(self) -> None:
        """Recompute state for every BLOCK-type signal in the network."""
        for signal in self._n.signals.values():
            if signal.signal_type == SignalType.BLOCK and signal.monitored_block_id:
                signal.state = self.compute_state(signal)

    def compute_state(self, signal) -> SignalState:
        """Calculate signal state from `(block_states, switch_routing,
        approaching_train_id)`.

        Priority (highest first):
          * RED: no/missing block, switch blocks route, Block A OCCUPIED, or
            Block A RESERVED for a different train / opposite direction.
          * YELLOW: Block A CAUTION, or Block A FREE / RESERVED-for-our-train
            but Block B is anything except FREE.
          * GREEN: Block A FREE (or RESERVED for our train) and Block B is
            FREE / absent / RESERVED-for-our-train.
        """
        n = self._n

        if not signal.monitored_block_id:
            return SignalState.RED

        first_block = n.blocks.get(signal.monitored_block_id)
        if not first_block:
            return SignalState.RED

        if not self.check_switch_routing(signal):
            return SignalState.RED

        if first_block.state == BlockState.OCCUPIED:
            return SignalState.RED

        if first_block.state == BlockState.RESERVED:
            signal_track = n.get_track(*signal.position)
            block_train = (
                first_block.reserved_for_train or first_block.occupied_by
            )
            if signal_track and signal_track.block_section_id:
                signal_block = n.blocks.get(signal_track.block_section_id)
                # The signal allows the monitored (reserved) block to be entered
                # only if it can establish continuity with the train approaching
                # from this signal's side. Two ways to do that:
                #   (a) signal_block is reserved_by the SAME path as first_block
                #       — the path is still wholly RESERVED (train hasn't entered
                #       any of it yet).
                #   (b) signal_block is OCCUPIED by the SAME train that first_block
                #       is reserved for — the train is rolling through the path
                #       and has already entered the signal's block. Without this
                #       branch, every reserved-block signal goes RED the instant
                #       the train enters the path's first block, because state
                #       transitions RESERVED → OCCUPIED clear `reserved_by_path`.
                same_path_reservation = bool(
                    signal_block
                    and signal_block.reserved_by_path
                    and signal_block.reserved_by_path == first_block.reserved_by_path
                )
                same_train_continuity = bool(
                    signal_block
                    and signal_block.occupied_by
                    and block_train
                    and signal_block.occupied_by == block_train
                )
                if same_path_reservation or same_train_continuity:
                    # Verify the path direction at the signal's block matches
                    # the signal's detection direction. Canonical home per
                    # design_new.md is `Block.reserved_direction`; when the
                    # signal's block is already OCCUPIED (train past entry,
                    # reservation consumed and direction cleared), the entry-
                    # time direction check has already vetted alignment so we
                    # do not re-check here.
                    if signal_block and signal_block.reserved_direction:
                        signal_dir = signal.detection_direction.upper()
                        path_dir = signal_block.reserved_direction.upper()
                        if signal_dir != path_dir:
                            return SignalState.RED
                    pass  # Continue to Block B evaluation
                else:
                    if first_block.reserved_direction:
                        signal_dir = signal.detection_direction.upper()
                        block_dir = first_block.reserved_direction.upper()
                        if signal_dir != block_dir:
                            return SignalState.RED

                    if (
                        signal.approaching_train_id
                        and block_train == signal.approaching_train_id
                    ):
                        pass
                    else:
                        return SignalState.RED
            else:
                if first_block.reserved_direction:
                    signal_dir = signal.detection_direction.upper()
                    block_dir = first_block.reserved_direction.upper()
                    if signal_dir != block_dir:
                        return SignalState.RED

                if (
                    signal.approaching_train_id
                    and block_train == signal.approaching_train_id
                ):
                    pass
                else:
                    return SignalState.RED

        elif first_block.state == BlockState.CAUTION:
            return SignalState.YELLOW

        # Block A FREE (or RESERVED for our train) — check Block B.
        second_block_id = self.find_next_block_after(
            signal.monitored_block_id, signal.detection_direction
        )

        if not second_block_id:
            return SignalState.GREEN

        second_block = n.blocks.get(second_block_id)
        if not second_block:
            return SignalState.GREEN

        if second_block.state == BlockState.FREE:
            return SignalState.GREEN
        elif (
            second_block.state == BlockState.RESERVED
            and signal.approaching_train_id
            and (second_block.reserved_for_train or second_block.occupied_by)
                == signal.approaching_train_id
        ):
            return SignalState.GREEN
        else:
            return SignalState.YELLOW

    # ------------------------------------------------------------------
    # Switch-routing check (used by compute_state)
    # ------------------------------------------------------------------

    def check_switch_routing(self, signal) -> bool:
        """True if a path from `signal` to its monitored block is currently
        open (i.e. the switches between them are aligned to allow it)."""
        n = self._n

        if not signal.monitored_block_id:
            return False

        x, y = signal.position
        detection_dir = signal.detection_direction.upper()
        approach_dir = signal.approach_direction.upper()

        signal_track = n.get_track(x, y)
        if not signal_track:
            return False

        if signal_track.block_section_id == signal.monitored_block_id:
            return True

        track_bits = signal_track.track_bits
        is_diagonal = track_bits in (4, 8, 16, 32)

        if is_diagonal:
            diagonal_edges = {
                4: ("N", "E"),
                8: ("N", "W"),
                16: ("S", "E"),
                32: ("S", "W"),
            }
            edge1, edge2 = diagonal_edges.get(track_bits, ("N", "E"))
            detection_edge = detection_dir[0]
            entry_edge = edge2 if detection_edge == edge1 else edge1
        else:
            entry_edge = approach_dir
            if not entry_edge:
                return True

        initial_exits = signal_track.get_exits(entry_edge, use_switch_state=True)
        if not initial_exits:
            return False

        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}

        for initial_exit in initial_exits:
            current_exit_edge = initial_exit
            current_x, current_y = x, y
            visited = set()

            for _ in range(50):
                pos_key = (current_x, current_y, current_exit_edge)
                if pos_key in visited:
                    break
                visited.add(pos_key)

                offset = edge_offsets.get(current_exit_edge)
                if not offset:
                    break
                dx, dy = offset
                next_x, next_y = current_x + dx, current_y + dy

                next_track = n.get_track(next_x, next_y)
                if not next_track:
                    break

                if next_track.block_section_id == signal.monitored_block_id:
                    return True

                entry_edge = {"N": "S", "S": "N", "E": "W", "W": "E"}.get(
                    current_exit_edge
                )
                if not entry_edge:
                    break

                if entry_edge not in next_track.edges:
                    break

                exits = next_track.get_exits(entry_edge, use_switch_state=True)

                if not exits:
                    break

                current_exit_edge = (
                    exits[0] if isinstance(exits, list) else list(exits)[0]
                )
                current_x, current_y = next_x, next_y

        return False

    # ------------------------------------------------------------------
    # BFS reachability (ignores switch state)
    # ------------------------------------------------------------------

    def can_reach_block(self, signal, target_block_id: str) -> bool:
        """True if any path through any switch configuration reaches
        `target_block_id` from `signal`'s detection direction."""
        n = self._n

        x, y = signal.position
        travel_dir = signal.detection_direction.upper()

        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}
        opposite_edges = {"N": "S", "S": "N", "E": "W", "W": "E"}

        initial_exit = travel_dir[0] if travel_dir else None
        if not initial_exit:
            return False

        queue = deque([(x, y, initial_exit)])
        visited = set()

        while queue:
            curr_x, curr_y, exit_edge = queue.popleft()

            state = (curr_x, curr_y, exit_edge)
            if state in visited:
                continue
            visited.add(state)

            if len(visited) > 500:
                return False

            offset = edge_offsets.get(exit_edge)
            if not offset:
                continue
            dx, dy = offset
            next_x, next_y = curr_x + dx, curr_y + dy

            next_track = n.get_track(next_x, next_y)
            if not next_track:
                continue

            if next_track.block_section_id == target_block_id:
                return True

            entry_edge = opposite_edges.get(exit_edge)
            if not entry_edge:
                continue

            if entry_edge not in next_track.edges:
                continue

            all_exits = next_track.get_exits(entry_edge, use_switch_state=False)
            if not all_exits:
                continue

            for next_exit in all_exits:
                queue.append((next_x, next_y, next_exit))

        return False

    def distance_to_block(self, signal, target_block_id: str) -> Optional[int]:
        """Shortest path in tiles from `signal` to `target_block_id`, or None."""
        n = self._n

        x, y = signal.position
        detection_dir = signal.detection_direction.upper()
        approach_dir = signal.approach_direction.upper()

        signal_track = n.get_track(x, y)
        if not signal_track:
            return None

        track_bits = signal_track.track_bits
        is_diagonal = track_bits in (4, 8, 16, 32)

        if is_diagonal:
            diagonal_edges = {
                4: ("N", "E"),
                8: ("N", "W"),
                16: ("S", "E"),
                32: ("S", "W"),
            }
            edge1, edge2 = diagonal_edges.get(track_bits, ("N", "E"))
            detection_edge = detection_dir[0]
            entry_edge = edge2 if detection_edge == edge1 else edge1
        else:
            entry_edge = approach_dir[0] if approach_dir else None
            if not entry_edge:
                return None

        initial_exits = signal_track.get_exits(entry_edge, use_switch_state=False)
        if not initial_exits:
            return None

        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}
        opposite_edges = {"N": "S", "S": "N", "E": "W", "W": "E"}

        queue = deque()
        for exit_edge in initial_exits:
            queue.append((x, y, exit_edge, 0))

        visited = set()

        while queue:
            curr_x, curr_y, exit_edge, dist = queue.popleft()

            state = (curr_x, curr_y, exit_edge)
            if state in visited:
                continue
            visited.add(state)

            if len(visited) > 500:
                return None

            offset = edge_offsets.get(exit_edge)
            if not offset:
                continue
            dx, dy = offset
            next_x, next_y = curr_x + dx, curr_y + dy

            next_track = n.get_track(next_x, next_y)
            if not next_track:
                continue

            if next_track.block_section_id == target_block_id:
                return dist + 1

            entry_edge = opposite_edges.get(exit_edge)
            if not entry_edge:
                continue

            if entry_edge not in next_track.edges:
                continue

            all_exits = next_track.get_exits(entry_edge, use_switch_state=False)
            if not all_exits:
                continue

            for next_exit in all_exits:
                queue.append((next_x, next_y, next_exit, dist + 1))

        return None

    # ------------------------------------------------------------------
    # Reachable-block enumeration (used by UI dropdowns)
    # ------------------------------------------------------------------

    def reachable_blocks(self, signal_id: str) -> List[str]:
        """Block IDs reachable from `signal_id`, sorted nearest-first.

        A signal's own block is included only when the signal sits on the
        block's entry tile (so trains entering from the right direction
        hit the signal first).
        """
        n = self._n

        signal = n.signals.get(signal_id)
        if not signal:
            return []

        x, y = signal.position
        track = n.get_track(x, y)
        own_block = track.block_section_id if track else None

        reachable_with_distance = []
        for block_id in n.blocks:
            if block_id == own_block:
                if not self.is_signal_on_entry_tile(signal):
                    continue
                reachable_with_distance.append((0, block_id))
                continue

            distance = self.distance_to_block(signal, block_id)
            if distance is not None:
                reachable_with_distance.append((distance, block_id))

        reachable_with_distance.sort(key=lambda item: item[0])
        return [block_id for _, block_id in reachable_with_distance]

    def is_signal_on_entry_tile(self, signal) -> bool:
        """True when `signal` sits on the entry tile of its own block in the
        signal's detection direction."""
        n = self._n

        x, y = signal.position
        track = n.get_track(x, y)
        if not track or not track.block_section_id:
            return False

        block_id = track.block_section_id
        detection_dir = signal.detection_direction.upper()

        path = track.get_path_at_edge(detection_dir)
        if not path:
            return False

        if path[0] == detection_dir:
            approach_edge = path[1]
        else:
            approach_edge = path[0]

        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}
        offset = edge_offsets.get(approach_edge)
        if not offset:
            return False

        dx, dy = offset
        neighbor = n.get_track(x + dx, y + dy)

        detect_offset = edge_offsets.get(detection_dir)
        if not detect_offset:
            return False
        detect_dx, detect_dy = detect_offset
        detect_neighbor = n.get_track(x + detect_dx, y + detect_dy)
        if not detect_neighbor or detect_neighbor.block_section_id != block_id:
            return False

        return neighbor is None or neighbor.block_section_id != block_id

    # ------------------------------------------------------------------
    # Two-block lookahead helpers
    # ------------------------------------------------------------------

    def find_next_block_after(
        self, block_id: str, detection_direction: str
    ) -> Optional[str]:
        """Find the block trains enter after `block_id` in
        `detection_direction` (current switch states)."""
        n = self._n

        travel_dir = detection_direction.upper()

        edge_offsets = {"N": (0, 1), "S": (0, -1), "E": (1, 0), "W": (-1, 0)}

        block_tracks = [
            (pos, t) for pos, t in n.tracks.items() if t.block_section_id == block_id
        ]
        if not block_tracks:
            return None

        exit_edge_for_travel = {
            "NORTH": "N",
            "SOUTH": "S",
            "EAST": "E",
            "WEST": "W",
        }.get(travel_dir.upper())
        if not exit_edge_for_travel:
            return None

        edge_tracks = []
        for pos, track in block_tracks:
            if exit_edge_for_travel in track.edges:
                offset = edge_offsets.get(exit_edge_for_travel)
                if offset:
                    next_x, next_y = pos[0] + offset[0], pos[1] + offset[1]
                    next_track = n.get_track(next_x, next_y)
                    if not next_track or next_track.block_section_id != block_id:
                        edge_tracks.append((pos, track))

        if not edge_tracks:
            vector = get_direction_vector(travel_dir)
            if not vector:
                return None
            dx, dy = vector
            furthest_pos = max(
                block_tracks, key=lambda pt: pt[0][0] * dx + pt[0][1] * dy
            )[0]
            furthest_track = n.get_track(*furthest_pos)
            if furthest_track:
                edge_tracks = [(furthest_pos, furthest_track)]

        for start_pos, _start_track in edge_tracks:
            current_x, current_y = start_pos
            current_exit_edge = exit_edge_for_travel
            visited = set()

            for _ in range(50):
                pos_key = (current_x, current_y, current_exit_edge)
                if pos_key in visited:
                    break
                visited.add(pos_key)

                offset = edge_offsets.get(current_exit_edge)
                if not offset:
                    break
                dx, dy = offset
                next_x, next_y = current_x + dx, current_y + dy

                next_track = n.get_track(next_x, next_y)
                if not next_track:
                    break

                if (
                    next_track.block_section_id
                    and next_track.block_section_id != block_id
                ):
                    return next_track.block_section_id

                entry_edge = {"N": "S", "S": "N", "E": "W", "W": "E"}.get(
                    current_exit_edge
                )
                if not entry_edge:
                    break

                if entry_edge not in next_track.edges:
                    break

                exits = next_track.get_exits(entry_edge, use_switch_state=True)
                if not exits:
                    break

                current_exit_edge = (
                    exits[0] if isinstance(exits, list) else list(exits)[0]
                )
                current_x, current_y = next_x, next_y

        return None

