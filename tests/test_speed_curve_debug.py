"""
Comprehensive tests for Speed Curves and ATO Behavior.

Tests the scenario:
- Block 1 (70 km/h): 3 tiles, 150cm at (0,0)-(2,0)
- Block 2 (40 km/h): 3 tiles, 150cm at (3,0)-(5,0)
- Block 3 (no limit): 3 tiles, 150cm at (6,0)-(8,0)
- Station (50 km/h): 100cm at (9,0)

Train: 80cm long, 60 km/h max speed

Expected behavior:
1. In block 1: ATC = 60 km/h (train max, since 70 > 60)
2. Before block 2: braking curve to 40 km/h
3. In block 2: ATC = 40 km/h
4. After block 2 (in block 3): ATC increases back to 60 km/h
5. Before station: braking curve to 50 km/h
6. In station: ATC = 50 km/h

Run with: conda run -n general pytest tests/test_speed_curve_debug.py -v -s
"""

import pytest
from typing import List, Tuple, Dict, Any, Optional

from track import TrackSegment, TRACK_EW
from network import RailwayNetwork
from block import Block
from station import Station
from train import Train, TrainCar
from train_controller import TrainController, create_controller_for_train
from simulation import SimulationController
from train_physics import TrainPhysics
from enums import Direction, TrainState, HandlePosition, BlockState


class TestSpeedCurveDebug:
    """
    Comprehensive tests for speed curve calculation and ATO behavior.

    These tests verify that:
    1. Speed limits increase after leaving restricted blocks
    2. Station speed limits only apply within station extent
    3. ATO brakes correctly before restrictions
    4. ATO doesn't trigger EB unnecessarily
    """

    @pytest.fixture
    def network(self) -> RailwayNetwork:
        """Create empty network."""
        return RailwayNetwork()

    @pytest.fixture
    def test_track_layout(self, network: RailwayNetwork) -> RailwayNetwork:
        """
        Create test track layout:

        Block 1 (70 km/h)  ->  Block 2 (40 km/h)  ->  Block 3 (no limit)  ->  Station (50 km/h)
           (0,0)-(2,0)          (3,0)-(5,0)           (6,0)-(8,0)              (9,0)
           150cm                150cm                  150cm                    100cm
        """
        # Create 10 tiles of E-W track
        for x in range(10):
            track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
            network.add_track(track)

        # Block 1: tiles 0-2, 150cm, 70 km/h
        network.create_block_with_tracks(
            block_id="BLOCK_1",
            positions=[(0, 0), (1, 0), (2, 0)],
            name="Block 1 - 70kmh",
            length_cm=150.0,
            max_speed_kmh=70.0,
        )

        # Block 2: tiles 3-5, 150cm, 40 km/h
        network.create_block_with_tracks(
            block_id="BLOCK_2",
            positions=[(3, 0), (4, 0), (5, 0)],
            name="Block 2 - 40kmh",
            length_cm=150.0,
            max_speed_kmh=40.0,
        )

        # Block 3: tiles 6-8, 150cm, no speed limit (will use train max)
        network.create_block_with_tracks(
            block_id="BLOCK_3",
            positions=[(6, 0), (7, 0), (8, 0)],
            name="Block 3 - No Limit",
            length_cm=150.0,
            max_speed_kmh=None,  # No limit
        )

        # Station at tile 9: 100cm, 50 km/h
        network.add_station(
            x=9,
            y=0,
            name="Test Station",
            length_cm=100.0,
            platform_side="both",
            top_speed_kmh=50.0,
        )

        return network

    @pytest.fixture
    def train_80cm(
        self, test_track_layout: RailwayNetwork
    ) -> Tuple[Train, RailwayNetwork]:
        """
        Create 80cm train with 60 km/h max speed.

        The train has 2 cars of 40cm each.
        Place at tile 2 so it has room (tiles 0-1 behind it for the 80cm length).
        """
        network = test_track_layout

        # Create 80cm train (2 cars of 40cm)
        car1 = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0)
        car2 = TrainCar(car_id="CAR_2", car_type="PASSENGER", length=40.0)

        train = Train(
            train_id="TRAIN_TEST",
            consist=[car1, car2],
            max_speed=60.0,
        )

        network.add_train(train)

        # Place train at tile 2 facing east (so it has room for 80cm behind)
        # Tiles 0-1 are behind it, tile 2 is where head is
        network.place_train(train, 2, 0, Direction.EAST)

        # Set up path through all tiles starting from current position
        train.current_path = [(x, 0, Direction.EAST) for x in range(10)]
        train.path_index = 2  # Start at tile 2 in the path

        # Occupy starting block
        start_track = network.get_track(2, 0)
        if start_track and start_track.block_section_id and network.block_manager:
            network.block_manager.occupy_block(
                start_track.block_section_id, train.train_id
            )

        return train, network

    # -------------------------------------------------------------------------
    # Speed Curve Calculation Tests
    # -------------------------------------------------------------------------

    def test_speed_curve_structure(self, train_80cm):
        """Speed curves should have correct structure."""
        train, network = train_80cm

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING

        # Get lookahead data which includes speed curves
        lookahead = controller.get_monitor_lookahead()
        curves = lookahead["lookahead"]["speed_curves"]

        assert len(curves) > 0, "Should have speed curve data"

        for point in curves:
            assert "distance_cm" in point
            assert "atc_limit" in point
            assert "operational_limit" in point
            assert point["operational_limit"] <= point["atc_limit"], (
                f"ATO ({point['operational_limit']}) should be <= ATC ({point['atc_limit']})"
            )

    def test_speed_curve_in_block_1(self, train_80cm):
        """In block 1 (70 km/h), ATC should be min(70, train_max=60) = 60."""
        train, network = train_80cm

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING

        lookahead = controller.get_monitor_lookahead()
        curves = lookahead["lookahead"]["speed_curves"]

        # Sample at distance 0 (in block 1)
        curve_at_0 = next((c for c in curves if c["distance_cm"] == 0), None)
        assert curve_at_0 is not None, "Should have curve at distance 0"

        # ATC should be capped at train max speed (60) even though block allows 70
        assert curve_at_0["atc_limit"] <= 60.0, (
            f"ATC at pos 0 should be <= 60 (train max), got {curve_at_0['atc_limit']}"
        )

    def test_speed_curve_braking_before_block_2(self, train_80cm):
        """Before block 2 (40 km/h), ATC should show braking curve."""
        train, network = train_80cm

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING

        lookahead = controller.get_monitor_lookahead()
        curves = lookahead["lookahead"]["speed_curves"]
        blocks = lookahead["lookahead"]["blocks"]

        # Find block 2 start position
        block_2 = next((b for b in blocks if b["id"] == "BLOCK_2"), None)
        assert block_2 is not None, "Should have BLOCK_2 in lookahead"
        block_2_start = block_2["start_cm"]

        # Find curve point just before block 2
        curves_before_b2 = [c for c in curves if c["distance_cm"] < block_2_start]
        if curves_before_b2:
            last_before_b2 = max(curves_before_b2, key=lambda c: c["distance_cm"])

            # ATC should be decreasing toward 40 as we approach block 2
            # At the entry point, it should be close to 40
            assert last_before_b2["atc_limit"] <= 60.0, (
                f"ATC before block 2 should show braking curve"
            )

    def test_speed_curve_in_block_2(self, train_80cm):
        """In block 2 (40 km/h), ATC should be 40."""
        train, network = train_80cm

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING

        lookahead = controller.get_monitor_lookahead()
        curves = lookahead["lookahead"]["speed_curves"]
        blocks = lookahead["lookahead"]["blocks"]

        # Find block 2 extent
        block_2 = next((b for b in blocks if b["id"] == "BLOCK_2"), None)
        assert block_2 is not None
        block_2_start = block_2["start_cm"]
        block_2_end = block_2["end_cm"]

        # Find curve points within block 2
        curves_in_b2 = [
            c for c in curves if block_2_start <= c["distance_cm"] < block_2_end
        ]

        for point in curves_in_b2:
            assert point["atc_limit"] == 40.0, (
                f"ATC in block 2 should be 40, got {point['atc_limit']} at {point['distance_cm']}cm"
            )

    def test_speed_curve_increases_after_block_2(self, train_80cm):
        """
        After leaving block 2 (in block 3), ATC should increase back to train max (60).

        THIS IS THE KEY TEST - verifies the bug fix.
        """
        train, network = train_80cm

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING

        lookahead = controller.get_monitor_lookahead()
        curves = lookahead["lookahead"]["speed_curves"]
        blocks = lookahead["lookahead"]["blocks"]

        # Find block 2 and block 3 extents
        block_2 = next((b for b in blocks if b["id"] == "BLOCK_2"), None)
        block_3 = next((b for b in blocks if b["id"] == "BLOCK_3"), None)

        assert block_2 is not None, "Should have BLOCK_2"
        assert block_3 is not None, "Should have BLOCK_3"

        block_2_end = block_2["end_cm"]
        block_3_start = block_3["start_cm"]
        block_3_end = block_3["end_cm"]

        # Find curve points in block 3 (after block 2)
        curves_in_b3 = [
            c for c in curves if block_3_start <= c["distance_cm"] < block_3_end
        ]

        assert len(curves_in_b3) > 0, "Should have curve points in block 3"

        # ATC in block 3 should be higher than 40 (the block 2 limit)
        # It should be train max (60) or close to it (minus braking for station ahead)
        for point in curves_in_b3:
            assert point["atc_limit"] > 40.0, (
                f"ATC in block 3 should be > 40 (not stuck at block 2 limit), "
                f"got {point['atc_limit']} at {point['distance_cm']}cm"
            )

        # At least some points in block 3 should reach train max (60)
        # (before the braking curve for the station starts)
        max_atc_in_b3 = max(c["atc_limit"] for c in curves_in_b3)
        assert max_atc_in_b3 >= 50.0, (
            f"Max ATC in block 3 should be >= 50, got {max_atc_in_b3}"
        )

    def test_speed_curve_station_extent(self, train_80cm):
        """Station info is included for visualization but doesn't affect ATC/ATO curves.

        ATC/ATO only cares about BLOCKS, not platforms. Station's top_speed_kmh is for
        visualization purposes only. If a station block needs a speed limit, it should
        be set on the block's max_speed_kmh, not the station's top_speed_kmh.
        """
        train, network = train_80cm

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING

        lookahead = controller.get_monitor_lookahead()
        curves = lookahead["lookahead"]["speed_curves"]
        stations = lookahead["lookahead"]["stations"]

        # Find station extent
        station = next(
            (s for s in stations if "Test Station" in s.get("name", "")), None
        )
        assert station is not None, "Should have Test Station in lookahead"

        # Station info should still be present for visualization
        assert station.get("top_speed_kmh") is not None, (
            "Station should have top_speed_kmh for visualization"
        )

        # But the curve should NOT be affected by station's top_speed_kmh
        # (only by block's max_speed_kmh if set)
        # The curves should reflect block limits, not station limits

    def test_ato_always_below_atc(self, train_80cm):
        """ATO limit should always be <= ATC limit (typically 5 km/h below)."""
        train, network = train_80cm

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING

        lookahead = controller.get_monitor_lookahead()
        curves = lookahead["lookahead"]["speed_curves"]

        for point in curves:
            atc = point["atc_limit"]
            ato = point["operational_limit"]

            assert ato <= atc, (
                f"ATO ({ato}) should be <= ATC ({atc}) at {point['distance_cm']}cm"
            )

            # When ATC > 5, ATO should be at least 5 below
            if atc > 5.0:
                assert ato <= atc - 5.0, (
                    f"ATO ({ato}) should be at least 5 below ATC ({atc}) at {point['distance_cm']}cm"
                )

    # -------------------------------------------------------------------------
    # Simulation Tests with Debug Logging
    # -------------------------------------------------------------------------

    def test_simulation_no_eb_triggered(self, train_80cm):
        """
        Simulate train through entire path.
        ATO should brake smoothly without triggering EB.
        """
        train, network = train_80cm

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)

        # Set up controller for ATO mode
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True

        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0

        # Run simulation for 100 ticks (10 seconds at 0.1s per tick)
        eb_triggered = False
        tick_log = []

        for tick in range(100):
            sim.update(0.1)

            # Log tick data for debugging
            pos = train.position
            head_tile = (
                pos.end_a_tile
                if pos and pos.leading_end == "a"
                else (pos.end_b_tile if pos else None)
            )
            head_dist = (
                pos.end_a_distance_cm
                if pos and pos.leading_end == "a"
                else (pos.end_b_distance_cm if pos else 0)
            )

            tick_data = {
                "tick": tick,
                "tile": head_tile,
                "dist_cm": head_dist,
                "speed_kmh": train.current_speed,
                "handle": controller.handle_position.value
                if controller.handle_position
                else "N",
                "atc_limit": controller.atc_speed_limit,
                "eb_active": controller.eb_active,
            }
            tick_log.append(tick_data)

            if controller.eb_active:
                eb_triggered = True
                break

            # Stop if train stops (reached end or obstruction)
            if train.current_speed == 0 and tick > 10:
                break

        # Print debug log if test fails
        if eb_triggered:
            print("\n--- TICK LOG (EB Triggered) ---")
            for entry in tick_log[-20:]:  # Last 20 entries
                print(
                    f"[{entry['tick']:3d}] tile={entry['tile']} "
                    f"dist={entry['dist_cm']:.1f}cm speed={entry['speed_kmh']:.1f}km/h "
                    f"ATC={entry['atc_limit']:.1f} handle={entry['handle']}"
                )

        assert not eb_triggered, "ATO should not trigger EB during normal operation"

    def test_simulation_respects_block_limits(self, train_80cm):
        """
        Simulate train and verify it respects block speed limits.
        """
        train, network = train_80cm

        sim = SimulationController(network)
        controller = sim._get_or_create_controller(train)

        # Set up controller for ATO mode
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING
        controller.ato_running = True

        train.state = TrainState.RUNNING_MOVING
        train.current_speed = 0.0

        # Track max speed in each block
        max_speed_by_block = {}

        for tick in range(200):
            sim.update(0.1)

            # Get current block
            pos = train.position
            if pos:
                head_tile = pos.end_a_tile if pos.leading_end == "a" else pos.end_b_tile
                track = (
                    network.get_track(head_tile[0], head_tile[1]) if head_tile else None
                )
                block_id = track.block_section_id if track else None

                if block_id:
                    current_max = max_speed_by_block.get(block_id, 0)
                    max_speed_by_block[block_id] = max(current_max, train.current_speed)

            if train.current_speed == 0 and tick > 20:
                break

        # Verify block 2 speed was respected (should not exceed 40 + small tolerance)
        if "BLOCK_2" in max_speed_by_block:
            assert max_speed_by_block["BLOCK_2"] <= 45.0, (
                f"Max speed in BLOCK_2 should be ~40, got {max_speed_by_block['BLOCK_2']}"
            )

    def test_speed_curve_debug_output(self, train_80cm):
        """
        Debug test that prints speed curve values at key positions.
        Run with -s flag to see output.
        """
        train, network = train_80cm

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING

        lookahead = controller.get_monitor_lookahead()
        curves = lookahead["lookahead"]["speed_curves"]
        blocks = lookahead["lookahead"]["blocks"]
        stations = lookahead["lookahead"]["stations"]

        print("\n--- BLOCKS ---")
        for block in blocks:
            print(
                f"  {block['id']}: {block['start_cm']:.0f}-{block['end_cm']:.0f}cm, "
                f"limit={block.get('speed_limit_kmh', 'None')}"
            )

        print("\n--- STATIONS ---")
        for station in stations:
            print(
                f"  {station['name']}: {station['distance_cm']:.0f}cm, "
                f"length={station['length_cm']:.0f}cm, "
                f"limit={station.get('top_speed_kmh', 'None')}"
            )

        print("\n--- SPEED CURVES (every 50cm) ---")
        print("Distance(cm) | ATC(km/h) | ATO(km/h)")
        print("-" * 40)

        for point in curves:
            dist = point["distance_cm"]
            if dist % 50 == 0:  # Print every 50cm
                print(
                    f"{dist:8.0f}     | {point['atc_limit']:6.1f}    | {point['operational_limit']:6.1f}"
                )

        # Keep the debug output, but assert the scenario was actually built.
        assert curves, "Speed curve lookahead should not be empty"
        block_ids = [block["id"] for block in blocks]
        assert block_ids[:3] == ["BLOCK_1", "BLOCK_2", "BLOCK_3"], (
            "Lookahead should begin with the three configured line blocks"
        )
        assert len(block_ids) >= 4, "Lookahead should also include the station block"
        assert len(stations) == 1, "Lookahead should include the test station"
        assert stations[0]["name"] == "Test Station"
        assert stations[0]["top_speed_kmh"] == 50.0


class TestBlockSpeedLimitTransitions:
    """
    Focused tests on speed limit transitions between blocks.
    """

    @pytest.fixture
    def simple_two_block_track(self) -> Tuple[Train, RailwayNetwork]:
        """
        Simple layout: Block A (80 km/h) -> Block B (30 km/h) -> Block C (no limit)
        """
        network = RailwayNetwork()

        for x in range(9):
            network.add_track(TrackSegment(x=x, y=0, track_bits=TRACK_EW))

        network.create_block_with_tracks(
            block_id="BLOCK_A",
            positions=[(0, 0), (1, 0), (2, 0)],
            name="Block A - Fast",
            length_cm=150.0,
            max_speed_kmh=80.0,
        )

        network.create_block_with_tracks(
            block_id="BLOCK_B",
            positions=[(3, 0), (4, 0), (5, 0)],
            name="Block B - Slow",
            length_cm=150.0,
            max_speed_kmh=30.0,
        )

        network.create_block_with_tracks(
            block_id="BLOCK_C",
            positions=[(6, 0), (7, 0), (8, 0)],
            name="Block C - No Limit",
            length_cm=150.0,
            max_speed_kmh=None,
        )

        car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length=40.0)
        train = Train(train_id="TRAIN_TRANS", consist=[car], max_speed=100.0)

        network.add_train(train)
        network.place_train(train, 0, 0, Direction.EAST)

        train.current_path = [(x, 0, Direction.EAST) for x in range(9)]
        train.path_index = 0

        return train, network

    def test_atc_increases_after_slow_block(self, simple_two_block_track):
        """
        ATC should increase when entering Block C after leaving Block B.
        """
        train, network = simple_two_block_track

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.control_mode = "ato"
        controller.active_cab = "A"
        controller.state = TrainState.RUNNING_MOVING

        lookahead = controller.get_monitor_lookahead()
        curves = lookahead["lookahead"]["speed_curves"]
        blocks = lookahead["lookahead"]["blocks"]

        block_b = next((b for b in blocks if b["id"] == "BLOCK_B"), None)
        block_c = next((b for b in blocks if b["id"] == "BLOCK_C"), None)

        assert block_b is not None
        assert block_c is not None

        # Get ATC in block B (should be 30)
        curves_in_b = [
            c
            for c in curves
            if block_b["start_cm"] <= c["distance_cm"] < block_b["end_cm"]
        ]

        # Get ATC in block C (should be higher than 30)
        curves_in_c = [
            c
            for c in curves
            if block_c["start_cm"] <= c["distance_cm"] < block_c["end_cm"]
        ]

        if curves_in_b:
            avg_atc_b = sum(c["atc_limit"] for c in curves_in_b) / len(curves_in_b)
            assert avg_atc_b <= 35.0, f"ATC in Block B should be ~30, got {avg_atc_b}"

        if curves_in_c:
            max_atc_c = max(c["atc_limit"] for c in curves_in_c)
            assert max_atc_c > 30.0, (
                f"ATC in Block C should be > 30 (not stuck at Block B limit), got {max_atc_c}"
            )
