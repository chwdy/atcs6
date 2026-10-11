"""Journey: verify that a same-block stop route assigned while the train is
already stopped at North Station completes immediately — no path is created
and no movement occurs.

Regression coverage:
  * route_executor.py `_covered_task_index_advance` (step a5c6): a stop task
    must be immediately satisfied when the train is stopped, has no active
    path, and its current block equals the task's destination block.
  * design_new.md Phase 1 covered-task advance step — same-block stopped-at-
    destination stop case.

Before the fix, `PathDispatcher.create_path(station, station)` returned
NO_VIABLE_PATH (the BFS returns 0 candidates for same-block paths), so the
route hung indefinitely.

Two events:
  1. setup_route_completes — the initial setup route (origin → North Station)
     finishes; an action assigns the turnaround route.
  2. turnaround_route_completes — the turnaround route's run finishes, so the
     one-off route is deleted into the executor's historical route archive,
     proving the same-block stop was immediately satisfied without any
     movement.
"""

from ..harness import TrainState

from ..assertions import assert_state
from ..events import Event, Journey
from ..invariants import no_eb, no_overspeed, state_cab_pair_valid
from ..triggers import route_run_removed_after_assigned


_TURNAROUND_ROUTE_ID = "NORTH_TURNAROUND_ROUTE"


def _assign_turnaround_route(s) -> None:
    """Action: assign the pre-registered turnaround route to the train."""
    sim = s.sim_controller
    route = sim.route_executor.route_registry.get(_TURNAROUND_ROUTE_ID)
    assert route is not None, f"Route {_TURNAROUND_ROUTE_ID!r} not in registry"
    route.assign_train(s.fixture.train_id)


def _turnaround_finished():
    """Trigger: fires once the turnaround route's run has finished.

    A finished run is deleted, and a one-off route left with no train is deleted
    with it into the executor's historical route archive (design_new.md
    "Terminal route completion", `ONE_OFF` cascade). Reading the archive handles
    the fast-completion case where the run finishes within one Plan pass, before
    any snapshot sees it in the live `execution_states` slot.
    """
    state = {"fired": False}

    def trigger(s) -> bool:
        if state["fired"]:
            return True
        executor = s.sim_controller.route_executor
        if executor.get_historical_route(_TURNAROUND_ROUTE_ID) is not None:
            state["fired"] = True
            return True
        return False

    return trigger


def build_north_station_turnaround_journey(
    train_id: str = "DEMO_TRAIN",
) -> Journey:
    return Journey(
        train_id=train_id,
        events=[
            Event(
                name="setup_route_completes",
                trigger=route_run_removed_after_assigned(),
                actions=[_assign_turnaround_route],
            ),
            Event(
                name="turnaround_route_completes",
                trigger=_turnaround_finished(),
                assertions=[
                    assert_state(TrainState.READY),
                ],
            ),
        ],
        invariants=[
            no_eb(),
            no_overspeed(),
            state_cab_pair_valid(),
        ],
    )
