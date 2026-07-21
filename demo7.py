"""Scene baked from demo7.json by json_to_scene.py.

Auto-generated — do not hand-edit; re-run json_to_scene.py against the source
layout instead. Layout only: 28 tracks, 0 switches,
0 platforms (0 station groups),
1 block over 4 segments (0 auto-created platform blocks),
0 tunnels, 0 signals. Trains/routes are not
part of the layout JSON, so setup_demo_train() is a stub.
"""

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


SCENE_ID = 'demo7'


TILES = [
    (3, 6, TRACK_NE),
    (3, 7, TRACK_NS),
    (3, 8, TRACK_NS),
    (3, 9, TRACK_SE),
    (4, 5, TRACK_NE),
    (4, 6, TRACK_SW),
    (4, 9, TRACK_NW),
    (4, 10, TRACK_SE),
    (5, 5, TRACK_EW),
    (5, 10, TRACK_NW),
    (5, 11, TRACK_SE),
    (6, 5, TRACK_EW),
    (6, 11, TRACK_NW),
    (6, 12, TRACK_SE),
    (7, 5, TRACK_NW),
    (7, 6, TRACK_SE),
    (7, 12, TRACK_EW),
    (8, 6, TRACK_NW),
    (8, 7, TRACK_SE),
    (8, 12, TRACK_EW),
    (9, 7, TRACK_NW),
    (9, 8, TRACK_SE),
    (9, 11, TRACK_NE),
    (9, 12, TRACK_SW),
    (10, 8, TRACK_NW),
    (10, 9, TRACK_NS),
    (10, 10, TRACK_NS),
    (10, 11, TRACK_SW),
]


SWITCHES = [
]


STATIONS = [
]


PLATFORMS = [
]


BLOCKS = [
    ('BLK_AA62D2', '', [(3, 8), (3, 7), (3, 6), (4, 6), (4, 5), (5, 5), (6, 5)], 0.0, None),
    ('BLK_AA6FB5', '', [(6, 12), (6, 11), (5, 11), (5, 10), (4, 10), (4, 9), (3, 9)], 0.0, None),
    ('BLK_AA7B7E', '', [(10, 9), (10, 10), (10, 11), (9, 11), (9, 12), (8, 12), (7, 12)], 0.0, None),
    ('BLK_AA866E', '', [(7, 5), (7, 6), (8, 6), (8, 7), (9, 7), (9, 8), (10, 8)], 0.0, None),
]


TUNNELS = [
]


SIGNALS = [
]


# Block groupings: each entry is one block owning the listed segments, in
# connected run order. ``BLOCKS`` above builds each listed segment as its own
# one-segment block first (the default one-feed-per-segment layout); grouping
# then folds them into the single block that shares one power feed.
BLOCK_GROUPS = [
    ('D_001', '', ['BLK_AA62D2', 'BLK_AA866E', 'BLK_AA7B7E', 'BLK_AA6FB5']),
]


def _add_tracks(network: RailwayNetwork) -> None:
    """Place every baked track tile (dead-end ports are re-derived from geometry)."""
    for (x, y, bits) in TILES:
        network.add_track(Tile(x, y, track_bits=bits))


def _add_switches(network: RailwayNetwork) -> None:
    """Create the Switch entity for each switch tile and link the tile to it."""
    for (switch_id, x, y) in SWITCHES:
        network.add_switch(Switch(switch_id=switch_id, x=x, y=y))
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
    for (platform_id, name, first_tile, extra_tiles, side, length_cm, top_speed_kmh, block_name) in PLATFORMS:
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
        station_block = network.blocks.get(f"{platform.platform_id}_BLOCK")
        if station_block is not None:
            station_block.name = block_name


def _add_blocks(network: RailwayNetwork) -> None:
    """Create the non-platform blocks from their baked tile lists."""
    for (block_id, name, positions, length_cm, speed_limit_kmh) in BLOCKS:
        network.create_segment_with_tracks(
            segment_id=block_id,
            positions=positions,
            name=name,
            length_cm=length_cm,
            speed_limit_kmh=speed_limit_kmh,
        )


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
    """
    if not BLOCK_GROUPS:
        return
    grouping = network._get_block_grouping_manager()
    for (block_id, name, segment_ids) in BLOCK_GROUPS:
        grouping.group_into_block(
            block_id=block_id, segment_ids=segment_ids, name=name
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
    network.recompute_all_flank_protection()
    return network


LOOP_BLOCK_ID = "D_001"
HOLD_SECONDS = 5.0


def setup_demo_train(network, path_dispatcher=None, sim_controller=None):
    """Place one locomotive on the single-block oval.

    demo7 is one oval wired to one power feed, so the whole oval is a single
    ``Block`` (``D_001``) owning the four quadrant ``Segment``s that carry its
    geometry. A block admits one train at a time, which is exactly what this
    layout's shared feed always allowed.

    Because the oval is one block, there is no block-to-block move to route: a
    ``BlockTarget`` route needs at least two blocks to travel between, and
    segments are geometry only, so they are not stop targets. The scene
    therefore places and powers the loco rather than running a stop loop. To get
    a multi-stop loop demo back, bake a layout whose quadrants are separately
    fed — four blocks instead of one grouping.
    """
    import math
    from speed_profile import SpeedProfile, DEFAULT_SPEED_PROFILE

    # Layout-only loads (e.g. json_to_scene --verify calls create_comprehensive_demo
    # without a controller) have nothing to place.
    if sim_controller is None:
        return

    # Real-world segment lengths. The layout's true perimeter is two 315 mm-radius
    # semicircle ends (pi * 31.5 cm each) plus two 56 cm straights =
    # 2*(pi*31.5) + 2*56 = 309.92 cm. The baked layout splits the loop into four
    # equal-tile quadrant segments rather than a 2-curve + 2-straight split, so
    # the perimeter is spread evenly across the four segments — total lap length
    # (and therefore lap time) matches the real loop. Length lives on the
    # segment; the block's `length_cm` is the sum across its members.
    per_segment_cm = (2.0 * math.pi * 31.5 + 2.0 * 56.0) / 4.0
    for segment in network.blocks[LOOP_BLOCK_ID].segments:
        segment.length_cm = per_segment_cm

    # One shared, slow profile so a lap over the ~3.1 m loop is watchable.
    profile = SpeedProfile(
        profile_id="loop_profile",
        name="Loop Stock",
        power_speed_curve=list(DEFAULT_SPEED_PROFILE.power_speed_curve),
        voltage_power_curve=list(DEFAULT_SPEED_PROFILE.voltage_power_curve),
        lighting_voltage=DEFAULT_SPEED_PROFILE.lighting_voltage,
        acceleration_rate=3.0,
        deceleration_rate=4.2,
        emergency_brake_rate=5,
        friction_rate=DEFAULT_SPEED_PROFILE.friction_rate,
        operational_decel_ratio=DEFAULT_SPEED_PROFILE.operational_decel_ratio,
        air_resistance_enabled=True,
        drag_coefficient=0.001,
        reference_speed_kmh=100.0,
    )
    network.speed_profiles.register(profile)

    # Place one 20 cm loco at the bottom of the left column, facing north so it
    # heads forward around the loop (north out of (3, 8) runs into the second
    # segment of the block).
    train = sim_controller.add_train(
        train_id="T_LOOP",
        starting_block_id=LOOP_BLOCK_ID,
        initial_direction="NORTH",
        max_speed_kmh=15.0,
        length_cm=20.0,
        train_type="LOCOMOTIVE",
        initial_cab="A",
        start_tile=(3, 8),
    )
    train.speed_profile_id = profile.profile_id

    sim_controller.get_or_create_controller("T_LOOP").commands.enqueue_command(
        "power_on"
    )

    print(
        f"  Demo 7: 1 loco on the single-block oval {LOOP_BLOCK_ID} "
        f"({len(network.blocks[LOOP_BLOCK_ID].segments)} segments)."
    )
