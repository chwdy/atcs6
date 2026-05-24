"""
Operator Command Surface sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: the
Operator command surface owns the operator-side configuration state
(``cab`` selection, ``control_mode``) and the per-tick command inbox /
canonical drain order.

External callers access operator-command state as:

  - ``controller.commands.cab`` (``'A'`` / ``'B'`` / ``None``)
  - ``controller.commands.control_mode`` (``'ato'`` / ``'manual'``)
  - ``controller.commands.command_inbox`` (list of pending ``Command``s)
  - ``controller.commands.drain_order`` (canonical FIFO drain order)

The operator-facing setter and queue methods (``set_cab``,
``set_mode``, ``set_handle``, ``set_manual_mode_type``,
``set_manual_power_percent``, ``power_on_cmd``, ``power_off_cmd``,
``enqueue_command``) remain on the façade as the public command
surface because they orchestrate writes across multiple sub-modules
(eb, ato, manual, route_hooks, physics_sync) and the cleanest call
shape stays ``controller.<command>()``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, List

if TYPE_CHECKING:
    from train_controller import TrainController


class OperatorCommands:
    """Owns operator-side configuration state and the command inbox."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

        # Operator cab/mode selection.
        self.cab: Any = None  # 'A', 'B', or None
        self.control_mode: str = "ato"  # 'ato' or 'manual'

        # Per design_new.md "Command queue + drain": operator commands
        # land here and the tick-entry drain processes them in canonical
        # ``drain_order``.
        self.command_inbox: List[Any] = []
        self.drain_order: tuple = (
            "power_on",
            "set_cab",
            "set_mode",
            "set_manual_mode_type",
            "set_handle",
            "set_manual_power_percent",
            "activate_emergency_cmd",
            "release_emergency_cmd",
            "power_off",
        )
