"""Observe's emergency stop goes through `phase_latches.emergency_request`.

design_new.md Latch Table row `e9c7` and "Operator-command processing slot":
Observe never calls an EB method itself. It latches a `"brake"` or `"cut_out"`
request, the planning gate refuses to create or extend reservation while the
request is pending (`EMERGENCY_PENDING`), and the Action command drain applies
it after every queued operator command — so an `eb.release()` queued on the
same tick cannot undo it.
"""

from enums import ControllerCondition
from runtime_payloads import ErrorRecord
from tests.test_reserved_block_taken_by_another_train import (
    build_abc,
    rolling_train_with_authority_to_c,
)


def _rolling():
    network = build_abc()
    sim, dispatcher, controller, train, path_id = rolling_train_with_authority_to_c(
        network
    )
    return sim, controller


class _RunStub:
    """The gate reads only `train_id` off the run; this train has no route."""

    train_id = "T1"


def test_a_pending_request_is_an_active_condition_and_blocks_planning():
    sim, controller = _rolling()
    executor = sim.route_executor

    controller.route_hooks.phase_latches.set_emergency_request(
        "brake", ErrorRecord("test", "locked_foreign_entry")
    )

    assert ControllerCondition.EMERGENCY_PENDING in controller.active_conditions
    assert executor.is_clean_for_create("T1") is False
    # The train holds a live path, so this is the extend branch of the gate.
    assert controller.path_projection.received_path_plan is not None
    assert executor._eligible_for_authority_acquisition(controller, _RunStub()) is False


def test_the_drain_applies_a_cut_out_request_and_clears_it():
    sim, controller = _rolling()
    latches = controller.route_hooks.phase_latches
    latches.set_emergency_request("cut_out", ErrorRecord("test", "overrun_entry"))

    controller.commands._drain_command_inbox(0.0)

    assert latches.emergency_request is None
    assert controller.eb.active is True
    assert controller.eb.cut_out is True
    assert ControllerCondition.EMERGENCY_PENDING not in controller.active_conditions


def test_the_request_is_applied_after_a_same_tick_release():
    """The release runs in drain slot 8; the request is applied after it, so the
    train ends the drain emergency-braked."""
    sim, controller = _rolling()
    controller.commands.enqueue_command("release_emergency_cmd")
    controller.route_hooks.phase_latches.set_emergency_request(
        "brake", ErrorRecord("test", "locked_foreign_entry")
    )

    controller.commands._drain_command_inbox(0.0)

    assert controller.eb.active is True
    assert controller.eb.cut_out is False


def test_a_request_on_an_already_braked_train_is_cleared_as_a_no_op():
    sim, controller = _rolling()
    assert controller.eb.activate().success
    latches = controller.route_hooks.phase_latches
    latches.set_emergency_request("cut_out", ErrorRecord("test", "overrun_entry"))

    controller.commands._drain_command_inbox(0.0)

    assert latches.emergency_request is None
    assert controller.eb.active is True
    # `activate_cut_out()` is rejected on a latched train, so the plain brake stays.
    assert controller.eb.cut_out is False


def test_one_running_tick_applies_a_request_latched_before_it():
    """Under running ticks the request is applied in the same tick's Action."""
    sim, controller = _rolling()
    controller.route_hooks.phase_latches.set_emergency_request(
        "brake", ErrorRecord("test", "locked_foreign_entry")
    )
    sim.update(0.1)
    assert controller.route_hooks.phase_latches.emergency_request is None
    assert controller.eb.active is True
