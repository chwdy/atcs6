"""Scene for same-block turnaround regression: get DEMO_TRAIN to North
Station via a no-dwell setup route, then leave the turnaround route in the
registry unassigned so the journey can assign it at the right moment.

The setup route (NORTH_SETUP_ROUTE) gets the train from its (0,0) origin to
the North Station platform with one stop and no dwell. The turnaround route
(NORTH_TURNAROUND_ROUTE) is a single-stop route at the same North Station
platform block, simulating the operator assigning a new route while the train
is already at the destination.
"""

from train_route import (
    PlatformTarget,
    Route,
    RouteMode,
    RouteTask,
)

from .base import SceneFixture
from .demo_scene import DemoScene


class NorthStationTurnaroundScene:
    """Scene factory: train travels to North Station via setup route; turnaround
    route is pre-registered but unassigned (journey assigns it via action)."""

    train_id: str = "DEMO_TRAIN"
    setup_route_id: str = "NORTH_SETUP_ROUTE"
    turnaround_route_id: str = "NORTH_TURNAROUND_ROUTE"

    def build(self) -> SceneFixture:
        fixture = DemoScene().build()
        sim = fixture.sim_controller
        network = fixture.network
        train_id = fixture.train_id

        # DemoScene may have left V1 or V2 active; deactivate both.
        for rid in ("DEMO_ROUTE_V1", "DEMO_ROUTE_V2"):
            r = sim.route_executor.route_registry.get(rid)
            if r is not None:
                r.is_active = False
                r.unassign_train(train_id)

        north_platform_obj = next(
            p for p in network.stations.values() if p.name == "North Station"
        )
        north_platform = north_platform_obj.platform_id

        # Setup route: travel to North Station, no dwell.
        setup_route = Route(
            route_id=self.setup_route_id,
            name="Setup: origin → North Station",
            mode=RouteMode.ONE_OFF,
            is_active=True,
        )
        setup_route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=PlatformTarget(platform_id=north_platform),
                task_direction="forward",
            )
        )
        sim.route_executor.register_route(setup_route)
        setup_route.assign_train(train_id)

        # Turnaround route: same platform, same-block stop (regression target).
        # Direction "reverse" was one of the two cases the user reported failing.
        turnaround_route = Route(
            route_id=self.turnaround_route_id,
            name="Turnaround: same-block stop at North Station",
            mode=RouteMode.ONE_OFF,
            is_active=True,
        )
        turnaround_route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=PlatformTarget(platform_id=north_platform),
                task_direction="forward",
            )
        )
        sim.route_executor.register_route(turnaround_route)
        # Not assigned yet — the journey action assigns it after setup completes.

        return fixture
