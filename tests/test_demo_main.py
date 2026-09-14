"""
Demo Main Tests

Ensures demo setup configures speed profiles for demo trains, and locks the
bootstrap contracts called out in design_new.md (current_span set before the
first tick, operator commands queued, Path created on first Phase 1 tick).
"""

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime
from demo_main import create_comprehensive_demo, setup_demo_train
from path_dispatcher import PathDispatcher
from train_controller import ATO_SPEED_MARGIN_KMH

pytestmark = pytest.mark.scene("demo_main")


def test_demo_train_speed_profile_setup():
    """Demo train should use a custom speed profile."""
    from simulation import SimulationController

    network = create_comprehensive_demo()
    dispatcher = PathDispatcher(network)
    sim_controller = SimulationController(network, dispatcher)

    setup_demo_train(network, dispatcher, sim_controller)

    demo_train = network.trains.get("DEMO_TRAIN")
    assert demo_train is not None
    assert demo_train.speed_profile_id == "demo_profile"

    profile = network.speed_profiles.get("demo_profile")
    assert profile is not None
    assert profile.acceleration_rate == 3.0
    assert profile.deceleration_rate == 4.2


def test_bootstrap_demo_train_respects_east_terminal_speed_limit_at_3x():
    """Server-start demo train must respect East Terminal's platform speed limit."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, _, sim_controller, _ = bootstrap_runtime(demo=True, setup_train=True)

    train = network.trains["DEMO_TRAIN"]
    controller = sim_controller._get_or_create_controller(train)
    east_terminal = next(
        platform
        for platform in network.platforms.values()
        if platform.name == "East Terminal"
    )
    terminal_blocks = {
        network.get_track(x, y).block_id
        for x, y in east_terminal.positions
        if network.get_track(x, y)
    }

    assert terminal_blocks, "East Terminal should have a station block"
    for block_id in terminal_blocks:
        assert network.blocks[block_id].speed_limit_kmh == east_terminal.top_speed_kmh

    terminal_limit = east_terminal.top_speed_kmh
    max_terminal_speed = 0.0
    reached_terminal = False
    eb_events = []
    terminal_overspeed_events = []

    for tick in range(1200):
        sim_controller.update(0.3)

        head_tile = controller._get_head_tile()
        head_track = network.get_track(*head_tile) if head_tile else None
        block_id = head_track.block_id if head_track else None

        if controller.eb.active:
            eb_events.append(
                {
                    "tick": tick,
                    "head_tile": head_tile,
                    "block_id": block_id,
                    "speed": controller.physics_sync.current_speed_kmh,
                    "atc_limit": controller.atc.speed_limit,
                    "reason": controller.speed_limit_reason,
                    "handle_reason": controller.ops.handle_reason,
                }
            )

        if block_id in terminal_blocks:
            reached_terminal = True
            max_terminal_speed = max(max_terminal_speed, controller.physics_sync.current_speed_kmh)
            if controller.physics_sync.current_speed_kmh > terminal_limit + 0.5:
                terminal_overspeed_events.append(
                    {
                        "tick": tick,
                        "head_tile": head_tile,
                        "speed": controller.physics_sync.current_speed_kmh,
                        "limit": terminal_limit,
                        "atc_limit": controller.atc.speed_limit,
                        "handle": controller.ops.handle_position.value,
                        "handle_reason": controller.ops.handle_reason,
                    }
                )

        # Per design_new.md "Train ownership", operational state lives on the
        # controller; check the controller's state machine directly.
        if (
            tick > 30
            and controller.physics_sync.current_speed_kmh <= 0.5
            and controller.powered
            and controller.path_projection.received_path_plan is None
        ):
            break

    final_head_tile = controller._get_head_tile()
    final_track = network.get_track(*final_head_tile) if final_head_tile else None
    final_block_id = final_track.block_id if final_track else None

    assert reached_terminal, (
        f"Demo train should reach East Terminal; final block={final_block_id}, "
        f"head={final_head_tile}, speed={controller.physics_sync.current_speed_kmh:.2f}"
    )
    assert final_block_id in terminal_blocks, (
        f"Demo train should stop in East Terminal; final block={final_block_id}, "
        f"terminal blocks={terminal_blocks}"
    )
    assert not eb_events, f"Demo run should not trigger EB: {eb_events[:3]}"
    assert not terminal_overspeed_events, (
        "Demo train exceeded East Terminal platform speed limit: "
        f"{terminal_overspeed_events[:3]}"
    )
    assert max_terminal_speed <= terminal_limit + 0.5
    # The lower bound asserts the ATO isn't over-decelerating into the
    # terminal — the train should still be travelling near the platform
    # limit at the moment its head crosses into the terminal block. The
    # 2.5 km/h headroom below `(terminal_limit - ATO_SPEED_MARGIN_KMH)`
    # accommodates per-tick sample timing at dt=0.3: the moment the head
    # first lands inside the terminal block can fall anywhere within a
    # 0.3s window, and at typical approach decel the sample can come up
    # ~0.5 km/h below the steady-state operational limit.
    assert max_terminal_speed >= terminal_limit - ATO_SPEED_MARGIN_KMH - 2.5


def test_bootstrap_writes_current_span_before_first_tick():
    """Per design_new.md, SimulationController.add_train must populate
    TrainController.current_span before any simulation tick runs."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, _, sim_controller, _ = bootstrap_runtime(demo=True, setup_train=True)

    controller = sim_controller.get_or_create_controller("DEMO_TRAIN")
    assert controller.physics_sync.current_span is not None, (
        "current_span must be written by add_train(...) before any tick"
    )


def test_bootstrap_queues_power_on_for_first_tick_drain():
    """Bootstrap must enqueue the operator start-up: power_on then set_mode(ato)
    to engage. Phase 2 drains both; an engaged ATO train then drives once Phase 1
    grants authority (design_new.md "ato.engaged")."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, _, sim_controller, _ = bootstrap_runtime(demo=True, setup_train=True)

    controller = sim_controller.get_or_create_controller("DEMO_TRAIN")

    # Before first tick: power_on + set_mode queued, controller still OFF, head
    # frame pre-set by add_train's `initial_cab="A"` placement input.
    queued_kinds = [cmd.kind for cmd in controller.commands.command_inbox]
    assert queued_kinds == ["power_on", "set_mode"], (
        f"Expected queued [power_on, set_mode]; got {queued_kinds}"
    )
    assert not controller.powered
    assert controller._framed_head_end == "end_a"

    # One tick should drain the queue and power the train on.
    sim_controller.update(0.0)

    assert controller.commands.command_inbox == []
    assert controller.powered


def test_bootstrap_creates_path_during_phase1_after_power_on():
    """Per design_new.md, paths are created on demand by Phase 1 once the
    train is powered on; bootstrap must NOT pre-create the demo Path itself."""
    with contextlib.redirect_stdout(io.StringIO()):
        network, path_dispatcher, sim_controller, _ = bootstrap_runtime(
            demo=True, setup_train=True
        )

    # Before any tick: no path exists yet.
    assert path_dispatcher.active_path_id_by_train("DEMO_TRAIN") is None

    # Tick 1: Phase 1 sees OFF and skips; Phase 2 drains power_on. Still no path.
    sim_controller.update(0.0)
    assert path_dispatcher.active_path_id_by_train("DEMO_TRAIN") is None

    # Tick 2: Phase 1 sees READY, acquires authority for DEMO_ROUTE_V2.
    sim_controller.update(0.0)
    assert path_dispatcher.active_path_id_by_train("DEMO_TRAIN") is not None


def test_route_executor_register_route_round_trips():
    """RouteExecutor.register_route / route_registry / unregister_route."""
    from route_executor import RouteExecutor
    from train_route import Route, RouteMode, RouteTask

    network = create_comprehensive_demo()
    executor = RouteExecutor(network)

    route = Route(
        route_id="UNIT_ROUTE",
        name="unit",
        mode=RouteMode.ONE_OFF,
        tasks=[RouteTask(task_kind="dwell", task_location="STA_X")],
    )
    executor.register_route(route)
    assert executor.route_registry.get("UNIT_ROUTE") is route

    removed = executor.unregister_route("UNIT_ROUTE")
    assert removed is route
    assert "UNIT_ROUTE" not in executor.route_registry
    # Unregistering a missing id is a no-op.
    assert executor.unregister_route("NOPE") is None
