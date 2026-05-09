"""
Cab switching tests - Cab switching and entry edge

Run with: pytest tests/test_trains_cab.py -v
"""

import logging
import pytest

from track import TrackSegment, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, LeadingEnd, TrainState, HandlePosition
from train import Train, TrainCar, TrainPosition
from tests.train_test_helpers import make_train_position
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
        track = TrackSegment(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network

class TestCabSwitchEndPositionsBugFix:
    """
    Comprehensive tests for cab switching behavior (corrected 2026-02-04).

    When switching cabs (A <-> B), the physical train DOES NOT MOVE:
    - end_a_tile and end_b_tile stay at their original locations
    - The driver conceptually walks to the other cab
    - Only leading_end changes (from "a" to "b" or vice versa)
    - Entry edges flip for BOTH ends (train will travel opposite direction)
    - Distances flip for BOTH ends (measured from opposite edge now)
    - occupied_tiles order stays the same (train hasn't moved)
    - facing_direction reverses (EAST -> WEST or vice versa)
    """

    def _setup_two_tile_train(self, network):
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=80.0)
        train = Train(train_id="CAB_SWAP_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True

        return train, controller

    def test_cab_switch_tiles_dont_move(self, network):
        """When switching cabs, tiles stay in place - physical train doesn't move."""
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        original_end_a = train.position.end_a_tile
        original_end_b = train.position.end_b_tile

        controller.set_cab("B")

        # Tiles should NOT change - physical train stays in place
        assert train.position.leading_end == LeadingEnd.B
        assert train.position.end_a_tile == original_end_a  # unchanged
        assert train.position.end_b_tile == original_end_b  # unchanged

    def test_cab_switch_flips_entry_edges_for_both_ends(self, network):
        """When switching cabs, entry edges flip for BOTH ends."""
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        original_entry_a = train.position.end_a_entry_edge
        original_entry_b = train.position.end_b_entry_edge

        # Calculate expected flipped entries using track geometry
        track_a = network.get_track(*train.position.end_a_tile)
        track_b = network.get_track(*train.position.end_b_tile)

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

        controller.set_cab("B")

        # BOTH ends should have flipped entry edges
        assert train.position.end_a_entry_edge == expected_new_entry_a
        assert train.position.end_b_entry_edge == expected_new_entry_b

    def test_cab_switch_preserves_occupied_tiles_order(self, network):
        """occupied_tiles order stays the same - physical train didn't move."""
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        original_occupied = train.position.occupied_tiles.copy()

        controller.set_cab("B")

        # occupied_tiles should NOT change - train didn't physically move
        assert train.position.occupied_tiles == original_occupied
        # First tile is still end_a
        assert train.position.occupied_tiles[0] == train.position.end_a_tile
        # Last tile is still end_b
        assert train.position.occupied_tiles[-1] == train.position.end_b_tile


    def test_cab_switch_reverses_facing_direction(self, network):
        """Switching cabs reverses facing_direction."""
        train, controller = self._setup_two_tile_train(network)

        controller.set_cab("A")
        assert train.position.facing_direction == Direction.EAST

        controller.set_cab("B")
        assert train.position.facing_direction == Direction.WEST

        controller.set_cab("A")
        assert train.position.facing_direction == Direction.EAST



class TestCabSwitchEntryEdgeFlipBugFix:
    """
    Comprehensive tests for cab switching entry edge reversal (corrected 2026-02-04).

    BUG: Switching cabs in manual mode flips leading_end/facing_direction but
    leaves entry_edges unchanged, so movement/renderer keep the old direction.

    FIX: On cab switch (A <-> B), flip entry_edge for BOTH ends since the train
    will now travel in the opposite direction on the track.
    """

    def _setup_straight_line(self, network, length_cm: float, head_x: int = 1):
        for x in range(-2, 4):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAB_EDGE_TEST", "LOCOMOTIVE", length_cm=length_cm)
        train = Train(train_id="CAB_EDGE_TEST", consist=[car])
        network.add_train(train)
        network.place_train(train, head_x, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
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

        controller.set_cab("A")
        original_entry_a = train.position.end_a_entry_edge
        original_entry_b = train.position.end_b_entry_edge

        # Both entries should flip
        expected_entry_a = self._expected_reverse_entry_edge(
            network, train.position.end_a_tile, original_entry_a
        )
        expected_entry_b = self._expected_reverse_entry_edge(
            network, train.position.end_b_tile, original_entry_b
        )

        controller.set_cab("B")

        assert train.position.leading_end == LeadingEnd.B
        # BOTH ends flip their entry edges
        assert train.position.end_a_entry_edge == expected_entry_a
        assert train.position.end_b_entry_edge == expected_entry_b

    def test_cab_switch_flips_entry_edge_multi_tile(self, network):
        """Multi-tile train: BOTH ends flip entry edges on cab switch."""
        train, controller = self._setup_straight_line(network, length_cm=140.0)

        controller.set_cab("A")
        original_entry_a = train.position.end_a_entry_edge
        original_entry_b = train.position.end_b_entry_edge

        expected_entry_a = self._expected_reverse_entry_edge(
            network, train.position.end_a_tile, original_entry_a
        )
        expected_entry_b = self._expected_reverse_entry_edge(
            network, train.position.end_b_tile, original_entry_b
        )

        controller.set_cab("B")

        assert train.position.leading_end == LeadingEnd.B
        # BOTH ends flip their entry edges
        assert train.position.end_a_entry_edge == expected_entry_a
        assert train.position.end_b_entry_edge == expected_entry_b



class TestSetCabDoubleFlipBugFix:
    """
    Comprehensive tests for set_cab() double-flip bug fixed 2026-02-10.

    BUG: TrainController.set_cab() had two conflicting operations when switching
    cabs (A <-> B):

    1. It synced train.facing_direction from the computed property
       train.position.facing_direction — which already returned the CORRECT
       new direction because leading_end was already changed by train.set_cab().

    2. It then explicitly flipped train.facing_direction to its opposite —
       which UNDID the correct direction from step 1, putting it back to the
       original direction.

    For multi-tile trains, the computed TrainPosition.facing_direction property
    derives direction from leading_end + tile geometry. After leading_end changes,
    it automatically returns the reversed direction. The explicit flip was
    redundant and destructive.

    FIX: Replaced the sync-then-flip logic with direction-aware handling:
    - Multi-tile trains (end_a_tile != end_b_tile): Use the computed property
      directly — it already returns the correct direction.
    - Single-tile trains (end_a_tile == end_b_tile): Explicitly flip the legacy
      direction field since the computed property falls back to it.

    This bug caused turn_around() (which delegates to set_cab()) to silently
    "succeed" but leave the train facing the SAME direction it was before,
    breaking backward path assignment after route completion.
    """

    def _setup_multi_tile_train(self, network):
        """Create a 2-tile train on E-W track, facing EAST with cab A."""
        for x in range(5):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=80.0, max_speed=80.0)
        train = Train(train_id="FLIP_TEST", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.set_cab("A")

        assert train.facing_direction == Direction.EAST
        assert controller.active_cab == "A"
        assert train.position.end_a_tile != train.position.end_b_tile, (
            "Setup must produce a multi-tile train"
        )

        return train, controller

    def _setup_single_tile_train(self, network):
        """Create a 1-tile train on E-W track, facing EAST with cab A."""
        for x in range(3):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=20.0, max_speed=80.0)
        train = Train(train_id="FLIP_TEST_1T", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True
        controller.set_cab("A")

        assert train.facing_direction == Direction.EAST
        assert controller.active_cab == "A"
        assert train.position.end_a_tile == train.position.end_b_tile, (
            "Setup must produce a single-tile train"
        )

        return train, controller

    # ---- Multi-tile tests (the original bug case) ----

    def test_multi_tile_set_cab_flips_train_facing_direction(self, network):
        """The specific case that triggered the bug: multi-tile train
        facing_direction stays wrong after set_cab()."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("B")

        assert train.facing_direction == Direction.WEST, (
            f"BUG REGRESSION: train.facing_direction should be WEST after "
            f"switching to cab B, got {train.facing_direction}"
        )

    def test_multi_tile_set_cab_flips_computed_direction(self, network):
        """Computed property on TrainPosition also returns correct direction."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("B")

        assert train.position.facing_direction == Direction.WEST

    def test_multi_tile_set_cab_facing_direction_consistent(self, network):
        """After set_cab, Train.facing_direction delegates to position correctly."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("B")

        # Both access paths should return the same direction
        via_train = train.facing_direction  # Reads Train.facing_direction property
        via_position = train.position.facing_direction  # Reads TrainPosition property
        assert via_train == via_position == Direction.WEST, (
            f"Train ({via_train}) and position ({via_position}) should both be WEST"
        )

    def test_multi_tile_double_flip_returns_to_original(self, network):
        """Switching cab twice returns to original direction (A->B->A)."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("B")
        assert train.facing_direction == Direction.WEST

        controller.set_cab("A")
        assert train.facing_direction == Direction.EAST

    def test_multi_tile_triple_flip(self, network):
        """Three cab switches (A->B->A->B) end correctly."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("B")
        assert train.facing_direction == Direction.WEST

        controller.set_cab("A")
        assert train.facing_direction == Direction.EAST

        controller.set_cab("B")
        assert train.facing_direction == Direction.WEST

    def test_multi_tile_turn_around_flips_direction(self, network):
        """turn_around() delegates to set_cab() and correctly flips direction."""
        train, controller = self._setup_multi_tile_train(network)

        result = controller.turn_around()
        assert result is True, "turn_around() should succeed"
        assert train.facing_direction == Direction.WEST
        assert controller.active_cab == "B"

    def test_multi_tile_turn_around_twice_returns_original(self, network):
        """Two turn_around() calls return to original direction."""
        train, controller = self._setup_multi_tile_train(network)

        controller.turn_around()
        assert train.facing_direction == Direction.WEST

        controller.turn_around()
        assert train.facing_direction == Direction.EAST

    # ---- Single-tile tests ----

    def test_single_tile_set_cab_flips_direction(self, network):
        """Single-tile train also flips direction on cab switch."""
        train, controller = self._setup_single_tile_train(network)

        controller.set_cab("B")

        assert train.facing_direction == Direction.WEST, (
            f"Single-tile train should flip to WEST, got {train.facing_direction}"
        )

    def test_single_tile_double_flip(self, network):
        """Single-tile: A->B->A returns to original."""
        train, controller = self._setup_single_tile_train(network)

        controller.set_cab("B")
        assert train.facing_direction == Direction.WEST

        controller.set_cab("A")
        assert train.facing_direction == Direction.EAST

    def test_single_tile_turn_around(self, network):
        """Single-tile: turn_around() works correctly."""
        train, controller = self._setup_single_tile_train(network)

        result = controller.turn_around()
        assert result is True
        assert train.facing_direction == Direction.WEST
        assert controller.active_cab == "B"

    # ---- Edge cases ----

    def test_set_cab_from_none_does_not_flip(self, network):
        """Setting cab from None (first selection) should NOT flip direction."""
        for x in range(5):
            network.add_track(TrackSegment(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=80.0, max_speed=80.0)
        train = Train(train_id="FIRST_CAB", consist=[car], max_speed=80.0)
        network.add_train(train)
        network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.power_on = True

        # active_cab is None initially
        assert controller.active_cab is None

        # First cab selection should sync from computed, not flip
        controller.set_cab("A")
        assert train.facing_direction == Direction.EAST, (
            "First cab selection should NOT flip direction"
        )

    def test_set_same_cab_does_not_flip(self, network):
        """Setting the same cab again should NOT flip direction."""
        train, controller = self._setup_multi_tile_train(network)

        controller.set_cab("A")  # Already A, setting A again
        assert train.facing_direction == Direction.EAST, (
            "Re-selecting same cab should NOT flip direction"
        )

    def test_turn_around_fails_while_moving(self, network):
        """turn_around() should fail if train is moving."""
        train, controller = self._setup_multi_tile_train(network)

        controller.current_speed_kmh = 10.0  # Moving

        result = controller.turn_around()
        assert result is False
        assert train.facing_direction == Direction.EAST, (
            "Direction should not change on failed turn_around"
        )

    def test_turn_around_fails_while_ato_running(self, network):
        """turn_around() should fail if ATO is actively running."""
        train, controller = self._setup_multi_tile_train(network)

        controller.ato_running = True

        result = controller.turn_around()
        assert result is False
        assert train.facing_direction == Direction.EAST

    def test_entry_edges_flip_with_direction(self, network):
        """Entry edges and distances also flip correctly alongside direction."""
        train, controller = self._setup_multi_tile_train(network)

        orig_a_edge = train.position.end_a_entry_edge
        orig_b_edge = train.position.end_b_entry_edge
        orig_a_dist = train.position.end_a_distance_cm
        orig_b_dist = train.position.end_b_distance_cm

        controller.set_cab("B")

        # Entry edges should have flipped
        assert train.position.end_a_entry_edge != orig_a_edge, (
            "end_a entry edge should flip on cab switch"
        )
        assert train.position.end_b_entry_edge != orig_b_edge, (
            "end_b entry edge should flip on cab switch"
        )
        # Distances should have changed (tile_length - original)
        assert train.position.end_a_distance_cm != orig_a_dist
        assert train.position.end_b_distance_cm != orig_b_dist

        # Direction must be correct too
        assert train.facing_direction == Direction.WEST

    # ---- Real-world scenario: the actual bug trigger ----

    def test_turn_around_after_ato_stops_in_idle_state(self, network):
        """
        Real-world scenario: train arrives at terminal via ATO, route is
        cancelled (train goes to IDLE), then turn_around() is called.

        This was the actual bug trigger: after ATO stops and route is
        cancelled, the train is in IDLE state with a cab still selected.
        turn_around() -> set_cab() must work in this state.
        """
        train, controller = self._setup_multi_tile_train(network)

        # Simulate: train was running, ATO stopped, route cancelled -> IDLE
        controller.state = TrainState.READY
        train.state = TrainState.READY
        controller.ato_running = False
        controller.current_speed_kmh = 0.0

        # Active cab is still A (route cancellation doesn't clear cab)
        assert controller.active_cab == "A"

        result = controller.turn_around()
        assert result is True, (
            f"turn_around() should work from IDLE state with cab selected, "
            f"train.state={train.state}"
        )
        assert train.facing_direction == Direction.WEST
        assert controller.active_cab == "B"



