"""Operator `manual_override = True` must relax `Block.allowed_travel_directions`.

Per design_new.md `disabled` and `manual` availability flags canonical home
(under TrackResource) and the `Block.allowed_travel_directions` definition:

    `manual_override` relaxes the `manual` filter and the
    `Block.allowed_travel_directions` filter; it never relaxes the
    `disabled` filter.

These tests pin the two enforcement sites:

  1. `RailwayPathfinder._walk_passes_filters(...)` — pathfinder-level
     direction-policy check over every block the walk traverses.
  2. `BlockManager.reserve_blocks_for_path(...)` — per-block direction
     eligibility check.

Both must skip the `Block.allowed_travel_directions` check when
`manual_override = True` and enforce it when `manual_override = False`.
The `requested_destination_direction` match itself is always enforced —
the override relaxes the block's policy, not the caller's request.
"""

import pytest

from runtime_payloads import (
    AcquisitionMode,
    PathAcquisitionFailureKind,
)
from block_manager import BlockManager, ReservedBlockTraversal
from path_dispatcher import PathDispatcher
from network import RailwayNetwork
from simulation import SimulationController
from track import Tile, TRACK_EW
from block import Block
from train import Train, TrainCar
from enums import Direction
from tests.train_test_helpers import set_train_span
from tests.segment_test_helpers import make_segment_block
from entity_ids import segment_id_for_block


# ---------------------------------------------------------------------------
# Shared linear network: A — B — C, all EW tracks.
# ---------------------------------------------------------------------------


@pytest.fixture
def linear_network():
    network = RailwayNetwork()
    for x in range(4):
        network.add_track(Tile(x=x, y=0, track_bits=TRACK_EW))

    block_a = make_segment_block(network, "BLOCK_A", "Block A")
    block_a.length_cm = 100.0
    network.get_track(0, 0).segment_id = segment_id_for_block("BLOCK_A")

    block_b = make_segment_block(network, "BLOCK_B", "Block B")
    block_b.length_cm = 200.0
    network.get_track(1, 0).segment_id = segment_id_for_block("BLOCK_B")
    network.get_track(2, 0).segment_id = segment_id_for_block("BLOCK_B")

    block_c = make_segment_block(network, "BLOCK_C", "Block C")
    block_c.length_cm = 100.0
    network.get_track(3, 0).segment_id = segment_id_for_block("BLOCK_C")

    return network


@pytest.fixture
def dispatcher(linear_network):
    return PathDispatcher(linear_network)


def _place_probe_train(network, dispatcher):
    """Place the probe train at BLOCK_A (0,0, facing EAST) and seed a
    controller span so the dispatcher can resolve its current block.

    v1: the train's head/current block derives from
    `TrainController.physics_sync.current_span`, which the dispatcher reads
    through its `simulation_controller` controller registry. Wire the
    dispatcher to a `SimulationController` over the same network and
    create+seed the controller through that sim.
    """
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="T_PROBE", consist=[car], max_speed=60.0)
    network.add_train(train)
    network.place_train(train, 0, 0, Direction.EAST)

    sim = SimulationController(network, dispatcher)
    controller = sim._get_or_create_controller(train)
    set_train_span(
        controller,
        (0, 0),
        facing_direction=Direction.EAST,
        occupied_blocks=["BLOCK_A"],
    )
    return train


# ---------------------------------------------------------------------------
# Pathfinder + dispatcher: terminal-direction policy under operator override
# ---------------------------------------------------------------------------


class TestCreatePathDirectionOverride:
    """`PathDispatcher.create_path(..., manual_override=True)` must accept a
    target whose direction is not in the block's allowed set. Without the
    override, the same call must fail with `NO_VIABLE_PATH`.
    """

    def test_override_true_succeeds_against_policy(
        self, linear_network, dispatcher
    ):
        """Restrict BLOCK_C to reverse-only, then ask for forward as an
        operator with `manual_override = True`. Expect success.
        """
        _place_probe_train(linear_network, dispatcher)
        # The probe train travels east through A -> B -> C, so the natural
        # terminal traversal at BLOCK_C is "forward". Disallow forward to
        # force the override path.
        linear_network.blocks["BLOCK_C"].allowed_travel_directions = {"reverse"}

        result = dispatcher.create_path(
            train_id="T_PROBE",
            requested_destination_block_id="BLOCK_C",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.ALL_OR_NOTHING,
            manual_override=True,
        )

        assert result.success, (
            "Operator-initiated create_path with manual_override=True must "
            "succeed against a block whose allowed_travel_directions does "
            "not include the resolved terminal direction. Got "
            f"failure_kind={result.failure_kind!r}, reason={result.reason!r}"
        )
        assert result.authority_end_block_id == "BLOCK_C"

    def test_override_false_rejects_against_policy(
        self, linear_network, dispatcher
    ):
        """Same setup as above without the override. `create_path` must
        reject the request for the direction-policy reason.

        Per design_new.md:779 (reservation eligibility) and :931/:963
        (pathfinder rejection), `manual_override == False` keeps the
        `Block.allowed_travel_directions` filter active. The rejection may
        surface as `NO_VIABLE_PATH` (pathfinder filtered the candidate) or
        as `BLOCKED_BEFORE_TARGET` with reason `direction_not_allowed`
        (reservation eligibility caught it — the case for a single-tile
        terminal block, whose terminal direction the pathfinder cannot
        pre-compute). Either is acceptable; what matters is that the
        rejection is direction-driven.
        """
        _place_probe_train(linear_network, dispatcher)
        linear_network.blocks["BLOCK_C"].allowed_travel_directions = {"reverse"}

        result = dispatcher.create_path(
            train_id="T_PROBE",
            requested_destination_block_id="BLOCK_C",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.ALL_OR_NOTHING,
            manual_override=False,
        )

        assert not result.success
        assert result.failure_kind in (
            PathAcquisitionFailureKind.NO_VIABLE_PATH,
            PathAcquisitionFailureKind.BLOCKED_BEFORE_TARGET,
        )
        assert "direction" in (result.reason or "").lower(), (
            "Rejection must be direction-driven, not an unrelated failure. "
            f"Got reason={result.reason!r}"
        )

    def test_default_call_still_enforces_direction(
        self, linear_network, dispatcher
    ):
        """Belt-and-braces: omitting `manual_override` entirely (the
        automatic-caller shape per design_new.md:563) must enforce the
        direction policy. Same rejection surface as the explicit
        `manual_override=False` case above.
        """
        _place_probe_train(linear_network, dispatcher)
        linear_network.blocks["BLOCK_C"].allowed_travel_directions = {"reverse"}

        result = dispatcher.create_path(
            train_id="T_PROBE",
            requested_destination_block_id="BLOCK_C",
            requested_destination_direction="forward",
            acquisition_mode=AcquisitionMode.ALL_OR_NOTHING,
        )

        assert not result.success
        assert result.failure_kind in (
            PathAcquisitionFailureKind.NO_VIABLE_PATH,
            PathAcquisitionFailureKind.BLOCKED_BEFORE_TARGET,
        )
        assert "direction" in (result.reason or "").lower(), (
            "Rejection must be direction-driven, not an unrelated failure. "
            f"Got reason={result.reason!r}"
        )


# ---------------------------------------------------------------------------
# BlockManager direct call: per-block eligibility under operator override
# ---------------------------------------------------------------------------


class TestReserveBlocksForPathDirectionOverride:
    """`BlockManager.reserve_blocks_for_path(..., manual_override=True)` must
    accept a traversal whose `block_direction` is not in the block's
    `allowed_travel_directions`. Without the override, the same call must
    fail with `direction_not_allowed`.
    """

    @pytest.fixture
    def block_manager_with_one_block(self):
        # Construct a minimal block registry without going through the
        # network setup; this keeps the test focused on BlockManager's
        # eligibility math.
        block = Block(block_id="BLOCK_X", name="Block X")
        block.allowed_travel_directions = {"forward"}
        registry = {"BLOCK_X": block}
        manager = BlockManager(blocks=registry, network=None)
        return manager, block

    def test_override_true_accepts_disallowed_direction(
        self, block_manager_with_one_block
    ):
        manager, block = block_manager_with_one_block
        traversal = ReservedBlockTraversal(
            exit_edge="W", block_direction="reverse"
        )

        result = manager.reserve_blocks_for_path(
            block_ids=["BLOCK_X"],
            path_id="PATH_OPERATOR",
            train_id="T_OPERATOR",
            directions={"BLOCK_X": traversal},
            manual_override=True,
        )

        assert result.success, (
            "reserve_blocks_for_path with manual_override=True must accept "
            "a traversal whose block_direction is not in "
            "allowed_travel_directions. Got "
            f"failed_block_id={result.failed_block_id!r}, "
            f"reason={result.reason!r}"
        )
        assert block.reserved_by_path == "PATH_OPERATOR"
        assert block.reservation is not None
        assert block.reservation.exit_edge == "W"
        # The block's stored policy must NOT be mutated by the override.
        assert block.allowed_travel_directions == {"forward"}

    def test_override_false_rejects_disallowed_direction(
        self, block_manager_with_one_block
    ):
        manager, block = block_manager_with_one_block
        traversal = ReservedBlockTraversal(
            exit_edge="W", block_direction="reverse"
        )

        result = manager.reserve_blocks_for_path(
            block_ids=["BLOCK_X"],
            path_id="PATH_AUTO",
            train_id="T_AUTO",
            directions={"BLOCK_X": traversal},
            manual_override=False,
        )

        assert not result.success
        assert result.failed_block_id == "BLOCK_X"
        assert result.reason == "direction_not_allowed"
        # No reservation written on rejection.
        assert block.reserved_by_path is None
