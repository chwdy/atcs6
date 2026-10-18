"""Scene baked from demo_test.json by json_to_scene.py.

Auto-generated — do not hand-edit; re-run json_to_scene.py against the source
layout instead. Layout only: 95 tracks, 2 switches,
2 platforms (0 station groups),
13 blocks (2 auto-created platform blocks),
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
from entity_ids import segment_id_for_block


SCENE_ID = 'demo_test'


TILES = [
    (2, 2, TRACK_SE),
    (3, 2, TRACK_NW),
    (3, 3, TRACK_SE),
    (4, 3, TRACK_NW),
    (4, 4, TRACK_SE),
    (5, 4, TRACK_NW),
    (5, 5, TRACK_SE),
    (6, 5, TRACK_NW),
    (6, 6, TRACK_SE),
    (7, 6, TRACK_NW),
    (7, 7, TRACK_SE),
    (8, 7, TRACK_EW | TRACK_NW),  # SW_8_7
    (8, 8, TRACK_SE),
    (9, 7, TRACK_EW),
    (9, 8, TRACK_NW),
    (9, 9, TRACK_SE),
    (10, 7, TRACK_EW),
    (10, 9, TRACK_NW),
    (10, 10, TRACK_SE),
    (11, 7, TRACK_EW),
    (11, 10, TRACK_NW),
    (11, 11, TRACK_SE),
    (12, 7, TRACK_EW),
    (12, 11, TRACK_NW),
    (12, 12, TRACK_SE),
    (13, 7, TRACK_EW),
    (13, 12, TRACK_NW),
    (13, 13, TRACK_SE),
    (14, 7, TRACK_EW),
    (14, 13, TRACK_NW),
    (14, 14, TRACK_SE),
    (15, 6, TRACK_NE),
    (15, 7, TRACK_SW),
    (15, 14, TRACK_NW),
    (15, 15, TRACK_SE),
    (16, 5, TRACK_NE),
    (16, 6, TRACK_SW),
    (16, 15, TRACK_NW),
    (16, 16, TRACK_SE),
    (17, 4, TRACK_NE),
    (17, 5, TRACK_SW),
    (17, 16, TRACK_NW),
    (17, 17, TRACK_SE),
    (18, 3, TRACK_NE),
    (18, 4, TRACK_SW),
    (18, 17, TRACK_NW),
    (18, 18, TRACK_SE),
    (19, 2, TRACK_NE),
    (19, 3, TRACK_SW),
    (19, 18, TRACK_NW),
    (19, 19, TRACK_SE),
    (20, 2, TRACK_EW),
    (20, 19, TRACK_NW),
    (20, 20, TRACK_SE),
    (21, 2, TRACK_EW),
    (21, 13, TRACK_NE),
    (21, 14, TRACK_NS),
    (21, 15, TRACK_NS),
    (21, 16, TRACK_NS),
    (21, 17, TRACK_NS),
    (21, 18, TRACK_NS),
    (21, 19, TRACK_NS),
    (21, 20, TRACK_NS | TRACK_NW),  # SW_21_20
    (21, 21, TRACK_SE),
    (22, 2, TRACK_EW),
    (22, 12, TRACK_NE),
    (22, 13, TRACK_SW),
    (22, 21, TRACK_NW),
    (22, 22, TRACK_SE),
    (23, 2, TRACK_NW),
    (23, 3, TRACK_SE),
    (23, 11, TRACK_NE),
    (23, 12, TRACK_SW),
    (23, 22, TRACK_NW),
    (23, 23, TRACK_SE),
    (24, 3, TRACK_NW),
    (24, 4, TRACK_SE),
    (24, 10, TRACK_NE),
    (24, 11, TRACK_SW),
    (24, 23, TRACK_NW),
    (24, 24, TRACK_SE),
    (25, 4, TRACK_NW),
    (25, 5, TRACK_SE),
    (25, 9, TRACK_NE),
    (25, 10, TRACK_SW),
    (25, 24, TRACK_NW),
    (25, 25, TRACK_SE),
    (26, 5, TRACK_NW),
    (26, 6, TRACK_NS),
    (26, 7, TRACK_NS),
    (26, 8, TRACK_NS),
    (26, 9, TRACK_SW),
    (26, 25, TRACK_NW),
    (26, 26, TRACK_SE),
    (27, 26, TRACK_NW),
]


SWITCHES = [
    ('SW_8_7', 8, 7),
    ('SW_21_20', 21, 20),
]


STATIONS = [
]


PLATFORMS = [
    ('PLAT_513670', 'p2', (2, 2), [(3, 2), (3, 3), (4, 3), (4, 4), (5, 4), (5, 5), (6, 5)], 'right', 60.0, None, 'Station Block - PLAT_513670'),
    ('PLAT_532737', 'p1', (23, 23), [(24, 23), (24, 24), (25, 24), (25, 25), (26, 25), (26, 26), (27, 26)], 'right', 60.0, None, 'Station Block - PLAT_532737'),
]


BLOCKS = [
    ('BLK_2F8674', '', [(6, 6), (7, 6), (7, 7)], 20.0, None),
    ('BLK_2F9133', '', [(9, 7), (10, 7), (11, 7), (12, 7), (13, 7), (14, 7)], 40.0, None),
    ('BLK_2F9DB3', '', [(15, 7), (15, 6), (16, 6), (16, 5), (17, 5), (17, 4), (18, 4), (18, 3), (19, 3), (19, 2)], 40.0, None),
    ('BLK_2FA65B', '', [(22, 2), (21, 2), (20, 2)], 40.0, None),
    ('BLK_2FB0B5', '', [(26, 5), (25, 5), (25, 4), (24, 4), (24, 3), (23, 3), (23, 2)], 40.0, None),
    ('BLK_2FB8FA', '', [(26, 8), (26, 7), (26, 6)], 40.0, None),
    ('BLK_2FC44E', '', [(21, 13), (22, 13), (22, 12), (23, 12), (23, 11), (24, 11), (24, 10), (25, 10), (25, 9), (26, 9)], 40.0, None),
    ('BLK_2FD0FF', '', [(21, 19), (21, 18), (21, 17), (21, 16), (21, 15), (21, 14)], 40.0, None),
    ('BLK_2FDE9B', '', [(8, 8), (9, 8), (9, 9), (10, 9), (10, 10), (11, 10), (11, 11), (12, 11), (12, 12), (13, 12), (13, 13), (14, 13)], 40.0, None),
    ('BLK_2FEE7D', '', [(14, 14), (15, 14), (15, 15), (16, 15), (16, 16), (17, 16), (17, 17), (18, 17), (18, 18), (19, 18), (19, 19), (20, 19), (20, 20)], 40.0, None),
    ('BLK_2FFFE0', '', [(21, 21), (22, 21), (22, 22), (23, 22)], 20.0, None),
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
            # The tuple's id is this scene's BLOCK id: saved routes, tests and
            # hardware bindings already name it, so it is pinned and the
            # segment takes the matching geometry-axis id (see entity_ids).
            block_id=block_id,
            segment_id=segment_id_for_block(block_id),
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


def create_comprehensive_demo() -> RailwayNetwork:
    """Build the baked network from scratch and return it."""
    network = RailwayNetwork()
    _add_tracks(network)
    _add_switches(network)
    _add_stations(network)
    _add_platforms(network)
    _add_blocks(network)
    _add_tunnels(network)
    _add_signals(network)
    network.recompute_all_flank_protection()
    return network


def setup_demo_train(network, path_dispatcher=None, sim_controller=None):
    """Place the test train in p1 and give it a one-off route to p2.

    Hand-written (train placement and routes are not part of the layout JSON, so
    re-baking this module from ``layouts/demo_test.json`` would reset this back
    to a stub — keep this function when re-baking, the demo_6.py convention).

    The layout has two branches between p1 (SE) and p2 (NW): a SHORT branch
    through ``BLK_2FEE7D`` -> ``BLK_2FDE9B`` and a LONG branch through seven
    blocks. With no per-block curve limits the pathfinder picks the SHORT
    branch. The train's minimum curve radius is 315mm (``min_radius_cm`` is in
    centimetres, so 31.5); tightening the SHORT branch's blocks below that makes
    the SHORT branch infeasible and the train takes the LONG branch.
    """
    from train_route import PlatformTarget, Route, RouteMode, RouteTask
    from speed_profile import SpeedProfile, DEFAULT_SPEED_PROFILE

    # 50cm train in p1. The platform is a NW->SE staircase; placing the head at
    # the NW-corner tile (23, 23) facing SOUTH puts the train's leading end
    # toward the throat switch (SW_21_20) so it departs forward toward p2.
    train = sim_controller.add_train(
        train_id="TEST_TRAIN",
        starting_block_id="PLAT_532737_BLOCK",
        initial_direction="SOUTH",
        max_speed_kmh=60.0,
        length_cm=50.0,
        train_type="LOCOMOTIVE",
        initial_cab="A",
        start_tile=(23, 23),
    )
    train.min_radius_cm = 31.5  # 315mm minimum curve radius this train can take.
    train.max_slope = 15.0  # can climb up to a 15% uphill grade before stalling.

    profile = SpeedProfile(
        profile_id="demo_test_profile",
        name="Demo Test Profile",
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
    train.speed_profile_id = profile.profile_id

    # One-off route p1 -> p2: stop at p2 and stay there.
    route = Route(
        route_id="DEMO_TEST_ROUTE",
        name="Demo Test: p1 to p2",
        mode=RouteMode.ONE_OFF,
    )
    route.add_task(
        RouteTask(
            task_kind="stop",
            task_location=PlatformTarget(platform_id="PLAT_513670"),
            task_direction="either",
        )
    )
    sim_controller.route_executor.register_route(route)
    route.assign_train(train.train_id)

    # Issue the operator start-up: power on, then engage ATO. Plan acquires the
    # route's path on the first powered tick and the engaged driving rule moves
    # the train.
    controller = sim_controller.get_or_create_controller(train.train_id)
    controller.commands.power_on_cmd()
    controller.commands.set_mode("ato")
    controller.commands.ato_go()
