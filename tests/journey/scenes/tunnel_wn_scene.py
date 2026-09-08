"""TunnelWNScene — verifies the pathfinder + lookahead handle a tunnel
that changes the train's cardinal heading mid-hop.

A W↔N tunnel is harder than an E↔W one because (a) `(dx, dy)` between
consecutive path tiles is meaningless across the hop, and (b) the
destination tile's actual entry edge ("N") is not the cardinal opposite
of the previous tile's exit edge ("W"). The pathfinder's "extend through
the destination block" walk used to break on (b); the resulting path
covered only the entry tile of the destination block, the lookahead
reported a one-tile clearance, and the train braked to a stop near the
tunnel mouth instead of proceeding to the far end of the block.

Layout:

    BLOCK_WEST (horizontal, EW)
    [1,0]──[2,0]──[3,0]
       │
    free dead-end at (1,0).W                            tunnel ↕
                                            BLOCK_NORTH (vertical, NS)
                                                  [11,10]
                                                  [11, 9]
                                                  [11, 8]
                                                    │
                                            free dead-end at (11,8).S
"""

from train_route import BlockTarget, Route, RouteMode, RouteTask
from bootstrap import bootstrap_runtime
from tests.segment_test_helpers import make_segment_block_with_tracks
from track import TRACK_EW, TRACK_NS, Tile

from .base import SceneFixture


class TunnelWNScene:
    train_id: str = "WN_TRAIN"
    west_block_id: str = "BLOCK_WEST"
    north_block_id: str = "BLOCK_NORTH"
    route_id: str = "WN_ROUTE"

    def build(self) -> SceneFixture:
        network, _, sim_controller, _ = bootstrap_runtime(
            demo=False, setup_train=False
        )

        for x in range(1, 4):
            network.add_track(Tile(x, 0, track_bits=TRACK_EW))
        for y in (10, 9, 8):
            network.add_track(Tile(11, y, track_bits=TRACK_NS))

        make_segment_block_with_tracks(
            network,
            block_id=self.west_block_id,
            positions=[(1, 0), (2, 0), (3, 0)],
            name="west",
            length_cm=150.0,
            speed_limit_kmh=30.0,
        )
        make_segment_block_with_tracks(
            network,
            block_id=self.north_block_id,
            positions=[(11, 10), (11, 9), (11, 8)],
            name="north",
            length_cm=150.0,
            speed_limit_kmh=30.0,
        )

        network.add_tunnel((1, 0, "W"), (11, 10, "N"))

        sim_controller.add_train(
            train_id=self.train_id,
            starting_block_id=self.west_block_id,
            initial_direction="WEST",
            max_speed_kmh=30.0,
            length_cm=20.0,
            initial_cab="A",
        )

        route = Route(
            route_id=self.route_id,
            name="WN tunnel destination route",
            mode=RouteMode.ONE_OFF,
            is_active=True,
        )
        route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=BlockTarget(block_id=self.north_block_id),
                task_direction="either",
            )
        )
        sim_controller.route_executor.register_route(route)
        route.assign_train(self.train_id)

        controller = sim_controller.get_or_create_controller(self.train_id)
        controller.commands.enqueue_command("power_on")
        controller.commands.enqueue_command("set_mode", {"mode": "ato"})

        return SceneFixture(
            network=network,
            sim_controller=sim_controller,
            train_id=self.train_id,
        )
