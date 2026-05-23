"""Scene that drives DEMO_TRAIN through the north-branch turnaround
end-state: origin -> North Station -> BLOCK_BRANCH_2 dead-end -> back
to North Station. The return leg auto-flips the cab, so the train ends
parked at North Station facing south.

Used by tests/test_north_station_caution_turnaround_journey.py to pin the
direction-aware CAUTION clear: at the end state, ``BLOCK_BRANCH_1``
(south of the station) must be ``FREE`` because the OCCUPIED station
block holds a train whose ``travel_direction`` is now ``S`` — opposite
the ``N`` ``caution.exit_edge`` recorded when the train first arrived.
``BLOCK_BRANCH_2`` (north of the station) must still be ``CAUTION``
because its ``S`` ``caution.exit_edge`` matches the train's current
``travel_direction`` (train is still moving away from BRANCH_2's
protection chain in the same direction the protection was recorded).

A single multi-stop route with no dwells avoids the wall-clock dwell
timer in route_executor that the V2 demo route relies on.
"""

from train_route import (
    BlockTarget,
    PlatformTarget,
    Route,
    RouteMode,
    RouteTask,
)

from .base import SceneFixture
from .demo_scene import DemoScene


class NorthStationCautionTurnaroundScene:
    """Scene factory: origin -> North Station -> BLOCK_BRANCH_2 -> back."""

    train_id: str = "DEMO_TRAIN"
    route_id: str = "NORTH_CAUTION_TURNAROUND_ROUTE"

    def build(self) -> SceneFixture:
        fixture = DemoScene().build()
        sim = fixture.sim_controller
        network = fixture.network
        train_id = fixture.train_id

        # DemoScene re-points the train at DEMO_ROUTE_V1; clear both demo
        # routes so this scene owns the single active route.
        for rid in ("DEMO_ROUTE_V1", "DEMO_ROUTE_V2"):
            r = sim.route_executor.route_registry.get(rid)
            if r is not None:
                r.is_active = False
                r.unassign_train(train_id)

        north_platform = next(
            p for p in network.stations.values() if p.name == "North Station"
        )

        route = Route(
            route_id=self.route_id,
            name="origin -> North Station -> BLOCK_BRANCH_2 -> back",
            mode=RouteMode.ONE_OFF,
            is_active=True,
        )
        route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=PlatformTarget(
                    platform_id=north_platform.platform_id
                ),
                task_direction="either",
            )
        )
        route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=BlockTarget(block_id="BLOCK_BRANCH_2"),
                task_direction="either",
            )
        )
        route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=PlatformTarget(
                    platform_id=north_platform.platform_id
                ),
                task_direction="either",
            )
        )
        sim.route_executor.register_route(route)
        route.assign_train(train_id)

        return fixture
