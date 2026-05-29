"""Auto-recovery on extend_path INVALID_REVERSE.

When `extend_path()` rejects a candidate as a 180-degree reversal at the
authority-end (failure_kind=INVALID_REVERSE), the route executor must NOT
surface a hard `last_error` to the operator. Instead it latches
`pending_path_cancel_request` so Phase 3 cancels the stale path once the
train stops, and the next eligible Phase 1 acquires fresh authority via
`create_path()` from the train's current span — which is free to pick a
forward route in either direction.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from runtime_payloads import (
    AcquisitionMode,
    PathAcquisitionFailureKind,
    PhaseLatches,
)
from route_executor import RouteExecutor, RouteExecutionState


class _FakeDispatcher:
    """Minimal PathDispatcher stand-in for _authority_acquisition_step."""

    def __init__(self, train_id, active_path_id, extend_result):
        self._train_id = train_id
        self._active_path_id = active_path_id
        self._extend_result = extend_result
        self.extend_calls = []
        self.create_calls = []

    def active_path_id_by_train(self, train_id):
        return self._active_path_id if train_id == self._train_id else None

    def extend_path(self, **kwargs):
        self.extend_calls.append(kwargs)
        return self._extend_result

    def create_path(self, **kwargs):  # pragma: no cover — must not be hit here
        self.create_calls.append(kwargs)
        raise AssertionError(
            "create_path must not be called on the same tick as the failed "
            "extend_path; the recovery latch is consumed by Phase 3 instead."
        )


def _make_controller():
    latches = PhaseLatches()
    route_hooks = SimpleNamespace(phase_latches=latches)
    controller = SimpleNamespace(route_hooks=route_hooks)
    return controller, latches


def _make_projection(target_block="BLK_DEST", direction="forward", walk_pos=1):
    resolved_target = SimpleNamespace(
        requested_destination_block_id=target_block,
        requested_destination_direction=direction,
    )
    return SimpleNamespace(
        resolved_target=resolved_target,
        walk_position=walk_pos,
    )


def _make_exec_state():
    state = MagicMock(spec=RouteExecutionState)
    state.effective_acquisition_mode = MagicMock(return_value=AcquisitionMode.BEST_EFFORT)
    state.task_runs = []
    state.last_error = None
    return state


def test_invalid_reverse_latches_cancel_and_skips_last_error():
    train_id = "T3"
    train = SimpleNamespace(train_id=train_id)
    route = MagicMock()

    extend_result = SimpleNamespace(
        success=False,
        failure_kind=PathAcquisitionFailureKind.INVALID_REVERSE,
        reason="extend would reverse at authority end",
        path_id="P1",
    )
    dispatcher = _FakeDispatcher(train_id, active_path_id="P1", extend_result=extend_result)

    executor = RouteExecutor(network=MagicMock(), path_dispatcher=dispatcher)
    controller, latches = _make_controller()
    exec_state = _make_exec_state()
    projection = _make_projection()

    result = executor._authority_acquisition_step(
        train=train,
        route=route,
        exec_state=exec_state,
        controller=controller,
        projection=projection,
    )

    assert result is False
    assert len(dispatcher.extend_calls) == 1
    assert dispatcher.create_calls == []

    # The recovery path latches a Phase-3 cancellation instead of bubbling
    # the failure up as last_error.
    assert latches.path_cancel_request is not None
    assert latches.path_cancel_request.initiator == "auto_recreate_for_reverse"
    assert "reverse" in (latches.path_cancel_request.reason or "")

    # No hard error should be recorded — the operator does not need to act.
    assert exec_state.last_error is None


def test_other_failure_kinds_still_record_last_error():
    """Sanity check: the auto-recovery branch only fires for INVALID_REVERSE.

    Any other failure_kind must keep the existing behavior — record a hard
    last_error so the operator sees something is wrong.
    """
    train_id = "T3"
    train = SimpleNamespace(train_id=train_id)
    route = MagicMock()

    extend_result = SimpleNamespace(
        success=False,
        failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
        reason="No forward path from BLK_X to BLK_Y",
        path_id="P1",
    )
    dispatcher = _FakeDispatcher(train_id, active_path_id="P1", extend_result=extend_result)

    executor = RouteExecutor(network=MagicMock(), path_dispatcher=dispatcher)
    # _sync_task_runs is a real method that touches Route internals; stub it
    # since this test only cares about the error-recording branch.
    executor._sync_task_runs = MagicMock()
    executor._now = MagicMock(return_value=12345.0)

    controller, latches = _make_controller()
    exec_state = _make_exec_state()
    projection = _make_projection()

    result = executor._authority_acquisition_step(
        train=train,
        route=route,
        exec_state=exec_state,
        controller=controller,
        projection=projection,
    )

    assert result is False
    # NO recovery latch for other failure kinds.
    assert latches.path_cancel_request is None
    # Hard error recorded.
    assert exec_state.last_error is not None
    assert "No forward path" in exec_state.last_error.text
