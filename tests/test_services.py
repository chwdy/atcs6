"""Service-layer unit tests.

These exercise the service functions directly, without going through
HTTP. They are the architectural win of the refactor: the same code path
the Flask handlers use is now callable from any plain Python context
(CLI tools, journey tests, scripted scenarios) and raises typed
exceptions instead of returning Flask responses.

Run with: ``conda run -n general python -m pytest tests/test_services.py -q``
"""

from datetime import datetime
import threading

import pytest

from app_state import AppState
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController
from track import Tile
from train import Train, TrainCar
from train_route import RouteTask

from entity_ids import block_id_for_segment, segment_id_for_block
from services import ConflictError, GoneError, NotFoundError, ValidationError
from tests.segment_test_helpers import make_segment_block, make_segment_block_with_tracks
from services import (
    block_service,
    platform_service,
    segment_service,
    speed_profile_service,
    switch_service,
    track_service,
    train_route_service,
    train_service,
)


@pytest.fixture
def state():
    """Fresh AppState with no HTTP layer attached."""
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    persistence_manager = PersistenceManager(storage_dir="layouts")

    return AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        simulation_lock=threading.Lock(),
        simulation_running=False,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )


@pytest.fixture
def state_with_train(state):
    """AppState with one train registered."""
    car = TrainCar(car_id="CAR_1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id="TRAIN_A", consist=[car], max_speed=80.0)
    state.network.add_train(train)
    return state


# ---------------------------------------------------- train_route_service ----


class TestTrainRouteService:
    def test_create_route_no_http(self, state):
        """`create_route` registers a Route and returns it. No Flask required."""
        route = train_route_service.create_route(
            state,
            route_id="ROUTE_T1",
            name="Test Route",
            mode="one_off",
        )

        assert route.route_id == "ROUTE_T1"
        assert route.name == "Test Route"
        assert route.mode.value == "one_off"

        registry = state.sim_controller.route_executor.route_registry
        assert "ROUTE_T1" in registry
        assert registry["ROUTE_T1"] is route

    def test_create_route_invalid_mode_raises_validation(self, state):
        """An unknown mode string surfaces as ValidationError."""
        with pytest.raises(ValidationError):
            train_route_service.create_route(
                state, route_id="ROUTE_BAD", mode="not_a_mode"
            )

    def test_update_then_delete_route(self, state_with_train):
        """`update_route` flips name; `delete_route` clears the registry."""
        train_route_service.create_route(
            state_with_train, route_id="ROUTE_T2", name="Initial"
        )
        updated = train_route_service.update_route(
            state_with_train, "ROUTE_T2", name="Renamed"
        )
        assert updated.name == "Renamed"
        assert updated.last_edited_at is not None

        train_route_service.delete_route(state_with_train, "ROUTE_T2")
        registry = state_with_train.sim_controller.route_executor.route_registry
        assert "ROUTE_T2" not in registry

    def test_assigning_train_to_new_route_unassigns_previous(self, state_with_train):
        """design_new.md "Route assignment writers" — a train is assigned to
        at most one route. A route create/update that assigns an already-
        assigned train unassigns it from its previous route first (no
        double-assignment across routes)."""
        r1 = train_route_service.create_route(
            state_with_train, route_id="ROUTE_R1", name="r1",
            assigned_train_ids=["TRAIN_A"],
        )
        assert "TRAIN_A" in r1.assigned_train_ids

        # Creating a second route assigned to the same train moves the assignment.
        r2 = train_route_service.create_route(
            state_with_train, route_id="ROUTE_R2", name="r2",
            assigned_train_ids=["TRAIN_A"],
        )
        assert "TRAIN_A" in r2.assigned_train_ids
        assert "TRAIN_A" not in r1.assigned_train_ids

        # Updating R1 to re-add the train moves it back (and off R2).
        train_route_service.update_route(
            state_with_train, "ROUTE_R1", assigned_train_ids=["TRAIN_A"],
        )
        assert "TRAIN_A" in r1.assigned_train_ids
        assert "TRAIN_A" not in r2.assigned_train_ids

    def test_require_route_raises_not_found(self, state):
        """`require_route` raises NotFoundError; `get_route` returns None."""
        assert train_route_service.get_route(state, "MISSING") is None
        with pytest.raises(NotFoundError):
            train_route_service.require_route(state, "MISSING")

    def test_activate_route_with_no_tasks_rejected(self, state):
        """`activate_route` rejects 0-task routes with ValidationError.

        Pins the fix for the empty-route auto-finish bug: Phase 1 step
        `9c4f` would otherwise allocate a RouteRun for an empty walk,
        `_find_next_task_target` would return None on the first tick,
        and `finish_route_run` would immediately deactivate the route
        (for ONE_OFF mode) and unassign all its trains — leaving the
        user with a "successfully activated" toast and a silently
        broken route.
        """
        train_route_service.create_route(
            state, route_id="ROUTE_EMPTY", name="empty", mode="one_off"
        )
        with pytest.raises(ValidationError, match="no tasks"):
            train_route_service.activate_route(state, "ROUTE_EMPTY")


class TestRouteDefinitionValidation:
    """design_new.md "Route-definition validation": the dwell-follow rule, the
    CIRCULAR / ROUND_TRIP distinct-location guard, and the priority range are
    enforced at create/update; non-dwell-exists and the terminal-task
    requirement are enforced at activation (an incomplete draft may still be
    saved)."""

    def _stop(self, loc):
        return {"task_kind": "stop", "task_location": loc}

    def _dwell(self, loc):
        return {"task_kind": "dwell", "task_location": loc, "task_time": 5.0}

    def _waypoint(self, loc):
        return {"task_kind": "waypoint", "task_location": loc}

    def test_create_rejects_dwell_not_following_stop_or_run(self, state):
        with pytest.raises(ValidationError, match="directly follow a stop or run"):
            train_route_service.create_route(
                state, route_id="R", mode="one_off",
                tasks=[self._waypoint("A"), self._dwell("A")],
            )

    def test_create_rejects_dwell_at_different_location(self, state):
        with pytest.raises(ValidationError, match="same task_location"):
            train_route_service.create_route(
                state, route_id="R", mode="one_off",
                tasks=[self._stop("A"), self._dwell("B")],
            )

    def test_create_rejects_circular_with_one_location(self, state):
        with pytest.raises(ValidationError, match="two non-dwell tasks"):
            train_route_service.create_route(
                state, route_id="R", mode="circular",
                tasks=[self._stop("A"), self._stop("A")],
            )

    def test_create_rejects_round_trip_with_one_location(self, state):
        with pytest.raises(ValidationError, match="two non-dwell tasks"):
            train_route_service.create_route(
                state, route_id="R", mode="round_trip",
                tasks=[self._stop("A"), self._waypoint("A")],
            )

    def test_create_rejects_priority_out_of_range(self, state):
        with pytest.raises(ValidationError, match="0..9"):
            train_route_service.create_route(
                state, route_id="R", mode="one_off",
                tasks=[self._stop("A")], priority=15,
            )

    def test_create_accepts_valid_stop_dwell_and_distinct_circular(self, state):
        # A terminal stop followed by a dwell at the same location is valid.
        train_route_service.create_route(
            state, route_id="R1", mode="one_off",
            tasks=[self._stop("A"), self._dwell("A")],
        )
        # A CIRCULAR route with two distinct locations is valid.
        train_route_service.create_route(
            state, route_id="R2", mode="circular",
            tasks=[self._stop("A"), self._stop("B")],
        )

    def test_create_allows_incomplete_draft(self, state):
        # A waypoint-only ONE_OFF has no terminal stop/run, but that is an
        # activation-time completeness check — the draft may still be created.
        train_route_service.create_route(
            state, route_id="R", mode="one_off",
            tasks=[self._waypoint("A")],
        )

    def test_activate_rejects_non_circular_without_terminal_stop(self, state):
        train_route_service.create_route(
            state, route_id="R", mode="one_off",
            tasks=[self._waypoint("A"), self._waypoint("B")],
        )
        with pytest.raises(ValidationError, match="end in a stop or run"):
            train_route_service.activate_route(state, "R")


# ------------------------------------------------- speed_profile_service ----


class TestSpeedProfileService:
    def test_list_and_get_seeded_default(self, state):
        """A fresh registry seeds a `default` profile the service can read."""
        assert speed_profile_service.get_profile(state, "default") is not None
        assert len(speed_profile_service.list_profiles(state)) >= 1

    def test_create_profile_registers(self, state):
        """`create_profile` registers a profile callable without HTTP."""
        profile = speed_profile_service.create_profile(
            state, data={"profile_id": "P1", "name": "Profile 1"}
        )
        assert profile.profile_id == "P1"
        assert speed_profile_service.get_profile(state, "P1") is not None

    def test_registry_is_the_network_store(self, state):
        """The service registry and the physics store are one object.

        `AppState.speed_profile_registry` returns `network.speed_profiles`,
        which is what `TrainPhysics`/`TrainController._get_speed_profile`
        read. A profile created through the service must therefore be visible
        on that read path immediately -- no second store to sync.
        """
        assert state.speed_profile_registry is state.network.speed_profiles
        created = speed_profile_service.create_profile(
            state, data={"profile_id": "P_PHYS", "name": "Physics", "acceleration_rate": 7.77}
        )
        seen_by_physics = state.network.speed_profiles.get("P_PHYS")
        assert seen_by_physics is created
        assert seen_by_physics.acceleration_rate == 7.77

    def test_create_duplicate_raises_conflict(self, state):
        """A repeated profile_id surfaces as ConflictError (HTTP 409)."""
        speed_profile_service.create_profile(
            state, data={"profile_id": "P1", "name": "Profile 1"}
        )
        with pytest.raises(ConflictError):
            speed_profile_service.create_profile(
                state, data={"profile_id": "P1", "name": "Dup"}
            )

    def test_create_missing_fields_raises_validation(self, state):
        """Both profile_id and name are required."""
        with pytest.raises(ValidationError):
            speed_profile_service.create_profile(state, data={"name": "No id"})
        with pytest.raises(ValidationError):
            speed_profile_service.create_profile(state, data={"profile_id": "P2"})

    def test_require_profile_raises_not_found(self, state):
        """`require_profile` raises; `get_profile` returns None."""
        assert speed_profile_service.get_profile(state, "MISSING") is None
        with pytest.raises(NotFoundError):
            speed_profile_service.require_profile(state, "MISSING")

    def test_update_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            speed_profile_service.update_profile(state, "MISSING", data={"name": "x"})

    def test_update_id_mismatch_raises_validation(self, state):
        """A body profile_id that disagrees with the URL id is rejected."""
        speed_profile_service.create_profile(
            state, data={"profile_id": "P1", "name": "Profile 1"}
        )
        with pytest.raises(ValidationError):
            speed_profile_service.update_profile(
                state, "P1", data={"profile_id": "OTHER", "name": "x"}
            )

    def test_delete_default_raises_conflict(self, state):
        """The seeded `default` profile may never be deleted."""
        with pytest.raises(ConflictError):
            speed_profile_service.delete_profile(state, "default")

    def test_delete_in_use_raises_conflict(self, state_with_train):
        """A profile assigned to a train cannot be deleted."""
        speed_profile_service.create_profile(
            state_with_train, data={"profile_id": "INUSE", "name": "In Use"}
        )
        state_with_train.network.trains["TRAIN_A"].speed_profile_id = "INUSE"
        with pytest.raises(ConflictError):
            speed_profile_service.delete_profile(state_with_train, "INUSE")

    def test_delete_success(self, state):
        speed_profile_service.create_profile(
            state, data={"profile_id": "P9", "name": "Temp"}
        )
        assert speed_profile_service.delete_profile(state, "P9") is True
        assert speed_profile_service.get_profile(state, "P9") is None


# -------------------------------------------------------- switch_service ----


@pytest.fixture
def state_with_switch_track(state):
    """AppState with one switch-track tile at (5, 0).

    track_bits = EW | NE (0b000110) makes the tile a switch: it has both a
    long path (EW) and a short path (NE), so switch_points is non-empty and
    `is_switch` is True -- the same shape demo_3 uses for a switch tile.
    """
    state.network.add_track(Tile(x=5, y=0, track_bits=0b000110))
    return state


class TestSwitchService:
    def test_create_switch_at_track_is_idempotent(self, state_with_switch_track):
        """First call creates `SW_5_0`; a second returns the same switch."""
        state = state_with_switch_track
        switch = switch_service.create_switch_at_track(state, 5, 0, data={})
        assert switch.switch_id == "SW_5_0"
        assert state.network.get_switch("SW_5_0") is switch

        again = switch_service.create_switch_at_track(state, 5, 0, data={})
        assert again is switch
        assert len(state.network.switches) == 1

    def test_create_switch_no_track_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            switch_service.create_switch_at_track(state, 9, 9, data={})

    def test_create_switch_non_switch_track_raises_validation(self, state):
        """A straight (EW-only) tile is a track but not a switch."""
        state.network.add_track(Tile(x=1, y=0, track_bits=2))
        with pytest.raises(ValidationError):
            switch_service.create_switch_at_track(state, 1, 0, data={})

    def test_update_switch_forwards_config_fields(self, state_with_switch_track):
        """Non-availability fields forward to RailwayNetwork.update_switch."""
        state = state_with_switch_track
        switch_service.create_switch_at_track(state, 5, 0, data={})
        updated = switch_service.update_switch(
            state, "SW_5_0", data={"name": "Renamed", "main_length_cm": 12.5}
        )
        assert updated.name == "Renamed"
        assert updated.main_length_cm == 12.5

    def test_disable_occupied_switch_raises_conflict(self, state_with_switch_track):
        """Enabling `disabled` on an occupied switch is rejected (HTTP 409)."""
        state = state_with_switch_track
        switch = switch_service.create_switch_at_track(state, 5, 0, data={})
        switch.add_route_occupant(frozenset({"N", "S"}), "TRAIN_A")
        with pytest.raises(ConflictError):
            switch_service.update_switch(state, "SW_5_0", data={"disabled": True})
        assert switch.disabled is False  # rejected, not applied

    def test_disable_free_switch_succeeds(self, state_with_switch_track):
        """A free switch accepts the availability flags."""
        state = state_with_switch_track
        switch_service.create_switch_at_track(state, 5, 0, data={})
        updated = switch_service.update_switch(
            state, "SW_5_0", data={"disabled": True, "manual": True}
        )
        assert updated.disabled is True
        assert updated.manual is True

    def test_delete_switch_missing_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            switch_service.delete_switch(state, "NOPE")

    def test_request_position_change_returns_command_id(self, state_with_switch_track):
        """A valid request echoes the caller's command id (outcome is async)."""
        state = state_with_switch_track
        switch_service.create_switch_at_track(state, 5, 0, data={})
        command_id = switch_service.request_position_change(
            state, "SW_5_0", new_state=1, command_id="CMD-1"
        )
        assert command_id == "CMD-1"

    def test_request_position_change_invalid_state_raises_validation(
        self, state_with_switch_track
    ):
        state = state_with_switch_track
        switch_service.create_switch_at_track(state, 5, 0, data={})
        with pytest.raises(ValidationError):
            switch_service.request_position_change(state, "SW_5_0", new_state=2)

    def test_request_position_change_unknown_switch_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            switch_service.request_position_change(state, "NOPE", new_state=0)


# --------------------------------------------------------- train_service ----


class TestTrainService:
    def test_require_train_raises_not_found(self, state):
        assert train_service.get_train(state, "UNIDENTIFIED") is None
        with pytest.raises(NotFoundError):
            train_service.require_train(state, "UNIDENTIFIED")

    def test_create_train_editor_path(self, state):
        """No-position create builds + registers a train, no Flask required."""
        train = train_service.create_train(state, train_id="T_NEW", max_speed_kmh=90.0)
        assert train.train_id == "T_NEW"
        assert state.network.trains.get("T_NEW") is train
        assert train.max_speed == 90.0

    def test_create_train_missing_id_raises_validation(self, state):
        with pytest.raises(ValidationError):
            train_service.create_train(state, train_id="")

    def test_update_train_fields(self, state_with_train):
        train = train_service.update_train(
            state_with_train, "TRAIN_A", max_speed=55.0, car_number="C9"
        )
        assert train.max_speed == 55.0
        assert train.car_number == "C9"

    def test_update_train_bad_value_raises_validation(self, state_with_train):
        with pytest.raises(ValidationError, match="max_speed"):
            train_service.update_train(
                state_with_train, "TRAIN_A", max_speed="not-a-number"
            )

    def test_update_train_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            train_service.update_train(state, "UNIDENTIFIED", max_speed=10.0)

    def test_delete_train(self, state_with_train):
        assert train_service.delete_train(state_with_train, "TRAIN_A") is True
        assert train_service.get_train(state_with_train, "TRAIN_A") is None

    def test_delete_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            train_service.delete_train(state, "UNIDENTIFIED")

    def test_assign_speed_profile_missing_id_raises_validation(self, state_with_train):
        with pytest.raises(ValidationError):
            train_service.assign_speed_profile(
                state_with_train, "TRAIN_A", profile_id=""
            )

    def test_assign_speed_profile_unknown_profile_raises_not_found(
        self, state_with_train
    ):
        with pytest.raises(NotFoundError):
            train_service.assign_speed_profile(
                state_with_train, "TRAIN_A", profile_id="NOPE"
            )

    def test_assign_speed_profile_success(self, state_with_train):
        """The seeded `default` profile assigns onto the train."""
        train = train_service.assign_speed_profile(
            state_with_train, "TRAIN_A", profile_id="default"
        )
        assert train.speed_profile_id == "default"

    def test_assign_route_unknown_train_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            train_service.assign_route(state, "UNIDENTIFIED", route_id=None)

    def test_assign_route_unknown_route_raises_not_found(self, state_with_train):
        with pytest.raises(NotFoundError):
            train_service.assign_route(state_with_train, "TRAIN_A", route_id="UNIDENTIFIED")

    def test_assign_route_empty_route_raises_validation(self, state_with_train):
        train_route_service.create_route(
            state_with_train, route_id="R_EMPTY", name="empty"
        )
        with pytest.raises(ValidationError, match="no tasks"):
            train_service.assign_route(state_with_train, "TRAIN_A", route_id="R_EMPTY")

    def test_assign_route_success_conflict_and_force(self, state_with_train):
        """Assign succeeds; reassign needs force; same-route is idempotent."""
        state = state_with_train
        r1 = train_route_service.create_route(state, route_id="R1", name="r1")
        r1.add_task(RouteTask(task_kind="dwell", task_location="STA_X"))
        r2 = train_route_service.create_route(state, route_id="R2", name="r2")
        r2.add_task(RouteTask(task_kind="dwell", task_location="STA_Y"))

        train_service.assign_route(state, "TRAIN_A", route_id="R1")
        executor = state.sim_controller.route_executor
        assert executor.assigned_route_id_for("TRAIN_A") == "R1"

        with pytest.raises(ConflictError):
            train_service.assign_route(state, "TRAIN_A", route_id="R2")

        # Same-route assign is an idempotent no-op success.
        train_service.assign_route(state, "TRAIN_A", route_id="R1")
        assert executor.assigned_route_id_for("TRAIN_A") == "R1"

        # Force reassign retargets to R2.
        train_service.assign_route(state, "TRAIN_A", route_id="R2", force=True)
        assert executor.assigned_route_id_for("TRAIN_A") == "R2"

    def test_set_cab_invalid_raises_validation(self, state_with_train):
        with pytest.raises(ValidationError):
            train_service.set_cab(state_with_train, "TRAIN_A", cab="Z")

    def test_set_mode_invalid_raises_validation(self, state_with_train):
        with pytest.raises(ValidationError):
            train_service.set_mode(state_with_train, "TRAIN_A", mode="bogus")

    def test_power_on_returns_command_result(self, state_with_train):
        """Operator commands return the controller's CommandResult."""
        result = train_service.power_on(state_with_train, "TRAIN_A")
        assert hasattr(result, "success")


# --------------------------------------------------------- block_service ----


def _straight_ew(state, xs):
    """Add a straight EW run of tracks at ``xs`` (all joined)."""
    from track import TRACK_EW

    for x in xs:
        state.network.add_track(Tile(x, 0, TRACK_EW))


class TestBlockService:
    """Grouping + config surface. Geometry lives on segments (see below)."""

    def test_require_block_raises_not_found(self, state):
        assert block_service.get_block(state, "UNIDENTIFIED") is None
        with pytest.raises(NotFoundError):
            block_service.require_block(state, "UNIDENTIFIED")

    def test_create_block_groups_segments(self, state):
        _straight_ew(state, range(4))
        state.network.create_segment_with_tracks("SEG_A", [(0, 0), (1, 0)])
        state.network.create_segment_with_tracks("SEG_B", [(2, 0), (3, 0)])
        block = block_service.create_block(
            state, block_id="BLK_1", segment_ids=["SEG_A", "SEG_B"], name="Yard"
        )
        assert block.block_id == "BLK_1"
        assert block.segment_ids == ["SEG_A", "SEG_B"]
        # Grouping removes the members' prior single-segment blocks.
        assert "SEG_A" not in state.network.blocks
        assert "SEG_B" not in state.network.blocks

    def test_create_block_autogenerates_id(self, state):
        _straight_ew(state, [0, 1])
        state.network.create_segment_with_tracks("SEG_ONLY", [(0, 0), (1, 0)])
        block = block_service.create_block(
            state, segment_ids=["SEG_ONLY"], name="auto"
        )
        assert block.block_id.startswith("BLK_")

    def test_create_block_empty_segment_ids_raises(self, state):
        with pytest.raises(ValidationError):
            block_service.create_block(state, block_id="BLK_E", segment_ids=[])

    def test_create_block_unknown_segment_raises(self, state):
        with pytest.raises(ValidationError):
            block_service.create_block(
                state, block_id="BLK_U2", segment_ids=["UNIDENTIFIED"]
            )

    def test_update_block_name(self, state):
        make_segment_block(state.network, "BLK_U", "old")
        updated = block_service.update_block(state, "BLK_U", data={"name": "new"})
        assert updated.name == "new"

    def test_update_block_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            block_service.update_block(state, "UNIDENTIFIED", data={"name": "x"})

    def test_update_block_state_field_raises_gone(self, state):
        """The removed direct `state` mutation surfaces as GoneError (410)."""
        make_segment_block(state.network, "BLK_G")
        with pytest.raises(GoneError):
            block_service.update_block(state, "BLK_G", data={"state": "OCCUPIED"})

    def test_update_block_rejects_geometry_field(self, state):
        """A block config PATCH carrying geometry is rejected (edit the
        segment)."""
        make_segment_block(state.network, "BLK_GEO")
        with pytest.raises(ValidationError):
            block_service.update_block(state, "BLK_GEO", data={"length_cm": 50.0})

    def test_disable_occupied_block_raises_conflict(self, state):
        block = make_segment_block(state.network, "BLK_O")
        block.occupied_by = "TRAIN_A"
        with pytest.raises(ConflictError):
            block_service.update_block(state, "BLK_O", data={"disabled": True})
        assert block.disabled is False  # rejected, not applied

    def test_disable_free_block_succeeds(self, state):
        make_segment_block(state.network, "BLK_F")
        updated = block_service.update_block(
            state, "BLK_F", data={"disabled": True, "manual": True}
        )
        assert updated.disabled is True
        assert updated.manual is True

    def test_allowed_travel_directions_invalid_raises_validation(self, state):
        make_segment_block(state.network, "BLK_D")
        with pytest.raises(ValidationError):
            block_service.update_block(
                state, "BLK_D", data={"allowed_travel_directions": ["sideways"]}
            )

    def test_allowed_travel_directions_valid(self, state):
        make_segment_block(state.network, "BLK_D2")
        updated = block_service.update_block(
            state, "BLK_D2", data={"allowed_travel_directions": ["forward"]}
        )
        assert updated.allowed_travel_directions == {"forward"}

    def test_delete_block_ungroups_to_own_blocks(self, state):
        """Dissolving a grouping re-homes each member into its own block; the
        geometry survives."""
        _straight_ew(state, range(4))
        state.network.create_segment_with_tracks("SEG_A", [(0, 0), (1, 0)])
        state.network.create_segment_with_tracks("SEG_B", [(2, 0), (3, 0)])
        block_service.create_block(
            state, block_id="BLK_1", segment_ids=["SEG_A", "SEG_B"]
        )

        assert block_service.delete_block(state, "BLK_1") is True

        assert block_service.get_block(state, "BLK_1") is None
        # Each freed segment is back in its own single-segment block; the tiles
        # were never deleted.
        assert state.network.get_segment("SEG_A") is not None
        assert state.network.get_segment("SEG_B") is not None
        # A re-homed segment lands in its own block, whose id is the segment's
        # derived onto the block axis — never the same string.
        assert block_id_for_segment("SEG_A") in state.network.blocks
        assert block_id_for_segment("SEG_B") in state.network.blocks
        assert state.network.get_track(0, 0).segment_id == "SEG_A"

    def test_delete_block_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            block_service.delete_block(state, "UNIDENTIFIED")

    def test_add_and_remove_member_segment(self, state):
        _straight_ew(state, range(4))
        state.network.create_segment_with_tracks("SEG_A", [(0, 0), (1, 0)])
        state.network.create_segment_with_tracks("SEG_B", [(2, 0), (3, 0)])
        block_service.create_block(state, block_id="BLK_1", segment_ids=["SEG_A"])

        block = block_service.add_segment_to_block(state, "BLK_1", "SEG_B")
        assert block.segment_ids == ["SEG_A", "SEG_B"]

        assert block_service.remove_segment_from_block(state, "BLK_1", "SEG_B") is True
        assert state.network.get_block("BLK_1").segment_ids == ["SEG_A"]
        # Re-homed to its own block, under the derived block-axis id.
        assert block_id_for_segment("SEG_B") in state.network.blocks

    def test_add_segment_unknown_block_raises_not_found(self, state):
        _straight_ew(state, [0, 1])
        state.network.create_segment_with_tracks("SEG_A", [(0, 0), (1, 0)])
        with pytest.raises(NotFoundError):
            block_service.add_segment_to_block(state, "UNIDENTIFIED", "SEG_A")


# ------------------------------------------------------- segment_service ----


class TestSegmentService:
    """Geometry surface: create-with-tracks, geometry PATCH, tiles, delete."""

    def test_create_segment_with_tracks(self, state):
        _straight_ew(state, range(3))
        block = segment_service.create_segment_with_tracks(
            state, segment_id="SEG_T", positions=[[0, 0], [1, 0], [2, 0]], name="T"
        )
        assert block.segment_ids == ["SEG_T"]
        assert state.network.get_segment("SEG_T") is not None

    def test_update_segment_geometry(self, state):
        _straight_ew(state, [0, 1])
        segment_service.create_segment_with_tracks(
            state, segment_id="SEG_G", positions=[[0, 0], [1, 0]]
        )
        seg = segment_service.update_segment(
            state,
            "SEG_G",
            data={"length_cm": 120.0, "min_radius_cm": 30.0, "grade_percent": 2.0},
        )
        assert seg.length_cm == 120.0
        assert seg.min_radius_cm == 30.0
        assert seg.grade_percent == 2.0

    def test_update_segment_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            segment_service.update_segment(state, "UNIDENTIFIED", data={"length_cm": 1.0})

    def test_delete_segment_cascades_to_owning_platform(self, state):
        # A station/platform owning exactly one block: deleting the block's only
        # segment empties the block, which must drop it from the platform's
        # block_assignments and, since that empties the platform, delete the
        # platform too (design_new.md "Platform and station model").
        from track import TRACK_NS

        state.network.add_track(Tile(0, 0, TRACK_NS))
        platform = state.network.add_station(
            0, 0, station_id="STA_CASCADE", name="Cascade", length_cm=200.0
        )
        block_id = f"{platform.platform_id}_BLOCK"
        assert block_id in state.network.blocks
        assert platform.platform_id in state.network.platforms
        assert block_id in platform.get_all_assigned_blocks()

        # Deleting is on the geometry axis, and a block id never names a
        # segment, so the block's one member is addressed by its derived id.
        assert (
            segment_service.delete_segment(state, segment_id_for_block(block_id))
            is True
        )

        assert block_id not in state.network.blocks
        # The platform held only this block, so the cascade removed it too.
        assert platform.platform_id not in state.network.platforms

    def test_remove_last_tile_cascades_to_owning_platform(self, state):
        # Removing the LAST tile of a platform-owned block empties its segment;
        # the auto-delete must run the same downward platform cascade, so the
        # emptied platform is torn down instead of left with a dangling
        # block_assignments entry (design_new.md "Platform and station model").
        from track import TRACK_NS

        state.network.add_track(Tile(0, 0, TRACK_NS))
        platform = state.network.add_station(
            0, 0, station_id="STA_RMTILE", name="RmTile", length_cm=200.0
        )
        block_id = f"{platform.platform_id}_BLOCK"
        assert block_id in state.network.blocks
        assert platform.platform_id in state.network.platforms

        # Remove the segment's only tile -> segment empties -> block auto-deleted.
        segment_service.remove_tracks_from_segment(
            state, segment_id_for_block(block_id), positions=[[0, 0]]
        )

        assert block_id not in state.network.blocks
        # The auto-delete cascaded through the service layer: the now-empty
        # platform is gone, not left dangling.
        assert platform.platform_id not in state.network.platforms

    def test_delete_segment_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            segment_service.delete_segment(state, "UNIDENTIFIED")

    def test_add_tracks_unknown_segment_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            segment_service.add_tracks_to_segment(state, "UNIDENTIFIED", positions=[[0, 0]])

    def test_remove_tracks_unknown_segment_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            segment_service.remove_tracks_from_segment(
                state, "UNIDENTIFIED", positions=[[0, 0]]
            )

    def test_can_extend_unknown_segment_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            segment_service.can_extend_segment(state, "UNIDENTIFIED", x=0, y=0)


# --------------------------------------------------------- track_service ----


class TestTrackService:
    def test_get_and_require_track(self, state):
        assert track_service.get_track(state, 9, 9) is None
        with pytest.raises(NotFoundError):
            track_service.require_track(state, 9, 9)

    def test_update_missing_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            track_service.update_track(state, 9, 9, data={"track_bits": 2})

    def test_update_invalid_body_raises_validation(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        with pytest.raises(ValidationError):
            track_service.update_track(state, 0, 0, data=["not", "a", "dict"])

    def test_update_track_bits_updates(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        result = track_service.update_track(state, 0, 0, data={"track_bits": 1})
        assert result["status"] == "updated"
        assert result["track"].track_bits == 1

    def test_delete_missing_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            track_service.delete_track(state, 9, 9)

    def test_delete_track(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        assert track_service.delete_track(state, 0, 0) is True
        assert track_service.get_track(state, 0, 0) is None

    def test_add_tracks_bulk_creates(self, state):
        result = track_service.add_tracks_bulk(
            state,
            tracks_data=[
                {"x": 0, "y": 0, "track_bits": 2},
                {"x": 1, "y": 0, "track_bits": 2},
            ],
        )
        assert len(result["added"]) == 2
        assert len(result["updated"]) == 0
        assert track_service.get_track(state, 0, 0) is not None

    def test_add_tracks_bulk_updates_existing(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        result = track_service.add_tracks_bulk(
            state, tracks_data=[{"x": 0, "y": 0, "track_bits": 2}]
        )
        assert len(result["updated"]) == 1
        assert len(result["added"]) == 0

    def test_delete_tracks_bulk_no_impact(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        state.network.add_track(Tile(x=1, y=0, track_bits=2))
        result = track_service.delete_tracks_bulk(state, positions=[[0, 0], [1, 0]])
        assert result["status"] == "done"
        assert result["deleted"] == [[0, 0], [1, 0]]
        assert track_service.get_track(state, 0, 0) is None

    # H4: editor connectivity edits must refresh switch flank protection so it
    # cannot go stale and corrupt acquisition (design_new.md "Flank
    # protection computation"). Spy on the recompute to assert each edit
    # path triggers it.
    def _spy_flank(self, state, monkeypatch):
        calls = []
        monkeypatch.setattr(
            state.network,
            "recompute_all_flank_protection",
            lambda: calls.append(1),
        )
        return calls

    def test_delete_track_recomputes_flank(self, state, monkeypatch):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        calls = self._spy_flank(state, monkeypatch)
        track_service.delete_track(state, 0, 0)
        assert calls, "delete_track must recompute switch flank protection"

    def test_update_track_recomputes_flank(self, state, monkeypatch):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        calls = self._spy_flank(state, monkeypatch)
        track_service.update_track(state, 0, 0, data={"track_bits": 1})
        assert calls, "update_track must recompute switch flank protection"

    def test_add_tracks_bulk_recomputes_flank(self, state, monkeypatch):
        calls = self._spy_flank(state, monkeypatch)
        track_service.add_tracks_bulk(
            state, tracks_data=[{"x": 0, "y": 0, "track_bits": 2}]
        )
        assert calls, "add_tracks_bulk must recompute switch flank protection"

    def test_delete_tracks_bulk_recomputes_flank(self, state, monkeypatch):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        calls = self._spy_flank(state, monkeypatch)
        track_service.delete_tracks_bulk(state, positions=[[0, 0]])
        assert calls, "delete_tracks_bulk must recompute switch flank protection"


# ------------------------------------------------------ platform_service ----


class TestPlatformService:
    def test_get_and_require_platform(self, state):
        assert platform_service.get_platform(state, "UNIDENTIFIED") is None
        with pytest.raises(NotFoundError):
            platform_service.require_platform(state, "UNIDENTIFIED")

    def test_create_platform(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        platform = platform_service.create_platform(
            state, x=0, y=0, platform_id="PLT_1", name="P1"
        )
        assert platform.platform_id == "PLT_1"
        assert state.network.get_platform("PLT_1") is platform

    def test_update_platform(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        platform_service.create_platform(state, x=0, y=0, platform_id="PLT_U", name="old")
        updated = platform_service.update_platform(
            state, "PLT_U", data={"name": "new", "disabled": True}
        )
        assert updated.name == "new"
        assert updated.disabled is True

    def test_update_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.update_platform(state, "UNIDENTIFIED", data={"name": "x"})

    def test_update_bad_platform_side_raises_validation_error(self, state):
        # Regression: an invalid platform_side must surface as ValidationError
        # (-> HTTP 400) rather than a bare ValueError leaking as HTTP 500, so
        # update_platform matches the create/extend bad-input contract.
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        platform_service.create_platform(state, x=0, y=0, platform_id="PLT_S")
        with pytest.raises(ValidationError):
            platform_service.update_platform(
                state, "PLT_S", data={"platform_side": "sideways"}
            )

    def test_delete_platform(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        platform_service.create_platform(state, x=0, y=0, platform_id="PLT_X")
        assert platform_service.delete_platform(state, "PLT_X") is True
        assert platform_service.get_platform(state, "PLT_X") is None

    def test_delete_unknown_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.delete_platform(state, "UNIDENTIFIED")

    def test_validate_placement(self, state):
        assert platform_service.validate_placement(state, 9, 9)["can_place"] is False
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        assert platform_service.validate_placement(state, 0, 0)["can_place"] is True

    def test_can_extend_unknown_platform_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.can_extend(state, "UNIDENTIFIED", x=0, y=0)

    def test_assign_tile_unknown_platform_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.assign_tile(state, "UNIDENTIFIED", x=0, y=0, block_id="BLK")

    def test_assign_tile_unknown_block_raises_not_found(self, state):
        state.network.add_track(Tile(x=0, y=0, track_bits=2))
        platform_service.create_platform(state, x=0, y=0, platform_id="PLT_A")
        with pytest.raises(NotFoundError):
            platform_service.assign_tile(
                state, "PLT_A", x=0, y=0, block_id="UNIDENT_BLK"
            )

    def test_unassign_tile_unknown_platform_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.unassign_tile(state, "UNIDENTIFIED", x=0, y=0)

    def test_remove_tile_unknown_platform_raises_not_found(self, state):
        with pytest.raises(NotFoundError):
            platform_service.remove_tile(state, "UNIDENTIFIED", x=0, y=0)


class TestRouteServiceLeadingDwell:
    """design_new.md "Route-definition validation" — a route's first task must
    not be a dwell."""

    def test_create_route_rejects_leading_dwell(self, state):
        with pytest.raises(ValidationError):
            train_route_service.create_route(
                state,
                name="bad-leading-dwell",
                tasks=[
                    {"task_kind": "dwell", "task_location": "B1"},
                    {"task_kind": "stop", "task_location": "B2"},
                ],
            )

    def test_update_route_rejects_leading_dwell(self, state):
        route = train_route_service.create_route(
            state,
            name="ok",
            tasks=[{"task_kind": "stop", "task_location": "B1"}],
        )
        with pytest.raises(ValidationError):
            train_route_service.update_route(
                state,
                route.route_id,
                # A new name alongside the invalid task list: the rejection must
                # not partially apply the name before validating the tasks.
                name="renamed",
                tasks=[
                    {"task_kind": "dwell", "task_location": "B1"},
                    {"task_kind": "stop", "task_location": "B2"},
                ],
            )
        # Validate-then-mutate: the rejected update left the live route untouched
        # (name and the original single stop task both unchanged).
        assert route.name == "ok"
        assert [t.task_kind for t in route.tasks] == ["stop"]

    def test_create_route_allows_leading_stop_then_dwell(self, state):
        route = train_route_service.create_route(
            state,
            name="good",
            tasks=[
                {"task_kind": "stop", "task_location": "B1"},
                {"task_kind": "dwell", "task_location": "B1"},
            ],
        )
        assert route.tasks[0].task_kind == "stop"
        assert route.tasks[1].task_kind == "dwell"
