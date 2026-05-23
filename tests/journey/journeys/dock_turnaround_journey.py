"""Journey: drive DEMO_TRAIN through east block -> (0,0) dock -> North
Station, pinning the design contracts that govern a turn-around at a
no-platform dock.

Regression coverage:
  * design_new.md:703 / :1751 — CAUTION is a TrainController speed cap,
    not an obstruction. A train must be able to re-enter a block that
    is in stale CAUTION; otherwise the train cannot reach a dock it
    previously departed.
  * design_new.md:1145 — `task_direction == "either"` for a BlockTarget
    picks whichever block-relative direction is reachable from the
    current span. The (0,0) dead-end can only be reached "reverse"
    from the east; resolving "either" to "forward" by route walk
    direction alone (the previous behaviour) leaves the path requested
    in an unreachable direction, so stop-completion at the dock never
    fires.

Asserts at the key transitions:
  - reaches (0,0): cab is "B" (auto-flipped from "A" when planning the
    westward leg after task 0 finished).
  - reaches North Station (3,2): cab is "A" again (auto-flipped at the
    (0,0) dock after task 1 finished).
  - route completes: the run-removed-after-assigned transition fires,
    controller settles in READY.
"""

from enums import TrainState

from ..assertions import assert_cab, assert_state
from ..events import Event, Journey
from ..invariants import no_eb, no_overspeed, state_cab_pair_valid
from ..triggers import head_visited, route_run_removed_after_assigned


def _returned_to_after(tile, gate_tile):
    """Fire once the head reaches `tile` after first visiting `gate_tile`.

    Needed because the train starts at the dock (0,0); a plain
    `head_visited((0,0))` would fire on tick 0 before any movement.
    """
    state = {"gate_seen": False, "fired": False}

    def trigger(snapshot) -> bool:
        if state["fired"]:
            return True
        if snapshot.head_tile == gate_tile:
            state["gate_seen"] = True
        if state["gate_seen"] and snapshot.head_tile == tile:
            state["fired"] = True
            return True
        return False

    return trigger


def build_dock_turnaround_journey(
    train_id: str = "DEMO_TRAIN",
) -> Journey:
    return Journey(
        train_id=train_id,
        events=[
            Event(
                name="reaches_dock_at_0_0",
                # The train starts at (0,0); fire only on the return.
                # Pins design_new.md:703/:1751 — clearance walks through a
                # cautioned own-start block instead of truncating there.
                trigger=_returned_to_after((0, 0), gate_tile=(5, 0)),
                assertions=[assert_cab("B")],
            ),
            Event(
                name="reaches_north_station",
                # Pins design_new.md:1145 — "either" for the (0,0) task is
                # resolved to "reverse" by geometry, the stop completes,
                # and the auto cab-flip from READY turns the train around.
                trigger=head_visited((3, 2)),
                assertions=[assert_cab("A")],
            ),
            Event(
                name="route_completes",
                # Pins the single-tile destination completion: stop check
                # passes even though `block_direction_for_compass_exit` and
                # the pathfinder's `_terminal_direction` use independent
                # conventions for single-tile blocks (design_new.md:687
                # defines direction only against multi-tile ordered_tiles).
                trigger=route_run_removed_after_assigned(),
                assertions=[assert_state(TrainState.READY)],
            ),
        ],
        invariants=[
            no_eb(),
            no_overspeed(),
            state_cab_pair_valid(),
        ],
    )
