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
  2. turnaround_route_completes — the turnaround route appears in the executor's
     historical archive as FINISHED, proving the same-block stop was immediately
     satisfied without any movement.
"""

from ..harness import TrainState
from train_route import RouteRunStatus

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


def _turnaround_finished_in_archive():
    """Trigger: fires once the turnaround route run appears as FINISHED in the
    executor's historical archive.

    This handles the fast-completion case where the route finishes within one
    Phase 1 tick (the run is moved to _historical_runs before the next snapshot
    can see it in the live execution_states slot).
    """
    state = {"fired": False}

    def trigger(s) -> bool:
        if state["fired"]:
            return True
        executor = s.sim_controller.route_executor
        for run in executor._historical_runs:
            if (
                run.route_id == _TURNAROUND_ROUTE_ID
                and run.current_route_status == RouteRunStatus.FINISHED
            ):
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
                trigger=_turnaround_finished_in_archive(),
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
