"""`power_off_cmd()` while already OFF is a no-op
(design_new.md "Power/cab/mode command contracts").

Regression for the gap-analysis finding: power_off from OFF armed the
`power_off` Phase-3 latch unconditionally. That stale latch survived a later
`power_on_cmd()` and tore the freshly-powered train straight back to OFF in the
next Phase-3 cleanup.

Run with: ``conda run -n general python -m pytest
tests/test_power_off_while_off_noop.py -q``
"""

from network import RailwayNetwork
from train import Train, TrainCar
from enums import TrainState
from train_controller import create_controller_for_train


def _off_controller(train_id="T_OFF"):
    network = RailwayNetwork()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id=train_id, consist=[car], max_speed=60.0)
    network.add_train(train)
    controller = create_controller_for_train(train_id, network)
    controller.ops.state = TrainState.OFF
    return controller


def test_power_off_while_off_is_accepted_noop():
    controller = _off_controller()
    assert controller.ops.state == TrainState.OFF

    result = controller.commands.power_off_cmd()

    assert result.success is True  # accepted as a no-op
    # The crux: the power_off latch must NOT be armed — the train is already off.
    assert controller.route_hooks.phase_latches.power_off is False
    assert controller.route_hooks.phase_latches.is_any_pending() is False


def test_power_off_then_power_on_while_off_does_not_tear_back_to_off():
    """OFF -> power_off -> power_on must land READY with no stale power_off latch."""
    controller = _off_controller()

    assert controller.commands.power_off_cmd().success is True
    on = controller.commands.power_on_cmd()

    assert on.success is True
    assert controller.ops.state == TrainState.READY
    # No stale latch left to drag the powered train back to OFF in Phase 3.
    assert controller.route_hooks.phase_latches.power_off is False
