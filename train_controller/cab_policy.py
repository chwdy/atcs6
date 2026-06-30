"""CabFlipPolicy — decide which cab a TrainController's current path requires.

Extracted from train_controller/__init__.py with behavior unchanged. The policy
is constructed once per controller and reads controller state through the
back-reference ``self._c`` (read-only). The public entry is
:meth:`required_cab_for_path`; the rest are its helpers.
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

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

        Strategy (in priority order):
        1. **Track connectivity** — compare the head tile's entry edge with
           the edge toward the path's next tile.  Reliable on zigzag /
           diagonal staircase tracks where cardinal directions are misleading.
        2. **Direction fallback** — when the head tile isn't in the path (e.g.
           the path was prepended from a different tile), compare the
           ``facing_direction`` with the path's overall direction vector.

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

        # When the path contains a tunnel hop (a tile pair that is not
        # grid-adjacent), the cardinal-direction fallback below cannot be
        # trusted: it projects a straight-line displacement across the
        # tunnel, which has no physical meaning. Trust Approach 1's
        # "no flip" verdict in that case as long as the connectivity check
        # was conclusive (head tile in path with a next tile). For ordinary
        # grid-contiguous paths we still fall through to Approach 2 to
        # preserve the diagonal-staircase return-trip case, where the
        # per-tile direction is 90° from facing but the overall vector is
        # the true reversal. See design_new.md "Tunnels": tile-pair
        # adjacency can be non-grid.
        if (
            self._connectivity_check_was_conclusive(train)
            and self._path_has_tunnel_hop()
        ):
            return current_cab

        # --- Approach 2: direction fallback ---
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

    def _path_has_tunnel_hop(self) -> bool:
        """Return True when the current path's tile sequence contains a hop
        between two tiles that are not grid-adjacent — i.e. the hop crosses
        a tunnel edge. Used by ``required_cab_for_path`` to decide whether
        the cardinal-direction fallback is safe to consult: across a tunnel
        the dominant-vector heuristic projects a meaningless straight-line
        displacement, so we have to trust the connectivity-based verdict.
        """
        path_tiles = self._c._get_effective_path_tiles()
        if not path_tiles or len(path_tiles) < 2:
            return False
        for i in range(len(path_tiles) - 1):
            a = path_tiles[i]
            b = path_tiles[i + 1]
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            # Grid-adjacent tiles differ by exactly one cardinal unit. Any
            # other delta (zero, diagonal, or non-unit) signals a tunnel hop
            # for v1 (paths never repeat a tile in one direction of travel,
            # per design_new.md "v1 path geometry forbids tile re-visits in
            # a single direction of travel" — train_controller.py:1281).
            if (abs(dx), abs(dy)) != (1, 0) and (abs(dx), abs(dy)) != (0, 1):
                return True
        return False

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
