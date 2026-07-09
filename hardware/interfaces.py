"""Real interface adapters: map a logical id to a chip + port.

These satisfy the existing abstract contracts unchanged — `OutputInterface`
(block track voltage), `SwitchOutputInterface` (switch throw), and
`InputInterface` (block current) — so the runtime keeps addressing hardware by
`block_id` / `switch_id` and never sees a chip or port. The binding map (logical
id -> (chip_id, port)) is passed in from persisted config; this is the only place
that consumes it.
"""

from typing import Any, Dict, List, Optional, Tuple

from block_occupancy import InputInterface
from block_power import MAX_TRACK_VOLTAGE, OutputInterface
from switch_power import SwitchOutputInterface

from hardware.catalog import (
    CHIP_CATALOG,
    ROLE_OCCUPANCY,
    ROLE_SWITCH,
    ROLE_TRACK,
)
from hardware.chips.hc165 import HC165Chain
from hardware.chips.hc595 import HC595Chain
from hardware.chips.pca9685 import PCA9685

# A binding map: logical id -> (chip_id, port).
BindingMap = Dict[str, Tuple[str, int]]


def build_chip(device: Any, spec: Any) -> Any:
    """Instantiate the chip driver for a chip spec (duck-typed: chip_id, kind,
    i2c_address, chain_length, spi_select)."""
    ck = CHIP_CATALOG[spec.kind]
    if spec.kind == "pca9685":
        address = spec.i2c_address if spec.i2c_address is not None else ck.default_i2c_address
        return PCA9685(device, address=address)
    if spec.kind == "hc595":
        return HC595Chain(
            device, select=spec.spi_select or ck.default_spi_select,
            chain_length=spec.chain_length,
        )
    if spec.kind == "hc165":
        return HC165Chain(
            device, select=spec.spi_select or ck.default_spi_select,
            chain_length=spec.chain_length,
        )
    raise ValueError(f"unknown chip kind {spec.kind!r}")


class Ch347TrackOutputInterface(OutputInterface):
    """Block track voltage over PCA9685 channel pairs (H-bridge per port)."""

    def __init__(self, chips_by_id: Dict[str, PCA9685], bindings: BindingMap) -> None:
        self._chips = chips_by_id
        self._bindings = bindings
        self._voltages: Dict[str, float] = {}

    def set_voltage(self, block_id: str, voltage: float) -> bool:
        binding = self._bindings.get(block_id)
        if binding is None:
            return False
        chip_id, port = binding
        chip = self._chips.get(chip_id)
        if chip is None:
            return False
        duty = min(abs(voltage) / MAX_TRACK_VOLTAGE, 1.0) if MAX_TRACK_VOLTAGE else 0.0
        in1, in2 = 2 * port, 2 * port + 1   # H-bridge inputs
        if voltage >= 0:
            chip.set_duty_cycle(in1, duty)
            chip.set_duty_cycle(in2, 0.0)
        else:
            chip.set_duty_cycle(in1, 0.0)
            chip.set_duty_cycle(in2, duty)
        self._voltages[block_id] = voltage
        return True

    def get_actual_voltage(self, block_id: str) -> float:
        return self._voltages.get(block_id, 0.0)

    def get_all_voltages(self) -> Dict[str, float]:
        return dict(self._voltages)

    def enter_safe_state(self) -> None:
        for chip in self._chips.values():
            chip.all_off()
        for block_id in self._voltages:
            self._voltages[block_id] = 0.0


class Ch347SwitchOutputInterface(SwitchOutputInterface):
    """Switch throw over 74HC595 output bits (one bit per switch)."""

    def __init__(self, chips_by_id: Dict[str, HC595Chain], bindings: BindingMap) -> None:
        self._chips = chips_by_id
        self._bindings = bindings
        self._last: Dict[str, int] = {}

    def set_throw(self, switch_id: str, target_state: int) -> bool:
        binding = self._bindings.get(switch_id)
        if binding is None:
            return False
        chip_id, port = binding
        chip = self._chips.get(chip_id)
        if chip is None:
            return False
        state = 0 if target_state == 0 else 1
        chip.set_output(port, bool(state))
        self._last[switch_id] = state
        return True

    def get_last_throw(self, switch_id: str) -> Optional[int]:
        return self._last.get(switch_id)

    def enter_safe_state(self) -> None:
        for chip in self._chips.values():
            chip.all_off()
        self._last.clear()


class Ch347OccupancyInputInterface(InputInterface):
    """Block current over 74HC165 input bits (one bit per block)."""

    def __init__(self, chips_by_id: Dict[str, HC165Chain], bindings: BindingMap) -> None:
        self._chips = chips_by_id
        self._bindings = bindings

    def read_block_currents(self) -> Dict[str, bool]:
        # Read each chain once per call, then map bound blocks to their bit.
        cache: Dict[str, bytes] = {}
        result: Dict[str, bool] = {}
        for block_id, (chip_id, port) in self._bindings.items():
            chip = self._chips.get(chip_id)
            if chip is None:
                result[block_id] = False
                continue
            if chip_id not in cache:
                cache[chip_id] = chip.read_all()
            data = cache[chip_id]
            byte_idx, bit_idx = divmod(port, 8)
            result[block_id] = (
                bool(data[byte_idx] & (1 << bit_idx)) if byte_idx < len(data) else False
            )
        return result


def build_real_interfaces(
    device: Any,
    chip_specs: List[Any],
    block_track: BindingMap,
    block_occupancy: BindingMap,
    switch_output: BindingMap,
) -> Tuple[Optional[InputInterface], Optional[OutputInterface], Optional[SwitchOutputInterface]]:
    """Instantiate + initialize chips grouped by role and wire the three
    interfaces. A role with no chip returns `None` (its simulated default is
    used). Performs live init (PWM reset/freq, shift-register clear) so hardware
    starts in a known state."""
    track_chips: Dict[str, PCA9685] = {}
    switch_chips: Dict[str, HC595Chain] = {}
    input_chips: Dict[str, HC165Chain] = {}

    for spec in chip_specs:
        ck = CHIP_CATALOG[spec.kind]
        chip = build_chip(device, spec)
        if ck.role == ROLE_TRACK:
            chip.configure()
            track_chips[spec.chip_id] = chip
        elif ck.role == ROLE_SWITCH:
            chip.all_off()
            switch_chips[spec.chip_id] = chip
        elif ck.role == ROLE_OCCUPANCY:
            input_chips[spec.chip_id] = chip

    input_iface = (
        Ch347OccupancyInputInterface(input_chips, block_occupancy) if input_chips else None
    )
    block_out = (
        Ch347TrackOutputInterface(track_chips, block_track) if track_chips else None
    )
    switch_out = (
        Ch347SwitchOutputInterface(switch_chips, switch_output) if switch_chips else None
    )
    return input_iface, block_out, switch_out
