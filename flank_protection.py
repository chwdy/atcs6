"""FlankProtectionCalculator — flank-protection graph walk pulled out of
`RailwayNetwork`.

Per design_new.md:683-687, 990: for each switch, find nearby switches whose
active branch can point back at it (a head-on flank approach) and record the
`blocking_state` acquisition must later lock away from. This module owns that
self-contained graph walk; it reads the network graph through the back-reference
`self._n` and writes each `Switch.flank_protection`. Only
`RailwayNetwork.recompute_all_flank_protection` keeps a thin forwarder (it has
external callers); the rest of the cluster is internal and moves entirely.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional, Set, Tuple, TYPE_CHECKING

from constants import FLANK_PROTECTION_DISTANCE_CM

if TYPE_CHECKING:
    from network import RailwayNetwork
    from switch import Switch


class FlankProtectionCalculator:
    """Computes per-switch flank protection for one `RailwayNetwork`."""

    def __init__(self, network: "RailwayNetwork") -> None:
        self._n = network

    def _switch_at_tile(self, x: int, y: int) -> Optional["Switch"]:
        """Resolve the Switch entity anchored at a tile via the tile's
        `switch_id` back-reference, or None when the tile is not a switch."""
        track = self._n.get_track(x, y)
        if track is None or track.switch_id is None:
            return None
        return self._n.switches.get(track.switch_id)

    def compute_flank_protection_for(
        self, switch: "Switch"
    ) -> List[Tuple[str, int]]:
        """Compute the `(neighbour_switch_id, blocking_state)` flank list for
        one switch (design_new.md:683-687).

        Searches the network graph outward from the switch tile within
        `FLANK_PROTECTION_DISTANCE_CM` and, for every neighbour switch found
        within that radius, records the neighbour state whose active branch
        points back toward this switch — the `blocking_state` that would
        create a head-on flank approach. Acquisition later locks the
        neighbour to the safe state `1 - blocking_state` (design_new.md:990).

        The walk accumulates per-tile length via `_get_tile_length_cm`.
        Because switch tiles are ~0 cm and ordinary block tiles are
        `DEFAULT_TILE_LENGTH_CM` (50 cm), the small 5 cm budget captures
        directly track-connected neighbour switches (crossover throats,
        ladders) without crossing a full block — the doc's "nearby switches
        that require mutual locking".
        """
        origin = (switch.x, switch.y)
        budget = FLANK_PROTECTION_DISTANCE_CM
        # BFS over track connectivity, tracking accumulated cm at each tile.
        # Seed neighbours of the origin at distance 0 (we step *out* of the
        # zero-or-near-zero-length switch tile into its connected neighbours).
        visited: Set[Tuple[int, int]] = {origin}
        found: Dict[str, int] = {}
        queue: deque = deque()
        origin_track = self._n.get_track(*origin)
        if origin_track is None:
            return []
        # Sorted edge iteration: the first pop of a tile fixes its walk
        # distance for the budget prune, so visit order must not depend on
        # str-hash set order.
        for edge in sorted(origin_track.edges):
            neighbor_coords = self._n._get_neighbor_coords(origin[0], origin[1], edge)
            # `get_neighbor` also resolves tunnels; map back to coords.
            neighbor = self._n.get_neighbor(origin[0], origin[1], edge)
            if neighbor is None:
                continue
            ncoords = (neighbor.x, neighbor.y)
            if ncoords in visited:
                continue
            queue.append((ncoords, 0.0))

        while queue:
            (cx, cy), dist_so_far = queue.popleft()
            if (cx, cy) in visited:
                continue
            visited.add((cx, cy))

            neighbour_switch = self._switch_at_tile(cx, cy)
            if neighbour_switch is not None and neighbour_switch.switch_id != switch.switch_id:
                blocking_state = self._neighbour_blocking_state(
                    neighbour_switch, toward=origin
                )
                if blocking_state is not None:
                    # Keep the most-constraining (first found) entry per
                    # neighbour; a neighbour appears once in the list.
                    found.setdefault(neighbour_switch.switch_id, blocking_state)
                # A switch is a hazard boundary: do not search past it.
                continue

            # Extend the walk to this tile's neighbours while within budget.
            tile_len = self._n._get_tile_length_cm(cx, cy)
            next_dist = dist_so_far + tile_len
            if next_dist > budget:
                continue
            tile = self._n.get_track(cx, cy)
            if tile is None:
                continue
            for edge in sorted(tile.edges):
                nb = self._n.get_neighbor(cx, cy, edge)
                if nb is None:
                    continue
                ncoords = (nb.x, nb.y)
                if ncoords not in visited:
                    queue.append((ncoords, next_dist))

        return sorted(found.items())

    def _neighbour_blocking_state(
        self, neighbour: "Switch", *, toward: Tuple[int, int]
    ) -> Optional[int]:
        """Return the neighbour switch state (0/1) whose active branch points
        back toward the `toward` tile through the connecting edge — the
        `blocking_state` for flank protection (design_new.md:990).

        Returns None when the neighbour is not track-connected toward the
        origin, or when the connecting edge is active in BOTH states. The
        latter is the in-series-on-a-through-line case (e.g. a depot-throat
        pair where the connecting edge carries the main route in either
        position): the neighbour always points at the origin, so no setting
        of it provides flank protection — there is no safe state to lock — and
        recording an arbitrary one would be both meaningless and
        iteration-order-dependent. Flank protection is meaningful only when the
        neighbour points at the origin in exactly one state.
        """
        ncoords = (neighbour.x, neighbour.y)
        ntrack = self._n.get_track(*ncoords)
        if ntrack is None:
            return None
        # Which edge of the neighbour faces the walk that reached it. We look
        # for the connecting edge between the neighbour and any already-walked
        # tile in the direction of the origin; the immediate connection is the
        # edge whose neighbour tile is closer to `toward`. Use the connecting
        # edge to the tile we came from by scanning the neighbour's edges for
        # one whose far tile is on the path back to origin.
        facing_edge = self._edge_from_neighbour_toward(neighbour, toward)
        if facing_edge is None:
            return None
        # Collect the states in which an active branch is connected through the
        # facing edge — i.e. a train on the neighbour set to that state could
        # run out through that edge into the protected switch.
        # enumerate_traversals(entry) yields (exit_edge, required_state) per
        # legal traversal, so the facing edge is "active in state S" when it
        # appears as either the entry or the exit of a traversal legal in S.
        states_pointing: Set[int] = set()
        for entry in ntrack.edges:
            for exit_edge, required_state in ntrack.enumerate_traversals(entry):
                if required_state is None:
                    continue
                if entry == facing_edge or exit_edge == facing_edge:
                    states_pointing.add(int(required_state))
        # Exactly one state points at the origin -> that is the blocking state
        # (its binary complement is the safe state to lock). Both states (no
        # safe state) or neither (not a hazard) -> no flank relationship.
        if len(states_pointing) == 1:
            return next(iter(states_pointing))
        return None

    def _edge_from_neighbour_toward(
        self, neighbour: "Switch", toward: Tuple[int, int]
    ) -> Optional[str]:
        """The edge on the neighbour switch tile that connects, in one hop,
        toward the origin tile. Resolves the direct connection edge when the
        neighbour is adjacent to `toward`; otherwise the edge whose far tile
        is the nearest step back toward `toward` by Manhattan heuristic."""
        ncoords = (neighbour.x, neighbour.y)
        # Direct connection (adjacent or tunnel) to the origin.
        direct = self._n.get_connection_edges(ncoords, toward)
        if direct is not None:
            return direct[0]
        # Otherwise pick the connected edge whose far tile reduces Manhattan
        # distance to the origin the most (the step back along the walk).
        ntrack = self._n.get_track(*ncoords)
        if ntrack is None:
            return None
        best_edge: Optional[str] = None
        best_dist: Optional[int] = None
        for edge in sorted(ntrack.edges):
            nb = self._n.get_neighbor(ncoords[0], ncoords[1], edge)
            if nb is None:
                continue
            d = abs(nb.x - toward[0]) + abs(nb.y - toward[1])
            if best_dist is None or d < best_dist:
                best_dist = d
                best_edge = edge
        return best_edge

    def recompute_all_flank_protection(self) -> None:
        """Recompute `flank_protection` for every switch in the network.

        Called after any topology change that can alter switch adjacency
        (add/remove/update switch, bulk `from_dict` load). Cheap relative to
        the per-tick loop and run only on between-tick topology edits, which
        v1 forbids during simulation anyway (editor and runtime are binary
        modes per session)."""
        for switch in self._n.switches.values():
            switch.flank_protection = self.compute_flank_protection_for(switch)
