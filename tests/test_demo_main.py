"""
Demo Main Tests

Ensures demo setup configures speed profiles for demo trains.
"""

import contextlib
import io

from bootstrap import bootstrap_runtime
from demo_main import create_comprehensive_demo, setup_demo_train
from enums import TrainState
from path_dispatcher import PathDispatcher
from train_controller import ATO_SPEED_MARGIN_KMH


def test_demo_train_speed_profile_setup():
    """Demo train should use a custom speed profile."""
    network = create_comprehensive_demo()
    dispatcher = PathDispatcher(network)

    setup_demo_train(network, dispatcher)

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
        network.get_track(x, y).block_section_id
        for x, y in east_terminal.positions
        if network.get_track(x, y)
    }

    assert terminal_blocks, "East Terminal should have a station block"
    for block_id in terminal_blocks:
        assert network.blocks[block_id].max_speed_kmh == east_terminal.top_speed_kmh

    terminal_limit = east_terminal.top_speed_kmh
    max_terminal_speed = 0.0
    reached_terminal = False
    eb_events = []
    terminal_overspeed_events = []

    for tick in range(1200):
        sim_controller.update(0.3)

        head_tile = train.position.head_tile if train.position else None
        head_track = network.get_track(*head_tile) if head_tile else None
        block_id = head_track.block_section_id if head_track else None

        if controller.eb_active:
            eb_events.append(
                {
                    "tick": tick,
                    "head_tile": head_tile,
                    "block_id": block_id,
                    "speed": train.current_speed,
                    "atc_limit": controller.atc_speed_limit,
                    "reason": controller.speed_limit_reason,
                    "handle_reason": controller.handle_reason,
                }
            )

        if block_id in terminal_blocks:
            reached_terminal = True
            max_terminal_speed = max(max_terminal_speed, train.current_speed)
            if train.current_speed > terminal_limit + 0.5:
                terminal_overspeed_events.append(
                    {
                        "tick": tick,
                        "head_tile": head_tile,
                        "speed": train.current_speed,
                        "limit": terminal_limit,
                        "atc_limit": controller.atc_speed_limit,
                        "handle": controller.handle_position.value,
                        "handle_reason": controller.handle_reason,
                    }
                )

        if (
            tick > 30
            and train.current_speed <= 0.5
            and train.state
            in (
                TrainState.STANDBY,
                TrainState.RUNNING_AT_STATION,
                TrainState.RUNNING_WAITING,
            )
        ):
            break

    final_head_tile = train.position.head_tile if train.position else None
    final_track = network.get_track(*final_head_tile) if final_head_tile else None
    final_block_id = final_track.block_section_id if final_track else None

    assert reached_terminal, (
        f"Demo train should reach East Terminal; final block={final_block_id}, "
        f"head={final_head_tile}, speed={train.current_speed:.2f}"
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
    assert max_terminal_speed >= terminal_limit - ATO_SPEED_MARGIN_KMH - 2.0
