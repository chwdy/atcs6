"""
Cab switching tests - Cab switching and entry edge

Run with: pytest tests/test_trains_cab.py -v
"""

import logging
import pytest

from track import Tile, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, TrainState, HandlePosition
from train import Train, TrainCar
from train_span import SpanEndLocator, TrainSpan
from simulation import SimulationController
from train_controller import TrainController, create_controller_for_train
from path_dispatcher import PathDispatcher
from train_route import Route, RouteMode, RouteTask
from switch import Switch


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network

class TestCabSwitchEndPositionsBugFix:
    """
    Comprehensive tests for cab switching behavior (corrected 2026-02-04).

    When switching cabs (A <-> B), the physical train DOES NOT MOVE:
    - end_a.tile and end_b.tile stay at their original locations
    - The driver conceptually walks to the other cab
    - Only the head end changes (the cab selects it: A -> end_a, B -> end_b)
    - Entry edges flip for BOTH ends (train will travel opposite direction)
    - Distances flip for BOTH ends (measured from opposite edge now)
    - occupied_tiles order stays the same (train hasn't moved)
    - facing direction reverses (EAST -> WEST or vice versa)
    """

    def _setup_two_tile_train(self, network):
        for x in range(3):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=80.0)
        train = Train(train_id="CAB_SWAP_TEST", consist=[car])
        network.add_train(train)
        span = network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.commands.control_mode = "manual"  # cab selection is manual-only
        # place_train returns the span but does not seed the controller; the
        # canonical live store is controller.physics_sync.current_span.
        controller.physics_sync.current_span = span

        return train, controller

    def test_cab_switch_tiles_dont_move(self, network):
        """When switching cabs, tiles stay in place - physical train doesn't move."""
        train, controller = self._setup_two_tile_train(network)

        controller.commands.set_cab("A")
        original_end_a = controller.physics_sync.current_span.end_a.tile
        original_end_b = controller.physics_sync.current_span.end_b.tile

        controller.commands.set_cab("B")

        # Tiles should NOT change - physical train stays in place
        assert controller.commands.cab == "B"
        assert controller.physics_sync.current_span.end_a.tile == original_end_a
        assert controller.physics_sync.current_span.end_b.tile == original_end_b

    def test_cab_switch_flips_entry_edges_for_both_ends(self, network):
        """When switching cabs, entry edges flip for BOTH ends."""
        train, controller = self._setup_two_tile_train(network)

        controller.commands.set_cab("A")
        span = controller.physics_sync.current_span
        original_entry_a = span.end_a.entry_edge
        original_entry_b = span.end_b.entry_edge

        # Calculate expected flipped entries using track geometry
        track_a = network.get_track(*span.end_a.tile)
        track_b = network.get_track(*span.end_b.tile)

        expected_new_entry_a = original_entry_a
        if track_a:
            exits = track_a.get_exits(original_entry_a, use_switch_state=True)
            if exits:
                expected_new_entry_a = exits[0]

        expected_new_entry_b = original_entry_b
        if track_b:
            exits = track_b.get_exits(original_entry_b, use_switch_state=True)
            if exits:
                expected_new_entry_b = exits[0]

        controller.commands.set_cab("B")

        # BOTH ends should have flipped entry edges
        span = controller.physics_sync.current_span
        assert span.end_a.entry_edge == expected_new_entry_a
        assert span.end_b.entry_edge == expected_new_entry_b

    def test_cab_switch_preserves_occupied_tiles_order(self, network):
        """occupied_tiles order stays the same - physical train didn't move."""
        train, controller = self._setup_two_tile_train(network)

        controller.commands.set_cab("A")
        original_occupied = controller.physics_sync.current_span.occupied_tiles.copy()

        controller.commands.set_cab("B")

        # occupied_tiles should NOT change - train didn't physically move
        span = controller.physics_sync.current_span
        assert span.occupied_tiles == original_occupied
        # First tile is still end_a
        assert span.occupied_tiles[0] == span.end_a.tile
        # Last tile is still end_b
        assert span.occupied_tiles[-1] == span.end_b.tile


    def test_cab_switch_reverses_facing_direction(self, network):
        """Switching cabs reverses the train's facing direction."""
        train, controller = self._setup_two_tile_train(network)

        controller.commands.set_cab("A")
        assert controller._facing_direction() == Direction.EAST

        controller.commands.set_cab("B")
        assert controller._facing_direction() == Direction.WEST

        controller.commands.set_cab("A")
        assert controller._facing_direction() == Direction.EAST



class TestCabSwitchEntryEdgeFlipBugFix:
    """
    Comprehensive tests for cab switching entry edge reversal (corrected 2026-02-04).

    BUG: Switching cabs in manual mode flips the head end / facing direction
    but leaves entry_edges unchanged, so movement/renderer keep the old
    direction.

    FIX: On cab switch (A <-> B), flip entry_edge for BOTH ends since the train
    will now travel in the opposite direction on the track.
    """

    def _setup_straight_line(self, network, length_cm: float, head_x: int = 1):
        for x in range(-2, 4):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAB_EDGE_TEST", "LOCOMOTIVE", length_cm=length_cm)
        train = Train(train_id="CAB_EDGE_TEST", consist=[car])
        network.add_train(train)
        span = network.place_train(train, head_x, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.commands.control_mode = "manual"  # cab selection is manual-only
        controller.physics_sync.current_span = span
        return train, controller

    def _expected_reverse_entry_edge(self, network, tile, entry_edge: str) -> str:
        track = network.get_track(*tile)
        assert track is not None
        exits = track.get_exits(entry_edge, use_switch_state=True)
        assert exits
        return exits[0]

    def test_cab_switch_flips_entry_edge_single_tile(self, network):
        """Single-tile train: BOTH ends flip entry edges on cab switch."""
        train, controller = self._setup_straight_line(network, length_cm=40.0)

        controller.commands.set_cab("A")
        span = controller.physics_sync.current_span
        original_entry_a = span.end_a.entry_edge
        original_entry_b = span.end_b.entry_edge

        # Both entries should flip
        expected_entry_a = self._expected_reverse_entry_edge(
            network, span.end_a.tile, original_entry_a
        )
        expected_entry_b = self._expected_reverse_entry_edge(
            network, span.end_b.tile, original_entry_b
        )

        controller.commands.set_cab("B")

        assert controller.commands.cab == "B"
        # BOTH ends flip their entry edges
        span = controller.physics_sync.current_span
        assert span.end_a.entry_edge == expected_entry_a
        assert span.end_b.entry_edge == expected_entry_b

    def test_cab_switch_flips_entry_edge_multi_tile(self, network):
        """Multi-tile train: BOTH ends flip entry edges on cab switch."""
        train, controller = self._setup_straight_line(network, length_cm=140.0)

        controller.commands.set_cab("A")
        span = controller.physics_sync.current_span
        original_entry_a = span.end_a.entry_edge
        original_entry_b = span.end_b.entry_edge

        expected_entry_a = self._expected_reverse_entry_edge(
            network, span.end_a.tile, original_entry_a
        )
        expected_entry_b = self._expected_reverse_entry_edge(
            network, span.end_b.tile, original_entry_b
        )

        controller.commands.set_cab("B")

        assert controller.commands.cab == "B"
        # BOTH ends flip their entry edges
        span = controller.physics_sync.current_span
        assert span.end_a.entry_edge == expected_entry_a
        assert span.end_b.entry_edge == expected_entry_b



class TestSetCabDoubleFlipBugFix:
    """
    Comprehensive tests for set_cab() direction handling (regression guard
    for the double-flip bug fixed 2026-02-10).

    When switching cabs (A <-> B) the head end is reselected and the train's
    facing direction must reverse exactly once:
    - Cab "A" makes end_a the head; cab "B" makes end_b the head.
    - For a multi-tile span the facing direction is derived from tile geometry,
      so swapping the head end reverses it automatically.
    - For a single-tile span the facing direction is derived from the head's
      entry_edge + offsets, which set_cab flips for both ends.

    The earlier bug double-flipped the direction so it ended up unchanged,
    which broke backward path assignment after route completion. These tests
    pin the direction to exactly one reversal per A <-> B switch.
    """

    def _setup_multi_tile_train(self, network):
        """Create a 2-tile train on E-W track, facing EAST with cab A."""
        for x in range(5):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=80.0, max_speed=80.0)
        train = Train(train_id="FLIP_TEST", consist=[car], max_speed=80.0)
        network.add_train(train)
        span = network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.commands.control_mode = "manual"  # cab selection is manual-only
        controller.physics_sync.current_span = span
        controller.commands.set_cab("A")

        assert controller._facing_direction() == Direction.EAST
        assert controller.commands.cab == "A"
        span = controller.physics_sync.current_span
        assert span.end_a.tile != span.end_b.tile, (
            "Setup must produce a multi-tile train"
        )

        return train, controller

    def _setup_single_tile_train(self, network):
        """Create a 1-tile train on E-W track, facing EAST with cab A."""
        for x in range(3):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=20.0, max_speed=80.0)
        train = Train(train_id="FLIP_TEST_1T", consist=[car], max_speed=80.0)
        network.add_train(train)
        span = network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.commands.control_mode = "manual"  # cab selection is manual-only
        controller.physics_sync.current_span = span
        controller.commands.set_cab("A")

        assert controller._facing_direction() == Direction.EAST
        assert controller.commands.cab == "A"
        span = controller.physics_sync.current_span
        assert span.end_a.tile == span.end_b.tile, (
            "Setup must produce a single-tile train"
        )

        return train, controller

    # ---- Multi-tile tests (the original bug case) ----

    def test_multi_tile_set_cab_flips_train_facing_direction(self, network):
        """The specific case that triggered the bug: multi-tile train
        facing direction stays wrong after set_cab()."""
        train, controller = self._setup_multi_tile_train(network)

        controller.commands.set_cab("B")

        assert controller._facing_direction() == Direction.WEST, (
            f"BUG REGRESSION: facing direction should be WEST after "
            f"switching to cab B, got {controller._facing_direction()}"
        )

    def test_multi_tile_set_cab_flips_computed_direction(self, network):
        """travel_direction (edge form) also reverses to point WEST."""
        train, controller = self._setup_multi_tile_train(network)

        controller.commands.set_cab("B")

        assert controller.travel_direction == "W"

    def test_multi_tile_set_cab_facing_direction_consistent(self, network):
        """After set_cab, the enum and edge derived reads agree on WEST."""
        train, controller = self._setup_multi_tile_train(network)

        controller.commands.set_cab("B")

        # Both derived reads on the controller should agree on the direction.
        via_enum = controller._facing_direction()  # Direction enum
        via_edge = controller.travel_direction  # compass-edge form
        assert via_enum == Direction.WEST and via_edge == "W", (
            f"enum ({via_enum}) and edge ({via_edge}) should both be WEST"
        )

    def test_multi_tile_double_flip_returns_to_original(self, network):
        """Switching cab twice returns to original direction (A->B->A)."""
        train, controller = self._setup_multi_tile_train(network)

        controller.commands.set_cab("B")
        assert controller._facing_direction() == Direction.WEST

        controller.commands.set_cab("A")
        assert controller._facing_direction() == Direction.EAST

    def test_multi_tile_triple_flip(self, network):
        """Three cab switches (A->B->A->B) end correctly."""
        train, controller = self._setup_multi_tile_train(network)

        controller.commands.set_cab("B")
        assert controller._facing_direction() == Direction.WEST

        controller.commands.set_cab("A")
        assert controller._facing_direction() == Direction.EAST

        controller.commands.set_cab("B")
        assert controller._facing_direction() == Direction.WEST

    def test_multi_tile_set_cab_opposite_flips_direction(self, network):
        """Switching to the opposite cab via set_cab flips facing direction."""
        train, controller = self._setup_multi_tile_train(network)

        result = controller.commands.set_cab("B")
        assert result.success is True
        assert controller._facing_direction() == Direction.WEST
        assert controller.commands.cab == "B"

    def test_multi_tile_set_cab_opposite_twice_returns_original(self, network):
        """Two opposite-cab switches return to original direction."""
        train, controller = self._setup_multi_tile_train(network)

        controller.commands.set_cab("B")
        assert controller._facing_direction() == Direction.WEST

        controller.commands.set_cab("A")
        assert controller._facing_direction() == Direction.EAST

    # ---- Single-tile tests ----

    def test_single_tile_set_cab_flips_direction(self, network):
        """Single-tile train also flips direction on cab switch."""
        train, controller = self._setup_single_tile_train(network)

        controller.commands.set_cab("B")

        assert controller._facing_direction() == Direction.WEST, (
            f"Single-tile train should flip to WEST, got {controller._facing_direction()}"
        )

    def test_single_tile_double_flip(self, network):
        """Single-tile: A->B->A returns to original."""
        train, controller = self._setup_single_tile_train(network)

        controller.commands.set_cab("B")
        assert controller._facing_direction() == Direction.WEST

        controller.commands.set_cab("A")
        assert controller._facing_direction() == Direction.EAST

    def test_single_tile_set_cab_opposite(self, network):
        """Single-tile: switching cab via set_cab flips direction."""
        train, controller = self._setup_single_tile_train(network)

        result = controller.commands.set_cab("B")
        assert result.success is True
        assert controller._facing_direction() == Direction.WEST
        assert controller.commands.cab == "B"

    # ---- Edge cases ----

    def test_set_cab_from_none_does_not_flip(self, network):
        """Setting cab from None (first selection) should NOT flip direction."""
        for x in range(5):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=80.0, max_speed=80.0)
        train = Train(train_id="FIRST_CAB", consist=[car], max_speed=80.0)
        network.add_train(train)
        span = network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.commands.control_mode = "manual"  # cab selection is manual-only
        controller.physics_sync.current_span = span

        # active_cab is None initially
        assert controller.commands.cab is None

        # First cab selection should adopt the placed orientation, not flip it.
        controller.commands.set_cab("A")
        assert controller._facing_direction() == Direction.EAST, (
            "First cab selection should NOT flip direction"
        )

    def test_set_same_cab_does_not_flip(self, network):
        """Setting the same cab again should NOT flip direction."""
        train, controller = self._setup_multi_tile_train(network)

        controller.commands.set_cab("A")  # Already A, setting A again
        assert controller._facing_direction() == Direction.EAST, (
            "Re-selecting same cab should NOT flip direction"
        )

    def test_set_cab_fails_while_moving(self, network):
        """set_cab is rejected if train is moving."""
        train, controller = self._setup_multi_tile_train(network)

        controller.physics_sync.current_speed_kmh = 10.0  # Moving

        result = controller.commands.set_cab("B")
        assert result.success is False
        assert controller._facing_direction() == Direction.EAST, (
            "Direction should not change on failed set_cab"
        )

    def test_set_cab_none_rejected_in_running_at_station(self, network):
        """`set_cab(None)` from RUNNING_AT_STATION must reject.

        Per design_new.md "Controller state machine",
        `(RUNNING_AT_STATION, None)` is not a legal pair. Deselecting cab
        while dwelling must return success=False with an explanatory
        diagnostic and leave both `cab` and `state` unchanged.
        """
        train, controller = self._setup_multi_tile_train(network)

        # Place the train in RUNNING_AT_STATION (dwell). is_stopped() is
        # true because current_speed_kmh defaults to 0.0.
        controller.ops.state = TrainState.RUNNING_AT_STATION

        result = controller.commands.set_cab(None)

        assert result.success is False
        assert "dwell" in (result.message or "").lower()
        assert controller.commands.cab == "A", "cab must be preserved on rejection"
        assert controller.ops.state == TrainState.RUNNING_AT_STATION, (
            "state must be preserved on rejection"
        )

    def test_entry_edges_flip_with_direction(self, network):
        """Entry edges and distances also flip correctly alongside direction."""
        train, controller = self._setup_multi_tile_train(network)

        span = controller.physics_sync.current_span
        orig_a_edge = span.end_a.entry_edge
        orig_b_edge = span.end_b.entry_edge
        orig_a_dist = span.end_a.distance_cm
        orig_b_dist = span.end_b.distance_cm

        controller.commands.set_cab("B")

        # Entry edges should have flipped
        span = controller.physics_sync.current_span
        assert span.end_a.entry_edge != orig_a_edge, (
            "end_a entry edge should flip on cab switch"
        )
        assert span.end_b.entry_edge != orig_b_edge, (
            "end_b entry edge should flip on cab switch"
        )
        # Distances should have changed (tile_length - original)
        assert span.end_a.distance_cm != orig_a_dist
        assert span.end_b.distance_cm != orig_b_dist

        # Direction must be correct too
        assert controller._facing_direction() == Direction.WEST

    # ---- Real-world scenario: the actual bug trigger ----

    def test_set_cab_after_ato_stops_in_ready_state(self, network):
        """
        Real-world scenario: train arrives at terminal via ATO, route is
        cancelled, train sits in READY with a cab still selected. Switching
        to the opposite cab must succeed in this state.
        """
        train, controller = self._setup_multi_tile_train(network)

        # Simulate: train was running, ATO stopped, route cancelled -> READY
        controller.ops.state = TrainState.READY
        controller.ato.running = False
        controller.physics_sync.current_speed_kmh = 0.0

        # Active cab is still A (route cancellation doesn't clear cab)
        assert controller.commands.cab == "A"

        result = controller.commands.set_cab("B")
        assert result.success is True, (
            f"set_cab(\"B\") should work from READY state with cab selected, "
            f"controller.ops.state={controller.ops.state}"
        )
        assert controller._facing_direction() == Direction.WEST
        assert controller.commands.cab == "B"



