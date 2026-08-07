"""CabFlipPolicy — decide which cab a TrainController's current path requires.

Extracted from train_controller/__init__.py with behavior unchanged. The policy
is constructed once per controller and reads controller state through the
back-reference ``self._c`` (read-only). The public entry is
:meth:`required_cab_for_path`; the rest are its helpers.
"""
from __future__ import annotations

from typing import Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from train_controller import TrainController


class CabFlipPolicy:
    """Resolves the required cab (``"A"``/``"B"``) for a controller's path."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

    def _path_needs_cab_flip(self) -> bool:
        """Check if the current path requires the train to reverse direction.

        Uses track connectivity: compares the head tile's entry edge with
        the edge leading to the path's next tile.  If they match, the path
        goes backward through the head (back the way it came), so the cab
        must flip.

        This is more reliable than comparing cardinal directions, which can
        be misleading on zigzag / diagonal staircase tracks where the
        per-tile direction differs from the overall travel direction.

        Tunnel-aware: the next tile in the path may be a tunnel-far tile
        rather than a grid neighbour, in which case the raw (dx, dy) is not
        a unit vector. We delegate edge resolution to
        ``RailwayNetwork.get_connection_edges(...)``, which already handles
        both grid adjacency and tunnel adjacency per design_new.md
        "Pathfinding home" / "Tunnels".

        Returns:
            True if cab should be flipped, False otherwise or if the check
            cannot be performed.
        """
        train = self._c._get_train()
        if not train:
            return False

        path_tiles = self._c._get_effective_path_tiles()
        if not path_tiles or len(path_tiles) < 2:
            return False

        head_tile = self._c._get_head_tile()
        head_entry_edge = self._c._get_head_entry_edge()
        if not head_tile or not head_entry_edge:
            return False

        # Find head tile in path
        head_idx = -1
        for i, entry in enumerate(path_tiles):
            if (entry[0], entry[1]) == head_tile:
                head_idx = i
                break

        if head_idx < 0 or head_idx + 1 >= len(path_tiles):
            return False

        # Which edge of the head tile leads to the path's next tile?
        # ``get_connection_edges`` resolves both ordinary grid adjacency and
        # tunnel adjacency (where ``next_entry`` is the tunnel-far tile, not
        # a grid neighbour). Falling back to raw ``(dx, dy)`` math would
        # silently return ``None`` on every tunneled hop, which made
        # ``_path_needs_cab_flip()`` return False regardless of the actual
        # head-edge geometry and let ``required_cab_for_path()`` fall
        # through to the cardinal-direction fallback (which then
        # mis-projects across the tunnel).
        next_entry = path_tiles[head_idx + 1]
        network = self._c.network
        needed_edge: Optional[str] = None
        if network is not None:
            edges = network.get_connection_edges(
                (head_tile[0], head_tile[1]),
                (next_entry[0], next_entry[1]),
            )
            if edges is not None:
                needed_edge = edges[0]

        if not needed_edge:
            return False

        # Entry edge = the edge the head entered from (backward direction).
        # If the path's next tile is through the same edge, the path goes
        # backward → the cab must flip.
        return needed_edge == head_entry_edge

    def required_cab_for_path(self) -> Optional[str]:
        """Determine the required cab for the current path.

        The head is "the physical end of the current span on the authority side"
        (design_new.md:150), so the required cab is the one that puts the head
        there. Both tests below answer that question off real geometry; only the
        last resort guesses from cardinal directions.

        Strategy (in priority order):
        1. **Track connectivity** — compare the head tile's entry edge with
           the edge toward the path's next tile. Exact, and reliable on curves,
           zigzag / diagonal staircases and tunnel hops, where cardinal
           directions are misleading. When it can answer at all it is the whole
           answer. It can only answer when the head tile is itself enumerated by
           the path, which is *not* the common case at path-creation time — see
           below.
        2. **Authority side** — the rule design_new.md:2213 states: compare
           ``TrainSpan.end_a`` and ``TrainSpan.end_b`` against the ordered
           ``resource_sequence``, which runs tail-to-authority, and take the end
           that sits later in it. This is what answers for a train whose span
           covers more than one resource, which is exactly where test 1 goes
           quiet.
        3. **Direction fallback** — the dead heat, when both ends resolve to the
           same resource so neither test above can separate them. Only here do
           cardinal directions get a vote.

        Returns:
            ``"A"`` or ``"B"``, or ``None`` if the check cannot be performed.
        """
        train = self._c._get_train()
        if not train:
            return None

        current_cab = self._c.commands.cab or "A"

        # --- Approach 1: track connectivity (preferred) ---
        flip = self._path_needs_cab_flip()
        if flip:
            return "B" if current_cab == "A" else "A"

        # A conclusive connectivity verdict is exact geometry, so it is the
        # whole answer: the head's entry edge against the edge toward the next
        # path tile decides whether the path leaves back the way the head came
        # in, which is what a reversal *is*. Nothing the cardinal-direction
        # fallbacks add can improve on that, and on a curved head tile they
        # actively contradict it.
        #
        # A curve is the case that bites. `facing` is the heading the head
        # *entered* its tile on, and through a curve the rail bends away from
        # it, so the exit heading the path takes is 90° off — the same trap
        # `get_span_end_exits` documents for span-end seeds. Neither the
        # equal nor the opposite test below then matches, and the decision
        # falls to the gross start->end vector, which on a loop points back at
        # the train and flips the cab. The flip is not a harmless mislabel:
        # it moves the head to the other end of the consist, that end's tile
        # is not on the path the dispatcher just built from the old head, and
        # the very next progress report reads out_of_sequence and cancels the
        # path — leaving the train with no authority, an ATC limit of 0, and a
        # dead rail under it.
        #
        # A tunnel hop is the same argument by a different route: the fallback
        # projects a straight-line displacement across a non-grid tile pair,
        # which has no physical meaning (design_new.md "Tunnels").
        if self._connectivity_check_was_conclusive(train):
            return current_cab

        # --- Approach 2: authority side (design_new.md:2213) ---
        authority_side_cab = self._authority_side_cab()
        if authority_side_cab is not None:
            return authority_side_cab

        # --- Approach 3: direction fallback ---
        # Compare the path's travel direction with the train's facing
        # direction. This also resolves the "90 degrees at the head but overall
        # reverse" diagonal case after the direct backtracking check above.
        facing = self._c._facing_direction()
        if facing is None:
            return (
                current_cab if self._connectivity_check_was_conclusive(train) else None
            )

        path_direction = self._c._get_path_direction_at_head(train)
        if path_direction is not None:
            if path_direction == facing:
                return current_cab
            if path_direction == facing.opposite():
                return "B" if current_cab == "A" else "A"

        # 90 degrees or path direction unknown — the overall start->end vector
        # heuristic. This only makes sense at the START of a path: it picks the
        # initial cab for a fresh return trip whose first move is 90° from
        # facing (the diagonal-staircase reversal). Once the train is already
        # moving ALONG the path (head past the first tile) the cab was fixed at
        # the start, and v1 paths are monotonic forward (a path never revisits a
        # resource), so a 90° turn is just a bend, not a reversal. Applying the
        # gross-displacement heuristic mid-path would flip the cab at every bend
        # of a winding approach (e.g. the depot lead climbing north then the
        # ladder doubling back), spuriously tripping WRONG_DIRECTION_CAB. A real
        # mid-path wrong cab (operator flipped to the wrong end) reverses the
        # per-tile direction at the head and is already caught by the opposite
        # check above, which runs regardless of position. So restrict the gross
        # vector flip to the path-start (head-at-first-tile) case.
        path_tiles = self._c._get_effective_path_tiles()
        if path_tiles and len(path_tiles) >= 2:
            head_tile = self._c._get_head_tile()
            head_index = -1
            if head_tile is not None:
                for i, entry in enumerate(path_tiles):
                    if (entry[0], entry[1]) == head_tile:
                        head_index = i
                        break
            # head_index <= 0 means the head is at the path's first tile (fresh
            # path) or could not be located (path prepended from elsewhere —
            # keep the original conservative behaviour). head_index > 0 means
            # the train is mid-path, so the bend is not a reversal.
            if head_index <= 0:
                overall = self._c._dominant_direction_from_tiles(
                    path_tiles[0], path_tiles[-1]
                )
                if overall is not None and overall == facing.opposite():
                    return "B" if current_cab == "A" else "A"

        return current_cab

    def _authority_side_cab(self) -> Optional[str]:
        """The cab that puts the head on the span's authority-side end, or None
        when the two ends cannot be separated this way.

        design_new.md:2213 defines the comparison: the ordered
        ``resource_sequence`` is the covered span plus forward authority in
        **tail-to-authority order**, so of the two physical span ends, the one
        whose resource sits *later* in that sequence is the authority side —
        which is what ``head`` means (design_new.md:150).

        This is the test that answers at path-creation time. ``create_path``
        seeds the start block's entry with the tile the train will *leave*
        through, not the tile it is standing on, so the head tile is usually
        absent from the path and the exact connectivity check above stays
        silent. Before this existed the decision fell all the way through to the
        gross start->end displacement vector, which reads a curve or a
        doubling-back destination as a reversal and flips the cab — dropping the
        head onto the other end of the consist, off the path, where the next
        progress report cancels the authority and the rail goes dead.

        ``None`` means "no opinion", not "no flip": both ends in the same
        resource (a short consist inside one block, where tail-to-authority
        order has nothing to order), or neither end resolvable. The caller falls
        through to the cardinal-direction fallback, which is the documented
        dead-heat case.
        """
        plan = self._c.path_projection.received_path_plan
        span = getattr(self._c.physics_sync, "current_span", None)
        if plan is None or span is None or self._c.network is None:
            return None

        # First occurrence wins: a lapping path can carry one block at two
        # indices (the repeated-resource rule), and the earlier one is always
        # the covered end. Reading both span ends the same way keeps the
        # comparison between them honest whichever half of the lap they are in.
        first_index: Dict[str, int] = {}
        for entry in plan.resource_sequence:
            if entry.resource_id not in first_index:
                first_index[entry.resource_id] = entry.sequence_index

        def index_of_end(locator) -> Optional[int]:
            tile = getattr(locator, "tile", None)
            if tile is None:
                return None
            track = self._c.network.get_track(int(tile[0]), int(tile[1]))
            if track is None:
                return None
            # A span end sitting on a switch tile carries no block id; the
            # switch is a resource in its own right, so try it too.
            for resource_id in (track.block_id, getattr(track, "switch_id", None)):
                if resource_id and resource_id in first_index:
                    return first_index[resource_id]
            return None

        index_a = index_of_end(span.end_a)
        index_b = index_of_end(span.end_b)

        if index_a is None and index_b is None:
            return None
        # An end whose resource the path never mentions is behind the seed, so
        # it is the tail: the other end is the authority side.
        if index_a is None:
            return "B"
        if index_b is None:
            return "A"
        if index_a == index_b:
            return None
        return "A" if index_a > index_b else "B"

    def _connectivity_check_was_conclusive(self, train) -> bool:
        """Return True if _path_needs_cab_flip had enough data to decide.

        Specifically: the head tile was found in the path and there was a
        next tile to compare against.  If the head wasn't found, the
        connectivity check was inconclusive and the caller should fall back.
        """
        if not train:
            return False
        path_tiles = self._c._get_effective_path_tiles()
        if not path_tiles or len(path_tiles) < 2:
            return False
        head_tile = self._c._get_head_tile()
        if not head_tile:
            return False
        for i, entry in enumerate(path_tiles):
            if (entry[0], entry[1]) == head_tile:
                return i + 1 < len(path_tiles)  # has a next tile
        return False  # head not in path
