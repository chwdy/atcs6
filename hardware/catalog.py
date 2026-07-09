"""Chip + controller catalog — the metadata that drives the Hardware Setup
dropdowns, config validation, and per-chip port counts.

Adding support for a new chip is one entry here: its bus, the interface role it
fills, how many ports it contributes, and which parameters the user must set.
Everything else (validation, the catalog REST payload, the binding-port pool)
reads this table, so no chip knowledge is duplicated elsewhere.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

# Interface roles a chip can fill (one per abstract interface).
ROLE_TRACK = "track_output"        # OutputInterface        (block track voltage)
ROLE_SWITCH = "switch_output"      # SwitchOutputInterface  (switch throw)
ROLE_OCCUPANCY = "occupancy_input"  # InputInterface        (block current)
ROLES = (ROLE_TRACK, ROLE_SWITCH, ROLE_OCCUPANCY)

BUS_I2C = "i2c"
BUS_SPI = "spi"

# Physical SPI select/latch lines the CH347 exposes, named by *pad* (not the
# off-by-one ch347api name). See ch347_device for the pad -> library mapping.
SPI_SELECT_LINES: List[str] = ["cs0", "cs1"] + [f"gpio{i}" for i in range(8)]


@dataclass(frozen=True)
class ChipKind:
    """One selectable chip type."""

    kind: str
    label: str
    bus: str
    role: str
    ports_per_unit: int          # ports one physical chip contributes
    cascadable: bool             # SPI shift registers chain; the PCA9685 does not
    needs_i2c_address: bool
    needs_spi_select: bool
    port_kind: str               # "bipolar_pair" | "digital_out" | "digital_in"
    default_i2c_address: Optional[int] = None
    default_spi_select: Optional[str] = None
    port_help: str = ""


CHIP_CATALOG: Dict[str, ChipKind] = {
    "pca9685": ChipKind(
        kind="pca9685",
        label="PCA9685 · 16-ch PWM",
        bus=BUS_I2C,
        role=ROLE_TRACK,
        # 16 PWM channels -> 8 bipolar track ports. Each port is an H-bridge
        # channel pair (2*port = IN1, 2*port+1 = IN2): magnitude -> duty,
        # sign -> which input drives, so track voltage stays bipolar (±12V).
        ports_per_unit=8,
        cascadable=False,
        needs_i2c_address=True,
        needs_spi_select=False,
        port_kind="bipolar_pair",
        default_i2c_address=0x40,
        port_help="H-bridge channel pair per port; magnitude→duty, sign→direction.",
    ),
    "hc595": ChipKind(
        kind="hc595",
        label="74HC595 · shift-out",
        bus=BUS_SPI,
        role=ROLE_SWITCH,
        ports_per_unit=8,
        cascadable=True,
        needs_i2c_address=False,
        needs_spi_select=True,
        port_kind="digital_out",
        default_spi_select="cs0",
        port_help="One output bit per port; level = MAIN(0)/DIVERGE(1).",
    ),
    "hc165": ChipKind(
        kind="hc165",
        label="74HC165 · shift-in",
        bus=BUS_SPI,
        role=ROLE_OCCUPANCY,
        ports_per_unit=8,
        cascadable=True,
        needs_i2c_address=False,
        needs_spi_select=True,
        port_kind="digital_in",
        default_spi_select="cs1",
        port_help="One input bit per port; 1 = current present on the block.",
    ),
}


@dataclass(frozen=True)
class ControllerKind:
    """One selectable controller (transport) type. v1 ships CH347 only; the
    table stays open so a new bridge is one entry."""

    kind: str
    label: str
    needs_device_port: bool


CONTROLLER_CATALOG: Dict[str, ControllerKind] = {
    "ch347": ControllerKind(
        kind="ch347",
        label="CH347 · USB-HID bridge",
        needs_device_port=False,   # opened by VID/PID; device_port optional
    ),
}


def ports_for_chip(kind: str, chain_length: int) -> int:
    """Number of ports a configured chip contributes (chain-aware)."""
    ck = CHIP_CATALOG[kind]
    units = max(1, int(chain_length)) if ck.cascadable else 1
    return ck.ports_per_unit * units


def catalog_payload() -> dict:
    """JSON-serializable catalog for `GET /api/hardware/catalog` (frontend
    dropdowns)."""
    return {
        "controllers": [
            {"kind": c.kind, "label": c.label, "needs_device_port": c.needs_device_port}
            for c in CONTROLLER_CATALOG.values()
        ],
        "chips": [
            {
                "kind": ck.kind,
                "label": ck.label,
                "bus": ck.bus,
                "role": ck.role,
                "ports_per_unit": ck.ports_per_unit,
                "cascadable": ck.cascadable,
                "needs_i2c_address": ck.needs_i2c_address,
                "needs_spi_select": ck.needs_spi_select,
                "port_kind": ck.port_kind,
                "default_i2c_address": ck.default_i2c_address,
                "default_spi_select": ck.default_spi_select,
                "port_help": ck.port_help,
            }
            for ck in CHIP_CATALOG.values()
        ],
        "spi_select_lines": SPI_SELECT_LINES,
        "roles": {
            "track_output": ROLE_TRACK,
            "switch_output": ROLE_SWITCH,
            "occupancy_input": ROLE_OCCUPANCY,
        },
    }
