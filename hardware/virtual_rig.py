"""Train-level scenarios for the Virtual Hardware device.

The rig writes the configured detector channels, never model occupancy. The
ordinary HAL, debounce, attribution, ghost, and safety paths therefore consume
the scenario exactly as they consume physical detector responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from hardware.interfaces import build_chip


@dataclass
class VirtualRig:
    blocks: List[str] = field(default_factory=list)
    index: Optional[int] = None
    stalled: bool = False

    def status(self) -> Dict[str, Any]:
        return {
            "active": self.index is not None,
            "blocks": list(self.blocks),
            "index": self.index,
            "block_id": self.current_block,
            "stalled": self.stalled,
        }

    @property
    def current_block(self) -> Optional[str]:
        if self.index is None or not (0 <= self.index < len(self.blocks)):
            return None
        return self.blocks[self.index]

    def start(self, cfg: Any, device: Any, blocks: List[str]) -> Dict[str, Any]:
        blocks = [str(block_id) for block_id in blocks if str(block_id)]
        if not blocks:
            raise ValueError("scenario needs at least one block_id")
        for block_id in blocks:
            if block_id not in cfg.block_occupancy and block_id not in cfg.block_occupancy_current:
                raise ValueError(f"block {block_id!r} has no occupancy binding")
        self.remove(cfg, device)
        self.blocks = blocks
        for block_id in blocks:
            self._set_present(cfg, device, block_id, False)
        self.index = 0
        self.stalled = False
        self._set_present(cfg, device, blocks[0], True)
        return self.status()

    def advance(self, cfg: Any, device: Any) -> Dict[str, Any]:
        if self.index is None:
            raise ValueError("no Virtual Rig scenario is active")
        if self.stalled:
            return self.status()
        if self.index + 1 >= len(self.blocks):
            return self.status()
        old = self.blocks[self.index]
        new = self.blocks[self.index + 1]
        # Model a completed boundary crossing: new detector confirms while the
        # prior detector clears. Both changes land under the route's lock.
        self._set_present(cfg, device, new, True)
        self._set_present(cfg, device, old, False)
        self.index += 1
        return self.status()

    def set_stalled(self, stalled: bool) -> Dict[str, Any]:
        if self.index is None:
            raise ValueError("no Virtual Rig scenario is active")
        self.stalled = bool(stalled)
        return self.status()

    def remove(self, cfg: Any, device: Any) -> Dict[str, Any]:
        if self.current_block is not None:
            self._set_present(cfg, device, self.current_block, False)
        self.blocks = []
        self.index = None
        self.stalled = False
        return self.status()

    @staticmethod
    def _chip_spec(cfg: Any, chip_id: str) -> Any:
        return next((chip for chip in cfg.chips if chip.chip_id == chip_id), None)

    def _set_present(self, cfg: Any, device: Any, block_id: str, present: bool) -> None:
        digital = cfg.block_occupancy.get(block_id)
        if digital is not None:
            spec = self._chip_spec(cfg, digital.chip_id)
            if spec is None:
                raise ValueError(f"occupancy binding for {block_id!r} names a missing chip")
            chip = build_chip(device, spec)
            raw_bit = (not present) if bool(getattr(spec, "active_low", False)) else present
            device.set_input_bit(
                chip.select, digital.port, raw_bit, width=spec.chain_length
            )

        current = cfg.block_occupancy_current.get(block_id)
        if current is not None:
            spec = self._chip_spec(cfg, current.chip_id)
            if spec is None:
                raise ValueError(f"current binding for {block_id!r} names a missing chip")
            chip = build_chip(device, spec)
            baseline = float(current.baseline_ma or 0.0)
            threshold = float(current.threshold_ma or 3.0)
            milliamps = baseline + threshold + 5.0 if present else baseline
            if spec.kind == "ina219":
                device.set_ina219_current(chip.address, milliamps / 1000.0)
            else:
                device.set_ina3221_current(
                    chip.address, current.port + 1, milliamps / 1000.0
                )
