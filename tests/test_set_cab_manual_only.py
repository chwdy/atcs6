"""Operator `set_cab` is manual-only (design_new.md set_cab contract).

Pins the v1 contract added when cab selection became a manual-driving command:

- An operator-issued ``set_cab`` is rejected unless ``control_mode == "manual"``.
- The ATO auto-cab-selection path (``internal=True``) bypasses that gate — the
  system aligns the cab during ``depart()``, it is not the operator.
- The drain runs ``set_mode`` before ``set_cab`` so a one-tick "switch to manual
  then pick a cab" batch is accepted in a single drain.

Run with: ``conda run -n general python -m pytest
tests/test_set_cab_manual_only.py -q``
"""

import pytest

from network import RailwayNetwork
from train import Train, TrainCar
from enums import TrainState
from train_controller import create_controller_for_train


def _ready_controller(train_id="T_GATE"):
    network = RailwayNetwork()
    car = TrainCar(car_id="C1", car_type="LOCOMOTIVE", length_cm=20.0)
    train = Train(train_id=train_id, consist=[car], max_speed=60.0)
    network.add_train(train)
    controller = create_controller_for_train(train_id, network)
    controller.ops.state = TrainState.READY
    return controller


def test_operator_set_cab_rejected_in_ato_mode():
    controller = _ready_controller()
    assert controller.commands.control_mode == "ato"  # construction default

    result = controller.commands.set_cab("A")

    assert result.success is False
    assert "manual" in (result.message or "").lower()
    assert controller.commands.cab is None  # cab unchanged


def test_operator_set_cab_accepted_in_manual_mode():
    controller = _ready_controller()
    controller.commands.control_mode = "manual"

    result = controller.commands.set_cab("A")

    assert result.success is True
    assert controller.commands.cab == "A"
    assert controller.ops.state == TrainState.READY


def test_internal_set_cab_bypasses_manual_gate_in_ato():
    """ATO auto-cab-selection (depart) uses internal=True and is not gated."""
    controller = _ready_controller()
    assert controller.commands.control_mode == "ato"

    result = controller.commands.set_cab("A", internal=True)

    assert result.success is True
    assert controller.commands.cab == "A"


def test_drain_runs_set_mode_before_set_cab_one_tick():
    """A single drain with both commands lands in manual mode WITH cab set,
    proving set_mode (slot 2) precedes set_cab (slot 3). Under the old order
    (set_cab before set_mode) the cab write would be rejected (still ATO) and
    the cab would stay None."""
    controller = _ready_controller()
    assert controller.commands.control_mode == "ato"

    controller.commands.enqueue_command("set_mode", {"mode": "manual"})
    controller.commands.enqueue_command("set_cab", {"cab": "A"})
    controller.commands._drain_command_inbox(0.0)

    assert controller.commands.control_mode == "manual"
    assert controller.commands.cab == "A"
