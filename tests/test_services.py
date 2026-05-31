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
from speed_profile import SpeedProfileRegistry
from track import TrackSegment
from train import Train, TrainCar

from services import ConflictError, NotFoundError, ValidationError
from services import (
    schedule_service,
    speed_profile_service,
    switch_service,
    train_route_service,
)


@pytest.fixture
def state():
    """Fresh AppState with no HTTP layer attached."""
    network = RailwayNetwork()
    path_dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, path_dispatcher)
    persistence_manager = PersistenceManager(storage_dir="layouts")
    speed_profile_registry = SpeedProfileRegistry()

    return AppState(
        network=network,
        path_dispatcher=path_dispatcher,
        sim_controller=sim_controller,
        persistence_manager=persistence_manager,
        speed_profile_registry=speed_profile_registry,
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


# ----------------------------------------------------- schedule_service ----


class TestScheduleService:
    def test_create_schedule_attaches_to_train(self, state_with_train):
        """`create_schedule` adds to network.schedules and sets train.schedule."""
        schedule = schedule_service.create_schedule(
            state_with_train,
            train_id="TRAIN_A",
            stops=[
                {
                    "station_name": "Stop 1",
                    "station_pos": [0, 0],
                    "arrival_time": "2026-01-01T10:00:00",
                    "departure_time": "2026-01-01T10:05:00",
                }
            ],
            priority=3,
        )

        assert schedule.schedule_id in state_with_train.network.schedules
        assert schedule.priority == 3
        assert len(schedule.stops) == 1

        train = state_with_train.network.trains["TRAIN_A"]
        assert train.schedule is schedule

    def test_create_schedule_unknown_train_raises_not_found(self, state):
        """An unknown train_id surfaces as NotFoundError."""
        with pytest.raises(NotFoundError):
            schedule_service.create_schedule(
                state, train_id="GHOST_TRAIN", stops=[]
            )

    def test_create_schedule_missing_train_id_raises_validation(self, state):
        """A blank train_id is a validation error, not a not-found."""
        with pytest.raises(ValidationError):
            schedule_service.create_schedule(state, train_id="", stops=[])


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
    state.network.add_track(TrackSegment(x=5, y=0, track_bits=0b000110))
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
        state.network.add_track(TrackSegment(x=1, y=0, track_bits=2))
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
        switch.occupied_by = "TRAIN_A"
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
