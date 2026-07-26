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

    def set_power_percent(self, percent: float) -> None:
        """Sanctioned operator-input writer for `manual_power_percent`.

        Per design_new.md: the operator setter (`TrainController.commands.
        set_manual_power_percent(...)`) writes through this method. No other
        writer sets a positive value.
        """
        self.manual_power_percent = percent

    def reset_power_percent_for_safety(self) -> None:
        """Sanctioned safety-reset writer for `manual_power_percent`.

        Per design_new.md: ATC / service-stop / EB / power-off orchestration
        calls this to zero the retained command; those safety workflows never
        assign the field directly.
        """
        self.manual_power_percent = 0.0
