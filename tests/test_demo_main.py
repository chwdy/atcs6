"""
Demo Main Tests

Ensures demo setup configures speed profiles for demo trains.
"""

from demo_main import create_comprehensive_demo, setup_demo_train
from path_dispatcher import PathDispatcher


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
