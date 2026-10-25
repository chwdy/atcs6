"""A clear read from under a train is a detection fault, not a vacate.

design_new.md "Block occupancy detection":
- A newly CLEAR block is vacated for its holder only once the holder's
  calculated tail has reached the block's exit seam; otherwise it is held.
- A clear read from under the train is a detection fault: Block.occupied_by
  stays, the block keeps its voltage, and the span does not move.
- The fault is reported once per block, and the held clear is re-offered until
  the tail reaches the seam (or the block reads current again).
- When no block in a train's evidence set reads confirmed occupied, that train
  is ghosted instead of holding a single block.
"""

from typing import Dict, Set

from block import Block
from block_occupancy import OccupancyInput
from enums import Direction
from entity_ids import segment_id_for_block
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from track import Tile, TRACK_EW
from train import Train, TrainCar


class FakeDetectorInput(OccupancyInput):
    """A detector that answers for `detected` and reports current on `live`."""

    def __init__(self, detected: Set[str], live: Set[str]) -> None:
        self.detected = set(detected)
        self.live = set(live)

    def detected_block_ids(self) -> Set[str]:
        return set(self.detected)

    def read_block_currents(self) -> Dict[str, bool]:
        return {bid: bid in self.live for bid in self.detected}


def build_line(num_tiles: int = 5) -> RailwayNetwork:
    network = RailwayNetwork()
    for x in range(num_tiles):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        track.segment_id = segment_id_for_block(f"BLK_{x}")
        network.add_track(track)
        network.blocks[f"BLK_{x}"] = Block(
            block_id=f"BLK_{x}", length_cm=100.0, speed_limit_kmh=80.0
        )
    return network


def place(network, head_x, length_cm, train_id="T"):
    sim = SimulationController(network)
    train = Train(
        train_id=train_id,
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=length_cm)],
        max_speed=80.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, head_x, 0, Direction.EAST
    )
    physics = sim._get_or_create_physics(train)
    return sim, train, controller, physics


def wire_detector(sim, detected, confirmed):
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected=detected, live=confirmed
    )
    sim.block_occupancy_detector.seed_occupied(confirmed)
    return sim.block_occupancy_detector


def _fault_count(sim) -> int:
    return sum(
        1
        for n in sim.notification_center.peek_recent(100)
        if n.error is not None and n.error.origin == "detection_lost_under_train"
    )


# --- the tail-reached-exit-seam predicate (geometry) ----------------------


def test_tail_has_reached_exit_seam_geometry():
    network = build_line()
    # 150cm train, head on tile 1's exit seam: spans BLK_1 (head) and BLK_0
    # (tail sits 50cm into BLK_0 — mid-block).
    sim, train, controller, physics = place(network, head_x=1, length_cm=150.0)
    assert "BLK_0" in controller.physics_sync.current_span.occupied_blocks

    # The tail is mid-BLK_0, so it has not reached BLK_0's exit seam.
    assert physics.tail_has_reached_exit_seam(train, "BLK_0") is False
    # BLK_1 is the head block; the tail is far short of its exit seam.
    assert physics.tail_has_reached_exit_seam(train, "BLK_1") is False

    # A short train two blocks ahead has left BLK_0 entirely.
    network2 = build_line()
    _sim2, train2, _c2, physics2 = place(
        network2, head_x=2, length_cm=40.0, train_id="U"
    )
    assert physics2.tail_has_reached_exit_seam(train2, "BLK_0") is True


# --- the hold + one fault + re-offer lifecycle ----------------------------


def test_clear_under_a_train_is_held_not_vacated():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=1, length_cm=150.0)
    # Both span blocks confirmed to start with.
    wire_detector(sim, detected={"BLK_0", "BLK_1", "BLK_2"}, confirmed={"BLK_0", "BLK_1"})
    assert network.blocks["BLK_0"].occupied_by == "T"

    # BLK_0 loses current while BLK_1 keeps it: a clear from under the stationary
    # train, whose tail is mid-BLK_0.
    sim.block_occupancy_detector.input_interface.live = {"BLK_1"}
    for _ in range(sim.block_occupancy_detector.off_threshold + 2):
        sim._detect_block_occupancy()

    # Held, not vacated: occupied_by stays, and it is tracked as a held clear.
    assert network.blocks["BLK_0"].occupied_by == "T"
    assert "BLK_0" in sim._held_clear_blocks
    # The train is not ghosted: BLK_1 still reads occupied.
    assert controller.presence_unknown is False
    # Reported exactly once, however long the block stays quiet.
    assert _fault_count(sim) == 1
    # The held clear was not offered to the vacate path.
    assert "BLK_0" not in controller.physics_sync.pending_detected_vacated_blocks


def test_all_clear_ghosts_rather_than_holding():
    """When every evidence block reads clear the train is ghosted; a single
    block is not held as a detection fault."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=1, length_cm=40.0)
    wire_detector(sim, detected={"BLK_0", "BLK_1", "BLK_2"}, confirmed={"BLK_1"})

    sim.block_occupancy_detector.input_interface.live = set()
    for _ in range(sim.block_occupancy_detector.off_threshold + 2):
        sim._detect_block_occupancy()

    assert controller.presence_unknown is True
    assert "BLK_1" not in sim._held_clear_blocks
    assert _fault_count(sim) == 0


def test_held_clear_is_reoffered_once_the_tail_clears():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=1, length_cm=150.0)
    wire_detector(sim, detected={"BLK_0", "BLK_1", "BLK_2"}, confirmed={"BLK_0", "BLK_1"})

    # Hold BLK_0 first.
    sim.block_occupancy_detector.input_interface.live = {"BLK_1"}
    for _ in range(sim.block_occupancy_detector.off_threshold + 2):
        sim._detect_block_occupancy()
    assert "BLK_0" in sim._held_clear_blocks

    # The train's tail advances past BLK_0 (now spanning BLK_1 + BLK_2). The
    # block stays held through Block.occupied_by until the vacate commits.
    controller.physics_sync.current_span = network.place_train(
        train, 2, 0, Direction.EAST
    )
    network.blocks["BLK_0"].occupied_by = "T"
    sim.block_occupancy_detector.input_interface.detected = {"BLK_0", "BLK_1", "BLK_2"}
    sim.block_occupancy_detector.input_interface.live = {"BLK_1", "BLK_2"}
    sim.block_occupancy_detector.seed_occupied({"BLK_1", "BLK_2"})

    sim._detect_block_occupancy()

    # The tail has now reached BLK_0's exit seam, so the held clear is vacated.
    assert "BLK_0" not in sim._held_clear_blocks
    assert "BLK_0" in controller.physics_sync.pending_detected_vacated_blocks


# --- current returning under a held block is `held`, not an overrun --------


def _train_with_path_to(destination, live):
    """A powered, standing 10cm train on BLK_1 holding a path to `destination`,
    with every block detected and `live` confirmed to start with."""
    network = build_line()
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    train = Train(
        train_id="T",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=10.0)],
        max_speed=80.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 1, 0, Direction.EAST
    )
    sim._get_or_create_physics(train)
    wire_detector(sim, detected={f"BLK_{x}" for x in range(5)}, confirmed=live)
    assert dispatcher.create_path(
        train_id="T", requested_destination_block_id=destination
    ).success
    assert controller.commands.power_on_cmd().success
    return sim, network, controller


def _overrun_count(sim) -> int:
    """Overrun warnings `SimulationController` raised (the cut-out request it
    latches carries the same error origin, so count the warning alone)."""
    return sum(
        1
        for n in sim.notification_center.peek_recent(200)
        if n.error is not None
        and n.error.origin == "overrun_entry"
        and n.operation == "_detect_block_occupancy"
    )


def _read(sim, times):
    for _ in range(times):
        sim.update(0.1)


def test_current_returning_under_a_held_block_is_not_an_overrun():
    """The 2026-10-24 rig run: an empty block ahead read current, so the train
    entered it on paper and its reservation was consumed; the block then read
    clear and was held under a detection fault. When the locomotive really
    arrived, the returning current was judged by the reservation alone and cut
    the train out. A block already in the train's block list is the `held`
    result (rank 1 of the match order), and nothing changes for it."""
    sim, network, controller = _train_with_path_to("BLK_2", live={"BLK_1"})
    detector = sim.block_occupancy_detector
    ahead = network.blocks["BLK_2"]

    # Current with no locomotive behind it: an entry that consumes the
    # reservation.
    detector.input_interface.live.add("BLK_2")
    _read(sim, detector.on_threshold + 1)
    assert ahead.occupied_by == "T"
    assert ahead.reserved_for_train is None

    # It goes quiet with the train still standing on it: held, not vacated.
    detector.input_interface.live.discard("BLK_2")
    _read(sim, detector.off_threshold + 2)
    assert sim._held_clear_blocks.get("BLK_2") == "T"

    # The locomotive arrives and current returns.
    detector.input_interface.live.add("BLK_2")
    _read(sim, detector.on_threshold + 1)

    assert _overrun_count(sim) == 0
    assert controller.eb.active is False
    assert ahead.occupied_by == "T"
    assert "BLK_2" not in sim._held_clear_blocks


def test_a_block_neither_reserved_nor_held_is_still_an_overrun():
    """The held exemption covers only the train's own blocks: current on the
    block past its authority end still cuts the train out."""
    sim, network, controller = _train_with_path_to("BLK_2", live={"BLK_1"})
    detector = sim.block_occupancy_detector

    detector.input_interface.live.add("BLK_2")
    _read(sim, detector.on_threshold + 1)
    assert network.blocks["BLK_2"].occupied_by == "T"

    detector.input_interface.live.add("BLK_3")
    _read(sim, detector.on_threshold + 1)

    assert _overrun_count(sim) == 1
    assert controller.eb.active is True
