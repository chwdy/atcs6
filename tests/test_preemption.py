"""Preemption: a strictly higher-priority run takes reserved-but-unlocked track
from a lower-priority one (design_new.md "Preemption").

Drives the dispatcher's preemption decision directly with a hand-built victim
reservation and injected run priorities, so the logic is exercised without a
full two-train junction sim. The victim `P_LOCAL` reserves BLK_2..BLK_4 with
only BLK_2 locked (its braking floor); BLK_3/BLK_4 are reserved-but-unlocked —
the preemptible tail. `T_EXPRESS` is higher priority than `T_LOCAL`.
"""

from types import SimpleNamespace

from block_manager import ReservedBlockTraversal
from path import Path, ResourceSequenceEntry
from path_dispatcher import PathDispatcher
from runtime_payloads import (
    PathSegment,
    ValidationError,
    ValidationErrorType,
    ValidationResult,
)

from tests.test_block_occupancy_cap import build_line, place


def _fwd(exit_edge="E"):
    return ReservedBlockTraversal(exit_edge=exit_edge, block_direction="forward")


def _setup(priorities, local_reserved, local_locked, local_seq):
    """A wired line + a hand-built victim reservation P_LOCAL, with run
    priorities injected onto the real route executor."""
    network = build_line(6)
    sim, _express, _controller, _physics = place(
        network, head_x=0, length_cm=40.0, train_id="T_EXPRESS"
    )
    # place() builds a SimulationController with no dispatcher; wire one that
    # resolves its route executor (and its priorities) through the sim.
    disp = PathDispatcher(network)
    disp.wire_dependencies(
        route_executor=sim.route_executor,
        notification_center=sim.notification_center,
        controllers=sim.controllers,
    )
    sim.route_executor.effective_priority_for = lambda tid: priorities.get(tid, 5)
    bm = sim.block_manager
    bm.reserve_blocks_for_path(
        local_reserved, "P_LOCAL", "T_LOCAL",
        directions={b: _fwd() for b in local_reserved},
    )
    if local_locked:
        bm.lock_blocks_for_path(local_locked, "P_LOCAL")
    disp.paths["P_LOCAL"] = Path(
        path_id="P_LOCAL",
        train_id="T_LOCAL",
        resource_sequence=[
            ResourceSequenceEntry(sequence_index=i, resource_type="block", resource_id=b)
            for i, b in enumerate(local_seq)
        ],
        current_resource_index=0,
        network=network,
    )
    return network, disp


def _reserved_conflict(block_id):
    """A failed validation whose sole error is `block_id` reserved by another."""
    return ValidationResult(
        valid=False,
        errors=[
            ValidationError(
                type=ValidationErrorType.BLOCK_RESERVED,
                block_id=block_id,
                locked_by_path="P_LOCAL",
                message=f"Block {block_id} is reserved",
            )
        ],
    )


def _express_segment(block_id, x):
    return PathSegment(tiles=[(x, 0)], blocks=[block_id])


# --- _can_preempt (priority half) ----------------------------------------


def test_can_preempt_only_when_strictly_higher():
    _network, disp = _setup(
        {"T_EXPRESS": 9, "T_LOCAL": 5}, ["BLK_2"], ["BLK_2"], ["BLK_1", "BLK_2"]
    )
    assert disp._can_preempt("T_EXPRESS", "T_LOCAL") is True
    # Equal priority never preempts (first-come-first-served).
    disp._route_executor.effective_priority_for = (
        lambda tid: 5
    )
    assert disp._can_preempt("T_EXPRESS", "T_LOCAL") is False
    # Lower priority never preempts.
    disp._route_executor.effective_priority_for = (
        lambda tid: {"T_EXPRESS": 3, "T_LOCAL": 5}.get(tid, 5)
    )
    assert disp._can_preempt("T_EXPRESS", "T_LOCAL") is False


# --- _apply_preemption ----------------------------------------------------


def test_higher_priority_takes_a_reserved_but_unlocked_block():
    network, disp = _setup(
        {"T_EXPRESS": 9, "T_LOCAL": 5},
        ["BLK_2", "BLK_3", "BLK_4"],
        ["BLK_2"],
        ["BLK_1", "BLK_2", "BLK_3", "BLK_4"],
    )
    result = disp._apply_preemption(
        _reserved_conflict("BLK_3"), _express_segment("BLK_3", 3),
        "P_EXP", "T_EXPRESS", False,
    )
    # The express's segment now validates — BLK_3 was taken.
    assert result.valid is True
    # The victim was truncated back to its lock end (BLK_2), never cancelled.
    assert disp.paths["P_LOCAL"].get_reservation_end_block_id() == "BLK_2"
    assert network.blocks["BLK_3"].reserved_by_path is None
    assert network.blocks["BLK_4"].reserved_by_path is None
    # Its locked floor block is untouched.
    assert network.blocks["BLK_2"].locked_by_path == "P_LOCAL"


def test_equal_priority_never_preempts():
    network, disp = _setup(
        {"T_EXPRESS": 5, "T_LOCAL": 5},
        ["BLK_2", "BLK_3", "BLK_4"],
        ["BLK_2"],
        ["BLK_1", "BLK_2", "BLK_3", "BLK_4"],
    )
    result = disp._apply_preemption(
        _reserved_conflict("BLK_3"), _express_segment("BLK_3", 3),
        "P_EXP", "T_EXPRESS", False,
    )
    # No preemption: the failed validation is returned unchanged and the victim
    # keeps its whole reservation (first-come-first-served).
    assert result.valid is False
    assert network.blocks["BLK_3"].reserved_by_path == "P_LOCAL"


def test_a_locked_block_is_never_taken():
    network, disp = _setup(
        {"T_EXPRESS": 9, "T_LOCAL": 5},
        ["BLK_2", "BLK_3", "BLK_4"],
        ["BLK_2", "BLK_3"],  # BLK_3 is LOCKED — committed track
        ["BLK_1", "BLK_2", "BLK_3", "BLK_4"],
    )
    result = disp._apply_preemption(
        _reserved_conflict("BLK_3"), _express_segment("BLK_3", 3),
        "P_EXP", "T_EXPRESS", False,
    )
    assert result.valid is False
    assert network.blocks["BLK_3"].reserved_by_path == "P_LOCAL"
    assert network.blocks["BLK_3"].locked_by_path == "P_LOCAL"


# --- switch-preemption building blocks -----------------------------------


def _entry(i, rtype, rid):
    return ResourceSequenceEntry(sequence_index=i, resource_type=rtype, resource_id=rid)


def test_forward_resource_index_picks_the_forward_occurrence():
    _network, disp = _setup({}, ["BLK_2"], [], ["BLK_1", "BLK_2"])
    path = Path(
        path_id="P",
        resource_sequence=[
            _entry(0, "block", "B0"), _entry(1, "switch", "SW1"),
            _entry(2, "block", "B1"), _entry(3, "switch", "SW1"),
        ],
        current_resource_index=0,
    )
    assert disp._forward_resource_index(path, "switch", "SW1") == 1
    assert disp._forward_resource_index(path, "block", "B1") == 2
    assert disp._forward_resource_index(path, "block", "NOPE") is None
    # A lapping path holds SW1 twice; from a head past the first, the forward
    # occurrence is the later index.
    path.current_resource_index = 2
    assert disp._forward_resource_index(path, "switch", "SW1") == 3


def test_lock_end_index_is_the_lock_end_or_the_head():
    network, disp = _setup(
        {}, ["BLK_2", "BLK_3"], ["BLK_2"], ["BLK_1", "BLK_2", "BLK_3"]
    )
    # Lock end is BLK_2 at index 1.
    assert disp._lock_end_index(disp.paths["P_LOCAL"]) == 1
    # A path that locks nothing floors at the head index.
    unlocked = Path(
        path_id="P2",
        resource_sequence=[_entry(0, "block", "BLK_1"), _entry(1, "block", "BLK_2")],
        current_resource_index=0,
        network=network,
    )
    assert disp._lock_end_index(unlocked) == 0


def test_switch_committed_by_train_reads_occupancy_and_protection():
    _network, disp = _setup({}, ["BLK_2"], [], ["BLK_1", "BLK_2"])
    occupied = SimpleNamespace(
        physical_occupants={"route_a": "T_LOCAL"}, train_protection_claims=[]
    )
    assert disp._switch_committed_by_train(occupied, "T_LOCAL") is True
    assert disp._switch_committed_by_train(occupied, "T_OTHER") is False
    protected = SimpleNamespace(
        physical_occupants={}, train_protection_claims=[("T_LOCAL", "SW_SRC", 1)]
    )
    assert disp._switch_committed_by_train(protected, "T_LOCAL") is True
    free = SimpleNamespace(physical_occupants={}, train_protection_claims=[])
    assert disp._switch_committed_by_train(free, "T_LOCAL") is False
