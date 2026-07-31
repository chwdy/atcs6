"""Shared physical-output inhibit for a hardware-backed session.

The PCA9685 /OE lines are active-high disables.  This supervisor owns them so
every safe-state path follows one order: inhibit track output first, then clear
PWM and solenoid outputs.  Enabling is the reverse: clear every output first,
then release the inhibits last.

This direct inhibit handles commanded shutdown and in-process failures.  Real
Hardware activation must additionally prove an external renewable heartbeat
gate; a process-local object cannot protect against host or process death.
"""

from __future__ import annotations

import threading
from typing import Iterable, Mapping, Optional, Protocol, Tuple


class OutputSafetyLease(Protocol):
    """Independent hardware gate controlled through a renewable permission.

    The implementation must fail disabled when renewal stops, including when
    this process or host dies. ``acquire`` must prove the gate before returning
    true; ``revoke`` must request immediate disable.
    """

    def acquire(self) -> bool: ...

    def renew(self) -> bool: ...

    def revoke(self) -> None: ...


class PhysicalOutputSupervisor:
    """Own the active-high track-output inhibit and the global safe pattern."""

    def __init__(
        self,
        track_chips: Mapping[str, object],
        switch_chips: Mapping[str, object],
        output_enable: Mapping[str, Tuple[str, int]],
        lease: Optional[OutputSafetyLease] = None,
    ) -> None:
        self._track_chips = dict(track_chips)
        self._switch_chips = dict(switch_chips)
        self._output_enable = dict(output_enable)
        self._lease = lease
        self._lock = threading.RLock()
        self._active = False

        for chip_id, chip in self._switch_chips.items():
            ignored = self._inhibit_ports(chip_id)
            configure = getattr(chip, "set_guard_ignored_high", None)
            if callable(configure):
                configure(ignored)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def has_external_lease(self) -> bool:
        return self._lease is not None

    def activate(self) -> None:
        """Clear every output, then release active-high inhibits last."""
        with self._lock:
            self._active = False
            self._write_switch_safe_patterns()
            for chip in self._track_chips.values():
                chip.all_off()
            if self._lease is not None and not self._lease.acquire():
                raise RuntimeError("external output-safety lease proof failed")
            for _track_id, (chip_id, port) in self._output_enable.items():
                chip = self._switch_chips.get(chip_id)
                if chip is None:
                    raise RuntimeError(
                        f"output inhibit references missing chip {chip_id!r}"
                    )
                chip.set_output(port, False)
            self._active = True

    def renew(self) -> bool:
        """Renew physical output permission before issuing a device command."""
        with self._lock:
            if not self._active:
                return False
            if self._lease is None:
                return True
            try:
                renewed = bool(self._lease.renew())
            except Exception:  # noqa: BLE001 - any lease error means no permission
                renewed = False
            if renewed:
                return True
            try:
                self.revoke()
            except Exception:
                pass
            return False

    def revoke(self) -> None:
        """Inhibit first, then best-effort clear every physical output."""
        with self._lock:
            self._active = False
            errors = []
            if self._lease is not None:
                try:
                    self._lease.revoke()
                except Exception as exc:  # noqa: BLE001 - still inhibit locally
                    errors.append(exc)
            # One atomic pattern per chain: all coils low and every /OE high.
            try:
                self._write_switch_safe_patterns()
            except Exception as exc:  # noqa: BLE001 - continue clearing PWM
                errors.append(exc)
            for chip in self._track_chips.values():
                try:
                    chip.all_off()
                except Exception as exc:  # noqa: BLE001 - clear remaining chips
                    errors.append(exc)
            if errors:
                raise RuntimeError(
                    "one or more physical outputs could not be safed: "
                    + "; ".join(str(exc) for exc in errors)
                )

    def initialize_disabled(self) -> None:
        """Latch the safe pattern before slower device initialization."""
        with self._lock:
            self._active = False
            self._write_switch_safe_patterns()

    def _inhibit_ports(self, chip_id: str) -> Iterable[int]:
        return (
            port
            for _track_id, (drive_chip_id, port) in self._output_enable.items()
            if drive_chip_id == chip_id
        )

    def _write_switch_safe_patterns(self) -> None:
        for chip_id, chip in self._switch_chips.items():
            chip.set_high_outputs(self._inhibit_ports(chip_id))
