"""
Demo 3: Four-Station NE-Zigzag Double-Track Loop

Layout (rendered horizontally on the rotated screen):

  ┌── S1-N ──┐        ┌── S2-N ──┐        ┌── S3-N ──┐        ┌── S4-N ──┐
  ├──────────┤───X1───├──────────┤── plain ─├──────────┤───X2───├──────────┤   ← N track (NW-side)
  │          │        │          │          │          │        │          │
  ├──────────┤───X1───├──────────┤── plain ─├──────────┤───X2───├──────────┤   ← S track (SE-side)
  └── S1-S ──┘        └── S2-S ──┘        └── S3-S ──┘        └── S4-S ──┘

  Each platform spans 3 zigzag tiles (60 cm). Between stations:
    S1↔S2 and S3↔S4: 30 cm plain block (per track) → 1-tile scissors crossover (shared) → 30 cm plain block.
    S2↔S3: two 40 cm plain blocks per track (zigzag, no crossover) — block A is 2 tiles, block B is 1 tile.

  S1, S4 = island-style platforms (each track's platform faces the other track).
  S2, S3 = side platforms (each track's platform faces outward).

Route (CIRCULAR, all 4 trains share it):
  STN_1 → dwell 2s → PLT_S2_S → dwell 2s → PLT_S3_S → dwell 2s → STN_4
    → dwell 2s → PLT_S3_N → dwell 2s → PLT_S2_N → dwell 2s → loop.

Initial placement:
  T1 at S1-S (facing N, next stop S2-S)
  T2 at S2-S (facing N, next stop S3-S)
  T3 at S4-N (facing W, next stop S3-N)
  T4 at S2-N (facing W, next stop S1)
"""

from network import RailwayNetwork
from track import Tile, TRACK_NS, TRACK_EW, TRACK_NW, TRACK_SE
from path_dispatcher import PathDispatcher
from switch import Switch
from entity_ids import segment_id_for_block


# Identifier read by bootstrap.py and by scene-gated tests to recognise this
# scene. Each scene module declares its own ID; tests compare against the
# active scene (see bootstrap.get_current_scene_id) to decide whether to run.
SCENE_ID = "demo_3"


# Scissors crossover bit pattern (NS + EW straights + NW + SE diagonals).
# Per design_new.md "Switch.position": 0 = MAIN, 1 = DIVERGE. The dispatcher
# derives the required switch state from track.enumerate_traversals
# (track.py:766-784): NS/EW straight bits → state 0 (MAIN), NW/SE diagonal
# bits → state 1 (DIVERGE). The route geometries below map as follows:
#
#   State 0 (MAIN) — swap routes via straight bits:
#     S→N swap uses NS (bit 0)
#     N→S swap uses EW (bit 1)  (entry W, exit E)
#
#   State 1 (DIVERGE) — stay-on-track routes via diagonal bits:
#     S track  go S→E uses SE (bit 4) — stays on S track
#     N track  go W→N uses NW (bit 3) — stays on N track
SCISSORS_BITS = TRACK_NS | TRACK_EW | TRACK_NW | TRACK_SE  # = 27


# ============= Demo Functions =============


# Track-tile layout. Each entry: (x, y, bits, role).
# S track runs SE-side of the corridor; N track runs NW-side.
# Both tracks zig-zag NE overall (the y=x line — appears horizontal on the
# rotated isometric screen).

S_TRACK_TILES = [
    (1, 0, TRACK_NW, "S1_S_t1"),
    (1, 1, TRACK_SE, "S1_S_t2"),
    (2, 1, TRACK_NW, "S1_S_t3"),
    (2, 2, TRACK_NS, "BLK_S1_S_TO_X1"),   # NS detour up to crossover X1
    # (2, 3) is crossover X1 — added separately below.
    (3, 3, TRACK_EW, "BLK_X1_TO_S2_S"),   # EW detour out of X1 toward S2-S
    (4, 3, TRACK_NW, "S2_S_t1"),
    (4, 4, TRACK_SE, "S2_S_t2"),
    (5, 4, TRACK_NW, "S2_S_t3"),
    # Plain zigzag between S2 and new S3 — 2 blocks (40 cm each), no crossover.
    (5, 5, TRACK_SE, "BLK_S2_S_TO_S3_S_A_t1"),
    (6, 5, TRACK_NW, "BLK_S2_S_TO_S3_S_A_t2"),
    (6, 6, TRACK_SE, "BLK_S2_S_TO_S3_S_B_t1"),
    (7, 6, TRACK_NW, "S3_S_t1"),
    (7, 7, TRACK_SE, "S3_S_t2"),
    (8, 7, TRACK_NW, "S3_S_t3"),
    (8, 8, TRACK_NS, "BLK_S3_S_TO_X2"),   # NS detour up to crossover X2
    # (8, 9) is crossover X2.
    (9, 9, TRACK_EW, "BLK_X2_TO_S4_S"),   # EW detour out of X2 toward S4-S
    (10, 9, TRACK_NW, "S4_S_t1"),
    (10, 10, TRACK_SE, "S4_S_t2"),
    (11, 10, TRACK_NW, "S4_S_t3"),
]

# N track is offset (-1, +1) from S track on every tile (NW direction).
N_TRACK_TILES = [
    (0, 1, TRACK_NW, "S1_N_t1"),
    (0, 2, TRACK_SE, "S1_N_t2"),
    (1, 2, TRACK_NW, "S1_N_t3"),
    (1, 3, TRACK_SE, "BLK_S1_N_TO_X1"),   # natural SE zigzag — enters X1 from W
    # (2, 3) is crossover X1 — N track passes through via NW bit.
    (2, 4, TRACK_SE, "BLK_X1_TO_S2_N"),   # natural SE zigzag — exits X1 to N
    (3, 4, TRACK_NW, "S2_N_t1"),
    (3, 5, TRACK_SE, "S2_N_t2"),
    (4, 5, TRACK_NW, "S2_N_t3"),
    # Plain zigzag between S2 and new S3 — 2 blocks (40 cm each), no crossover.
    (4, 6, TRACK_SE, "BLK_S2_N_TO_S3_N_A_t1"),
    (5, 6, TRACK_NW, "BLK_S2_N_TO_S3_N_A_t2"),
    (5, 7, TRACK_SE, "BLK_S2_N_TO_S3_N_B_t1"),
    (6, 7, TRACK_NW, "S3_N_t1"),
    (6, 8, TRACK_SE, "S3_N_t2"),
    (7, 8, TRACK_NW, "S3_N_t3"),
    (7, 9, TRACK_SE, "BLK_S3_N_TO_X2"),
    # (8, 9) is crossover X2.
    (8, 10, TRACK_SE, "BLK_X2_TO_S4_N"),
    (9, 10, TRACK_NW, "S4_N_t1"),
    (9, 11, TRACK_SE, "S4_N_t2"),
    (10, 11, TRACK_NW, "S4_N_t3"),
]


def _add_plain_tracks(network: RailwayNetwork) -> None:
    """Place all non-switch tiles for both tracks."""
    for tiles in (S_TRACK_TILES, N_TRACK_TILES):
        for (x, y, bits, _role) in tiles:
            network.add_track(Tile(x, y, track_bits=bits))


def _add_crossovers(network: RailwayNetwork) -> None:
    """Place the two scissors crossover tiles plus their Switch entities.

    Each crossover is a single tile whose bits encode both tracks' natural
    routes through the tile, AND the swap routes (NS for S→N, EW for N→S).
    The crossover tile is a switch boundary — it does NOT belong to any
    block.
    """
    from enums import SwitchState
    for (sx, sy, sid) in ((2, 3, "SW_X1"), (8, 9, "SW_X2")):
        tile = Tile(sx, sy, track_bits=SCISSORS_BITS)
        network.add_track(tile)
        switch = Switch(switch_id=sid, x=sx, y=sy)
        # DIVERGE (1) = stay-on-track via NW/SE diagonals — the demo's
        # nominal alignment. MAIN (0) = swap via NS/EW straights.
        switch.position = SwitchState.DIVERGE
        network.add_switch(switch)
        tile.switch_id = switch.switch_id


# Each block: (block_id, positions, name, length_cm, speed_limit_kmh or None).
# Station blocks are created automatically by add_station (named
# `{platform_id}_BLOCK`) so they are NOT pre-created here. Pre-creating them
# would split the block per-tile and end up with the wrong length (the
# station's input length_cm is only honored when the tile is not already in
# a block; see station_registry.add_station).
BLOCKS = [
    # Plain blocks bracketing X1
    ("BLK_S1_S_TO_X1", [(2, 2)], "S1-S → X1", 30.0, 50.0),
    ("BLK_S1_N_TO_X1", [(1, 3)], "S1-N → X1", 30.0, 50.0),
    ("BLK_X1_TO_S2_S", [(3, 3)], "X1 → S2-S", 30.0, 50.0),
    ("BLK_X1_TO_S2_N", [(2, 4)], "X1 → S2-N", 30.0, 50.0),
    # Plain zigzag between S2 and new S3 (no crossover; 2 blocks of 40 cm per track).
    # Block A holds 2 tiles, block B holds 1 tile — 3 tiles is the smallest
    # zigzag run that keeps the new S3 anchor on the same y-x=-1 line as S2 and S4.
    ("BLK_S2_S_TO_S3_S_A", [(5, 5), (6, 5)], "S2-S → S3-S (A)", 40.0, 50.0),
    ("BLK_S2_S_TO_S3_S_B", [(6, 6)],         "S2-S → S3-S (B)", 40.0, 50.0),
    ("BLK_S2_N_TO_S3_N_A", [(4, 6), (5, 6)], "S2-N → S3-N (A)", 40.0, 50.0),
    ("BLK_S2_N_TO_S3_N_B", [(5, 7)],         "S2-N → S3-N (B)", 40.0, 50.0),
    # Plain blocks bracketing X2
    ("BLK_S3_S_TO_X2", [(8, 8)], "S3-S → X2", 30.0, 50.0),
    ("BLK_S3_N_TO_X2", [(7, 9)], "S3-N → X2", 30.0, 50.0),
    ("BLK_X2_TO_S4_S", [(9, 9)], "X2 → S4-S", 30.0, 50.0),
    ("BLK_X2_TO_S4_N", [(8, 10)], "X2 → S4-N", 30.0, 50.0),
]


def _add_blocks(network: RailwayNetwork) -> None:
    """Register every block in BLOCKS via create_segment_with_tracks."""
    for (block_id, positions, name, length_cm, speed) in BLOCKS:
        kwargs = {
            # The tuple's id is this scene's BLOCK id: saved routes, tests and
            # hardware bindings already name it, so it is pinned and the
            # segment takes the matching geometry-axis id (see entity_ids).
            "block_id": block_id,
            "segment_id": segment_id_for_block(block_id),
            "positions": positions,
            "name": name,
            "length_cm": length_cm,
        }
        if speed is not None:
            kwargs["speed_limit_kmh"] = speed
        network.create_segment_with_tracks(**kwargs)


# Each station entry: (station_id, name, first_tile, extra_tiles, platform_side)
# platform_side values are interpreted by the renderer relative to the tile's
# orientation group; for NW_SE-bit tiles, "left" → SW visual side, "right" → NE.
# Island stations (S1, S4): N-track platform on its SW side, S-track on its NE
#   side — both platforms face inward toward the other track.
# Side-platform stations (S2, S3): N-track platform on NE side, S-track on SW
#   side — both face outward.
STATIONS = [
    # (group_id, group_name, [(platform_id, name, first_xy, extra_xys, side), ...])
    ("STN_1", "Station 1", [
        ("PLT_S1_S", "S1 South", (1, 0), [(1, 1), (2, 1)], "right"),
        ("PLT_S1_N", "S1 North", (0, 1), [(0, 2), (1, 2)], "left"),
    ]),
    ("STN_2", "Station 2", [
        ("PLT_S2_S", "S2 South", (4, 3), [(4, 4), (5, 4)], "left"),
        ("PLT_S2_N", "S2 North", (3, 4), [(3, 5), (4, 5)], "right"),
    ]),
    ("STN_3", "Station 3", [
        ("PLT_S3_S", "S3 South", (7, 6), [(7, 7), (8, 7)], "left"),
        ("PLT_S3_N", "S3 North", (6, 7), [(6, 8), (7, 8)], "right"),
    ]),
    ("STN_4", "Station 4", [
        ("PLT_S4_S", "S4 South", (10, 9), [(10, 10), (11, 10)], "right"),
        ("PLT_S4_N", "S4 North", (9, 10), [(9, 11), (10, 11)], "left"),
    ]),
]


def _add_stations(network: RailwayNetwork) -> None:
    for (group_id, group_name, platforms) in STATIONS:
        platform_ids = []
        for (pid, name, first_xy, extra_xys, side) in platforms:
            platform = network.add_station(
                x=first_xy[0],
                y=first_xy[1],
                station_id=pid,
                name=name,
                length_cm=60.0,
                platform_side=side,
                top_speed_kmh=40.0,
            )
            for extra in extra_xys:
                network.extend_station(platform.platform_id, [extra])
            platform_ids.append(platform.platform_id)
        network.create_station_group(
            station_id=group_id,
            name=group_name,
            platform_ids=platform_ids,
        )


def create_comprehensive_demo() -> RailwayNetwork:
    """Build the Demo 3 network from scratch and return it.

    Mirrors the name of demo_main.create_comprehensive_demo so server.py's
    `import demo_3 as demo_main` works without other edits.
    """
    print("\n" + "=" * 70)
    print("Demo 3: Four-Station NE-Zigzag Double-Track Loop")
    print("=" * 70)

    network = RailwayNetwork()

    _add_plain_tracks(network)
    _add_crossovers(network)
    _add_blocks(network)
    _add_stations(network)

    print(
        f"  ✓ tracks={len(network.tracks)}  "
        f"blocks={len(network.blocks)}  "
        f"platforms={len(network.platforms)}  "
        f"switches={len(network.switches)}"
    )

    return network


def setup_demo_train(
    network: RailwayNetwork,
    path_dispatcher: PathDispatcher,
    sim_controller,
):
    """Place the four demo trains and register the shared CIRCULAR route.

    Mirrors the name of demo_main.setup_demo_train so server.py's alias keeps
    working. Returns nothing — side-effects: adds trains, registers the route,
    queues power_on commands.
    """
    from train_route import (
        Route,
        RouteMode,
        RouteTask,
        PlatformTarget,
        StationTarget,
    )
    from runtime_payloads import AcquisitionMode
    from speed_profile import SpeedProfile, DEFAULT_SPEED_PROFILE

    # ---- Shared route (CIRCULAR, 12 tasks: stop+dwell × 6) ----
    route = Route(
        route_id="R_DEMO3_LOOP",
        name="Demo 3: Four-Station Loop",
        mode=RouteMode.CIRCULAR,
        acquisition_mode=AcquisitionMode.ALL_OR_NOTHING,
        is_active=True,
    )
    tasks = [
        # S1 (any platform) — terminus, train reverses here
        RouteTask(task_kind="stop",  task_location=StationTarget("STN_1"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=StationTarget("STN_1"),
                  task_time=2.0),
        # S2-S platform (outbound leg)
        RouteTask(task_kind="stop",  task_location=PlatformTarget("PLT_S2_S"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=PlatformTarget("PLT_S2_S"),
                  task_time=2.0),
        # S3-S platform (outbound leg)
        RouteTask(task_kind="stop",  task_location=PlatformTarget("PLT_S3_S"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=PlatformTarget("PLT_S3_S"),
                  task_time=2.0),
        # S4 (any platform) — terminus
        RouteTask(task_kind="stop",  task_location=StationTarget("STN_4"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=StationTarget("STN_4"),
                  task_time=2.0),
        # S3-N platform (return leg)
        RouteTask(task_kind="stop",  task_location=PlatformTarget("PLT_S3_N"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=PlatformTarget("PLT_S3_N"),
                  task_time=2.0),
        # S2-N platform (return leg)
        RouteTask(task_kind="stop",  task_location=PlatformTarget("PLT_S2_N"),
                  task_direction="either"),
        RouteTask(task_kind="dwell", task_location=PlatformTarget("PLT_S2_N"),
                  task_time=2.0),
    ]
    for t in tasks:
        route.add_task(t)
    sim_controller.route_executor.register_route(route)

    # ---- Shared speed profile ----
    demo_profile = SpeedProfile(
        profile_id="demo3_profile",
        name="Demo 3 Profile",
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
    network.speed_profiles.register(demo_profile)

    # ---- Place the four trains, each phase-shifted one stop apart ----
    # (train_id, starting_block, initial_direction, route-phase-task-index, start_tile)
    # Direction reasoning: starting tiles all have NW bits (N-W edges). A
    # train placed facing N exits N edge; facing W exits W edge. The 50 cm
    # body extends backward along the rail (not strict cardinal), so for
    # NORTH-facing trains at S1-S / S2-S we pin the head to the NE-end
    # tile of the platform so the body extends INTO the platform along
    # the W→S→W zigzag rather than off the layout edge. WEST-facing trains
    # at S2-N / S3-N use the block's default SW-end tile because the body
    # naturally extends N+E into the platform.
    #
    # `phase_idx` is the index of the dwell task that should be in flight
    # when the train spawns — task_runs[0..phase_idx-1] get marked finished
    # so the executor starts each train at its own station instead of
    # racing them all to STN_1.
    train_specs = [
        ("T1", "PLT_S1_S_BLOCK", "NORTH", 1,  (2, 1)),  # NE-end of S1-S
        ("T2", "PLT_S2_S_BLOCK", "NORTH", 3,  (5, 4)),  # NE-end of S2-S
        ("T3", "PLT_S4_N_BLOCK", "WEST",  7,  None),    # SW-end (default)
        ("T4", "PLT_S2_N_BLOCK", "WEST",  11, None),    # SW-end (default)
    ]

    import time as _time
    from route_executor import RouteExecutionState, RouteRunStatus
    from train_route import RouteTaskRun

    now = _time.time()
    for (train_id, block_id, direction, phase_idx, start_tile) in train_specs:
        train = sim_controller.add_train(
            train_id=train_id,
            starting_block_id=block_id,
            initial_direction=direction,
            max_speed_kmh=40.0,
            length_cm=50.0,
            train_type="LOCOMOTIVE",
            initial_cab="A",
            start_tile=start_tile,
        )
        train.speed_profile_id = demo_profile.profile_id
        route.assign_train(train.train_id)

        # Pre-create the per-train RouteExecutionState with task_runs
        # phase-shifted so this train's next planning task lands on the
        # dwell at its current station. `_sync_task_runs` will append the
        # remaining task rows on the first Plan tick, leaving the
        # phase-shifted prefix alone.
        executor = sim_controller.route_executor
        run_count = executor._run_count_for_route.get(route.route_id, 0) + 1
        executor._run_count_for_route[route.route_id] = run_count
        exec_state = RouteExecutionState(
            train_id=train_id,
            route_id=route.route_id,
            started_at=now,
            run_number=executor._compose_run_number(route.route_id, run_count),
            current_route_status=RouteRunStatus.RUNNING,
        )
        for i in range(phase_idx):
            exec_state.task_runs.append(
                RouteTaskRun(
                    task_index=i,
                    finished_at=now,
                    # The operating clock reads 0.0 until the first
                    # tick, and this prefill runs at build time.
                    finished_at_operating=0.0,
                )
            )
        exec_state.task_runs.append(RouteTaskRun(task_index=phase_idx))
        executor.execution_states[train_id] = exec_state

        controller = sim_controller.get_or_create_controller(train.train_id)
        controller.commands.enqueue_command("power_on")

    print(f"  ✓ Registered route {route.route_id!r} with {len(route.tasks)} tasks")
    print(f"  ✓ Placed 4 trains (phase-shifted): {[s[0] for s in train_specs]}")
