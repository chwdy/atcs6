"""
Route Hooks sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: Route
Hooks owns the five Action latch fields (``path_cancel_request``,
``route_abort``, ``power_off``, ``route_deactivate``, ``emergency_request``)
bundled under ``PhaseLatches``. The orchestrator entry point ``on_path_complete()``
stays flat on the façade because it is a direct external callback (from
``PathDispatcher``), but its body lives alongside the latch helpers here
when it needs to write latch state.

External callers access route-hook state and the latch surface as:

  - ``controller.route_hooks.phase_latches`` (the PhaseLatches aggregate)
  - ``controller.route_hooks.phase_latches.set_path_cancel_request(...)``
    / ``.set_route_abort(...)`` / ``.set_power_off(...)`` /
    ``.set_route_deactivate(...)`` / ``.set_emergency_request(...)`` —
    canonical writer helpers per
    design_new.md "Latch Table"
  - ``controller.route_hooks.phase_latches.path_cancel_request``
    (Optional[payload]) etc. — read-only field access

The façade keeps the orchestrator method name as the public callback
surface:

  - ``controller.on_path_complete()``

Those façade methods delegate to RouteHooks (or write through to other
sub-modules) when latch writes are needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from runtime_payloads import PhaseLatches

if TYPE_CHECKING:
    from train_controller import TrainController


class RouteHooks:
    """Owns the five Action retained latches bundled under PhaseLatches."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller
        # Inject the emit hook rather than letting the aggregate reach for a
        # controller: every latch write and every Action commit reports itself
        # on the one stream (design_new.md "Error events", Deferred actions are
        # events too), while `PhaseLatches` stays a plain data class.
        self.phase_latches: PhaseLatches = PhaseLatches()
        self.phase_latches.notify = controller.notifications.emit
