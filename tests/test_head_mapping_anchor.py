"""Head-mapping progress anchor (design_new.md "Head-mapping rule", :581-591).

One `resource_id` may appear at more than one `sequence_index` in a single live
`resource_sequence` — a lapping path that takes authority back into a block it
already passed and released. The controller must resolve the head to the lowest
`sequence_index` at or after its own progress anchor (the last index it reported
for the current `path_id`, reset to 0 on a `path_id` change), NOT to the highest
occurrence a last-wins map would return. These tests build a synthetic path
whose `resource_sequence` visits BLK_A twice (indices 0 and 2) and drive the real
`on_physics_updated` head resolution.
"""

from block import Block
from enums import Direction, PathStatus, TrainState
from entity_ids import segment_id_for_block
from network import RailwayNetwork
from path import Path, PathTileEntry, ResourceSequenceEntry
from runtime_payloads import PhysicsSpanUpdate
from simulation import SimulationController
from track import Tile, TRACK_EW
from train import Train, TrainCar

from tests.train_test_helpers import attach_synthetic_path, make_train_span


def _entry(idx: int, block_id: str, x: int, y: int) -> ResourceSequenceEntry:
    return ResourceSequenceEntry(
        sequence_index=idx,
        resource_type="block",
        resource_id=block_id,
        path_tiles=[
            PathTileEntry(
                x=x,
                y=y,
                entry_edge="W",
                exit_edge="E",
                resource_sequence_index=idx,
            )
        ],
        entry_edge="W",
        exit_edge="E",
    )


def _lapping_path(train_id: str) -> Path:
    # BLK_A (idx 0, tile (0,0)) -> BLK_B (idx 1, tile (1,0)) -> BLK_A (idx 2,
    # tile (0,0) again — the lap). BLK_A / tile (0,0) is the duplicated resource.
    return Path(
        path_id=f"PATH_{train_id}_LAP",
        status=PathStatus.ACTIVE,
        train_id=train_id,
        resource_sequence=[
            _entry(0, "BLK_A", 0, 0),
            _entry(1, "BLK_B", 1, 0),
            _entry(2, "BLK_A", 0, 0),
        ],
    )


def _controller_on_lapping_path():
    network = RailwayNetwork()
    for x, bid in ((0, "BLK_A"), (1, "BLK_B")):
        tile = Tile(x=x, y=0, track_bits=TRACK_EW)
        tile.segment_id = segment_id_for_block(bid)
        network.add_track(tile)
        network.blocks[bid] = Block(block_id=bid, length_cm=100.0, speed_limit_kmh=80.0)
    train = Train(
        train_id="T",
        consist=[TrainCar(car_id="c1", car_type="LOCOMOTIVE", length_cm=20.0)],
        max_speed=60.0,
    )
    network.add_train(train)
    sim = SimulationController(network)
    controller = sim._get_or_create_controller(train)
    controller.commands.control_mode = "manual"
    controller._apply_head_frame("end_a")  # end_a is head
    controller.ops.state = TrainState.RUNNING_MOVING
    controller.physics_sync.current_span = make_train_span(
        (0, 0), facing_direction=Direction.EAST, occupied_blocks=["BLK_A"]
    )
    attach_synthetic_path(controller, _lapping_path("T"))
    return controller


def _report_head_at(controller, head_tile):
    controller.on_physics_updated(
        event_time=1000.0,
        span_update=PhysicsSpanUpdate(
            new_span=make_train_span(
                head_tile, facing_direction=Direction.EAST, occupied_blocks=["BLK_A"]
            ),
            current_speed_kmh=10.0,
            added_tiles=[],
            removed_tiles=[],
        ),
    )
    return controller.physics_sync.queued_progress_reports[-1]


def test_first_visit_to_duplicate_maps_to_the_lower_index():
    controller = _controller_on_lapping_path()
    # Anchor starts at 0; the head is on BLK_A / tile (0,0), which appears at
    # index 0 and index 2. The lowest occurrence at or after the anchor is 0.
    report = _report_head_at(controller, (0, 0))
    assert report.current_head_sequence_index == 0, (
        "first visit to a duplicated resource maps to its earlier occurrence, "
        "not the last-wins highest index"
    )
    assert report.deviation is False


def test_second_visit_after_anchor_advances_maps_to_the_higher_index():
    controller = _controller_on_lapping_path()
    # Simulate having progressed past BLK_B (index 1): the anchor sits at 1.
    path_id = controller.path_projection.received_path_plan.path_id
    controller.physics_sync.head_progress_anchor = 1
    controller.physics_sync.head_progress_anchor_path_id = path_id
    # Back on BLK_A / tile (0,0): the lowest occurrence at or after anchor 1 is 2.
    report = _report_head_at(controller, (0, 0))
    assert report.current_head_sequence_index == 2, (
        "after the anchor advances past the first occurrence, a return to the "
        "duplicated resource maps to its later (lap) occurrence"
    )
    assert report.deviation is False


def test_anchor_resets_on_path_id_change():
    controller = _controller_on_lapping_path()
    # Pretend a stale anchor from a different path is set high.
    controller.physics_sync.head_progress_anchor = 2
    controller.physics_sync.head_progress_anchor_path_id = "SOME_OTHER_PATH"
    # The live path_id differs, so the anchor resets to 0 and the head maps to
    # the earlier occurrence again.
    report = _report_head_at(controller, (0, 0))
    assert report.current_head_sequence_index == 0
    assert controller.physics_sync.head_progress_anchor_path_id.endswith("_LAP")
