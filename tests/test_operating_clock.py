"""`AppState.operating_time` — the clock a schedule holds against.

design_new.md "Clocks" keeps three: wall, `AppState.operating_time`, and
`SimulationController.simulation_time`. This file pins the middle one, which is
the reason a route's timing basis exists at all.

It is seconds of *operation*: it advances with each running pass, is scaled by
playback, and adds nothing on a stopped pass. That is what makes an operator's
stop free — a dwell serves its whole hold however long they were away, instead
of the hold quietly elapsing while nothing was running.

It is accumulated rather than read off a real clock, which is what lets a
scene run four hundred ticks in a millisecond and still have forty seconds of
operation behind it.

Run with:
    /opt/homebrew/anaconda3/envs/general/bin/python -m pytest \
        tests/test_operating_clock.py -v
"""

import time

from block import Block
from block_occupancy import OccupancyInput
from enums import OperationMode
from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from track import Tile, TRACK_EW
from train_route import RouteTask
from entity_ids import segment_id_for_block


class _FakeDetector(OccupancyInput):
    def __init__(self):
        self.current = {}

    def read_block_currents(self):
        return dict(self.current)


def _sim(mode=OperationMode.SIM):
    network = RailwayNetwork()
    for x in range(2):
        track = Tile(x=x, y=0, track_bits=TRACK_EW)
        track.segment_id = segment_id_for_block(f"BLK_{x}")
        network.add_track(track)
        block = Block(block_id=f"BLK_{x}", length_cm=100.0)
        block.ordered_tiles = [(x, 0, "W")]
        network.blocks[f"BLK_{x}"] = block
    return SimulationController(
        network,
        PathDispatcher(network),
        operation_mode=mode,
        input_interface=_FakeDetector(),
    )


# ------------------------------------------------------- what advances it


def test_a_running_pass_advances_the_operating_clock():
    sim = _sim()
    assert sim.operating_clock() == 0.0

    sim.update(0.1)
    sim.update(0.1)

    assert sim.operating_clock() == pytest_approx(0.2)


def test_it_accumulates_the_tick_delta_not_real_time():
    """Four hundred ticks of 0.1 s are forty seconds of operation, however
    few milliseconds of real time they took to run."""
    sim = _sim()
    wall_before = time.time()

    for _ in range(400):
        sim.update(0.1)

    assert sim.operating_clock() == pytest_approx(40.0)
    assert time.time() - wall_before < 40.0  # nowhere near it in real time


def test_a_stopped_pass_adds_nothing():
    """The whole point: an operator's stop is free.

    A stopped hardware session still reads its detectors, and that pass must
    not advance the clock a dwell is holding against — otherwise the hold
    elapses while nothing is running.
    """
    sim = _sim(mode=OperationMode.REAL)
    sim.update(0.1)
    after_running = sim.operating_clock()

    for _ in range(10):
        sim.poll_hardware_occupancy()

    assert sim.operating_clock() == after_running


# -------------------------------------------------- where the value lives


def test_app_state_owns_the_accumulator():
    """`AppState` holds it, because a layout load builds a fresh
    `SimulationController` and must not reset the clock a schedule holds
    against. The controller only reads and advances it through the two seams
    `AppState` wires."""
    import threading
    from datetime import datetime

    from app_state import AppState
    from persistence import PersistenceManager

    sim = _sim()
    state = AppState(
        network=sim.network,
        path_dispatcher=sim.path_dispatcher,
        sim_controller=sim,
        persistence_manager=PersistenceManager(),
        simulation_lock=threading.Lock(),
        simulation_running=True,
        simulation_speed=1.0,
        last_update_time=datetime.now(),
    )

    sim.update(0.5)

    assert state.operating_time == pytest_approx(0.5)
    assert sim.operating_clock() == pytest_approx(0.5)


# ------------------------------------------------ what the basis governs


def test_a_dwell_holding_for_task_time_measures_on_the_basis():
    task = RouteTask(task_kind="dwell", task_location="STA_A", task_time=30.0)

    # 29 s of the basis clock: still holding, whatever wall says.
    assert task.should_depart(1_800_000_000.0, 29.0, 0.0) is False
    # 30 s: served.
    assert task.should_depart(1_800_000_000.0, 30.0, 0.0) is True


def test_a_scheduled_departure_is_read_in_wall_time_under_either_basis():
    """`target_time` is an absolute instant, and an absolute instant is only
    ever written in wall time (design_new.md "Clocks")."""
    task = RouteTask(
        task_kind="dwell", task_location="STA_A", target_time=1_000.0
    )

    # Basis clock far past the number; wall clock is what decides.
    assert task.should_depart(999.0, 999_999.0, 0.0) is False
    assert task.should_depart(1_000.0, 0.0, 0.0) is True


# ------------------------------------------------------ the tolerance knob


def test_the_delay_class_tolerance_is_a_runtime_setting():
    """design_new.md "Runtime settings" lists it as a tunable field, so it must
    appear in the settings payload and take a live update."""
    import constants
    import runtime_config

    original = runtime_config.get_runtime_config().delay_class_on_time_tolerance_seconds
    try:
        assert "delay_class_on_time_tolerance_seconds" in runtime_config.as_dict()

        runtime_config.apply_updates(
            {"delay_class_on_time_tolerance_seconds": 12.5}
        )

        assert runtime_config.get_runtime_config().delay_class_on_time_tolerance_seconds == 12.5
        # And it reaches the module global the consumer reads it from.
        assert constants.DELAY_CLASS_ON_TIME_TOLERANCE_SECONDS == 12.5
    finally:
        runtime_config.apply_updates(
            {"delay_class_on_time_tolerance_seconds": original}
        )


def pytest_approx(value, tol=1e-9):
    """Local float comparison so the file needs no pytest import for it."""

    class _Approx:
        def __eq__(self, other):
            return abs(other - value) <= tol

        def __repr__(self):
            return f"~{value}"

    return _Approx()
