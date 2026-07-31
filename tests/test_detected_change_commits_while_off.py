"""Phase-1 gap fixes for the Step-0 detector commit path.

Two contracts, both real-hardware-only in effect (the simulated detector builds
current from live spans, so neither fires in a pure-sim run), exercised here as
unit contracts:

- design_new.md ":1043"/":1045" — `direction` on
  `BlockManager.on_block_tail_vacated(...)` is `Optional[edge]`. With `None` (the
  train left the detected area and no exit edge is known) the block clears
  `occupied_by`, writes no caution, and goes straight to FREE. The Step-0
  preamble's clear for a detected occupant the app does not control routes
  through that same canonical entry (see the ghost-train tests), so the
  de-energize queueing, BLOCK_VACATED event, and history record are not skipped.

- design_new.md ":2181"/":2641" — Phase 3 is skipped for a still-OFF transaction
  only when it has nothing to commit. When Step 0 attributed a block entry or
  vacate to the train this tick, Phase 3 runs anyway: that is the only path that
  commits the entries/vacates of a train that moves without power (pushed by
  hand, or an occupant the app does not control).
"""

from block import Block
from block_manager import BlockManager
from enums import BlockState, Direction, TrainState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from track import Tile, TRACK_EW
from train import Train, TrainCar


# ------------------------------------------------- direction=None -> FREE


def _manager_with_occupied_block():
    blocks = {"B1": Block("B1", ordered_tiles=[(0, 0, "W")])}
    manager = BlockManager(blocks)
    assert bool(manager.occupy_blocks(["B1"], "T1"))
    manager.take_blocks_to_deenergize()  # drain the queue the occupy left behind
    return manager, blocks["B1"]


def test_tail_vacate_without_direction_goes_straight_to_free():
    manager, block = _manager_with_occupied_block()

    assert manager.on_block_tail_vacated("B1", "T1", None) is True

    assert block.occupied_by is None
    assert block.caution is None
    assert block.state == BlockState.FREE


def test_tail_vacate_without_direction_still_queues_the_deenergize():
    manager, _block = _manager_with_occupied_block()

    manager.on_block_tail_vacated("B1", "T1", None)

    assert manager.take_blocks_to_deenergize() == ["B1"]


def test_tail_vacate_with_an_edge_still_takes_caution():
    """The edge path is unchanged: a known exit edge still leaves CAUTION."""
    manager, block = _manager_with_occupied_block()

    assert manager.on_block_tail_vacated("B1", "T1", "E") is True

    assert block.caution is not None
    assert block.caution.exit_edge == "E"


# --------------------------------------- Phase 3 for a still-OFF transaction


class _RecordingDispatcher:
    """Minimal stand-in for the Phase-3 collaborator surface `_update_train`
    touches: the progress-report drain (3a) and the completion check (3b)."""

    def __init__(self):
        self.reports = []

    def on_controller_progress(self, report):
        self.reports.append(report)

    def active_path_id_by_train(self, train_id):
        return None


def _sim_with_off_train():
    network = RailwayNetwork()
    for x in range(3):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        track.segment_id = f"BLK_{x}"
        network.add_track(track)
        network.blocks[f"BLK_{x}"] = Block(block_id=f"BLK_{x}", length_cm=100.0)

    sim = SimulationController(network, PathDispatcher(network))
    train = Train(
        train_id="T1",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=40.0)],
        max_speed=80.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 0, 0, Direction.EAST
    )
    sim._get_or_create_physics(train)

    dispatcher = _RecordingDispatcher()
    sim.path_dispatcher = dispatcher
    # The train has never been powered on, which is also a ghost's standing
    # condition (design_new.md ":2641": its controller stays OFF).
    assert controller.ops.state == TrainState.OFF
    return sim, train, controller, dispatcher


def test_off_train_with_a_detected_block_change_runs_phase_3():
    sim, train, controller, dispatcher = _sim_with_off_train()
    controller.physics_sync.queued_progress_reports.append(object())
    sim._detected_change_train_ids = {"T1"}

    sim._update_train(train, 0.1)

    assert dispatcher.reports, "Phase 3 must drain the report Step 0 produced"
    assert controller.physics_sync.queued_progress_reports == []


def test_off_train_with_no_detected_block_change_still_skips_phase_3():
    sim, train, controller, dispatcher = _sim_with_off_train()
    controller.physics_sync.queued_progress_reports.append(object())
    sim._detected_change_train_ids = set()

    sim._update_train(train, 0.1)

    assert dispatcher.reports == []
    assert len(controller.physics_sync.queued_progress_reports) == 1
