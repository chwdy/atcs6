"""Scene baked from demo7_2.json by json_to_scene.py.

Auto-generated — do not hand-edit; re-run json_to_scene.py against the source
layout instead. Layout only: 43 tracks, 2 switches,
5 blocks,
0 block groups, 0 tunnels.
"""

import logging

from network import RailwayNetwork
from track import (
    Tile,
    TRACK_NS,
    TRACK_EW,
    TRACK_NE,
    TRACK_NW,
    TRACK_SE,
    TRACK_SW,
)
from switch import Switch


SCENE_ID = 'demo7_2'


TILES = [
    (3, 5, TRACK_NE),
    (3, 6, TRACK_NS),
    (3, 7, TRACK_NS),
    (3, 8, TRACK_SE),
    (4, 4, TRACK_NE),
    (4, 5, TRACK_SW),
    (4, 8, TRACK_NW),
    (4, 9, TRACK_SE),
    (5, 4, TRACK_EW),
    (5, 9, TRACK_NW),
    (5, 10, TRACK_SE),
    (6, 4, TRACK_EW),
    (6, 10, TRACK_NW),
    (6, 11, TRACK_SE),
    (7, 4, TRACK_NW),
    (7, 5, TRACK_NS | TRACK_SE),  # SW_7_5
    (7, 6, TRACK_NS),
    (7, 7, TRACK_SE),
    (7, 11, TRACK_NW),
    (7, 12, TRACK_SE),
    (8, 5, TRACK_NW),
    (8, 6, TRACK_SE),
    (8, 7, TRACK_NW),
    (8, 8, TRACK_SE),
    (8, 12, TRACK_NW),
    (8, 13, TRACK_SE),
    (9, 6, TRACK_NW),
    (9, 7, TRACK_SE),
    (9, 8, TRACK_NW),
    (9, 9, TRACK_SE),
    (9, 13, TRACK_EW),
    (10, 7, TRACK_NW),
    (10, 8, TRACK_SE),
    (10, 9, TRACK_EW),
    (10, 13, TRACK_EW),
    (11, 8, TRACK_NW),
    (11, 9, TRACK_EW | TRACK_SE),  # SW_11_9
    (11, 12, TRACK_NE),
    (11, 13, TRACK_SW),
    (12, 9, TRACK_NW),
    (12, 10, TRACK_NS),
    (12, 11, TRACK_NS),
    (12, 12, TRACK_SW),
]


SWITCHES = [
    ('SW_7_5', 7, 5),
    ('SW_11_9', 11, 9),
]


# Settings a switch carries besides its place (design_v2.md "Switches",
# A switch with an unavailable motor).
SWITCH_SETTINGS = {
    'SW_7_5': {'motor_unavailable': True, 'allow_exit_from_either_leg': True},
}


BLOCKS = [
    ('BLK_4F4CF0', 'SEG_4F4CF0', '', [(9, 13), (10, 13), (11, 12), (11, 13), (12, 9), (12, 10), (12, 11), (12, 12)], 99.6, None, 'Right'),
    ('BLK_4F5CE3', 'SEG_4F5CE3', '', [(3, 8), (4, 8), (4, 9), (5, 9), (5, 10), (6, 10), (6, 11), (7, 11), (7, 12), (8, 12), (8, 13)], 84.0, None, 'Up'),
    ('BLK_4F6CD7', 'SEG_4F6CD7', '', [(7, 6), (7, 7), (8, 7), (8, 8), (9, 8), (9, 9), (10, 9)], 84.0, None, 'Main'),
    ('BLK_4F7A2F', 'SEG_4F7A2F', '', [(8, 5), (8, 6), (9, 6), (9, 7), (10, 7), (10, 8), (11, 8)], 84.0, None, 'Sideline'),
    ('BLK_C37589A5', 'SEG_4F110A', '', [(3, 5), (3, 6), (3, 7), (4, 4), (4, 5), (5, 4), (6, 4), (7, 4)], 99.6, None, 'Left'),
]


BLOCK_GROUPS = [
]


TUNNELS = [
]


def _add_tracks(network: RailwayNetwork) -> None:
    """Place every baked track tile (dead-end ports are re-derived from geometry)."""
    for (x, y, bits) in TILES:
        network.add_track(Tile(x, y, track_bits=bits))


def _add_switches(network: RailwayNetwork) -> None:
    """Create the Switch entity for each switch tile and link the tile to it."""
    for (switch_id, x, y) in SWITCHES:
        switch = Switch(switch_id=switch_id, x=x, y=y)
        for name, value in SWITCH_SETTINGS.get(switch_id, {}).items():
            setattr(switch, name, value)
        network.add_switch(switch)
        track = network.get_track(x, y)
        if track is not None:
            track.switch_id = switch_id


def _add_blocks(network: RailwayNetwork) -> None:
    """Create the blocks from their baked tile lists.

    Each row states both ids: a block and its member segment are separate
    records that never share one (see ``entity_ids``). ``block_id`` is ``None``
    for a row whose block is an interim one dissolved by the ``BLOCK_GROUPS``
    fold below, and the default derivation supplies it.

    ``name`` names the segment the call creates; ``block_name`` names the block
    that owns it, and is empty unless the operator named that block separately.
    """
    for (block_id, segment_id, name, positions, length_cm, speed_limit_kmh,
         block_name) in BLOCKS:
        block = network.create_segment_with_tracks(
            block_id=block_id,
            segment_id=segment_id,
            positions=positions,
            name=name,
            length_cm=length_cm,
            speed_limit_kmh=speed_limit_kmh,
        )
        if block_name:
            block.name = block_name


def _add_tunnels(network: RailwayNetwork) -> None:
    """Join the baked tunnel endpoint pairs."""
    for (a, b) in TUNNELS:
        network.add_tunnel(a, b)


def _add_block_groups(network: RailwayNetwork) -> None:
    """Fold each baked grouping into one block owning its member segments.

    Grouping is an operator decision about shared power wiring, not something
    re-derivable from geometry, so it is baked. Each member segment already
    exists as its own one-segment block from ``_add_blocks``; grouping moves
    those segments into one block and drops the emptied ones. Runs after every
    block exists.

    A grouping whose members do not chain into one clean run is folded anyway,
    with a warning — the same reading `RailwayNetwork._fold_grouped_blocks`
    takes on load: dropping it would turn one single-train feed back into
    several independent feeds, which is the less safe reading of the layout.
    """
    if not BLOCK_GROUPS:
        return
    grouping = network._get_block_grouping_manager()
    for (block_id, name, segment_ids) in BLOCK_GROUPS:
        try:
            grouping.group_into_block(
                block_id=block_id, segment_ids=segment_ids, name=name
            )
        except ValueError as exc:
            logging.getLogger(__name__).warning(
                "Baked grouping %s is not one connected switch-free run (%s); "
                "folding it into a single block anyway to preserve its "
                "one-train power domain.",
                block_id,
                exc,
            )
            ordered, _ = grouping.plan_run(segment_ids)
            grouping.create_block(
                block_id=block_id,
                segment_ids=ordered,
                name=name,
                validate=False,
            )


def create_comprehensive_demo() -> RailwayNetwork:
    """Build the baked network from scratch and return it."""
    network = RailwayNetwork()
    _add_tracks(network)
    _add_switches(network)
    _add_blocks(network)
    _add_block_groups(network)
    _add_tunnels(network)
    return network

