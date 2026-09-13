"""A block a train reserved ahead becomes physically occupied by something else.

Under Virtual Hardware and Real Hardware a detector can report current on a
block the app already promised to a train. The train is nowhere near it — there
is a block in between still reading clear — so the current is a different
object, and the promise cannot be kept.

Three rules meet here, and this file covers all three end to end:

  * **Attribution** (design_new.md "Power", Attribution) does not credit the
    reserved block to the reserving train, because the train is not standing
    next to it. It becomes an unidentified train.
  * **Authority truncation** (design_new.md "Path And PathDispatcher") pulls the
    reserving train's live path back to end at the last block before the taken
    one, so ATC brakes it to a stop short of the obstruction.
  * **The forward correction** (design_new.md "Power", Physical detection
    outranks the calculated position) never drags the span across the clear
    block in between.

Before these rules the span teleported to the far block's entry seam, the train
claimed the obstruction's block as its own, and ATC escalated to EB on arrival
at an authority end it had never travelled to.
"""

from typing import Dict, Set

from block_occupancy import OccupancyInput
from enums import Direction, HandlePosition, PathStatus, TrainState
from network import RailwayNetwork
from path import Path, ResourceSequenceEntry
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from track import TRACK_EW, Tile
from train import Train, TrainCar
from tests.segment_test_helpers import make_segment_block_with_tracks


class FakeDetectorInput(OccupancyInput):
    def __init__(self, detected: Set[str], live: Set[str]) -> None:
        self.detected = set(detected)
        self.live = set(live)

    def detected_block_ids(self) -> Set[str]:
        return set(self.detected)

    def read_block_currents(self) -> Dict[str, bool]:
        return {bid: bid in self.live for bid in self.detected}


def build_abc(c_length_cm: float = 20.0) -> RailwayNetwork:
    """Three two-tile blocks in a straight line: BLK_A - BLK_B - BLK_C.

    BLK_C is short by default so that a head snapped onto it would have almost
    no authority left — which is what turned the teleport into an EB.
    """
    network = RailwayNetwork()
    for x in range(6):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(network, "BLK_A", positions=[(0, 0), (1, 0)], name="A", length_cm=200.0
    )
    make_segment_block_with_tracks(network, "BLK_B", positions=[(2, 0), (3, 0)], name="B", length_cm=200.0
    )
    make_segment_block_with_tracks(network, "BLK_C", positions=[(4, 0), (5, 0)], name="C", length_cm=c_length_cm
    )
    for block_id in ("BLK_A", "BLK_B", "BLK_C"):
        network.blocks[block_id].speed_limit_kmh = 60.0
    return network


def rolling_train_with_authority_to_c(network):
    """A train on BLK_A under manual power with an ACTIVE path through BLK_C,
    on a session where all three blocks are wired and only BLK_A reads current.
    """
    train = Train(
        train_id="T1",
        consist=[TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)],
        max_speed=60.0,
    )
    network.add_train(train)
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 1, 0, direction=Direction.EAST
    )

    result = dispatcher.create_path(
        train_id="T1", requested_destination_block_id="BLK_C"
    )
    assert result.success, getattr(result, "reason", None)

    controller.ops.state = TrainState.READY
    controller.commands.control_mode = "manual"
    controller._apply_head_frame("end_a")
    # Manual trains have no depart handshake; the driver drives. Put the train
    # in the moving state and give it a handle.
    controller.ops.enter_running_moving()
    assert controller.commands.set_handle(HandlePosition.P4).success

    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={"BLK_A", "BLK_B", "BLK_C"}, live={"BLK_A"}
    )
    sim.block_occupancy_detector.seed_occupied({"BLK_A"})

    for _ in range(20):
        sim.update(0.1)
    assert controller.physics_sync.current_speed_kmh > 1.0, "train should be rolling"

    return sim, dispatcher, controller, train, result.path_id


def test_foreign_current_on_a_reserved_block_does_not_move_the_train():
    network = build_abc()
    sim, dispatcher, controller, train, path_id = rolling_train_with_authority_to_c(
        network
    )
    assert controller.physics_sync.current_span.occupied_blocks == ["BLK_A"]

    # Something lands on BLK_C. BLK_B between them keeps reading clear.
    sim.block_occupancy_detector.input_interface.live = {"BLK_A", "BLK_C"}
    for _ in range(5):
        sim.update(0.1)

    span = controller.physics_sync.current_span
    assert span.occupied_blocks == ["BLK_A"], (
        f"span teleported to {span.occupied_blocks} across a BLK_B that never "
        f"confirmed occupied"
    )
    assert network.blocks["BLK_A"].occupied_by == "T1"
    assert controller.eb.active is False
    assert sim.failed_runtime is False


def test_the_foreign_occupant_becomes_an_unidentified_train():
    network = build_abc()
    sim, dispatcher, controller, train, path_id = rolling_train_with_authority_to_c(
        network
    )

    sim.block_occupancy_detector.input_interface.live = {"BLK_A", "BLK_C"}
    for _ in range(5):
        sim.update(0.1)

    occupant = network.blocks["BLK_C"].occupied_by
    assert occupant is not None and occupant != "T1"
    assert network.trains[occupant].is_unidentified is True


def test_the_reserving_train_keeps_a_shorter_path_instead_of_losing_it():
    network = build_abc()
    sim, dispatcher, controller, train, path_id = rolling_train_with_authority_to_c(
        network
    )
    path = dispatcher.paths[path_id]
    assert path.get_reservation_end_block_id() == "BLK_C"

    sim.block_occupancy_detector.input_interface.live = {"BLK_A", "BLK_C"}
    for _ in range(5):
        sim.update(0.1)

    # The path lives on, one block shorter, and the train still holds every
    # reservation up to the new end.
    assert path.status == PathStatus.ACTIVE
    assert dispatcher.active_path_id_by_train("T1") == path_id
    assert path.get_reservation_end_block_id() == "BLK_B"
    assert network.blocks["BLK_B"].reserved_for_train == "T1"
    assert network.blocks["BLK_C"].reserved_for_train is None


def test_the_train_never_reaches_the_taken_block_and_never_ebs():
    """The long run. Whatever the speed solution does while the train is held
    at the seam, it must never claim the taken block and must never be
    emergency-braked for arriving somewhere it never travelled to."""
    network = build_abc()
    sim, dispatcher, controller, train, path_id = rolling_train_with_authority_to_c(
        network
    )
    occupant_before = None

    sim.block_occupancy_detector.input_interface.live = {"BLK_A", "BLK_C"}
    for _ in range(200):
        sim.update(0.1)
        span = controller.physics_sync.current_span
        assert "BLK_C" not in (span.occupied_blocks or [])
        assert controller.eb.active is False, "ATC escalated to EB"
        occupant_before = network.blocks["BLK_C"].occupied_by

    assert sim.failed_runtime is False
    assert occupant_before is not None and occupant_before != "T1"


def test_current_on_the_next_reserved_block_is_still_the_train_arriving():
    """The rule must not fire on an ordinary arrival. BLK_B is reserved AND
    right next to the block the train is standing on, so current there is the
    locomotive getting there — not a second object, and no reason to cut
    anything back."""
    network = build_abc()
    sim, dispatcher, controller, train, path_id = rolling_train_with_authority_to_c(
        network
    )
    path = dispatcher.paths[path_id]

    sim.block_occupancy_detector.input_interface.live = {"BLK_A", "BLK_B"}
    for _ in range(5):
        sim.update(0.1)

    assert network.blocks["BLK_B"].occupied_by == "T1"
    assert "BLK_B" in controller.physics_sync.current_span.occupied_blocks
    assert path.get_reservation_end_block_id() == "BLK_C"
    assert all(
        not getattr(t, "is_unidentified", False) for t in network.trains.values()
    )


# ---------------------------------------------------------------------------
# truncate_path_before_block directly
# ---------------------------------------------------------------------------


def test_truncation_declines_a_block_that_is_not_on_the_path():
    network = build_abc()
    sim, dispatcher, controller, train, path_id = rolling_train_with_authority_to_c(
        network
    )

    assert (
        dispatcher.truncate_path_before_block(path_id, "BLK_NOPE", "test") is False
    )
    assert dispatcher.paths[path_id].get_reservation_end_block_id() == "BLK_C"


def test_truncation_declines_when_no_reservation_would_be_left():
    """Cutting before the block the train is already anchored on leaves nothing
    to hold, so the caller cancels the path instead."""
    network = build_abc()
    sim, dispatcher, controller, train, path_id = rolling_train_with_authority_to_c(
        network
    )
    path = dispatcher.paths[path_id]
    assert path.resource_sequence[path.current_resource_index].resource_id == "BLK_A"

    assert dispatcher.truncate_path_before_block(path_id, "BLK_A", "test") is False
    assert path.get_reservation_end_block_id() == "BLK_C"


# ---------------------------------------------------------------------------
# item-11 truncation-terminal rules (design_new.md line 1366)
# ---------------------------------------------------------------------------


def _straight_path_with_switches():
    """A -[SW1]- B -[SW2]- C - D live path, train anchored in A
    (current_resource_index 0), authority to D. B is one-way (a safe terminal);
    C is two-way and entered straight off SW2 (a two-way-switch foul). Synthetic
    switch entries suffice — the item-11 foul check reads the entry's
    resource_type and the block's `excludes_opposing_traversal`, not real switch
    tiles.
    """
    network = RailwayNetwork()
    for x in range(8):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    make_segment_block_with_tracks(network, "BLK_A", positions=[(0, 0), (1, 0)], name="A", length_cm=200.0)
    make_segment_block_with_tracks(network, "BLK_B", positions=[(2, 0), (3, 0)], name="B", length_cm=200.0)
    make_segment_block_with_tracks(network, "BLK_C", positions=[(4, 0), (5, 0)], name="C", length_cm=200.0)
    make_segment_block_with_tracks(network, "BLK_D", positions=[(6, 0), (7, 0)], name="D", length_cm=200.0)
    # B one-way → excludes_opposing_traversal → a safe terminal.
    network.blocks["BLK_B"].allowed_travel_directions = {Direction.EAST}
    # C two-way (no direction restriction, not caution) → foulable off a switch.
    network.blocks["BLK_C"].allowed_travel_directions = None
    network.blocks["BLK_C"].caution = None

    dispatcher = PathDispatcher(network)
    entries = [
        ResourceSequenceEntry(sequence_index=0, resource_type="block", resource_id="BLK_A", entry_edge="W", exit_edge="E"),
        ResourceSequenceEntry(sequence_index=1, resource_type="switch", resource_id="SW1"),
        ResourceSequenceEntry(sequence_index=2, resource_type="block", resource_id="BLK_B", entry_edge="W", exit_edge="E"),
        ResourceSequenceEntry(sequence_index=3, resource_type="switch", resource_id="SW2"),
        ResourceSequenceEntry(sequence_index=4, resource_type="block", resource_id="BLK_C", entry_edge="W", exit_edge="E"),
        ResourceSequenceEntry(sequence_index=5, resource_type="block", resource_id="BLK_D", entry_edge="W", exit_edge="E"),
    ]
    path = Path(path_id="P1", status=PathStatus.ACTIVE, train_id="T1", resource_sequence=entries, current_resource_index=0)
    dispatcher.paths["P1"] = path
    return network, dispatcher, path


def test_truncation_backs_off_a_two_way_switch_foul_terminal():
    network, dispatcher, path = _straight_path_with_switches()
    # The naive cut before the taken D is C, but C is a two-way block entered
    # straight off a switch — an item-11 junction foul — so truncation backs off
    # to B, the nearest legal (one-way) terminal (design_new.md line 1366).
    assert dispatcher.truncate_path_before_block("P1", "BLK_D", "taken") is True
    assert path.status == PathStatus.ACTIVE
    assert path.get_reservation_end_block_id() == "BLK_B"


def test_truncation_keeps_a_holding_point_terminal_over_a_foul():
    network, dispatcher, path = _straight_path_with_switches()
    # A holding point is the operator's explicit safe wait place, so it overrides
    # the two-way-switch foul (design_new.md item 11): the cut lands on C.
    network.blocks["BLK_C"].is_holding_point = True
    assert dispatcher.truncate_path_before_block("P1", "BLK_D", "taken") is True
    assert path.get_reservation_end_block_id() == "BLK_C"
