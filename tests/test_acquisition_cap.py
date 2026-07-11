"""Meet-coordination acquisition cap (design_new.md "Acquisition-cap rule",
line 1025).

A cap short of the requested target must:
  * leave ``requested_destination_block_id`` at the real route target,
  * truncate stored authority so ``authority_end_block_id`` lands on the cap,
  * report ``TRUNCATED_GREW`` / ``BLOCKED_NO_GROWTH`` (never
    ``REACHED_REQUESTED_TARGET``),
  * never satisfy ``try_complete_active_path(...)``, so the train holds at the
    cap.

Before the fix, Phase 1 capped the *requested target* instead of the
acquisition. A meet-held train then had ``authority_end == requested == ceiling``
and completed its path at the meet point; ``handle_path_completion`` then failed
to match the ceiling against the route task's real ``resolved_target`` and froze
the runtime through ``enter_failed_runtime``. These tests lock the cap mechanism
that prevents that.
"""

from network import RailwayNetwork
from track import Tile, TRACK_EW
from train import Train, TrainCar
from path_dispatcher import PathDispatcher
from runtime_payloads import PathAcquisitionResultKind
from simulation import SimulationController
from enums import Direction
from tests.train_test_helpers import set_train_span


def _linear_network():
    """A - B - C - D - E linear EW network (2 tiles per block)."""
    network = RailwayNetwork()
    for x in range(10):
        network.add_track(Tile(x, 0, track_bits=TRACK_EW))
    for blk, (x0, x1) in [
        ("BLOCK_A", (0, 1)),
        ("BLOCK_B", (2, 3)),
        ("BLOCK_C", (4, 5)),
        ("BLOCK_D", (6, 7)),
        ("BLOCK_E", (8, 9)),
    ]:
        network.create_block_with_tracks(
            block_id=blk, positions=[(x0, 0), (x1, 0)], name=blk, length_cm=100.0,
        )
    return network


def _train_at_a(network):
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=40.0)
    train = Train(train_id="T1", consist=[car], max_speed=60.0)
    network.add_train(train)
    network.place_train(train, 0, 0, Direction.EAST)
    if network.block_manager:
        network.block_manager.occupy_block("BLOCK_A", train.train_id)
    return train


def _dispatcher_with_seeded_head(network, train):
    dispatcher = PathDispatcher(network)
    sim = SimulationController(network, dispatcher)
    controller = sim._get_or_create_controller(train)
    set_train_span(
        controller, (0, 0), facing_direction=Direction.EAST,
        occupied_blocks=["BLOCK_A"],
    )
    return dispatcher


def test_create_path_caps_authority_and_never_completes():
    """create_path with a cap short of the target lands authority on the cap,
    keeps the requested target, reports TRUNCATED_GREW, and never completes."""
    network = _linear_network()
    train = _train_at_a(network)
    dispatcher = _dispatcher_with_seeded_head(network, train)

    result = dispatcher.create_path(
        train_id=train.train_id,
        requested_destination_block_id="BLOCK_E",
        acquisition_cap_block_id="BLOCK_C",
    )
    assert result.success, result.reason
    # Requested target is unchanged; authority is capped at the cap block.
    assert result.requested_destination_block_id == "BLOCK_E"
    assert result.result_kind == PathAcquisitionResultKind.TRUNCATED_GREW

    path = dispatcher.get_path(result.path_id)
    assert path.get_authority_end_block_id() == "BLOCK_C"
    assert path.requested_destination_block_id == "BLOCK_E"

    # The completion gate requires authority_end == requested. A capped authority
    # (BLOCK_C != BLOCK_E) can therefore never complete — the train holds at the
    # cap instead of completing at the meet point (which used to freeze the run).
    completion = dispatcher.try_complete_active_path(train.train_id)
    assert completion.success is False


def test_create_path_cap_equal_to_target_is_no_cap():
    """A cap equal to the requested target is the no-cap case: full acquisition."""
    network = _linear_network()
    train = _train_at_a(network)
    dispatcher = _dispatcher_with_seeded_head(network, train)

    result = dispatcher.create_path(
        train_id=train.train_id,
        requested_destination_block_id="BLOCK_C",
        acquisition_cap_block_id="BLOCK_C",
    )
    assert result.success, result.reason
    assert result.result_kind == PathAcquisitionResultKind.REACHED_REQUESTED_TARGET
    assert dispatcher.get_path(result.path_id).get_authority_end_block_id() == "BLOCK_C"


def test_create_path_no_cap_reaches_target():
    """Without a cap, create_path reaches the requested target (regression guard
    that the cap path does not affect ordinary acquisition)."""
    network = _linear_network()
    train = _train_at_a(network)
    dispatcher = _dispatcher_with_seeded_head(network, train)

    result = dispatcher.create_path(
        train_id=train.train_id,
        requested_destination_block_id="BLOCK_D",
    )
    assert result.success, result.reason
    assert result.result_kind == PathAcquisitionResultKind.REACHED_REQUESTED_TARGET
    assert dispatcher.get_path(result.path_id).get_authority_end_block_id() == "BLOCK_D"


def test_extend_path_holds_no_growth_when_already_at_cap():
    """extend_path toward a farther target with the cap at the current authority
    end returns BLOCKED_NO_GROWTH and does not grow past the cap."""
    network = _linear_network()
    train = _train_at_a(network)
    dispatcher = _dispatcher_with_seeded_head(network, train)

    created = dispatcher.create_path(
        train_id=train.train_id, requested_destination_block_id="BLOCK_C",
    )
    assert created.success, created.reason
    path_id = created.path_id
    assert dispatcher.get_path(path_id).get_authority_end_block_id() == "BLOCK_C"

    ext = dispatcher.extend_path(
        path_id=path_id,
        requested_destination_block_id="BLOCK_E",
        acquisition_cap_block_id="BLOCK_C",
    )
    assert ext.success is True
    assert ext.result_kind == PathAcquisitionResultKind.BLOCKED_NO_GROWTH
    assert dispatcher.get_path(path_id).get_authority_end_block_id() == "BLOCK_C"


def test_extend_path_caps_ahead_of_authority_end():
    """extend_path with a cap ahead of the current authority end grows only to
    the cap and reports TRUNCATED_GREW, keeping the requested target."""
    network = _linear_network()
    train = _train_at_a(network)
    dispatcher = _dispatcher_with_seeded_head(network, train)

    created = dispatcher.create_path(
        train_id=train.train_id, requested_destination_block_id="BLOCK_B",
    )
    assert created.success, created.reason
    path_id = created.path_id

    ext = dispatcher.extend_path(
        path_id=path_id,
        requested_destination_block_id="BLOCK_E",
        acquisition_cap_block_id="BLOCK_D",
    )
    assert ext.success is True, ext.reason
    assert ext.result_kind == PathAcquisitionResultKind.TRUNCATED_GREW
    path = dispatcher.get_path(path_id)
    assert path.get_authority_end_block_id() == "BLOCK_D"
    assert path.requested_destination_block_id == "BLOCK_E"


# --- create_path() / extend_path() return contract ---------------------------
def test_failed_acquisition_result_nulls_every_field_but_failure_kind_and_reason():
    """design_new.md "`create_path()` / `extend_path()` return contract":
    "On failure, `success = False`, `path_id = None`, `resource_sequence = None`,
    all target fields are `None`, `result_kind = None`, `failure_kind` is
    required, and `reason` is required."

    A rejection must not echo the caller's request back as if it were stored
    authority. The diagnostic context lives in `reason`, which callers may
    display but must never branch on.
    """
    from runtime_payloads import PathAcquisitionFailureKind

    dispatcher = PathDispatcher(RailwayNetwork())
    result = dispatcher._reject(
        failure_kind=PathAcquisitionFailureKind.INVALID_REQUEST,
        reason="no such block",
        requested_destination_direction="forward",
        requested_destination_block_id="BLOCK_E",
        path_id="PATH_1",
        authority_end_block_id="BLOCK_C",
    )

    assert result.success is False
    assert result.failure_kind is PathAcquisitionFailureKind.INVALID_REQUEST
    assert result.reason  # required

    assert result.path_id is None
    assert result.resource_sequence is None
    assert result.result_kind is None
    assert result.requested_destination_block_id is None
    assert result.requested_destination_direction is None
    assert result.authority_end_block_id is None

    # Nothing diagnostic was lost — it moved into `reason`.
    assert "PATH_1" in result.reason
    assert "BLOCK_E" in result.reason
    assert "BLOCK_C" in result.reason

    # and it serializes as null, not []
    assert result.to_dict()["resource_sequence"] is None
