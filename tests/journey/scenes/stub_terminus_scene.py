"""Scene: a two-platform dead-end terminus reached through one throat switch.

Purpose — confirm the *current* v1 behaviour when the platform a train is
routed to is already occupied by a parked, route-less train:

    approach (W→E)              ┌─ P_EAST  (5,0)  MAIN, straight — SHORTEST
    (0,0)(1,0)(2,0)(3,0) ─ SW ──┤
                          (4,0) └─ P_NORTH (4,1)(4,2)  DIVERGE — longer
    BLK_APPROACH_W | BLK_APPROACH_E              both platforms are stubs

The throat switch SW_THROAT at (4,0) carries the MAIN leg straight east to the
single-tile platform P_EAST and the DIVERGE leg north to the two-tile platform
P_NORTH. Both platforms are grouped into one station STN_TERM, so a route that
targets the station (a `StationTarget`) may resolve to either platform. P_EAST
is strictly the best target (fewer tiles, shorter walk, straight run), so the
station resolver ranks it first.

Trains:
  * A — parked on P_EAST with no route (its inbound run already ended there),
    so it occupies P_EAST_BLOCK and never moves. It is the "route-less"
    occupant the reversal-trap exclusion treats as exempt.
  * B — the hero. Powered, ATO, engaged, routed to STN_TERM. Its best target
    is the occupied P_EAST. The journey confirms B holds on the approach
    (clear of the throat switch) and does NOT divert to the free P_NORTH.

The station resolver's availability filter (`disabled` / `manual`) does not
read occupancy, and an extend blocked only by occupancy returns a no-growth
hold (`success = True`), so B never excludes P_EAST and never walks the
same-tick candidate list down to P_NORTH.
"""

from track import Tile, TRACK_EW, TRACK_NS, TRACK_NW
from switch import Switch
from enums import SwitchState
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from entity_ids import segment_id_for_block
from speed_profile import SpeedProfile, DEFAULT_SPEED_PROFILE
from network import RailwayNetwork
from train_route import Route, RouteMode, RouteTask, StationTarget, PlatformTarget

from .base import SceneFixture


# Plain (non-switch, non-platform) tiles: (x, y, bits).
_PLAIN_TILES = [
    (0, 0, TRACK_EW),   # BLK_APPROACH_W
    (1, 0, TRACK_EW),
    (2, 0, TRACK_EW),   # BLK_APPROACH_E
    (3, 0, TRACK_EW),
]

# Platform tiles — added bare here (with bits), attached to a platform/block
# by add_station below.
_PLATFORM_TILES = [
    (5, 0, TRACK_EW),   # P_EAST  (MAIN, straight, single tile)
    (4, 1, TRACK_NS),   # P_NORTH (DIVERGE, north)
    (4, 2, TRACK_NS),
]

# Approach split into two blocks so B rolls up to the block before the switch
# and holds there, rather than holding on its spawn block.
_BLOCKS = [
    ("BLK_APPROACH_W", [(0, 0), (1, 0)], "Approach W", 100.0, 50.0),
    ("BLK_APPROACH_E", [(2, 0), (3, 0)], "Approach E", 100.0, 50.0),
]


class StubTerminusScene:
    """Scene factory for the occupied-dead-end-platform behaviour test.

    Parameters select which of the three confirmed cases to build:
      * ``occupy_north=False, target="station"`` — the best platform (P_EAST)
        is occupied, P_NORTH is free, B is routed to the STATION. Expected:
        B DIVERTS to the free P_NORTH (occupancy-aware station resolution).
      * ``occupy_north=True, target="station"`` — BOTH platforms occupied, B
        routed to the station. Expected: B HOLDS before the throat switch.
      * ``occupy_north=False, target="platform_east"`` — only P_EAST occupied
        (P_NORTH free), but B is routed to the SPECIFIC platform P_EAST.
        Expected: B HOLDS before the throat switch (no alternative — a
        PlatformTarget names one block, so the station resolver's free-platform
        preference never applies).
    """

    train_id: str = "B"          # hero train the harness snapshots
    occupant_id: str = "A"       # parked route-less occupant of P_EAST
    north_occupant_id: str = "C"  # optional parked occupant of P_NORTH
    station_id: str = "STN_TERM"
    best_platform_block: str = "P_EAST_BLOCK"
    free_platform_block: str = "P_NORTH_BLOCK"
    switch_tile = (4, 0)
    approach_hold_block: str = "BLK_APPROACH_E"
    route_id: str = "R_B_TO_TERM"

    def __init__(self, occupy_north: bool = False, target: str = "station"):
        assert target in ("station", "platform_east")
        self.occupy_north = occupy_north
        self.target = target

    def build(self) -> SceneFixture:
        network = RailwayNetwork()

        # -- tracks --
        for (x, y, bits) in _PLAIN_TILES + _PLATFORM_TILES:
            network.add_track(Tile(x, y, track_bits=bits))

        # -- throat switch: entry W -> MAIN exit E (EW), DIVERGE exit N (NW) --
        sx, sy = self.switch_tile
        sw_tile = Tile(sx, sy, track_bits=TRACK_EW | TRACK_NW)
        network.add_track(sw_tile)
        switch = Switch(switch_id="SW_THROAT", x=sx, y=sy)
        switch.position = SwitchState.MAIN
        network.add_switch(switch)
        sw_tile.switch_id = switch.switch_id

        # -- approach blocks --
        for (block_id, positions, name, length_cm, speed) in _BLOCKS:
            network.create_segment_with_tracks(
                block_id=block_id,
                segment_id=segment_id_for_block(block_id),
                positions=positions,
                name=name,
                length_cm=length_cm,
                speed_limit_kmh=speed,
            )

        # -- platforms + station group --
        network.add_station(
            x=5, y=0, station_id="P_EAST", name="East (stub)",
            length_cm=60.0, platform_side="right", top_speed_kmh=40.0,
        )
        p_north = network.add_station(
            x=4, y=1, station_id="P_NORTH", name="North (stub)",
            length_cm=60.0, platform_side="right", top_speed_kmh=40.0,
        )
        network.extend_station(p_north.platform_id, [(4, 2)])
        network.create_station_group(
            station_id=self.station_id,
            name="Terminus",
            platform_ids=["P_EAST", "P_NORTH"],
        )

        # -- runtime --
        dispatcher = PathDispatcher(network)
        sim = SimulationController(network, dispatcher)

        profile = SpeedProfile(
            profile_id="stub_profile",
            name="Stub Terminus Profile",
            power_speed_curve=list(DEFAULT_SPEED_PROFILE.power_speed_curve),
            voltage_power_curve=list(DEFAULT_SPEED_PROFILE.voltage_power_curve),
            lighting_voltage=DEFAULT_SPEED_PROFILE.lighting_voltage,
            acceleration_rate=3.0,
            deceleration_rate=4.2,
            emergency_brake_rate=5.0,
            friction_rate=DEFAULT_SPEED_PROFILE.friction_rate,
            operational_decel_ratio=DEFAULT_SPEED_PROFILE.operational_decel_ratio,
            air_resistance_enabled=True,
            drag_coefficient=0.001,
            reference_speed_kmh=100.0,
        )
        network.speed_profiles.register(profile)

        # -- A: parked route-less occupant of the best platform --
        # Facing WEST: A's head exits toward the switch (a real neighbour) and
        # its 50 cm body fits within the 60 cm platform tile. This is also how a
        # train that arrived into the stub sits to reverse back out.
        occupant = sim.add_train(
            train_id=self.occupant_id,
            starting_block_id=self.best_platform_block,
            initial_direction="WEST",
            max_speed_kmh=40.0,
            length_cm=50.0,
            train_type="LOCOMOTIVE",
            initial_cab="A",
            start_tile=(5, 0),
        )
        occupant.speed_profile_id = profile.profile_id
        occ_ctrl = sim.get_or_create_controller(occupant.train_id)
        occ_ctrl.commands.enqueue_command("power_on")  # manual, no route -> sits

        # -- C: optional parked route-less occupant of P_NORTH --
        if self.occupy_north:
            # Facing SOUTH at (4,1): head exits toward the switch, body extends
            # north into (4,2), so C fills P_NORTH_BLOCK.
            north = sim.add_train(
                train_id=self.north_occupant_id,
                starting_block_id=self.free_platform_block,
                initial_direction="SOUTH",
                max_speed_kmh=40.0,
                length_cm=50.0,
                train_type="LOCOMOTIVE",
                initial_cab="A",
                start_tile=(4, 1),
            )
            north.speed_profile_id = profile.profile_id
            sim.get_or_create_controller(
                north.train_id
            ).commands.enqueue_command("power_on")

        # -- B: hero, ATO, routed to the station (or a specific platform) --
        task_location = (
            StationTarget(self.station_id)
            if self.target == "station"
            else PlatformTarget("P_EAST")
        )
        route = Route(
            route_id=self.route_id,
            name="B -> Terminus",
            mode=RouteMode.ONE_OFF,
            is_active=True,
        )
        route.add_task(
            RouteTask(
                task_kind="stop",
                task_location=task_location,
                task_direction="either",
            )
        )
        sim.route_executor.register_route(route)

        hero = sim.add_train(
            train_id=self.train_id,
            starting_block_id="BLK_APPROACH_W",
            initial_direction="EAST",
            max_speed_kmh=40.0,
            length_cm=50.0,
            train_type="LOCOMOTIVE",
            initial_cab="A",
            start_tile=(1, 0),
        )
        hero.speed_profile_id = profile.profile_id
        route.assign_train(hero.train_id)
        hero_ctrl = sim.get_or_create_controller(hero.train_id)
        hero_ctrl.commands.enqueue_command("power_on")
        hero_ctrl.commands.enqueue_command("set_mode", {"mode": "ato"})
        hero_ctrl.commands.enqueue_command("ato_go")

        return SceneFixture(
            network=network,
            sim_controller=sim,
            train_id=self.train_id,
        )
