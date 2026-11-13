"""Scene baked from demo7_2.json by json_to_scene.py.

Auto-generated — do not hand-edit; re-run json_to_scene.py against the source
layout instead. Layout only: 43 tracks, 2 switches,
0 platforms (0 station groups),
5 blocks (0 auto-created platform blocks),
0 block groups, 0 tunnels,
0 signals.
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


STATIONS = [
]


PLATFORMS = [
]


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


SIGNALS = [
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


def _add_stations(network: RailwayNetwork) -> None:
    """Build each platform from its anchor tile + extensions, then group them.

    add_station auto-creates one ``{platform_id}_BLOCK`` per platform, so those
    blocks are not listed in BLOCKS.
    """
    for (group_id, group_name, platforms) in STATIONS:
        platform_ids = []
        for (platform_id, name, first_tile, extra_tiles, side, length_cm, top_speed_kmh) in platforms:
            platform = network.add_station(
                x=first_tile[0],
                y=first_tile[1],
                station_id=platform_id,
                name=name,
                length_cm=length_cm,
                platform_side=side,
                top_speed_kmh=top_speed_kmh,
            )
            for extra in extra_tiles:
                network.extend_station(platform.platform_id, [extra])
            platform_ids.append(platform.platform_id)
        network.create_station_group(
            station_id=group_id,
            name=group_name,
            platform_ids=platform_ids,
        )


def _add_platforms(network: RailwayNetwork) -> None:
    """Build platforms that belong to no station group.

    Same anchor-tile + extensions construction as ``_add_stations`` but with no
    ``create_station_group`` call. Each platform still auto-creates its
    ``{platform_id}_BLOCK``.
    """
    for (platform_id, name, first_tile, extra_tiles, side, length_cm, top_speed_kmh, block_name, segment_name) in PLATFORMS:
        platform = network.add_station(
            x=first_tile[0],
            y=first_tile[1],
            station_id=platform_id,
            name=name,
            length_cm=length_cm,
            platform_side=side,
            top_speed_kmh=top_speed_kmh,
        )
        for extra in extra_tiles:
            network.extend_station(platform.platform_id, [extra])
        # add_station derives both names from the CURRENT platform name; a
        # platform renamed after creation keeps the older name on its block and
        # segment, so restore each from the layout rather than leaving the
        # freshly derived one.
        station_block = network.blocks.get(f"{platform.platform_id}_BLOCK")
        if station_block is not None:
            station_block.name = block_name
            if station_block.segments:
                station_block.segments[0].name = segment_name


def _add_blocks(network: RailwayNetwork) -> None:
    """Create the non-platform blocks from their baked tile lists.

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


def _add_signals(network: RailwayNetwork) -> None:
    """Rebuild signals via the canonical Signal.from_dict loader."""
    if not SIGNALS:
        return
    from railway_signal import Signal
    for sig in SIGNALS:
        signal = Signal.from_dict(sig)
        network.signals[signal.signal_id] = signal
        track = network.get_track(*signal.position)
        if track is not None and signal.signal_id not in track.signal_ids:
            track.signal_ids.append(signal.signal_id)


def _add_block_groups(network: RailwayNetwork) -> None:
    """Fold each baked grouping into one block owning its member segments.

    Grouping is an operator decision about shared power wiring, not something
    re-derivable from geometry, so it is baked. Each member segment already
    exists as its own one-segment block from ``_add_blocks``; grouping moves
    those segments into one block and drops the emptied ones. Runs after every
    block exists (a grouping may name a platform block).

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
    _add_stations(network)
    _add_platforms(network)
    _add_blocks(network)
    _add_block_groups(network)
    _add_tunnels(network)
    _add_signals(network)
    return network
