"""Train-level scenarios for the Virtual Hardware device.

The rig writes the configured detector channels, never model occupancy. The
ordinary HAL, debounce, attribution, unidentified train, and safety paths therefore consume
the scenario exactly as they consume physical detector responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from hardware.interfaces import build_chip


def _chip_spec(cfg: Any, chip_id: str) -> Any:
    return next((chip for chip in cfg.chips if chip.chip_id == chip_id), None)


def has_presence_binding(cfg: Any, block_id: str) -> bool:
    """Whether `block_id` has any detector this module can write."""
    return block_id in cfg.block_occupancy or block_id in cfg.block_occupancy_current


def set_block_presence(cfg: Any, device: Any, block_id: str, present: bool) -> None:
    """Write every detector source `block_id` is bound to, consistently.

    A block's occupancy is hybrid: which source the runtime believes depends on
    how hard the block is being driven (see `OccupancyDriver`), so writing only
    one of the two produces a reading the runtime may be ignoring. Placing a
    virtual object on the detectors therefore means writing both — the '165 bit
    AND a current comfortably clear of the channel's threshold — and lifting it
    means clearing both. Every caller that puts a train somewhere goes through
    here so the rig and the bench cannot drift apart on that rule.

    Raises ValueError when the block has no detector binding at all, or names a
    chip the config does not have.
    """
    digital = cfg.block_occupancy.get(block_id)
    current = cfg.block_occupancy_current.get(block_id)
    if digital is None and current is None:
        raise ValueError(f"block {block_id!r} has no occupancy binding")

    if digital is not None:
        spec = _chip_spec(cfg, digital.chip_id)
        if spec is None:
            raise ValueError(f"occupancy binding for {block_id!r} names a missing chip")
        chip = build_chip(device, spec)
        raw_bit = (not present) if bool(getattr(spec, "active_low", False)) else present
        device.set_input_bit(chip.select, digital.port, raw_bit, width=spec.chain_length)

    if current is not None:
        spec = _chip_spec(cfg, current.chip_id)
        if spec is None:
            raise ValueError(f"current binding for {block_id!r} names a missing chip")
        chip = build_chip(device, spec)
        baseline = float(current.baseline_ma or 0.0)
        threshold = float(current.threshold_ma or 3.0)
        # Clear of the threshold rather than at it: a reading that sits exactly
        # on the boundary tests the comparator, not the app's reaction.
        milliamps = baseline + threshold + 5.0 if present else baseline
        if spec.kind == "ina219":
            device.set_ina219_current(chip.address, milliamps / 1000.0)
        else:
            device.set_ina3221_current(chip.address, current.port + 1, milliamps / 1000.0)


def install_drive_swamp(cfg: Any, device: Any) -> None:
    """Make the fake device's '165 bit for each block read asserted while the
    block's rails are driven, as the swamped detector does on the rig
    (design_new.md "Hardware setup", Virtual Hardware). Only a block bound both
    to rails on a PCA9685 and to a '165 bit gets an entry."""
    entries = []
    for block_id, digital in cfg.block_occupancy.items():
        track = cfg.block_track.get(block_id)
        digital_spec = _chip_spec(cfg, digital.chip_id)
        track_spec = _chip_spec(cfg, track.chip_id) if track is not None else None
        if track_spec is None or digital_spec is None or track_spec.i2c_address is None:
            continue
        chip = build_chip(device, digital_spec)
        entries.append((
            track_spec.i2c_address,
            (track.forward, track.reverse),
            chip.select,
            digital.port,
            bool(getattr(digital_spec, "active_low", False)),
        ))
    device.set_drive_swamp(entries)


def bound_block_ids(cfg: Any) -> List[str]:
    """Every block this module can write a presence for, in configured order.

    Digital bindings first, then the current-only ones, so a block bound both
    ways keeps the position its detector binding gave it.
    """
    ordered = list(cfg.block_occupancy)
    seen = set(ordered)
    ordered.extend(b for b in cfg.block_occupancy_current if b not in seen)
    return ordered


def seed_startup_presence(
    cfg: Any, device: Any, *, occupied_block_id: Optional[str] = None
) -> Optional[str]:
    """Put a fresh virtual device into "one train parked on the layout".

    An untouched `FakeCh347Device` returns zeros for every '165 byte, and zero on
    an `active_low` chain means CURRENT DETECTED — so a Virtual Hardware session
    used to come up with *every* bound block occupied, and the bench's Place
    toggles offered to place trains where one already was. Writing the bindings
    explicitly here makes the rest state a property of this module rather than of
    whatever the register file happens to hold: every block is written clear
    through the same polarity-correcting path the Place toggle uses, and then one
    block is given a train.

    Returns the block the train landed on, or None when nothing is bound.
    """
    blocks = bound_block_ids(cfg)
    if not blocks:
        return None
    for block_id in blocks:
        set_block_presence(cfg, device, block_id, False)
    chosen = (
        occupied_block_id
        if occupied_block_id is not None and occupied_block_id in blocks
        else blocks[0]
    )
    set_block_presence(cfg, device, chosen, True)
    return chosen


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
            if not has_presence_binding(cfg, block_id):
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

    def _set_present(self, cfg: Any, device: Any, block_id: str, present: bool) -> None:
        set_block_presence(cfg, device, block_id, present)
