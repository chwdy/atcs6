"""
Physics Sync sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: Physics
Sync owns the live span and speed that ``TrainPhysics.tick(...)`` writes
back to the controller through the orchestrator entry point
``controller.on_physics_updated(...)``.

The orchestrator method stays flat on the façade because it (a) is the
canonical callback name ``TrainPhysics`` invokes, and (b) drives
cross-sub-module work (reactive state transitions on the operational
state machine, progress-report build that needs ``received_path_plan``,
etc.). The façade method delegates the simple field writes — and the
Phase-3 mailbox — to this sub-module.

External callers access live span and speed as:

  - ``controller.physics_sync.current_span`` (canonical TrainSpan
    storage; written by ``on_physics_updated``)
  - ``controller.physics_sync.current_speed_kmh`` (canonical live
    speed in km/h)
  - ``controller.physics_sync.queued_progress_reports`` (Phase-3
    mailbox; drained by ``SimulationController`` Phase 3)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List, Optional

from runtime_payloads import ControllerProgressReport

if TYPE_CHECKING:
    from train_controller import TrainController


class PhysicsSync:
    """Owns the controller's live span, live speed, and Phase-3 progress mailbox."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller
        self.current_span: Optional[Any] = None
        self.current_speed_kmh: float = 0.0
        self.queued_progress_reports: List[ControllerProgressReport] = []
