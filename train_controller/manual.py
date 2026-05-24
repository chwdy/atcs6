"""
Manual Driving sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: Manual
Driving owns the manual-mode submode flag (``manual_mode_type``:
``"simulation"`` vs ``"power_percent"``) and the manual power-percent
input (``manual_power_percent``: 0..100 voltage-style command).

The operator-facing setters ``set_manual_mode_type(mode_type)`` and
``set_manual_power_percent(percent)`` live on the operator command
surface (``controller.commands.*`` after PR 9; currently on the
façade); they write through to the fields on this class.

External callers access manual-driving state as:

  - ``controller.manual.manual_mode_type``
  - ``controller.manual.manual_power_percent``
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from train_controller import TrainController


class ManualDriving:
    """Owns the manual-mode submode flag and manual power-percent input."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller
        # 'simulation' or 'power_percent'
        self.manual_mode_type: str = "simulation"
        # 0..100 voltage-style command for manual power_percent mode
        self.manual_power_percent: int = 0
