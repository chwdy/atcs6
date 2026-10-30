"""A clear read from under a train is a detection fault, not a vacate.

design_new.md "Block occupancy detection":
- A newly CLEAR block is a `vacate` only when it is the rearmost block of its
  holder's block list; any other block the train holds is a `held_clear`.
- A held clear is a detection fault: Block.occupied_by stays, the block keeps
  its voltage, and the span does not move.
- The fault is reported once per block, and the held clear is re-offered until
  it is the rearmost block (or the block reads current again).
- A vacate moves the span's tail forward to the vacated block's seam.
- A clear on a block no train holds changes nothing and records one info event.
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


def _set_head_distance(train, controller, physics, distance_cm):
    """Put the head `distance_cm` into its tile and retrace the tail from it."""
    from train_physics import _span_from_work, _work_from_span

    span = controller.physics_sync.current_span
    head_is_a = controller._head_end_label() == "end_a"
    physics._work = _work_from_span(span, head_is_a)
    if head_is_a:
        physics._work.end_a_distance_cm = distance_cm
    else:
        physics._work.end_b_distance_cm = distance_cm
    physics._retrace_from_head(train)
    controller.physics_sync.current_span = _span_from_work(physics._work)
    physics._work = None


def _info_count(sim, block_id) -> int:
    return sum(
        1
        for n in sim.notification_center.peek_recent(200)
        if str(n.kind).lower().endswith("info") and f"block {block_id} read clear" in (n.message or "")
    )


# --- the block list, rear to front -----------------------------------------


def test_block_list_runs_rear_to_front():
    network = build_line()
    # 150cm train, head on tile 1's exit seam: tail 50cm into BLK_0.
    sim, train, controller, physics = place(network, head_x=1, length_cm=150.0)
    assert physics.block_list_rear_to_front() == ["BLK_0", "BLK_1"]

    # A held block behind the span — the locomotive standing behind the
    # calculated position — is the rearmost.
    network2 = build_line()
    _sim2, train2, _c2, physics2 = place(
        network2, head_x=2, length_cm=40.0, train_id="U"
    )
    network2.blocks["BLK_1"].occupied_by = "U"
    assert physics2.block_list_rear_to_front() == ["BLK_1", "BLK_2"]


# --- held clears: the fault, the once-per-block report, the re-offer -------


def test_a_clear_on_the_front_block_is_held_not_vacated():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=1, length_cm=150.0)
    wire_detector(sim, detected={"BLK_0", "BLK_1", "BLK_2"}, confirmed={"BLK_0", "BLK_1"})
    span_before = controller.physics_sync.current_span

    # The front block loses current while the rear keeps it. A span is never
    # moved backward, so a standing train's front block reading clear is held.
    sim.block_occupancy_detector.input_interface.live = {"BLK_0"}
    for _ in range(sim.block_occupancy_detector.off_threshold + 2):
        sim._detect_block_occupancy()

    assert network.blocks["BLK_1"].occupied_by == "T"
    assert sim._held_clear_blocks == {"BLK_1": "T"}
    assert controller.presence_unknown is False
    # Reported exactly once, however long the block stays quiet.
    assert _fault_count(sim) == 1
    assert "BLK_1" not in controller.physics_sync.pending_detected_vacated_blocks
    assert controller.physics_sync.current_span == span_before


def test_all_clear_ghosts_rather_than_holding():
    """A one-block list's only block is its rearmost, so its clear vacates and
    the train is ghosted; it is not held as a detection fault."""
    network = build_line()
    sim, train, controller, physics = place(network, head_x=1, length_cm=40.0)
    wire_detector(sim, detected={"BLK_0", "BLK_1", "BLK_2"}, confirmed={"BLK_1"})

    sim.block_occupancy_detector.input_interface.live = set()
    for _ in range(sim.block_occupancy_detector.off_threshold + 2):
        sim._detect_block_occupancy()

    assert controller.presence_unknown is True
    assert "BLK_1" not in sim._held_clear_blocks
    assert _fault_count(sim) == 0


def test_a_held_middle_block_vacates_once_the_blocks_behind_it_clear():
    network = build_line()
    # 250cm train: tail 50cm into BLK_0, head on BLK_2's exit seam.
    sim, train, controller, physics = place(network, head_x=2, length_cm=250.0)
    assert physics.block_list_rear_to_front() == ["BLK_0", "BLK_1", "BLK_2"]
    detector = wire_detector(
        sim, detected={f"BLK_{x}" for x in range(5)}, confirmed={"BLK_0", "BLK_1", "BLK_2"}
    )

    # The middle block goes quiet: held.
    detector.input_interface.live = {"BLK_0", "BLK_2"}
    for _ in range(detector.off_threshold + 2):
        sim._detect_block_occupancy()
    assert sim._held_clear_blocks == {"BLK_1": "T"}

    # The rear block clears too. It vacates, which leaves the held middle block
    # the rearmost, so the re-offer vacates it on the same read.
    detector.input_interface.live = {"BLK_2"}
    for _ in range(detector.off_threshold):
        sim._detect_block_occupancy()
    assert sim._held_clear_blocks == {}
    assert controller.physics_sync.pending_detected_vacated_blocks == {"BLK_0", "BLK_1"}
    assert _fault_count(sim) == 1


# --- a vacate moves the tail to the seam ------------------------------------


def test_a_rear_block_clear_vacates_and_moves_the_tail_to_its_seam():
    network = build_line()
    # 100cm train, head 5cm into BLK_2: tail 5cm into BLK_1.
    sim, train, controller, physics = place(network, head_x=2, length_cm=100.0)
    _set_head_distance(train, controller, physics, 5.0)
    network.blocks["BLK_1"].occupied_by = "T"
    assert set(controller.physics_sync.current_span.occupied_blocks) == {"BLK_1", "BLK_2"}
    detector = wire_detector(
        sim, detected={f"BLK_{x}" for x in range(5)}, confirmed={"BLK_1", "BLK_2"}
    )

    detector.input_interface.live = {"BLK_2"}
    for _ in range(detector.off_threshold):
        sim._detect_block_occupancy()

    assert controller.physics_sync.pending_detected_vacated_blocks == {"BLK_1"}
    assert _fault_count(sim) == 0
    span = controller.physics_sync.current_span
    # The tail sits on the BLK_1 / BLK_2 seam, so the span holds BLK_2 alone.
    assert span.occupied_blocks == ["BLK_2"]
    assert abs(span.end_a.distance_cm - 100.0) < 1e-6


def test_the_tail_stops_short_when_the_head_would_leave_its_blocks():
    """The head may move only over the train's own blocks, so a head near the
    end of its block holds the tail short of the vacated block's seam."""
    network = build_line()
    # 150cm train, head 60cm into BLK_2: tail 10cm into BLK_1, 90cm from the
    # seam, while the head has only 40cm left in BLK_2.
    sim, train, controller, physics = place(network, head_x=2, length_cm=150.0)
    _set_head_distance(train, controller, physics, 60.0)
    assert network.blocks["BLK_1"].occupied_by == "T"
    detector = wire_detector(
        sim, detected={f"BLK_{x}" for x in range(5)}, confirmed={"BLK_1", "BLK_2"}
    )

    detector.input_interface.live = {"BLK_2"}
    for _ in range(detector.off_threshold):
        sim._detect_block_occupancy()

    assert controller.physics_sync.pending_detected_vacated_blocks == {"BLK_1"}
    span = controller.physics_sync.current_span
    # The head stops on BLK_2's exit seam, 40cm on; the tail moves the same
    # 40cm and is still 50cm into BLK_1.
    assert span.end_a.tile == (2, 0)
    assert abs(span.end_a.distance_cm - 100.0) < 1e-6
    assert span.end_b.tile == (1, 0)
    assert abs(span.end_b.distance_cm - 50.0) < 1e-6


def test_a_clear_no_train_holds_records_one_info_event():
    network = build_line()
    sim, train, controller, physics = place(network, head_x=1, length_cm=150.0)
    detector = wire_detector(
        sim, detected={f"BLK_{x}" for x in range(5)}, confirmed={"BLK_0", "BLK_1", "BLK_4"}
    )
    assert network.blocks["BLK_4"].occupied_by is None

    detector.input_interface.live = {"BLK_0", "BLK_1"}
    for _ in range(detector.off_threshold + 2):
        sim._detect_block_occupancy()

    assert _info_count(sim, "BLK_4") == 1
    assert _fault_count(sim) == 0


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

    # The locomotive arrives and current returns. Nothing changes for a held
    # block: it reaches neither the train's entered slice nor route settlement.
    detector.input_interface.live.add("BLK_2")
    detector_reads = []
    head_entries = []
    detect = sim._detect_block_occupancy

    def detect_and_record(*args, **kwargs):
        detect(*args, **kwargs)
        detector_reads.append(set(controller.physics_sync.pending_detected_entered_blocks))

    sim._detect_block_occupancy = detect_and_record
    for _ in range(detector.on_threshold + 1):
        sim.update(0.1)
        head_entries += [
            event.resource_id
            for _, event in controller.physics_sync.entered_blocks_this_tick
        ]

    assert _overrun_count(sim) == 0
    assert controller.eb.active is False
    assert ahead.occupied_by == "T"
    assert "BLK_2" not in sim._held_clear_blocks
    assert all("BLK_2" not in entered for entered in detector_reads)
    assert "BLK_2" not in head_entries


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
