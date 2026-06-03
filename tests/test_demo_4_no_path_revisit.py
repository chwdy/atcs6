"""Regression test: extend_path must not append candidates that revisit
resources already in the path.

Scenario
--------
In demo_4 with the 4-train CIRCULAR loop already running, force-assign T3 to
the R_DEMO4_S_LINE route (the secondary south-track-only 4-stop route). T3
starts at PLT_S4_N (north track). The S-LINE's first stop is PLT_S1_S
(south track), which requires T3 to swap tracks through one of the
crossovers in MAIN.

After T3 completes its first loop (PLT_S4_N → via X2/X1 swap → PLT_S1_S →
PLT_S2_S → PLT_S3_S → PLT_S4_S → tunnel → PLT_S1_S), it starts the next
loop. At some point on that next loop, T3 wants to traverse a crossover
(say SW_X1 DIVERGE SE for PLT_S1_S → PLT_S2_S) but the crossover is held
in a conflicting state by another train's lock.

Pre-fix behaviour
-----------------
BEST_EFFORT extend_path returned a candidate that LOOPS the network (via
the south tunnel back through T3's start block) and used MAIN at the
contested crossover, then DIVERGE at the *second* visit of that crossover
after the loop. _best_effort_truncate truncated that candidate to the
12-resource closed loop and appended it to T3's resource_sequence every
Phase-1 tick — growing the path by 12 entries per tick (12,000+ entries
over a 1000-tick window) without the train moving.

When the train finally entered the crossover, physics followed the
*current* switch position rather than the path's planned direction, so the
train exited the switch onto a block not in its path. ATC dropped to 0
and EB fired.

Post-fix expectation (per design_new.md "Path extension" lines 1040,
1041, 1083: "repeated resources in a single path are invalid and should
be rejected during both create_path() and extend_path()")
------------------------------------------------------------------------
extend_path's candidate filter rejects any forward candidate whose
post-seam tile walk re-enters a block or switch already held by the
path. With no acceptable candidate, extend_path returns
`failure_kind=no_viable_path` and leaves the existing path unchanged.
T3 sits at its truncated authority end, no EB, no resource_sequence
bloat. Eventually the contested crossover lock clears (the offending
train moves on) and extend_path appends a clean direct suffix.

Three assertions pin the fix:
1. T3's `len(resource_sequence)` never exceeds a small threshold across
   the full run. Pre-fix it grew without bound; post-fix it stays
   bounded by the actual geometry of T3's route (16 resources is the
   longest single inter-stop segment in S-LINE).
2. T3 never EBs across the run.
3. No train (T1..T4) ever physically leaves its authority: its head is
   never on a block outside its active path, and never on a crossover
   switch tile locked by another path. This pins the silent-overrun
   variant of the same corrupt-path root cause -- the operator-observed
   case where a train failed to stop ahead of a switch locked in the
   opposite state and rolled onto the other track WITHOUT an EB. The
   corrupt/bloated path was what let the train claim authority past the
   switch; with the path bounded (assertion 1) the train stops at its
   truncated authority end and this assertion holds.
"""

import contextlib
import io
import time

import pytest

from bootstrap import bootstrap_runtime


pytestmark = pytest.mark.scene("demo_4")


# Maximum legitimate single-extend resource_sequence growth on demo_4's
# S-LINE-style routing: the longest direct segment between any two stops
# is roughly 6 resources (e.g. PLT_S1_S -> BLK -> SW -> BLK -> PLT_S2_S);
# across a full S-LINE loop it can reach ~16 once accumulated. Anything
# beyond ~40 means the path is being grown with repeated resources.
MAX_RESONABLE_RESOURCE_SEQUENCE_LEN = 40

# The two scissors crossovers in MAIN. Bug #3 is about a train rolling onto
# one of these while it is locked in the opposite state by another train.
CROSSOVERS = ("SW_X1", "SW_X2")
ALL_TRAINS = ("T1", "T2", "T3", "T4")


def _setup_demo_4():
    """Build demo_4 and back-date task_runs so dwell timers are already
    elapsed when ticking starts."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, dispatcher, sim, _persistence = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_4"
        )
    executor = sim.route_executor
    ten_minutes_ago = time.time() - 600.0
    for exec_state in executor.execution_states.values():
        exec_state.started_at = ten_minutes_ago
        for run in exec_state.task_runs:
            if run.finished_at is not None:
                run.finished_at = ten_minutes_ago
    return network, sim


def _reassign_t3_to_s_line(sim):
    """Mimic the operator force-assigning T3 to R_DEMO4_S_LINE."""
    t3 = sim.get_or_create_controller("T3")
    has_path = sim.path_dispatcher.active_path_id_by_train("T3") is not None
    t3.route_hooks.phase_latches.set_route_abort(
        reason="probe reassign", has_active_path=has_path
    )


def _assign_s_line(sim):
    s_line = sim.route_executor.route_registry.get("R_DEMO4_S_LINE")
    assert s_line is not None, "R_DEMO4_S_LINE not registered by demo_4 bootstrap"
    s_line.assign_train("T3")


@pytest.mark.parametrize("dt,label", [(0.1, "1x")])
def test_t3_s_line_reassignment_does_not_grow_repeated_resource_path(dt, label):
    """T3 reassigned to S-LINE must not accumulate repeated resources in
    its path, and must not EB during the run."""
    network, sim = _setup_demo_4()
    dispatcher = sim.path_dispatcher

    t3_reassigned = False
    max_len_observed = 0
    eb_events: list = []
    overrun_events: list = []

    # ~150 s of sim time at dt=0.1; enough for at least one full network
    # loop and several Phase-1 extend cycles, but bounded so the test runs
    # quickly. Pre-fix the bug manifested by tick 800 (every tick added 12
    # entries).
    max_ticks = max(50, int(150.0 / dt))
    for tick in range(1, max_ticks):
        sim.update(dt)

        if tick == 30:
            _reassign_t3_to_s_line(sim)
        if tick == 60 and not t3_reassigned:
            _assign_s_line(sim)
            t3_reassigned = True

        t3 = sim.get_or_create_controller("T3")
        if t3.eb.active:
            head_loc = t3._head_span_locator()
            head = (
                tuple(head_loc.tile)
                if head_loc is not None and head_loc.tile is not None
                else None
            )
            eb_events.append((tick, head))

        path = dispatcher.get_active_path("T3")
        if path is not None:
            seq_len = len(path.resource_sequence)
            if seq_len > max_len_observed:
                max_len_observed = seq_len

        # Bug #3 guard: across all four trains, the head must stay within the
        # train's own authority. Two off-authority signatures:
        #   - head on a normal block not in the path's acquired blocks
        #     (rolled fully past the authority end), or
        #   - head on a crossover switch tile whose locks belong to another
        #     path (rolled onto a switch the train does not own).
        for tid in ALL_TRAINS:
            tpath = dispatcher.get_active_path(tid)
            if tpath is None:
                continue
            ctrl = sim.get_or_create_controller(tid)
            head_tile = ctrl._get_head_tile()
            if not head_tile:
                continue
            track = network.tracks.get(head_tile)
            if track is None:
                continue
            if track.is_switch and track.switch_id in CROSSOVERS:
                sw = network.switches.get(track.switch_id)
                if sw is not None and sw.locks:
                    lock_owner_paths = {key[0] for key in sw.locks}
                    if tpath.path_id not in lock_owner_paths:
                        overrun_events.append(
                            (tick, tid, "intrusion", track.switch_id,
                             tpath.path_id, int(sw.position))
                        )
            elif not track.is_switch:
                head_block = track.block_section_id
                if (head_block is not None
                        and head_block not in set(tpath.get_all_blocks())):
                    overrun_events.append(
                        (tick, tid, "off_authority", head_block,
                         tpath.get_authority_end_block_id(), None)
                    )

    # Assertion 1: path stays bounded.
    assert max_len_observed <= MAX_RESONABLE_RESOURCE_SEQUENCE_LEN, (
        f"T3's path.resource_sequence grew to {max_len_observed} entries "
        f"across {max_ticks} ticks (threshold "
        f"{MAX_RESONABLE_RESOURCE_SEQUENCE_LEN}). Pre-fix extend_path "
        f"appended a closed-loop 12-resource segment every Phase-1 tick "
        f"because find_ordered_walk returned candidates that re-enter the "
        f"path's start block via the south tunnel. Per design_new.md "
        f"lines 1040, 1041, 1083 extend_path must reject any extension "
        f"that would cause a resource to appear twice in the combined path."
    )

    # Assertion 2: no EB across the run.
    assert not eb_events, (
        f"T3 EB'd during the run: {eb_events}. Pre-fix the train "
        f"physically entered a crossover whose position did not match the "
        f"path's planned direction (the path was corrupted by the "
        f"loop-accumulation bug) and emerged off-path, dropping ATC to "
        f"zero and tripping EB."
    )

    # Assertion 3: no train ever leaves its authority (silent-overrun guard).
    assert not overrun_events, (
        f"A train physically left its authority during the run: "
        f"{overrun_events[:10]}"
        f"{' ...' if len(overrun_events) > 10 else ''}. "
        f"Each tuple is (tick, train, kind, resource, path_or_authority, "
        f"switch_pos). 'off_authority' means the head reached a block not in "
        f"the train's path; 'intrusion' means the head rolled onto a crossover "
        f"locked by another path. This is the operator-observed silent overrun "
        f"(no EB): a train that failed to stop ahead of a switch locked in the "
        f"opposite state. It shares the corrupt/bloated-path root cause with "
        f"the EB variant; assertion 1 keeping the path bounded is what lets the "
        f"train stop at its truncated authority end instead of claiming "
        f"authority through the switch."
    )
