"""Finding the next block ahead of a train (design_v2.md "Claiming the next
block", The next block ahead).

The walk starts at the far end of a block, in a direction along that block's
tile order, and follows the rails across each switch it meets up to the first
block. It reads the track model only: tiles, blocks, and each switch's current
position. It knows nothing about trains.

- A switch met at its toe (its single end) leads on by the leg it is set to.
- A switch met from a leg leads on out of its toe when it is set to that leg.
  When it is set to the other leg, the walk ends: `reason` is
  `"switch_against"`.
- When the rails end before another block, the walk ends: `reason` is
  `"track_ends"`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Optional, Set, Tuple

if TYPE_CHECKING:  # pragma: no cover - typing only
    from network import RailwayNetwork

FORWARD = "forward"
REVERSE = "reverse"

TRACK_ENDS = "track_ends"
SWITCH_AGAINST = "switch_against"

_OPPOSITE_EDGE: Dict[str, str] = {"N": "S", "S": "N", "E": "W", "W": "E"}


def opposite_direction(direction: str) -> str:
    """`forward` for `reverse`, and `reverse` for `forward`."""
    return REVERSE if direction == FORWARD else FORWARD


@dataclass(frozen=True)
class WalkResult:
    """What one walk found.

    - `block_id`: the next block ahead, or None when there is none.
    - `direction`: the direction a train entering `block_id` this way runs it
      in, `forward` or `reverse` along that block's tile order. None when
      there is no next block.
    - `reason`: why there is no next block, `"track_ends"` or
      `"switch_against"`. None when there is one.
    - `switch_ids`: every switch the walk met, in order, including a switch
      that is set against the walk.
    """

    block_id: Optional[str]
    direction: Optional[str]
    reason: Optional[str]
    switch_ids: Tuple[str, ...]


def _block_end(network: "RailwayNetwork", block_id: str, direction: str):
    """The tile at the far end of `block_id` in `direction`, and the edge a
    train leaves that tile by. None when the block has no tile order."""
    block = network.blocks.get(block_id)
    entries = list(block.ordered_tiles) if block is not None else []
    if not entries:
        return None
    if direction == REVERSE:
        x, y, entry_edge = entries[0]
        tile = network.get_track(int(x), int(y))
        if tile is None or not entry_edge:
            return None
        return tile, str(entry_edge)
    x, y, entry_edge = entries[-1]
    tile = network.get_track(int(x), int(y))
    if tile is None or not entry_edge:
        return None
    exits = list(tile.get_exits(str(entry_edge), use_switch_state=False) or ())
    if len(exits) != 1:
        return None
    return tile, exits[0]


def _step(network: "RailwayNetwork", tile, exit_edge: str):
    """The tile entered by leaving `tile` through `exit_edge`, and the edge it
    is entered by. None when no rail continues there."""
    neighbour = network.get_neighbor(tile.x, tile.y, exit_edge)
    if neighbour is None:
        return None
    # Across a tunnel the far tile is entered through the tunnel's far edge;
    # between grid neighbours it is the opposite edge.
    tunnel_far = network.tunnels.get((tile.x, tile.y, exit_edge))
    if tunnel_far is not None and (tunnel_far[0], tunnel_far[1]) == (
        neighbour.x,
        neighbour.y,
    ):
        entry_edge = tunnel_far[2]
    else:
        entry_edge = _OPPOSITE_EDGE.get(exit_edge)
    if entry_edge is None or entry_edge not in (neighbour.edges or ()):
        return None
    return neighbour, entry_edge


def _entry_direction(network: "RailwayNetwork", block_id: str, tile, entry_edge: str) -> str:
    """The direction a train runs `block_id` in when it enters at `tile`
    through `entry_edge`."""
    block = network.blocks.get(block_id)
    entries = list(block.ordered_tiles) if block is not None else []
    if not entries:
        return FORWARD
    first = entries[0]
    if (int(first[0]), int(first[1])) == (tile.x, tile.y) and str(first[2]) == entry_edge:
        return FORWARD
    return REVERSE


def next_block_ahead(
    network: "RailwayNetwork",
    block_id: str,
    direction: str,
    switch_positions: Optional[Dict[str, int]] = None,
) -> WalkResult:
    """Walk from the far end of `block_id` in `direction` to the next block.

    `switch_positions` maps a switch id to a position to use in place of that
    switch's current one, for asking "where would this lead if that switch
    were thrown". A switch it does not name is read as currently set.
    """
    start = _block_end(network, block_id, direction)
    if start is None:
        return WalkResult(None, None, TRACK_ENDS, ())
    tile, exit_edge = start
    met: list = []
    visited: Set[Tuple[int, int, str]] = set()
    while True:
        key = (tile.x, tile.y, exit_edge)
        if key in visited:
            # Rails that loop round without ever reaching a block.
            return WalkResult(None, None, TRACK_ENDS, tuple(met))
        visited.add(key)

        step = _step(network, tile, exit_edge)
        if step is None:
            return WalkResult(None, None, TRACK_ENDS, tuple(met))
        neighbour, entry_edge = step

        reached = getattr(neighbour, "block_id", None)
        if reached:
            return WalkResult(
                reached,
                _entry_direction(network, reached, neighbour, entry_edge),
                None,
                tuple(met),
            )

        switch_id = getattr(neighbour, "switch_id", None)
        if switch_id and neighbour.is_switch:
            if switch_id not in met:
                met.append(switch_id)
            override = (switch_positions or {}).get(switch_id)
            exits = list(
                neighbour.get_exits(
                    entry_edge, use_switch_state=True, switch_state_override=override
                )
                or ()
            )
            if not exits:
                return WalkResult(None, None, SWITCH_AGAINST, tuple(met))
        else:
            exits = list(neighbour.get_exits(entry_edge, use_switch_state=False) or ())
            if not exits:
                return WalkResult(None, None, TRACK_ENDS, tuple(met))
        tile, exit_edge = neighbour, exits[0]


def switches_next_to(network: "RailwayNetwork", block_id: str) -> Set[str]:
    """Every switch that can be reached from either end of `block_id` across
    switches with no other block between, whatever way the switches are set
    (design_v2.md "Switches", A held switch is not thrown)."""
    found: Set[str] = set()
    for direction in (FORWARD, REVERSE):
        start = _block_end(network, block_id, direction)
        if start is None:
            continue
        frontier = [start]
        visited: Set[Tuple[int, int, str]] = set()
        while frontier:
            tile, exit_edge = frontier.pop()
            key = (tile.x, tile.y, exit_edge)
            if key in visited:
                continue
            visited.add(key)
            step = _step(network, tile, exit_edge)
            if step is None:
                continue
            neighbour, entry_edge = step
            if getattr(neighbour, "block_id", None):
                continue
            switch_id = getattr(neighbour, "switch_id", None)
            if switch_id and neighbour.is_switch:
                found.add(switch_id)
            for onward in neighbour.get_exits(entry_edge, use_switch_state=False) or ():
                frontier.append((neighbour, onward))
    return found


def neighbour_blocks(network: "RailwayNetwork", block_id: str) -> Dict[str, str]:
    """The blocks next to `block_id` across switches as currently set, with no
    other block between. Maps each to the direction a train coming from
    `block_id` would run it in."""
    out: Dict[str, str] = {}
    for direction in (FORWARD, REVERSE):
        walk = next_block_ahead(network, block_id, direction)
        if walk.block_id and walk.block_id != block_id and walk.direction:
            out.setdefault(walk.block_id, walk.direction)
    return out
