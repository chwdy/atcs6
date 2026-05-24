"""
Route Hooks sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: Route
Hooks owns the four Phase-3 latch fields (``path_cancel_request``,
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
    design_new.md "Latch table"
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
    """Owns the four Phase-3 retained latches bundled under PhaseLatches."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller
        self.phase_latches: PhaseLatches = PhaseLatches()
