"""How Phase 1 sorts extend_path failures into structural and temporary.

Per design_new.md **Planning Kernel**, a structural extend failure on a live
path — `no_viable_path` — latches `pending_path_cancel_request` rather than
surfacing a hard
an error event. Phase 3 cancels the stale path once the train stops, and the
next eligible Phase 1 acquires fresh authority via `create_path()` from the
train's current span, which is free to pick a route the extend could not.

A temporary failure is the opposite case: the live path is still valid, so it
is kept, the error is recorded for the operator, and Phase 1 retries later.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from runtime_payloads import (
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


def _make_projection(
    target_block="BLK_DEST", direction="forward", walk_pos=1, task_kind="stop"
):
    resolved_target = SimpleNamespace(
        requested_destination_block_id=target_block,
        requested_destination_direction=direction,
    )
    return SimpleNamespace(
        resolved_target=resolved_target,
        walk_position=walk_pos,
        # A `stop` by default: the Waypoint lookahead in step `66aa` applies
        # only to a served `waypoint`, so these extend-failure cases exercise
        # the ordinary path.
        task_kind=task_kind,
        direction=direction,
    )


def _runtime_with_stream():
    """A minimal runtime carrying the one event stream, so error events have
    somewhere to land (errors are events, not entity state)."""
    from notification_center import NotificationCenter

    return SimpleNamespace(notification_center=NotificationCenter())


def _emitted_errors(executor):
    """Error-carrying events on the one stream (design_new.md "Error events")."""
    nc = getattr(
        getattr(executor, "simulation_controller", None), "notification_center", None
    )
    if nc is None:
        return []
    return [n for n in nc.peek_recent(200) if n.error is not None]


def _make_exec_state():
    state = MagicMock(spec=RouteExecutionState)
    state.task_runs = []
    state.train_id = "T3"
    state.run_id = "T3:R1:1"
    state.current_waypoint_index = 0
    return state


def test_reverse_at_authority_end_latches_cancel_and_emits_no_hard_error():
    """A target that needs the train to turn around at the current authority
    end is `no_viable_path`: extension is forward-only, so no forward suffix
    reaches it. The `reason` names the reversal (design_new.md
    "`create_path()` / `extend_path()` return contract")."""
    train_id = "T3"
    train = SimpleNamespace(train_id=train_id)
    route = MagicMock()

    extend_result = SimpleNamespace(
        success=False,
        failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
        reason="extend would reverse at authority end",
        path_id="P1",
    )
    dispatcher = _FakeDispatcher(train_id, active_path_id="P1", extend_result=extend_result)

    executor = RouteExecutor(network=MagicMock(), path_dispatcher=dispatcher)
    executor.simulation_controller = _runtime_with_stream()
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
    # the failure up as an operator-facing error.
    assert latches.path_cancel_request is not None
    assert latches.path_cancel_request.error.origin == "revoked"
    assert "reverse" in (latches.path_cancel_request.error.text or "")

    # No hard error event — the operator does not need to act.
    assert _emitted_errors(executor) == []


def test_no_viable_path_also_latches_cancellation():
    """NO_VIABLE_PATH on a live path is structural, so it latches too.

    Per design_new.md **Planning Kernel**, `no_viable_path` is the failure that
    "latches cancellation before a fresh create attempt". No legal forward
    suffix to the target exists, and
    waiting will not produce one, so retrying against the same path would spin
    until the train ran out its authority. This is the failure a circular route
    hits at the lap seam when the only way on runs back through a block the
    live path still holds.
    """
    train_id = "T3"
    train = SimpleNamespace(train_id=train_id)
    route = MagicMock()

    extend_result = SimpleNamespace(
        success=False,
        failure_kind=PathAcquisitionFailureKind.NO_VIABLE_PATH,
        reason="BLK_Y is still held by this path",
        path_id="P1",
    )
    dispatcher = _FakeDispatcher(train_id, active_path_id="P1", extend_result=extend_result)

    executor = RouteExecutor(network=MagicMock(), path_dispatcher=dispatcher)
    executor.simulation_controller = _runtime_with_stream()
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
    assert latches.path_cancel_request is not None
    assert latches.path_cancel_request.error.origin == "revoked"
    assert "still held" in (latches.path_cancel_request.error.text or "")
    assert _emitted_errors(executor) == []


def test_temporary_failure_kinds_still_emit_a_hard_error():
    """A temporary failure keeps the live path and emits a hard error event.

    BLOCKED_BEFORE_TARGET means a viable candidate exists but a current
    occupancy/reservation/lock conflict stopped acquisition. Per design_new.md
    "Unreachable and broken task handling" the live path stays valid and Phase 1
    retries later, so this must not latch a cancellation.
    """
    train_id = "T3"
    train = SimpleNamespace(train_id=train_id)
    route = MagicMock()

    extend_result = SimpleNamespace(
        success=False,
        failure_kind=PathAcquisitionFailureKind.BLOCKED_BEFORE_TARGET,
        reason="No forward path from BLK_X to BLK_Y",
        path_id="P1",
    )
    dispatcher = _FakeDispatcher(train_id, active_path_id="P1", extend_result=extend_result)

    executor = RouteExecutor(network=MagicMock(), path_dispatcher=dispatcher)
    executor.simulation_controller = _runtime_with_stream()
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
    # No recovery latch — the live path is still valid and worth retrying.
    assert latches.path_cancel_request is None
    # The diagnostic is an event carrying an ErrorRecord, not entity state.
    errors = _emitted_errors(executor)
    assert errors, "a temporary acquisition failure must emit an error event"
    assert errors[-1].error.origin == "planning_failed"
    assert "No forward path" in errors[-1].error.text
