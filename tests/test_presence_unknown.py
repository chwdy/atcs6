"""Tests for the presence-unknown flag (design_new.md "Power", Presence
unknown).

A real-mode train whose wired blocks — span blocks plus held blocks — all read
confirmed clear has no physical detection backing its calculated position: the
locomotive may be gone, so the controller flags `presence_unknown` and the UI
shows a question mark and offers removal. The flag is computed, never latched:
detection returning anywhere in the evidence set clears it on the next read.
"""

from typing import Dict, Set

from block import Block
from block_occupancy import OccupancyInput
from enums import Direction
from network import RailwayNetwork
from simulation import SimulationController
from track import Tile, TRACK_EW
from train import Train, TrainCar
from entity_ids import segment_id_for_block


class FakeDetectorInput(OccupancyInput):
    def __init__(self, detected: Set[str], live: Set[str]) -> None:
        self.detected = set(detected)
        self.live = set(live)

    def detected_block_ids(self) -> Set[str]:
        return set(self.detected)

    def read_block_currents(self) -> Dict[str, bool]:
        return {bid: bid in self.live for bid in self.detected}


def build_line(num_tiles: int = 4) -> RailwayNetwork:
    network = RailwayNetwork()
    for x in range(num_tiles):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        track.segment_id = segment_id_for_block(f"BLK_{x}")
        network.add_track(track)
        network.blocks[f"BLK_{x}"] = Block(
            block_id=f"BLK_{x}", length_cm=100.0, speed_limit_kmh=80.0
        )
    return network


def place(network, head_x=1, length_cm=40.0, train_id="T"):
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
    return sim, train, controller


def release_all_blocks(network) -> None:
    """Stand in for the committed vacate: `Block.occupied_by` cleared."""
    for block in network.blocks.values():
        block.release()


def test_flagged_when_every_wired_evidence_block_reads_clear():
    network = build_line()
    sim, train, controller = place(network)
    release_all_blocks(network)
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={"BLK_0", "BLK_1", "BLK_2"}, live=set()
    )

    sim._detect_block_occupancy()

    assert controller.presence_unknown is True


def test_not_flagged_while_a_span_block_still_reads_occupied():
    network = build_line()
    sim, train, controller = place(network)
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={"BLK_0", "BLK_1", "BLK_2"}, live={"BLK_1"}
    )

    sim._detect_block_occupancy()

    assert controller.presence_unknown is False


def test_not_flagged_while_a_held_block_elsewhere_reads_occupied():
    """The loco standing on a held block behind the span is physical evidence:
    the train is not gone, it is somewhere the model knows about."""
    network = build_line()
    sim, train, controller = place(network, head_x=2)
    release_all_blocks(network)
    network.blocks["BLK_0"].occupied_by = "T"
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={"BLK_0", "BLK_1", "BLK_2"}, live={"BLK_0"}
    )
    sim.block_occupancy_detector.seed_occupied({"BLK_0"})

    sim._detect_block_occupancy()

    assert controller.presence_unknown is False


def test_inert_when_the_span_stands_only_on_unwired_blocks():
    network = build_line()
    sim, train, controller = place(network)
    release_all_blocks(network)
    # The session detects a block, but none the train could show up on.
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={"BLK_3"}, live=set()
    )

    sim._detect_block_occupancy()

    assert controller.presence_unknown is False


def test_inert_under_the_simulated_driver():
    network = build_line()
    sim, train, controller = place(network)
    release_all_blocks(network)

    sim._detect_block_occupancy()

    assert controller.presence_unknown is False


def test_detection_returning_clears_the_flag():
    network = build_line()
    sim, train, controller = place(network)
    release_all_blocks(network)
    fake = FakeDetectorInput(detected={"BLK_0", "BLK_1", "BLK_2"}, live=set())
    sim.block_occupancy_detector.input_interface = fake

    sim._detect_block_occupancy()
    assert controller.presence_unknown is True

    fake.live = {"BLK_1"}
    # One confirmed read is enough once the on-debounce passes.
    for _ in range(sim.block_occupancy_detector.on_threshold):
        sim._detect_block_occupancy()

    assert controller.presence_unknown is False
    # The returning current is the ghosted train's own: its retained span
    # stands in for its empty block list, so the block goes back to it rather
    # than to an unidentified train (design_new.md "Block occupancy
    # detection", A ghosted train's retained span stands in for its block list).
    assert controller.physics_sync.pending_detected_entered_blocks == {"BLK_1"}
    assert not any(
        getattr(network.trains.get(train_id), "is_unidentified", False)
        for train_id in sim.controllers
    )


def test_serialized_on_the_controller_payload():
    network = build_line()
    sim, train, controller = place(network)
    release_all_blocks(network)
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={"BLK_0", "BLK_1", "BLK_2"}, live=set()
    )

    sim._detect_block_occupancy()

    assert controller.to_dict()["presence_unknown"] is True


# ---------------------------------------------------------------------------
# Losing the train releases everything
# ---------------------------------------------------------------------------


def test_rising_edge_stops_the_train_and_releases_its_blocks():
    """With no detection behind it there is nothing to predict and no grounds
    to keep resources: the controller is written OFF at zero speed and every
    held block is released, on the tick the flag rises."""

    network = build_line()
    sim, train, controller = place(network)
    controller.physics_sync.current_speed_kmh = 42.0
    assert network.blocks["BLK_1"].occupied_by == "T"
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={"BLK_0", "BLK_1", "BLK_2"}, live=set()
    )
    # The placed block was seeded confirmed; let the off-debounce clear it so
    # the flag rises through the ordinary detector path.
    for _ in range(sim.block_occupancy_detector.off_threshold + 1):
        sim._detect_block_occupancy()

    assert controller.presence_unknown is True
    assert not controller.powered
    assert controller.physics_sync.current_speed_kmh == 0.0
    assert all(b.occupied_by is None for b in network.blocks.values())


def test_release_fires_once_per_rising_edge():
    network = build_line()
    sim, train, controller = place(network)
    release_all_blocks(network)
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={"BLK_0", "BLK_1", "BLK_2"}, live=set()
    )

    for _ in range(4):
        sim._detect_block_occupancy()

    ops = [n.operation for n in sim.notification_center.peek_recent(50)]
    assert ops.count("_release_lost_train") == 1


def test_span_survives_the_release_for_the_question_mark():
    network = build_line()
    sim, train, controller = place(network)
    release_all_blocks(network)
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={"BLK_0", "BLK_1", "BLK_2"}, live=set()
    )

    sim._detect_block_occupancy()

    assert controller.presence_unknown is True
    assert controller.physics_sync.current_span is not None
    assert "T" in network.trains


def test_lost_train_cancels_path_and_releases_every_reserved_block():
    """The question-mark identifier is diagnostic only, never live authority."""
    from enums import PathStatus
    from path_dispatcher import PathDispatcher

    network = build_line()
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    train = Train(
        train_id="T",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=40.0)],
        max_speed=80.0,
    )
    network.add_train(train)
    controller = sim._get_or_create_controller(train)
    controller.physics_sync.current_span = network.place_train(
        train, 1, 0, Direction.EAST
    )
    created = dispatcher.create_path(
        train_id="T", requested_destination_block_id="BLK_3"
    )
    assert created.success
    path_id = created.path_id
    assert path_id is not None
    assert any(b.reserved_for_train == "T" for b in network.blocks.values())

    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={"BLK_0", "BLK_1", "BLK_2", "BLK_3"}, live=set()
    )
    for _ in range(sim.block_occupancy_detector.off_threshold + 1):
        sim._detect_block_occupancy()

    # The rising edge raised the flag, froze the record, released its confirmed
    # blocks, and wrote the power-off + inclusive-cancel latches in Observe; the
    # path cancel and the controller OFF transition commit in Action, so a full
    # tick lands the teardown (design_new.md "Action is the sole explicit-cancel
    # and teardown committer").
    assert controller.presence_unknown is True
    assert controller.physics_sync.current_speed_kmh == 0.0
    sim.update(0.1)

    assert controller.presence_unknown is True
    assert not controller.powered
    assert controller.physics_sync.current_speed_kmh == 0.0
    assert dispatcher.paths[path_id].status == PathStatus.CANCELLED
    assert dispatcher.active_path_id_by_train("T") is None
    assert all(b.occupied_by is None for b in network.blocks.values())
    assert all(b.reserved_for_train is None for b in network.blocks.values())

    # Even authority attached between detector reads is contained before the
    # next Plan slot; an unknown identifier cannot be revived.
    assert controller.commands.power_on_cmd().success
    recreated = dispatcher.create_path(
        train_id="T", requested_destination_block_id="BLK_0"
    )
    assert recreated.success
    assert controller.powered
    assert any(b.reserved_for_train == "T" for b in network.blocks.values())

    sim.update(0.1)

    assert not controller.powered
    assert dispatcher.paths[recreated.path_id].status == PathStatus.CANCELLED
    assert dispatcher.active_path_id_by_train("T") is None
    assert all(b.reserved_for_train is None for b in network.blocks.values())


def test_ghost_energizes_nothing_so_its_span_de_energizes():
    """A ghosted train controls no `track_voltage`: its emission set is empty, so
    the retained span it keeps for the question mark is driven to `0` by the
    drain rather than held at lighting voltage (design_new.md "Power", A ghosted
    or unidentified train's emission set is empty for that tick)."""
    network = build_line()
    sim, train, controller = place(network)
    physics = sim._get_or_create_physics(train)

    # Before the flag rises, the train energizes the block it stands on.
    assert physics._energized_blocks_for_train(train)

    controller.presence_unknown = True

    # Emission set is empty, and the span is not held against the drain, so the
    # blocks it dropped are free to be zeroed.
    span_blocks = set(controller.physics_sync.current_span.occupied_blocks)
    assert physics._energized_blocks_for_train(train) == set()
    assert not (sim._calculated_hold_blocks() & span_blocks)


def test_hold_set_includes_confirmed_occupancy_outside_the_span():
    """A block a train is confirmed standing on (`Block.occupied_by`) that its
    calculated span does not cover is still held against the de-energize drain,
    matching the four-term emission set the drive side computes (design_new.md
    "Power", The drain tests both views of occupancy, never one alone)."""
    network = build_line()
    sim, train, controller = place(network, head_x=1)
    sim._get_or_create_physics(train)

    span_blocks = set(controller.physics_sync.current_span.occupied_blocks)
    assert "BLK_3" not in span_blocks
    network.blocks["BLK_3"].occupied_by = "T"

    held = sim._calculated_hold_blocks()

    assert "BLK_3" in held


def test_lost_train_refuses_commands_and_never_runs_physics():
    """A power command cannot revive an identifier backed by no train: a ghosted
    train refuses every operator command (design_new.md "Operator-command
    processing slot")."""

    network = build_line()
    sim, train, controller = place(network)
    release_all_blocks(network)
    sim.block_occupancy_detector.input_interface = FakeDetectorInput(
        detected={"BLK_0", "BLK_1", "BLK_2"}, live=set()
    )
    sim._detect_block_occupancy()
    assert controller.presence_unknown is True

    import threading
    from types import SimpleNamespace
    from services import train_service

    state = SimpleNamespace(
        network=network, sim_controller=sim, simulation_lock=threading.Lock()
    )
    result = train_service.power_on(state, train.train_id)
    assert result.success is False
    assert "lost detection" in result.message
    physics = sim._get_or_create_physics(train)
    old_span = controller.physics_sync.current_span
    old_frame = (
        old_span.end_a.tile,
        old_span.end_a.distance_cm,
        old_span.end_b.tile,
        old_span.end_b.distance_cm,
    )

    sim.update(1.0)

    assert not controller.powered
    assert controller.physics_sync.current_speed_kmh == 0.0
    assert physics.pending_entry_block is None
    span = controller.physics_sync.current_span
    assert (
        span.end_a.tile,
        span.end_a.distance_cm,
        span.end_b.tile,
        span.end_b.distance_cm,
    ) == old_frame
