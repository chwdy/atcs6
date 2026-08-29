"""
Route Hooks sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: Route
Hooks owns the four Action latch fields (``path_cancel_request``,
``route_abort``, ``power_off``, ``route_deactivate``) bundled under
``PhaseLatches``. The orchestrator entry points
``enter_station_dwell()``, ``finish_dwell()``, and
``on_path_complete()`` stay flat on the façade because they are direct
external callbacks (from ``RouteExecutor`` / ``PathDispatcher``), but
their bodies live alongside the latch helpers here when they need to
write latch state.

External callers access route-hook state and the latch surface as:

  - ``controller.route_hooks.phase_latches`` (the PhaseLatches aggregate)
  - ``controller.route_hooks.phase_latches.set_path_cancel_request(...)``
    / ``.set_route_abort(...)`` / ``.set_power_off(...)`` /
    ``.set_route_deactivate(...)`` — canonical writer helpers per
    design_new.md "Latch Table"
  - ``controller.route_hooks.phase_latches.path_cancel_request``
    (Optional[payload]) etc. — read-only field access

The façade keeps the orchestrator method names as the public callback
surface:

  - ``controller.enter_station_dwell()``
  - ``controller.finish_dwell()``
  - ``controller.on_path_complete()``
  - ``controller.depart()``

Those façade methods delegate to RouteHooks (or write through to other
sub-modules) when latch writes are needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from runtime_payloads import PhaseLatches

if TYPE_CHECKING:
    from train_controller import TrainController


class RouteHooks:
    """Owns the four Action retained latches bundled under PhaseLatches."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller
        # Inject the emit hook rather than letting the aggregate reach for a
        # controller: every latch write and every Action commit reports itself
        # on the one stream (design_new.md "Error events", Deferred actions are
        # events too), while `PhaseLatches` stays a plain data class.
        self.phase_latches: PhaseLatches = PhaseLatches()
        self.phase_latches.notify = controller.notifications.emit
