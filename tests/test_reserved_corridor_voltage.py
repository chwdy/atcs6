"""Pre-energized reserved corridor: a train's reserved-ahead blocks carry the
same `track_voltage` as the blocks it occupies, and a block no train holds is
de-energized to 0V.

Why this exists
---------------
On a DC block layout each block is fed its own voltage. Before this behavior,
voltage emission only ever drove a train's *occupied* blocks, so the reserved
corridor ahead sat at 0V and a block in the train's wake kept its last commanded
voltage forever. The first meant a loco would cross a block boundary into a dead
block (losing power at the insulated joint); the second left a growing trail of
stuck-energized track behind every train.

design_new.md "Power", Block power-controller role now specifies:
  * each tick `TrainPhysics` drives one `track_voltage` onto every block the
    train holds — occupied blocks *plus* the reserved-ahead corridor
    (`Block.reserved_for_train`) — so the next block is already energized; and
  * `BlockPowerController.deenergize_unheld_blocks()` returns any block with no
    `occupied_by` and no `reserved_for_train` to 0V once per tick.

This test drives the `demo_4` 4-train loop and asserts both, as invariants over
the whole run plus the concrete pre-energize / de-energize transitions.
"""

from tests.journey.scenes.demo_4_scene import Demo4Scene

# Above the ~2.5V lighting voltage: a block at this level is being driven for
# movement, not merely lit. Used to recognize the "running" energized state.
_RUNNING_V = 3.0
_TOL = 0.02


def _voltage(network, block_id: str) -> float:
    block = network.blocks.get(block_id)
    return float(getattr(block, "current_voltage", 0.0)) if block else 0.0


def _occupied_blocks(sim, train_id):
    ctrl = sim.get_controller(train_id)
    span = ctrl.get_current_span() if ctrl else None
    return list(span.occupied_blocks) if span else []


def _reserved_blocks(network, train_id):
    return [
        bid
        for bid, blk in network.blocks.items()
        if getattr(blk, "reserved_for_train", None) == train_id
    ]


def test_reserved_corridor_is_pre_energized_and_wake_is_de_energized():
    fixture = Demo4Scene().build()
    network = fixture.network
    sim = fixture.sim_controller

    parity_violations = []  # (tick, train, block, v_reserved, v_occupied)
    hot_orphans = []  # (tick, block, voltage)
    index_drift = []  # (tick, train, symmetric_difference)
    saw_preenergized_reserved = False
    saw_deenergized_wake = False

    prev_voltage = {}  # block_id -> voltage at end of previous tick

    for tick in range(1, 420):
        sim.update(0.1)

        for train_id in network.trains:
            occupied = _occupied_blocks(sim, train_id)
            if not occupied:
                continue
            # All occupied blocks of one train share a voltage; use the first.
            v_occ = _voltage(network, occupied[0])
            for bid in _reserved_blocks(network, train_id):
                v_res = _voltage(network, bid)
                # Invariant 1: reserved-ahead carries the occupied voltage.
                if abs(v_res - v_occ) > _TOL:
                    parity_violations.append((tick, train_id, bid, v_res, v_occ))
                # Feature 1: at least once, a reserved block is driven at the
                # running voltage (not just lit / not 0) — i.e. pre-energized.
                if abs(v_res) > _RUNNING_V and abs(v_res - v_occ) <= _TOL:
                    saw_preenergized_reserved = True

        # Index invariant: BlockManager's per-train reserved index must equal
        # the live Block.reserved_for_train fields exactly — the bidirectional
        # index must never drift from its source of truth.
        bm = getattr(network, "block_manager", None)
        if bm is not None:
            for train_id in network.trains:
                idx = set(bm.reserved_blocks_for_train(train_id))
                truth = {
                    bid
                    for bid, blk in network.blocks.items()
                    if getattr(blk, "reserved_for_train", None) == train_id
                }
                if idx != truth:
                    index_drift.append((tick, train_id, sorted(idx ^ truth)))

        for bid, blk in network.blocks.items():
            v = _voltage(network, bid)
            held = (
                getattr(blk, "occupied_by", None) is not None
                or getattr(blk, "reserved_for_train", None) is not None
            )
            # Invariant 2: a block no train holds is never left energized.
            if not held and abs(v) > 0.0:
                hot_orphans.append((tick, bid, v))
            # Feature 2: a block that was running-energized last tick and is now
            # unheld reads 0V — the wake de-energized on release/vacate.
            if (
                not held
                and abs(v) <= _TOL
                and abs(prev_voltage.get(bid, 0.0)) > _RUNNING_V
            ):
                saw_deenergized_wake = True

        prev_voltage = {bid: _voltage(network, bid) for bid in network.blocks}

    assert not parity_violations, (
        "reserved-ahead blocks must carry the occupied-block voltage; first few "
        f"violations: {parity_violations[:5]}"
    )
    assert not hot_orphans, (
        "blocks held by no train must be de-energized to 0V; first few "
        f"hot orphans: {hot_orphans[:5]}"
    )
    assert not index_drift, (
        "BlockManager reserved index must match Block.reserved_for_train; first "
        f"few drifts (tick, train, symmetric_diff): {index_drift[:5]}"
    )
    assert saw_preenergized_reserved, (
        "expected at least one reserved-ahead block driven at running voltage "
        "(pre-energized corridor) during the run"
    )
    assert saw_deenergized_wake, (
        "expected at least one block to drop from running voltage to 0V once no "
        "train held it (wake de-energized)"
    )
