"""`Path.reaches_target(block_id)` — the canonical "does this authority reach
that block?" read.

design_new.md "Path field schema": `True` when `block_id` appears as a `block`
entry in `resource_sequence` at a `sequence_index` at or after
`current_resource_index`.

It is deliberately not an equality test against `get_reservation_end_block_id()`.
Callers that asked "is this authority short of its target?" by comparing the
target with the authority end got the answer wrong for an authority that
legitimately runs *past* its target — reached, not missed. Entries before
`current_resource_index` are the train's own covered span behind the head, so
they never count.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_path_reaches_target.py -v
"""

from path import Path, ResourceSequenceEntry


def _path(*entries, current_resource_index: int = 0) -> Path:
    """A Path over `(resource_type, resource_id)` pairs, indexed in order."""
    return Path(
        path_id="PATH_1",
        train_id="T1",
        resource_sequence=[
            ResourceSequenceEntry(
                sequence_index=i, resource_type=kind, resource_id=rid
            )
            for i, (kind, rid) in enumerate(entries)
        ],
        current_resource_index=current_resource_index,
    )


def test_the_authority_end_is_reached():
    path = _path(("block", "A"), ("block", "B"))

    assert path.reaches_target("B") is True


def test_a_target_the_authority_runs_past_is_reached():
    """The case an `authority_end == target` equality test gets wrong: the
    authority was grown beyond the target, so the target is reached."""
    path = _path(("block", "A"), ("block", "B"), ("block", "C"))

    assert path.get_reservation_end_block_id() == "C"
    assert path.reaches_target("B") is True


def test_a_block_behind_the_head_is_not_reached():
    """Entries before `current_resource_index` are the train's own covered span
    behind the head — track it has already run through, not authority ahead."""
    path = _path(("block", "A"), ("block", "B"), current_resource_index=1)

    assert path.reaches_target("A") is False


def test_the_block_at_the_head_is_reached():
    """The rule is "at or after", so the head's own resource counts."""
    path = _path(("block", "A"), ("block", "B"), current_resource_index=1)

    assert path.reaches_target("B") is True


def test_a_block_not_in_the_sequence_is_not_reached():
    path = _path(("block", "A"), ("block", "B"))

    assert path.reaches_target("Z") is False


def test_a_switch_of_that_id_does_not_count():
    """Only `block` entries answer the question — a live authority end is
    always block-terminated (design_new.md "Path invariants")."""
    path = _path(("block", "A"), ("switch", "X"))

    assert path.reaches_target("X") is False


def test_a_lapping_path_reaches_the_occurrence_ahead_of_the_head():
    """One `resource_id` may sit at more than one `sequence_index` once a run
    laps back into a block it already passed and released. The occurrence
    behind the head does not count; the one ahead does."""
    path = _path(
        ("block", "A"),
        ("block", "B"),
        ("block", "A"),
        current_resource_index=1,
    )

    assert path.reaches_target("A") is True


def test_a_lapping_path_does_not_reach_an_occurrence_wholly_behind_it():
    path = _path(
        ("block", "A"),
        ("block", "B"),
        ("block", "A"),
        current_resource_index=3,
    )

    assert path.reaches_target("A") is False


def test_an_empty_target_is_not_reached():
    path = _path(("block", "A"))

    assert path.reaches_target("") is False


def test_an_empty_authority_reaches_nothing():
    path = _path()

    assert path.reaches_target("A") is False
