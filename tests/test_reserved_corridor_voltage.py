"""One block of pre-energized corridor: a train's *next* reserved block carries
the same `track_voltage` as the blocks it occupies, the rest of the corridor
carries none, and a block no train holds is de-energized to 0V.

Parity here is parity of magnitude. A commanded block voltage is signed, and the
sign is the block-relative direction the train runs through that block, so two
blocks drawn in opposite senses carry opposite signs at the same magnitude
(design_new.md "Power", A block voltage carries a direction). It is the
magnitude that must not gap at an insulated joint.

Why this exists
---------------
On a DC block layout each block is fed its own voltage. Two failures bracket the
correct behavior, and this test pins the band between them.

Energize too little and a loco crosses a block boundary into a dead block,
losing power at the insulated joint, while a block in its wake keeps its last
commanded voltage forever — a growing trail of stuck-energized track.

Energize too much and the layout stops enforcing the app's own authority. A
speed profile describes the locomotive it was calibrated for, so a real train
reaches a seam before the model says it has; with the whole reserved corridor
live it keeps accelerating over every block of it while the app still believes
it is a block back, and the detector confirmations arrive as a correction after
the fact.

design_new.md "Power", Block power-controller role specifies the band:
  * each tick `TrainPhysics` drives one `track_voltage` onto the blocks the
    train stands on plus the single next block of its reserved corridor, so the
    boundary in front of the loco is never dead;
  * no block of the corridor beyond that one is energized, so an overrun is
    bounded to one block and the rail past the authority end is dead; and
  * a block that leaves that set — its holder cleared, or it stopped being the
    next one ahead — is returned to 0V by the post-train drain.

This test drives the `demo_4` 4-train loop and asserts all three, as invariants
over the whole run plus the concrete pre-energize / de-energize transitions.
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


def _next_reserved(sim, train_id):
    physics = sim.physics_instances.get(train_id)
    return physics.next_reserved_block if physics is not None else None


def test_one_block_of_corridor_is_pre_energized_and_the_wake_is_de_energized():
    fixture = Demo4Scene().build()
    network = fixture.network
    sim = fixture.sim_controller

    parity_violations = []  # (tick, train, block, v_next, v_occupied)
    live_corridor = []  # (tick, train, block, voltage) — energized too far ahead
    hot_orphans = []  # (tick, block, voltage)
    index_drift = []  # (tick, train, symmetric_difference)
    saw_preenergized_next = False
    saw_reserved_corridor_beyond_the_next = False
    saw_deenergized_wake = False

    prev_voltage = {}  # block_id -> voltage at end of previous tick

    for tick in range(1, 420):
        sim.update(0.1)

        # The calculated half of the emission set, gathered over every train:
        # the blocks their spans cover plus the one block each is entering. A
        # span block leads debounced `Block.occupied_by`, so `occupied_by` alone
        # would read a legitimately driven block as an orphan.
        driven = set()
        for train_id in network.trains:
            occupied = _occupied_blocks(sim, train_id)
            driven.update(occupied)
            next_reserved = _next_reserved(sim, train_id)
            if next_reserved is not None:
                driven.add(next_reserved)
            pending = sim.physics_instances[train_id].pending_entry_block
            if pending is not None:
                driven.add(pending)
            if not occupied:
                continue
            # All occupied blocks of one train share a magnitude; use the first.
            v_occ = abs(_voltage(network, occupied[0]))

            if next_reserved is not None:
                v_next = abs(_voltage(network, next_reserved))
                # Invariant 1: the next reserved block carries the occupied
                # magnitude, so the boundary in front of the loco does not gap.
                if abs(v_next - v_occ) > _TOL:
                    parity_violations.append(
                        (tick, train_id, next_reserved, v_next, v_occ)
                    )
                # Feature 1: at least once, that block is driven at the running
                # voltage (not just lit / not 0) — i.e. genuinely pre-energized.
                if v_next > _RUNNING_V and abs(v_next - v_occ) <= _TOL:
                    saw_preenergized_next = True

            for bid in _reserved_blocks(network, train_id):
                if bid == next_reserved or bid in occupied:
                    continue
                saw_reserved_corridor_beyond_the_next = True
                # Invariant 2: no block of the corridor past the next one is
                # energized. This is what bounds an overrun to one block.
                if abs(_voltage(network, bid)) > _TOL:
                    live_corridor.append(
                        (tick, train_id, bid, _voltage(network, bid))
                    )

        # Index invariant: BlockManager's per-train reserved index must equal
        # the live Block.reserved_for_train fields exactly — the bidirectional
        # index must never drift from its source of truth.
        bm = getattr(network, "block_manager", None)
        if bm is not None:
            for train_id in network.trains:
                idx = set(bm.reserved_blocks_for_train(train_id))
                truth = set(_reserved_blocks(network, train_id))
                if idx != truth:
                    index_drift.append((tick, train_id, sorted(idx ^ truth)))

        for bid, blk in network.blocks.items():
            v = _voltage(network, bid)
            held = getattr(blk, "occupied_by", None) is not None or bid in driven
            # Invariant 3: a block no train stands on or is entering is never
            # left energized.
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
        "the next reserved block must carry the occupied-block magnitude; first "
        f"few violations: {parity_violations[:5]}"
    )
    assert not live_corridor, (
        "no reserved block past the next one may be energized — live rail "
        "further ahead than the train is authorized to be this instant is rail "
        f"an overrunning train keeps accelerating over; first few: {live_corridor[:5]}"
    )
    assert not hot_orphans, (
        "blocks no train stands on or is entering must be de-energized to 0V; "
        f"first few hot orphans: {hot_orphans[:5]}"
    )
    assert not index_drift, (
        "BlockManager reserved index must match Block.reserved_for_train; first "
        f"few drifts (tick, train, symmetric_diff): {index_drift[:5]}"
    )
    assert saw_preenergized_next, (
        "expected at least one next-reserved block driven at running voltage "
        "(pre-energized boundary) during the run"
    )
    assert saw_reserved_corridor_beyond_the_next, (
        "the run never held a corridor longer than one block, so the "
        "no-live-rail-further-ahead invariant was never actually exercised"
    )
    assert saw_deenergized_wake, (
        "expected at least one block to drop from running voltage to 0V once no "
        "train held it (wake de-energized)"
    )
