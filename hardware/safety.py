"""One place that drives every physical output to its safe state.

Track voltage and switch coils are safed together, in one order, by one
object. Splitting that across the two drivers would let them disagree about
whether the rig is driving — and a half-safe rig is the state where a
locomotive keeps rolling while the app believes it stopped everything.

What safe state means here: every '595 coil bit latched low, then every PWM
channel at zero duty. Coils go first because a solenoid point motor is the
output that destroys itself if left energized; track voltage only keeps a
train moving.

This covers a commanded stop, a stalled control loop (see `OutputWatchdog`),
and any failure the runtime reports. It cannot cover this process dying: the
PCA9685 holds its last duty and the '595 its last latched bits until something
writes to them, and a dead process writes nothing. Surviving that needs a
circuit outside the host holding the boards disabled, which this rig does not
have.
"""

from __future__ import annotations

import threading
from typing import List, Mapping, Sequence


class SafeStateError(RuntimeError):
    """One or more outputs did not reach their safe state.

    Carries the per-chip failures as data as well as in the sentence. Whoever
    reports this to an operator has to name the chips that did not answer, and
    re-parsing the message to recover them is not a contract worth having.
    """

    def __init__(self, failures: Sequence[str]) -> None:
        self.failures: List[str] = list(failures)
        super().__init__(
            "one or more physical outputs could not be safed: "
            + "; ".join(self.failures)
        )


class OutputSafety:
    """De-energizes every output the session can drive."""

    def __init__(
        self,
        track_chips: Mapping[str, object],
        switch_chips: Mapping[str, object],
    ) -> None:
        self._track_chips = dict(track_chips)
        self._switch_chips = dict(switch_chips)
        self._lock = threading.RLock()

    def enter_safe_state(self) -> None:
        """Latch every coil low, then zero every PWM channel.

        Best-effort by design: one chip failing must not leave the rest
        energized, so every chip is attempted and the failures are collected
        into one raise at the end. A caller that cannot act on the raise still
        gets the safest rig this transport can deliver.
        """
        with self._lock:
            errors = []
            for chip_id, chip in self._switch_chips.items():
                try:
                    # One atomic pattern per chain: every bit low.
                    chip.set_high_outputs(())
                except Exception as exc:  # noqa: BLE001 - continue clearing PWM
                    errors.append(f"{chip_id}: {exc}")
            for chip_id, chip in self._track_chips.items():
                try:
                    chip.all_off()
                except Exception as exc:  # noqa: BLE001 - clear remaining chips
                    errors.append(f"{chip_id}: {exc}")
            if errors:
                raise SafeStateError(errors)
