"""
Railway System Demo

This is the main demonstration script for the railway management system.
It imports all components from separate modules and creates a demo
showing train dispatching, scheduling, and safety systems in action.

Track Building using 6-bit track_bits system:
- Tile(x, y, track_bits=TRACK_EW): East-West straight track
- Tile(x, y, track_bits=TRACK_NS): North-South straight track
- Tile(x, y, track_bits=TRACK_NE): North-East diagonal track
- Tile(x, y, track_bits=TRACK_NW): North-West diagonal track
- Tile(x, y, track_bits=TRACK_SE): South-East diagonal track
- Tile(x, y, track_bits=TRACK_SW): South-West diagonal track
- Tile(x, y, track_bits=TRACK_EW | TRACK_NE): 3-way switch
- Dead-ends are auto-detected by propagation system based on neighbor connectivity
"""

from datetime import datetime
from network import RailwayNetwork
from track import (
    Tile,
    TRACK_NS,
    TRACK_EW,
    TRACK_NW,
    TRACK_SE,
    TRACK_SW,
)
from path_dispatcher import PathDispatcher
from railway_signal import SignalType
from scene_validation import validate_block_rules
from switch import Switch

# Identifier read by bootstrap.py and by scene-gated tests to recognise this
# scene. Each scene module declares its own ID; tests compare against the
# active scene (see bootstrap.get_current_scene_id) to decide whether to run.
SCENE_ID = "demo_main"

# ============= Demo Functions =============


def create_comprehensive_demo():
    """Create the demo railway network with proper Block objects.

    Block Structure:
    - BLOCK_MAIN_WEST: Tracks (0,0) to (2,0) - "Main Line West"
    - Switch at (3,0) is a BOUNDARY, not in any block
    - BLOCK_MAIN_EAST: Tracks (4,0) to (10,0) - "Main Line East"
    - BLOCK_BRANCH: Tracks (3,1) to (3,3) - "North Branch"
    """
    print("\n" + "=" * 70)
    print("Creating Railway Network with Block Management...")
    print("=" * 70)

    network = RailwayNetwork()

    # === Create Tracks and Blocks with Validated Assignment ===
    # Note: Blocks are now created WITH tracks using create_segment_with_tracks()

    # Main Line West: (0,0) to (2,0)
    for x in range(0, 3):
        track = Tile(x, 0, track_bits=TRACK_EW)
        network.add_track(track)

    # Create block with validated track assignment
    network.create_segment_with_tracks(
        segment_id="BLOCK_MAIN_WEST",
        positions=[(x, 0) for x in range(0, 3)],
        name="Main Line West",
        length_cm=150.0,  # 3 tiles * 50cm each
        speed_limit_kmh=60.0,
    )

    # Switch at (3,0) - BOUNDARY, NO block assignment
    # Connects main line (EW) with branch toward NORTH via NW
    track = Tile(3, 0, track_bits=TRACK_EW | TRACK_NW)
    track.switch_state = 0  # MAIN (EW) active
    # track.block_id intentionally left as None
    network.add_track(track)
    # Create switch entity
    switch = Switch(switch_id="SW_3_0", x=3, y=0)
    network.add_switch(switch)
    track.switch_id = switch.switch_id

    # Main Line East: (4,0) to (10,0)
    for x in range(4, 11):
        track = Tile(x, 0, track_bits=TRACK_EW)
        network.add_track(track)

    # Create block with validated track assignment
    network.create_segment_with_tracks(
        segment_id="BLOCK_MAIN_EAST",
        positions=[(x, 0) for x in range(4, 11)],
        name="Main Line East",
        length_cm=350.0,  # 7 tiles * 50cm each
        speed_limit_kmh=80.0,
    )

    # Station at (7,0) - will split BLOCK_MAIN_EAST into before/station/after
    central_platform = network.add_station(
        x=7,
        y=0,
        name="Central Station",
        length_cm=100.0,  # Platform length
        platform_side="both",
        top_speed_kmh=40.0,  # Reduced speed through station
    )
    network.create_station_group(
        station_id=central_platform.platform_id,
        name=central_platform.name,
        platform_ids=[central_platform.platform_id],
    )

    # North Branch: (3,1) to (3,3)
    for y in range(1, 4):
        track = Tile(3, y, track_bits=TRACK_NS)
        network.add_track(track)

    # Create block with validated track assignment
    network.create_segment_with_tracks(
        segment_id="BLOCK_BRANCH",
        positions=[(3, y) for y in range(1, 4)],
        name="North Branch",
        length_cm=150.0,  # 3 tiles * 50cm each
        speed_limit_kmh=40.0,
    )

    print(f"  Created {len(network.blocks)} blocks with validated track assignments")

    # Station at (3,2) on branch - will split BLOCK_BRANCH into before/station/after
    north_platform = network.add_station(
        x=3,
        y=2,
        name="North Station",
        length_cm=80.0,  # Smaller platform
        platform_side="right",
        top_speed_kmh=30.0,  # Lower speed for branch station
    )
    network.create_station_group(
        station_id=north_platform.platform_id,
        name=north_platform.name,
        platform_ids=[north_platform.platform_id],
    )

    # remove 9,0 if exist
    existing = network.get_track(9, 0)
    if existing:
        network.remove_track(9, 0)
    # Create switch at (9,0) - EW + NW to go up-left diagonally
    switch_track = Tile(9, 0, track_bits=TRACK_EW | TRACK_NW)
    switch_track.switch_state = 0  # MAIN (EW) active initially
    network.add_track(switch_track)
    # Create switch entity
    switch = Switch(switch_id="SW_9_0", x=9, y=0)
    network.add_switch(switch)
    switch_track.switch_id = switch.switch_id

    # Simple approach: vertical branch at x=9
    for y in range(1, 6):
        track = Tile(9, y, track_bits=TRACK_NS)
        network.add_track(track)

    network.add_track(Tile(9, 6, track_bits=TRACK_SE))
    network.add_track(Tile(10, 6, track_bits=TRACK_NW))
    network.add_track(Tile(10, 7, track_bits=TRACK_SE))
    network.add_track(Tile(11, 7, track_bits=TRACK_NW))
    network.add_track(Tile(11, 8, track_bits=TRACK_SE))
    network.add_track(Tile(12, 8, track_bits=TRACK_NW))

    network.create_segment_with_tracks(
        segment_id="BLOCK_EAST_BRANCH",
        positions=[(9, y) for y in range(1, 6)] + [(9, 6)],
        name="East Vertical Branch",
        length_cm=260.0,
        speed_limit_kmh=50.0,
    )

    network.create_segment_with_tracks(
        segment_id="BLOCK_EAST_DIAGONAL",
        positions=[(11, 8), (12, 8)],
        name="East Diagonal Approach",
        length_cm=80.0,
        speed_limit_kmh=30.0,
    )

    east_branch_station = network.add_station(
        x=10,
        y=7,
        name="east branch Station",
        length_cm=80.0,
        platform_side="right",
        top_speed_kmh=30.0,
    )
    network.create_station_group(
        station_id=east_branch_station.platform_id,
        name=east_branch_station.name,
        platform_ids=[east_branch_station.platform_id],
    )
    network.extend_station(east_branch_station.platform_id, [(11, 7)])
    network.extend_station(east_branch_station.platform_id, [(10, 6)])

    print(
        f"  ✓ Created vertical east branch (6 tiles), station with auto-block (3 tiles), and diagonal approach (2 tiles)"
    )

    # === Multi-tile Station on East Branch ===
    # Add station at top of east branch - spans 2 tiles
    east_terminal = network.add_station(
        x=9,
        y=3,
        name="East Terminal",
        length_cm=100.0,  # 2-tile platform
        platform_side="both",
        top_speed_kmh=20.0,  # Low speed for terminal station
    )
    network.create_station_group(
        station_id=east_terminal.platform_id,
        name=east_terminal.name,
        platform_ids=[east_terminal.platform_id],
    )

    # Extend station to cover 2 tiles (use station_id from returned station)
    network.extend_station(east_terminal.platform_id, [(9, 4)])

    print(f"  ✓ Created multi-tile East Terminal station spanning 2 tiles")

    # === Signal Testing (after all tracks are created) ===
    print("\n  Testing Signal System:")

    # Signal semantics:
    # - Signal "facing EAST" = light visible to trains coming FROM the east
    # - Those trains travel WEST (opposite direction)
    # - Signal monitors block in the WEST direction (where trains are going)
    #
    # Layout:
    #   BLOCK_MAIN_WEST: (0,0)-(2,0)  |  Switch (3,0)  |  BLOCK_MAIN_EAST: (4,0)-(10,0)
    #                                 |                |
    #                                 |  BLOCK_BRANCH: (3,1)-(3,3)

    # Signal 1: For westbound trains (coming from east, traveling west)
    # Placed at (5,0) in BLOCK_MAIN_EAST, facing EAST
    # Trains travel WEST, look WEST: skip (4,0) own block, (3,0) switch, find (2,0) BLOCK_MAIN_WEST ✓
    signal1 = network.add_signal(
        x=5,
        y=0,
        detection_direction="W",
        signal_type=SignalType.BLOCK,
        name="Westbound Signal",
        auto_connect=True,
    )
    network.update_signal(signal1.signal_id, state="green")
    print(
        f"    ✓ Created signal '{signal1.name}' at (5,0) facing EAST, monitors: {signal1.monitored_block_id}"
    )

    # Signal 2: For eastbound trains (coming from west, traveling east)
    # Placed at (1,0) in BLOCK_MAIN_WEST, facing WEST
    # Trains travel EAST, look EAST: skip (2,0) own block, (3,0) switch, find (4,0) BLOCK_MAIN_EAST ✓
    signal2 = network.add_signal(
        x=1,
        y=0,
        detection_direction="E",
        signal_type=SignalType.BLOCK,
        name="Eastbound Signal",
        auto_connect=True,
    )
    print(
        f"    ✓ Created signal '{signal2.name}' at (1,0) facing WEST, monitors: {signal2.monitored_block_id}"
    )

    # Signal 3: For trains exiting branch (from north, traveling south)
    # Placed at (3,1) in BLOCK_BRANCH, facing NORTH
    # Trains travel SOUTH via switch at (3,0) which has TRACK_EW | TRACK_NW
    # The NW diagonal connects N edge to W edge, so trains can only reach BLOCK_MAIN_WEST
    signal3 = network.add_signal(
        x=3,
        y=1,
        detection_direction="S",
        signal_type=SignalType.BLOCK,
        name="Branch Exit Signal",
        auto_connect=False,
    )

    # Test: Show reachable blocks for this signal
    reachable = network.get_reachable_blocks(signal3.signal_id)
    print(f"    ℹ Reachable blocks from signal at (3,1): {reachable}")

    # Test: Try to connect to a non-reachable block (should fail - no valid path)
    # Find any block that's NOT reachable from this signal
    non_reachable_blocks = [
        bid for bid in network.blocks.keys() if bid not in reachable
    ]
    if non_reachable_blocks:
        try:
            network.connect_signal_to_block(signal3.signal_id, non_reachable_blocks[0])
            print("    ✗ ERROR: Connection to non-reachable block should have failed!")
        except ValueError as e:
            print(
                f"    ✓ Correctly rejected invalid path to '{non_reachable_blocks[0]}': {e}"
            )

    # Connect to a reachable block (valid path via NW diagonal at switch)
    # Note: Original BLOCK_MAIN_WEST may have been split into child blocks
    if reachable:
        target_block = reachable[0]
        network.connect_signal_to_block(signal3.signal_id, target_block)
        print(
            f"    ✓ Created signal '{signal3.name}' at (3,1) facing NORTH, monitors: {signal3.monitored_block_id}"
        )
    else:
        print(f"    ⚠ No reachable blocks for signal at (3,1)")

    # Test: Try to place signal on switch (should fail)
    try:
        network.add_signal(
            x=3, y=0, detection_direction="E", name="Invalid Switch Signal"
        )
        print("    ✗ ERROR: Signal on switch should have failed!")
    except ValueError as e:
        print(f"    ✓ Correctly rejected signal on switch: {e}")

    # Test: Try invalid direction for track
    try:
        network.add_signal(x=6, y=0, detection_direction="N", name="Invalid Direction")
        print("    ✗ ERROR: Invalid direction should have failed!")
    except ValueError as e:
        print(f"    ✓ Correctly rejected invalid direction: {e}")

    # Test: Signal state cycling
    network.update_signal(signal3.signal_id, state="yellow")
    print(f"    ✓ Updated '{signal3.name}' state to YELLOW")

    # Test: Delete and recreate signal (use a plain track, not the crossing at 9,0)
    test_signal = network.add_signal(
        x=10, y=0, detection_direction="E", name="Temp Signal"
    )
    temp_id = test_signal.signal_id
    network.remove_signal(temp_id)
    print(f"    ✓ Created and deleted temp signal at (10,0)")

    # Test: Switch routing affects signal states
    print("\n  Testing Switch-Signal Interaction:")
    switch_track = network.get_track(3, 0)

    # Initial state: switch is MAIN (0), signals should have paths to their blocks
    network.update_signals_from_blocks()
    print(f"    Switch at (3,0) state: {switch_track.switch_state} (0=MAIN, 1=DIVERGE)")
    print(
        f"    Signal 1 (Westbound at 5,0): state={signal1.state.value}, monitors={signal1.monitored_block_id}"
    )
    print(
        f"    Signal 2 (Eastbound at 1,0): state={signal2.state.value}, monitors={signal2.monitored_block_id}"
    )
    print(
        f"    Signal 3 (Branch Exit at 3,1): state={signal3.state.value}, monitors={signal3.monitored_block_id}"
    )

    # Toggle switch to DIVERGE
    switch_track.switch_state = 1
    network.update_signals_from_blocks()
    print(f"\n    After toggling switch to DIVERGE (1):")
    print(
        f"    Signal 1 (Westbound at 5,0): state={signal1.state.value} (E entry not part of NW segment → RED)"
    )
    print(
        f"    Signal 2 (Eastbound at 1,0): state={signal2.state.value} (W→N via NW, not W→E → RED)"
    )
    print(
        f"    Signal 3 (Branch Exit at 3,1): state={signal3.state.value} (N→W via NW now active → GREEN)"
    )

    # Toggle back to MAIN
    switch_track.switch_state = 0
    network.update_signals_from_blocks()
    print(f"\n    After toggling switch back to MAIN (0):")
    print(
        f"    Signal 2 (Eastbound at 1,0): state={signal2.state.value} (path W→E restored)"
    )

    print(f"\n  Signal tests complete. Total signals: {len(network.signals)}")

    print("✓ Network created successfully")
    print(f"  Total tracks: {len(network.tracks)}")
    print(f"  Blocks: {', '.join(network.blocks.keys())}")
    print(f"  Signals: {len(network.signals)}")

    return network


def setup_demo_train(
    network: RailwayNetwork,
    path_dispatcher: PathDispatcher,
    sim_controller,
):
    """Set up the demo train and register both demo routes.

    Two demo routes are registered:
      * ``DEMO_ROUTE_V1`` — the original single-stop route from the origin
        to East Terminal (kept for tests and ad-hoc reassignment).
      * ``DEMO_ROUTE_V2`` — the extended journey through East Terminal,
        East Branch Terminal, a (0,0)-side turnaround at BLOCK_MAIN_WEST,
        and North Terminal, with 2 s dwells at each platform stop.

    The demo train is assigned to V2 by default; V1 stays registered but
    unassigned. Phase 1 acquires the V2 path on the first powered tick.
    """
    from train_route import (
        BlockTarget,
        PlatformTarget,
        Route,
        RouteMode,
        RouteTask,
    )
    from speed_profile import SpeedProfile, DEFAULT_SPEED_PROFILE

    # 1. Resolve the start block at the demo origin tile (0, 0).
    start_track = network.get_track(0, 0)
    starting_block_id = start_track.block_id

    # 2. Canonical placement: writes current_span before the first tick.
    demo_train = sim_controller.add_train(
        train_id="DEMO_TRAIN",
        starting_block_id=starting_block_id,
        initial_direction="EAST",
        max_speed_kmh=60.0,
        length_cm=40.0,
        train_type="LOCOMOTIVE",
        initial_cab="A",
    )

    # 3. Register the demo SpeedProfile (definition state, not controller state).
    demo_profile = SpeedProfile(
        profile_id="demo_profile",
        name="Demo Profile",
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
    demo_train.speed_profile_id = demo_profile.profile_id

    # 4. Look up stations by name. The demo network builds them in
    #    create_comprehensive_demo above; we re-resolve here so the route
    #    definitions don't depend on construction order.
    stations_by_name = {s.name: s for s in network.stations.values()}
    east_terminal = stations_by_name["East Terminal"]
    east_branch_terminal = stations_by_name["east branch Station"]
    north_terminal = stations_by_name["North Station"]

    east_terminal_target = PlatformTarget(platform_id=east_terminal.platform_id)
    east_branch_target = PlatformTarget(
        platform_id=east_branch_terminal.platform_id
    )
    north_terminal_target = PlatformTarget(platform_id=north_terminal.platform_id)
    west_dock_target = BlockTarget(block_id="BLOCK_MAIN_WEST")

    # 5. V1 route — preserved single-stop "origin → East Terminal" route.
    #    Registered for tests and manual reassignment; left inactive by
    #    default so the route panel shows it as INACTIVE (no train assigned).
    demo_route_v1 = Route(
        route_id="DEMO_ROUTE_V1",
        name="Demo v1: Origin to East Terminal",
        mode=RouteMode.ONE_OFF,
        is_active=False,
    )
    demo_route_v1.add_task(
        RouteTask(
            task_kind="stop",
            task_location=east_terminal_target,
            task_direction="forward",
        )
    )
    sim_controller.route_executor.register_route(demo_route_v1)

    # 6. V2 route — the new default extended journey.
    demo_route_v2 = Route(
        route_id="DEMO_ROUTE_V2",
        name=(
            "Demo v2: East Terminal → East Branch Terminal → "
            "BLOCK_MAIN_WEST turnaround → North Terminal → "
            "BLOCK_BRANCH_2 turnaround → back at North Station facing south"
        ),
        mode=RouteMode.ONE_OFF,
        is_active=True,
    )
    for task in (
        RouteTask(
            task_kind="stop",
            task_location=east_terminal_target,
            task_direction="either",
        ),
        RouteTask(
            task_kind="dwell",
            task_location=east_terminal_target,
            task_time=2.0,
        ),
        RouteTask(
            task_kind="stop",
            task_location=east_branch_target,
            task_direction="either",
        ),
        RouteTask(
            task_kind="dwell",
            task_location=east_branch_target,
            task_time=2.0,
        ),
        # Block-target stop forces the dock turnaround at BLOCK_MAIN_WEST
        # before the train heads back east toward the north branch.
        RouteTask(
            task_kind="stop",
            task_location=west_dock_target,
            task_direction="either",
        ),
        RouteTask(
            task_kind="stop",
            task_location=north_terminal_target,
            task_direction="either",
        ),
        RouteTask(
            task_kind="dwell",
            task_location=north_terminal_target,
            task_time=2.0,
        ),
        # Final turnaround: run up to the BLOCK_BRANCH_2 dead-end, then
        # return to the North Station platform.  The cab auto-flips on the
        # return trip, so the route ends with the train at North Station
        # facing south.
        RouteTask(
            task_kind="stop",
            task_location=BlockTarget(block_id="BLOCK_BRANCH_2"),
            task_direction="either",
        ),
        RouteTask(
            task_kind="stop",
            task_location=north_terminal_target,
            task_direction="either",
        ),
    ):
        demo_route_v2.add_task(task)
    sim_controller.route_executor.register_route(demo_route_v2)
    demo_route_v2.assign_train(demo_train.train_id)

    # 7. Queue the power_on operator command. Phase 2's drain executes it on
    #    the first tick; from tick 2 onward Phase 1 sees a powered-on
    #    controller, acquires the path for DEMO_ROUTE_V2, and auto-departs.
    controller = sim_controller.get_or_create_controller(demo_train.train_id)
    controller.commands.enqueue_command("power_on")


def create_diagonal_siding(network: RailwayNetwork):
    """Create a diagonal siding using alternating NW/SE tracks.

    This tests diagonal track connectivity and rendering.
    Pattern like user described:
      (12,0): Switch with EW + NW
      (12,1): SE
      (13,1): NW
      (13,2): SE
      (14,2): NW
      (14,3): SE (end)

    This creates a stair-step siding going diagonally SE from the main line.
    """
    print("\n  Creating Diagonal Siding:")

    # First, extend the main line to x=14 if needed
    for x in range(11, 15):
        existing = network.get_track(x, 0)
        if not existing:
            track = Tile(x, 0, track_bits=TRACK_EW)
            network.add_track(track)
            print(f"    Added main line track at ({x}, 0)")

    # Create switch at (12,0) - EW main line + NW diagonal branch
    existing = network.get_track(12, 0)
    if existing:
        network.remove_track(12, 0)
    switch_track = Tile(12, 0, track_bits=TRACK_EW | TRACK_NW)
    switch_track.switch_state = 0  # MAIN (EW) active initially
    network.add_track(switch_track)
    # Create switch entity
    switch = Switch(switch_id="SW_12_0", x=12, y=0)
    network.add_switch(switch)
    switch_track.switch_id = switch.switch_id
    print(f"    Created switch at (12,0) with EW+NW")

    # Diagonal siding pattern: alternating NW and SE
    # NW connects N-edge to W-edge
    # SE connects S-edge to E-edge
    #
    # From (12,0) NW, the N-edge connects to (12,1) from S
    # Then at (12,1) we need SE to go to (13,1) via E-edge

    # (12,1): SE - connects S to E
    # Entry from S (from switch NW at 12,0), exit E to (13,1)
    track = Tile(12, 1, track_bits=TRACK_SE)
    network.add_track(track)
    print(f"    Added SE track at (12,1)")

    # (13,1): NW - connects N to W
    # Entry from W (from 12,1 SE), exit N to (13,2)
    track = Tile(13, 1, track_bits=TRACK_NW)
    network.add_track(track)
    print(f"    Added NW track at (13,1)")

    # (13,2): SE - connects S to E
    # Entry from S (from 13,1 NW), exit E to (14,2)
    track = Tile(13, 2, track_bits=TRACK_SE)
    network.add_track(track)
    print(f"    Added SE track at (13,2)")

    # (14,2): NW - connects N to W
    # Entry from W (from 13,2 SE), exit N to (14,3)
    track = Tile(14, 2, track_bits=TRACK_NW)
    network.add_track(track)
    print(f"    Added NW track at (14,2)")

    # (14,3): SE - connects S to E (dead end on E side)
    # Entry from S (from 14,2 NW), exit E is dead end
    track = Tile(14, 3, track_bits=TRACK_SE)
    network.add_track(track)
    print(f"    Added SE track at (14,3) - end of siding")

    # Propagate to detect dead ends
    network.propagate_from_tile(14, 3)

    print(f"  ✓ Diagonal siding created: (12,0) switch -> (14,3) end")
    print(f"    Pattern: NW at switch -> SE -> NW -> SE -> NW -> SE")

    # Note: Cannot place station on diagonal tracks currently
    # The station system requires NS or EW tracks
    print(f"    Note: Stations on diagonal tracks not yet supported")

    return network


def run_comprehensive_demo():
    """Run a demonstration of the railway network."""

    # Run validation tests first
    validate_block_rules()

    # Create network
    network = create_comprehensive_demo()

    # Add diagonal siding to test diagonal track rendering
    network = create_diagonal_siding(network)

    print("\n" + "=" * 70)
    print("Isometric Demo - Track Layout Summary")
    print("=" * 70)
    print(f"Main line: (0,0) -> (14,0) using TRACK_EW (east-west)")
    print(
        f"  Total: {len([t for t in network.tracks.values() if t.y == 0])} tracks on main line"
    )
    print(f"North branch: (3,1) -> (3,3) using TRACK_NS (north)")
    print(
        f"  Total: {len([t for t in network.tracks.values() if t.x == 3 and t.y > 0])} tracks on north branch"
    )
    print(f"Switch at (3,0) connects main line with north branch")
    print(f"East branch: (9,1) -> (9,4) using TRACK_NS (vertical)")
    print(f"  Connected via crossing at (9,0)")
    print(f"Diagonal siding: (12,0) switch -> (14,3) via NW/SE alternating")
    print(f"Stations:")
    print(f"  - Central Station at (7,0) on main line")
    print(f"  - North Station at (3,2) on north branch")
    print(f"  - East Terminal at (9,3)-(9,4) - 2-tile multi-tile station")
    print(f"Signals: Westbound at (5,0), Eastbound at (1,0), Branch Exit at (3,1)")

    # Show network visualization
    network.visualization()

    print("\n" + "=" * 70)
    print("Demo Complete")
    print("=" * 70)


def run_regression_tests():
    """Run both Python and JavaScript test suites to prevent regressions.

    This runs before starting the demo server to ensure:
    1. Backend Python tests pass (using pytest)
    2. Frontend API tests pass (using node)

    If any tests fail, the demo will not start.
    """
    import subprocess
    import sys

    print("\n" + "=" * 70)
    print("Running Regression Test Suite")
    print("=" * 70)

    # Test 1: Run Python backend tests
    print("\n[1/2] Running Python Backend Tests...")
    print("-" * 70)

    try:
        # Try to run pytest
        result = subprocess.run(
            [
                "python3",
                "-m",
                "pytest",
                "tests/test_backend.py",
                "tests/test_demo_train_journey.py",
                "-v",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )

        if result.returncode != 0:
            print("❌ Python tests FAILED!")
            print("\nOutput:")
            print(result.stdout)
            print(result.stderr)

            # Check if pytest is not installed
            if "No module named pytest" in result.stderr:
                print("\n⚠️  WARNING: pytest not installed. Skipping Python tests.")
                print("   Install with: pip install pytest")
            else:
                print("\n🛑 BLOCKING: Python tests must pass before starting server.")
                return False
        else:
            print("✅ Python tests PASSED!")
            # Show summary
            if result.stdout:
                lines = result.stdout.split("\n")
                for line in lines:
                    if "passed" in line.lower() or "failed" in line.lower():
                        print(f"   {line.strip()}")

    except subprocess.TimeoutExpired:
        print("❌ Python tests TIMEOUT!")
        print("   Tests took longer than 60 seconds.")
        return False
    except Exception as e:
        print(f"⚠️  WARNING: Could not run Python tests: {e}")
        print("   Continuing anyway...")

    # Test 2: Run JavaScript API tests (requires server to be running)
    # Note: These tests need the server running, so we skip them in pre-flight
    # They should be run separately after server starts
    print("\n[2/2] JavaScript API Tests")
    print("-" * 70)
    print("ℹ️  JavaScript API tests require server to be running.")
    print("   Run manually with: node tests/test_api.js")
    print("   (after starting the demo server)")

    print("\n" + "=" * 70)
    print("✅ Pre-flight Tests Complete - Starting Demo")
    print("=" * 70)

    return True


if __name__ == "__main__":
    # Run regression tests before starting demo
    if not run_regression_tests():
        print("\n🛑 Demo startup ABORTED due to test failures.")
        print("   Fix failing tests before proceeding.")
        import sys

        sys.exit(1)

    # If tests pass, run the demo
    run_comprehensive_demo()
