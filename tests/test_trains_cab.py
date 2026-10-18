"""
Head-frame re-expression tests — the span frame follows the head end.

The operator cab is gone: the head end is derived from the live authority
(design_new.md "Authority-side head derivation"), and the single writer of the
framed head is `TrainController._apply_head_frame("end_a"/"end_b")`. Re-framing
the head to the other physical span end re-expresses both end locators (entry
edges and distances) into the new head-forward travel frame, without moving the
physical train. These tests pin that geometry, formerly driven through the
removed `set_cab` command.

Run with: pytest tests/test_trains_cab.py -v
"""

import logging
from types import SimpleNamespace

import pytest

from track import Tile, TRACK_NS, TRACK_EW, TRACK_NW
from network import RailwayNetwork
from block import Block
from enums import BlockState, Direction, HandlePosition
from train import Train, TrainCar
from train_span import SpanEndLocator, TrainSpan
from simulation import SimulationController
from train_controller import TrainController, create_controller_for_train
from path_dispatcher import PathDispatcher
from train_route import Route, RouteMode, RouteTask
from switch import Switch


def _give_authority(controller) -> None:
    """Make ``received_path_plan`` read non-None by stubbing the dispatcher, so
    the authority-gated ``travel_direction`` derives from the span frame. An
    empty ``resource_sequence`` is enough to lift the no-authority gate."""
    fake_path = SimpleNamespace(path_id="P", resource_sequence=[])
    controller.path_dispatcher = SimpleNamespace(
        get_active_path=lambda _train_id: fake_path
    )


@pytest.fixture
def network():
    return RailwayNetwork()


@pytest.fixture
def straight_ew_track(network):
    for x in range(5):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        network.add_track(track)
    return network

class TestHeadFrameEndPositions:
    """
    Re-framing the head end (end_a <-> end_b) does NOT move the physical train:
    - end_a.tile and end_b.tile stay at their original locations
    - Only the head end changes (end_a <-> end_b)
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
        # place_train returns the span but does not seed the controller; the
        # canonical live store is controller.physics_sync.current_span.
        controller.physics_sync.current_span = span

        return train, controller

    def test_reframe_tiles_dont_move(self, network):
        """Re-framing the head keeps tiles in place - physical train doesn't move."""
        train, controller = self._setup_two_tile_train(network)

        controller._apply_head_frame("end_a")
        original_end_a = controller.physics_sync.current_span.end_a.tile
        original_end_b = controller.physics_sync.current_span.end_b.tile

        controller._apply_head_frame("end_b")

        # Tiles should NOT change - physical train stays in place
        assert controller._framed_head_end == "end_b"
        assert controller.physics_sync.current_span.end_a.tile == original_end_a
        assert controller.physics_sync.current_span.end_b.tile == original_end_b

    def test_reframe_flips_entry_edges_for_both_ends(self, network):
        """Re-framing the head flips entry edges for BOTH ends."""
        train, controller = self._setup_two_tile_train(network)

        controller._apply_head_frame("end_a")
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

        controller._apply_head_frame("end_b")

        # BOTH ends should have flipped entry edges
        span = controller.physics_sync.current_span
        assert span.end_a.entry_edge == expected_new_entry_a
        assert span.end_b.entry_edge == expected_new_entry_b

    def test_reset_to_default_re_expresses_the_span_frame(self, network):
        """`_reset_head_frame_to_default()` (the end_b -> end_a power-off reset used
        by `complete_power_off()`) moves the head from end_b to end_a, so it must
        re-express both span end frames like any other head-frame writer
        (design_new.md "Reversed-frame re-expression")."""
        train, controller = self._setup_two_tile_train(network)

        controller._apply_head_frame("end_b")
        span = controller.physics_sync.current_span
        entry_a_at_b = span.end_a.entry_edge
        entry_b_at_b = span.end_b.entry_edge

        def _flip(tile, edge):
            track = network.get_track(*tile)
            if track:
                exits = track.get_exits(edge, use_switch_state=True)
                if exits:
                    return exits[0]
            return edge

        expected_entry_a = _flip(span.end_a.tile, entry_a_at_b)
        expected_entry_b = _flip(span.end_b.tile, entry_b_at_b)

        controller._reset_head_frame_to_default()

        assert controller._framed_head_end == "end_a"
        span = controller.physics_sync.current_span
        assert span.end_a.entry_edge == expected_entry_a
        assert span.end_b.entry_edge == expected_entry_b

    def test_reframe_preserves_occupied_tiles_order(self, network):
        """occupied_tiles order stays the same - physical train didn't move."""
        train, controller = self._setup_two_tile_train(network)

        controller._apply_head_frame("end_a")
        original_occupied = controller.physics_sync.current_span.occupied_tiles.copy()

        controller._apply_head_frame("end_b")

        # occupied_tiles should NOT change - train didn't physically move
        span = controller.physics_sync.current_span
        assert span.occupied_tiles == original_occupied
        # First tile is still end_a
        assert span.occupied_tiles[0] == span.end_a.tile
        # Last tile is still end_b
        assert span.occupied_tiles[-1] == span.end_b.tile


    def test_reframe_reverses_facing_direction(self, network):
        """Re-framing the head reverses the train's facing direction."""
        train, controller = self._setup_two_tile_train(network)

        controller._apply_head_frame("end_a")
        assert controller._facing_direction() == Direction.EAST

        controller._apply_head_frame("end_b")
        assert controller._facing_direction() == Direction.WEST

        controller._apply_head_frame("end_a")
        assert controller._facing_direction() == Direction.EAST



class TestHeadFrameEntryEdgeFlip:
    """
    Re-framing the head flips entry_edge for BOTH ends since the train will now
    travel in the opposite direction on the track.
    """

    def _setup_straight_line(self, network, length_cm: float, head_x: int = 1):
        for x in range(-2, 4):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAB_EDGE_TEST", "LOCOMOTIVE", length_cm=length_cm)
        train = Train(train_id="CAB_EDGE_TEST", consist=[car])
        network.add_train(train)
        span = network.place_train(train, head_x, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.physics_sync.current_span = span
        return train, controller

    def _expected_reverse_entry_edge(self, network, tile, entry_edge: str) -> str:
        track = network.get_track(*tile)
        assert track is not None
        exits = track.get_exits(entry_edge, use_switch_state=True)
        assert exits
        return exits[0]

    def test_reframe_flips_entry_edge_single_tile(self, network):
        """Single-tile train: BOTH ends flip entry edges on re-frame."""
        train, controller = self._setup_straight_line(network, length_cm=40.0)

        controller._apply_head_frame("end_a")
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

        controller._apply_head_frame("end_b")

        assert controller._framed_head_end == "end_b"
        # BOTH ends flip their entry edges
        span = controller.physics_sync.current_span
        assert span.end_a.entry_edge == expected_entry_a
        assert span.end_b.entry_edge == expected_entry_b

    def test_reframe_flips_entry_edge_multi_tile(self, network):
        """Multi-tile train: BOTH ends flip entry edges on re-frame."""
        train, controller = self._setup_straight_line(network, length_cm=140.0)

        controller._apply_head_frame("end_a")
        span = controller.physics_sync.current_span
        original_entry_a = span.end_a.entry_edge
        original_entry_b = span.end_b.entry_edge

        expected_entry_a = self._expected_reverse_entry_edge(
            network, span.end_a.tile, original_entry_a
        )
        expected_entry_b = self._expected_reverse_entry_edge(
            network, span.end_b.tile, original_entry_b
        )

        controller._apply_head_frame("end_b")

        assert controller._framed_head_end == "end_b"
        # BOTH ends flip their entry edges
        span = controller.physics_sync.current_span
        assert span.end_a.entry_edge == expected_entry_a
        assert span.end_b.entry_edge == expected_entry_b



class TestHeadFrameDoubleFlip:
    """
    Regression guard for the double-flip bug (fixed 2026-02-10).

    Re-framing the head (end_a <-> end_b) must reverse the train's facing
    direction exactly once:
    - end_a as head faces one way; end_b as head faces the other.
    - For a multi-tile span the facing direction is derived from tile geometry,
      so swapping the head end reverses it automatically.
    - For a single-tile span the facing direction is derived from the head's
      entry_edge + offsets, which the re-frame flips for both ends.

    The earlier bug double-flipped the direction so it ended up unchanged,
    which broke backward path assignment after route completion. These tests
    pin the direction to exactly one reversal per end_a <-> end_b re-frame.
    """

    def _setup_multi_tile_train(self, network):
        """Create a 2-tile train on E-W track, facing EAST framed at end_a."""
        for x in range(5):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=80.0, max_speed=80.0)
        train = Train(train_id="FLIP_TEST", consist=[car], max_speed=80.0)
        network.add_train(train)
        span = network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.physics_sync.current_span = span
        controller._apply_head_frame("end_a")

        assert controller._facing_direction() == Direction.EAST
        assert controller._framed_head_end == "end_a"
        span = controller.physics_sync.current_span
        assert span.end_a.tile != span.end_b.tile, (
            "Setup must produce a multi-tile train"
        )

        return train, controller

    def _setup_single_tile_train(self, network):
        """Create a 1-tile train on E-W track, facing EAST framed at end_a."""
        for x in range(3):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=20.0, max_speed=80.0)
        train = Train(train_id="FLIP_TEST_1T", consist=[car], max_speed=80.0)
        network.add_train(train)
        span = network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.physics_sync.current_span = span
        controller._apply_head_frame("end_a")

        assert controller._facing_direction() == Direction.EAST
        assert controller._framed_head_end == "end_a"
        span = controller.physics_sync.current_span
        assert span.end_a.tile == span.end_b.tile, (
            "Setup must produce a single-tile train"
        )

        return train, controller

    # ---- Multi-tile tests (the original bug case) ----

    def test_multi_tile_reframe_flips_train_facing_direction(self, network):
        """The specific case that triggered the bug: multi-tile train
        facing direction stays wrong after the re-frame."""
        train, controller = self._setup_multi_tile_train(network)

        controller._apply_head_frame("end_b")

        assert controller._facing_direction() == Direction.WEST, (
            f"BUG REGRESSION: facing direction should be WEST after "
            f"re-framing to end_b, got {controller._facing_direction()}"
        )

    def test_multi_tile_reframe_flips_computed_direction(self, network):
        """travel_direction (edge form) also reverses to point WEST."""
        train, controller = self._setup_multi_tile_train(network)
        _give_authority(controller)

        controller._apply_head_frame("end_b")

        assert controller.travel_direction == "W"

    def test_multi_tile_reframe_facing_direction_consistent(self, network):
        """After the re-frame, the enum and edge derived reads agree on WEST."""
        train, controller = self._setup_multi_tile_train(network)
        _give_authority(controller)

        controller._apply_head_frame("end_b")

        # Both derived reads on the controller should agree on the direction.
        via_enum = controller._facing_direction()  # Direction enum
        via_edge = controller.travel_direction  # compass-edge form
        assert via_enum == Direction.WEST and via_edge == "W", (
            f"enum ({via_enum}) and edge ({via_edge}) should both be WEST"
        )

    def test_multi_tile_double_flip_returns_to_original(self, network):
        """Re-framing twice returns to original direction (end_a->end_b->end_a)."""
        train, controller = self._setup_multi_tile_train(network)

        controller._apply_head_frame("end_b")
        assert controller._facing_direction() == Direction.WEST

        controller._apply_head_frame("end_a")
        assert controller._facing_direction() == Direction.EAST

    def test_multi_tile_triple_flip(self, network):
        """Three re-frames (end_a->end_b->end_a->end_b) end correctly."""
        train, controller = self._setup_multi_tile_train(network)

        controller._apply_head_frame("end_b")
        assert controller._facing_direction() == Direction.WEST

        controller._apply_head_frame("end_a")
        assert controller._facing_direction() == Direction.EAST

        controller._apply_head_frame("end_b")
        assert controller._facing_direction() == Direction.WEST

    def test_multi_tile_reframe_opposite_flips_direction(self, network):
        """Re-framing to the opposite head end flips facing direction."""
        train, controller = self._setup_multi_tile_train(network)

        result = controller._apply_head_frame("end_b")
        assert result is None
        assert controller._facing_direction() == Direction.WEST
        assert controller._framed_head_end == "end_b"

    def test_multi_tile_reframe_opposite_twice_returns_original(self, network):
        """Two opposite re-frames return to original direction."""
        train, controller = self._setup_multi_tile_train(network)

        controller._apply_head_frame("end_b")
        assert controller._facing_direction() == Direction.WEST

        controller._apply_head_frame("end_a")
        assert controller._facing_direction() == Direction.EAST

    # ---- Single-tile tests ----

    def test_single_tile_reframe_flips_direction(self, network):
        """Single-tile train also flips direction on re-frame."""
        train, controller = self._setup_single_tile_train(network)

        controller._apply_head_frame("end_b")

        assert controller._facing_direction() == Direction.WEST, (
            f"Single-tile train should flip to WEST, got {controller._facing_direction()}"
        )

    def test_single_tile_double_flip(self, network):
        """Single-tile: end_a->end_b->end_a returns to original."""
        train, controller = self._setup_single_tile_train(network)

        controller._apply_head_frame("end_b")
        assert controller._facing_direction() == Direction.WEST

        controller._apply_head_frame("end_a")
        assert controller._facing_direction() == Direction.EAST

    def test_single_tile_reframe_opposite(self, network):
        """Single-tile: re-framing to the opposite head end flips direction."""
        train, controller = self._setup_single_tile_train(network)

        result = controller._apply_head_frame("end_b")
        assert result is None
        assert controller._facing_direction() == Direction.WEST
        assert controller._framed_head_end == "end_b"

    # ---- Edge cases ----

    def test_reframe_to_current_default_does_not_flip(self, network):
        """Re-framing to the already-framed end (the placed end_a default) does
        NOT flip direction."""
        for x in range(5):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))

        car = TrainCar("CAR_1", "LOCOMOTIVE", length_cm=80.0, max_speed=80.0)
        train = Train(train_id="FIRST_CAB", consist=[car], max_speed=80.0)
        network.add_train(train)
        span = network.place_train(train, 1, 0, Direction.EAST)

        controller = create_controller_for_train(train.train_id, network)
        controller.physics_sync.current_span = span

        # The placed default frame is end_a.
        assert controller._framed_head_end == "end_a"

        # Re-framing to end_a should adopt the placed orientation, not flip it.
        controller._apply_head_frame("end_a")
        assert controller._facing_direction() == Direction.EAST, (
            "Re-framing to the current head end should NOT flip direction"
        )

    def test_reframe_same_end_does_not_flip(self, network):
        """Re-framing to the same head end again should NOT flip direction."""
        train, controller = self._setup_multi_tile_train(network)

        controller._apply_head_frame("end_a")  # Already end_a, re-applying
        assert controller._facing_direction() == Direction.EAST, (
            "Re-framing to the same head end should NOT flip direction"
        )

    def test_entry_edges_flip_with_direction(self, network):
        """Entry edges and distances also flip correctly alongside direction."""
        train, controller = self._setup_multi_tile_train(network)

        span = controller.physics_sync.current_span
        orig_a_edge = span.end_a.entry_edge
        orig_b_edge = span.end_b.entry_edge
        orig_a_dist = span.end_a.distance_cm
        orig_b_dist = span.end_b.distance_cm

        controller._apply_head_frame("end_b")

        # Entry edges should have flipped
        span = controller.physics_sync.current_span
        assert span.end_a.entry_edge != orig_a_edge, (
            "end_a entry edge should flip on re-frame"
        )
        assert span.end_b.entry_edge != orig_b_edge, (
            "end_b entry edge should flip on re-frame"
        )
        # Distances should have changed (tile_length - original)
        assert span.end_a.distance_cm != orig_a_dist
        assert span.end_b.distance_cm != orig_b_dist

        # Direction must be correct too
        assert controller._facing_direction() == Direction.WEST

    # ---- Real-world scenario: the actual bug trigger ----

    def test_reframe_after_ato_stops_in_ready_state(self, network):
        """
        Real-world scenario: train arrives at terminal via ATO, route is
        cancelled, train sits in READY. Re-framing to the opposite head end
        must succeed in this state (the frame is retained across authority loss).
        """
        train, controller = self._setup_multi_tile_train(network)

        # Simulate: train was running, ATO stopped, route cancelled -> powered
        # and stopped with no reservation.
        controller.powered = True
        controller.commands.engaged = False
        controller.physics_sync.current_speed_kmh = 0.0

        # The frame is retained (route cancellation doesn't reset it).
        assert controller._framed_head_end == "end_a"

        result = controller._apply_head_frame("end_b")
        assert result is None, (
            f"re-framing to end_b should work while stopped, "
            f"train_status={controller.train_status}"
        )
        assert controller._facing_direction() == Direction.WEST
        assert controller._framed_head_end == "end_b"
