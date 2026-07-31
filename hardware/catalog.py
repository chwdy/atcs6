"""Chip + controller catalog — the metadata that drives the Hardware Setup
dropdowns, config validation, and per-chip port counts.

Adding support for a new chip is one entry here: the buses it can be wired on,
the interface role it fills, how many ports it contributes, and how its ports
behave. Everything else (validation, the catalog REST payload, the binding-port
pool) reads this table, so no chip knowledge is duplicated elsewhere.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# Interface roles a chip can fill (one per abstract interface).
ROLE_TRACK = "track_output"        # TrackOutput        (block track voltage)
ROLE_SWITCH = "switch_output"      # SwitchOutput  (switch throw)
ROLE_OCCUPANCY = "occupancy_input"  # OccupancyInput        (block current)
ROLES = (ROLE_TRACK, ROLE_SWITCH, ROLE_OCCUPANCY)

# Buses a chip can be wired on. A chip *type* declares which of these it supports
# (its `buses`); each configured chip *instance* records the one it is actually
# wired on. A shift register is the reason a chip can have more than one: it can
# be driven by the hardware SPI peripheral (`spi`) or bit-banged over GPIO pins
# (`gpio`). A PWM/current chip is I2C only.
BUS_I2C = "i2c"
BUS_SPI = "spi"
BUS_GPIO = "gpio"

# Port kinds a chip's ports expose. Occupancy has two: a digital detector bit
# ('165) and an analog current-sense channel (INA3221); config validation routes
# the digital-occupancy vs current-occupancy binding maps by these.
PORT_BIPOLAR_PAIR = "bipolar_pair"
PORT_DIGITAL_OUT = "digital_out"
PORT_DIGITAL_IN = "digital_in"
PORT_CURRENT_SENSE = "current_sense"

# Each port kind implies a data direction. Direction picks the commissioning
# test the Hardware Setup screen offers: an output chip is *driven* (pulse /
# sweep a port), an input chip is *read* (live view of the bits / currents).
DIR_OUTPUT = "output"
DIR_INPUT = "input"
_PORT_DIRECTION: Dict[str, str] = {
    PORT_BIPOLAR_PAIR: DIR_OUTPUT,
    PORT_DIGITAL_OUT: DIR_OUTPUT,
    PORT_DIGITAL_IN: DIR_INPUT,
    PORT_CURRENT_SENSE: DIR_INPUT,
}

# The latch/select line each bus offers, named by CH347 *pad* (not the off-by-one
# ch347api name; see ch347_device for the pad -> library mapping). I2C addresses
# its chips instead of latching, so it has no lines. The hardware SPI peripheral
# has two chip-select lines; GPIO bit-banging can latch on any general-purpose pin.
SPI_SELECT_LINES: List[str] = ["cs0", "cs1"]
GPIO_LINES: List[str] = [f"gpio{i}" for i in range(8)]

# How long a switch coil stays energized on one throw, in seconds. A solenoid
# point motor is a momentary device: it throws on a short pulse and burns out if
# a coil is left driven, so every throw is a pulse and both coils rest low. This
# is the only place the width is set — the runtime driver and the commissioning
# pulse both read it, so a bench test energizes the coil for exactly as long as
# an operating session does.
SWITCH_THROW_PULSE_S = 0.01

# How long the bench port test drives one output line, in seconds. This is a
# different question from a throw: the port test asks "is this the wire I think
# it is", and the answer is read off a multimeter or a scope, so the line must
# stay high long enough to measure. A throw pulse is not — 10 ms reads as noise.
# Callers may override it per test (`run_port_test` / `pulse_output` take a
# width, and the REST endpoints pass one through), capped at
# MAX_PORT_TEST_PULSE_S so a stray request cannot ask for an open-ended hold.
#
# A hold this long on a bit wired to a solenoid coil is the thing
# SWITCH_THROW_PULSE_S exists to avoid, and the port grid will drive any bit,
# coil-bound included. That is deliberate — measuring a coil wire is the point —
# but it means the port test is a bench activity, not something to run against a
# layout with point motors energized.
PORT_TEST_PULSE_S = 1.0
MAX_PORT_TEST_PULSE_S = 5.0

# Longest any '595 output bit may stay high, in seconds, *without being
# refreshed*. Nothing in this design parks a shift-out bit: a coil bit is pulsed
# for SWITCH_THROW_PULSE_S, a driver standby bit drops with it, and a PCA9685 /OE
# bit is *held low*. So a bit still high after this long with nobody re-latching
# it is a fault — a caller that raised it and never lowered it, a crash between
# the two writes, an API client driving a raw bit — and `HC595Chain` forces it
# low rather than leave a solenoid energized. It is 100x the throw pulse, so it
# can never cut a legitimate throw short.
#
# A hold longer than this stays legitimate by re-latching inside the window (see
# `sleep_holding`), which is how a port test holds a line for a second without
# weakening this ceiling: the guard still fires within MAX_OUTPUT_HIGH_S of the
# moment the refreshes stop.
MAX_OUTPUT_HIGH_S = 1.0


def latch_lines_for(bus: str) -> List[str]:
    """Valid latch lines for a bus (empty for I2C, which addresses instead)."""
    if bus == BUS_SPI:
        return SPI_SELECT_LINES
    if bus == BUS_GPIO:
        return GPIO_LINES
    return []


@dataclass(frozen=True)
class ChipKind:
    """One selectable chip type."""

    kind: str
    label: str
    buses: Tuple[str, ...]       # buses this chip can be wired on; buses[0] = default
    role: str
    ports_per_unit: int          # ports one physical chip contributes
    cascadable: bool             # SPI/GPIO shift registers chain; the PCA9685 does not
    port_kind: str               # "bipolar_pair" | "digital_out" | "digital_in" | "current_sense"
    default_i2c_address: Optional[int] = None
    i2c_address_range: Optional[Tuple[int, int]] = None  # inclusive; None for non-I2C chips
    port_help: str = ""

    @property
    def direction(self) -> str:
        """"output" (driven) or "input" (read), derived from the port kind."""
        return _PORT_DIRECTION[self.port_kind]

    @property
    def lines_per_unit(self) -> int:
        """Individually-addressable hardware lines one physical chip exposes.

        Equal to `ports_per_unit` everywhere except a bipolar-pair PWM, whose
        port is a *pair* of PWM channels: one PCA9685 contributes 8 track ports
        but 16 channels. Both the port picker and the Hardware Setup port grid
        address a chip one line at a time, so both size themselves off this.
        See `lines_for_chip` for the chain-aware total."""
        return self.ports_per_unit * (2 if self.port_kind == PORT_BIPOLAR_PAIR else 1)

    @property
    def i2c_addresses(self) -> List[int]:
        """Every address pin strap this chip can be set to, low to high."""
        if self.i2c_address_range is None:
            return []
        lo, hi = self.i2c_address_range
        return list(range(lo, hi + 1))


CHIP_CATALOG: Dict[str, ChipKind] = {
    "pca9685": ChipKind(
        kind="pca9685",
        label="PCA9685 · 16-ch PWM",
        buses=(BUS_I2C,),
        role=ROLE_TRACK,
        # 16 PWM channels -> 8 bipolar track ports. Each port is an H-bridge
        # channel pair (2*port = IN1, 2*port+1 = IN2): magnitude -> duty,
        # sign -> which input drives, so track voltage stays bipolar (±12V).
        ports_per_unit=8,
        cascadable=False,
        port_kind=PORT_BIPOLAR_PAIR,
        default_i2c_address=0x40,
        # 6 address pins (A0-A5) give 64 straps, 0x40..0x7F.
        i2c_address_range=(0x40, 0x7F),
        port_help="H-bridge channel pair per port; magnitude→duty, sign→direction.",
    ),
    "hc595": ChipKind(
        kind="hc595",
        label="74HC595 · shift-out",
        buses=(BUS_SPI, BUS_GPIO),
        role=ROLE_SWITCH,
        ports_per_unit=8,
        cascadable=True,
        port_kind=PORT_DIGITAL_OUT,
        port_help=(
            "One output bit per port. A switch takes two — one coil per side — "
            "plus an optional bit for a driver standby/enable pin."
        ),
    ),
    "hc165": ChipKind(
        kind="hc165",
        label="74HC165 · shift-in",
        buses=(BUS_SPI, BUS_GPIO),
        role=ROLE_OCCUPANCY,
        ports_per_unit=8,
        cascadable=True,
        port_kind=PORT_DIGITAL_IN,
        port_help=(
            "One input bit per port. 1 = current present on the block, unless "
            "the chip is set active-low (a comparator with no inverter stage "
            "pulls its line low when current flows), which flips it."
        ),
    ),
    "ina3221": ChipKind(
        kind="ina3221",
        label="INA3221 · 3-ch current sense",
        buses=(BUS_I2C,),
        role=ROLE_OCCUPANCY,
        # 3 current-sense channels -> 3 occupancy ports (port 0 = CH1). One chip
        # covers up to three blocks; abs(current) above a per-channel threshold is
        # the *driving* occupancy signal in the hybrid detector. Not cascadable —
        # add more chips at 0x40..0x43 for more blocks.
        ports_per_unit=3,
        cascadable=False,
        port_kind=PORT_CURRENT_SENSE,
        default_i2c_address=0x40,
        # 1 address pin (A0: GND/VS/SDA/SCL) gives 4 straps, 0x40..0x43.
        i2c_address_range=(0x40, 0x43),
        port_help="One INA3221 channel per port (port 0 = CH1); abs(current) above threshold = occupied.",
    ),
    "ina219": ChipKind(
        kind="ina219",
        label="INA219 · 1-ch current sense",
        buses=(BUS_I2C,),
        role=ROLE_OCCUPANCY,
        # The single-channel current sensor: one chip covers one block. Same
        # driving-occupancy signal as the INA3221, so it fills the same binding
        # map; add more chips at 0x40..0x4f for more blocks.
        ports_per_unit=1,
        cascadable=False,
        port_kind=PORT_CURRENT_SENSE,
        default_i2c_address=0x40,
        # 2 address pins (A0, A1: GND/VS/SDA/SCL each) give 16 straps, 0x40..0x4F.
        i2c_address_range=(0x40, 0x4F),
        port_help="One current-sense channel; abs(current) above threshold = occupied.",
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


def lines_for_chip(kind: str, chain_length: int) -> int:
    """Number of individually-addressable hardware lines a configured chip
    exposes — the granularity the port picker and the track binding select at.

    This equals `ports_for_chip` for every chip except a bipolar-pair PWM
    (`pca9685`), whose *port* is a pair of PWM channels (an H-bridge's two
    inputs). The picker addresses those 16 channels one at a time so a block's
    two rails can be wired to any two of them, not only the fixed adjacent pair,
    so a bipolar-pair chip reports twice its port count."""
    ck = CHIP_CATALOG[kind]
    units = max(1, int(chain_length)) if ck.cascadable else 1
    return ck.lines_per_unit * units


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
                "buses": list(ck.buses),
                "role": ck.role,
                "direction": ck.direction,
                "ports_per_unit": ck.ports_per_unit,
                "lines_per_unit": ck.lines_per_unit,
                "cascadable": ck.cascadable,
                "port_kind": ck.port_kind,
                "default_i2c_address": ck.default_i2c_address,
                "i2c_addresses": ck.i2c_addresses,
                "port_help": ck.port_help,
            }
            for ck in CHIP_CATALOG.values()
        ],
        "spi_select_lines": SPI_SELECT_LINES,
        "gpio_lines": GPIO_LINES,
        "roles": {
            "track_output": ROLE_TRACK,
            "switch_output": ROLE_SWITCH,
            "occupancy_input": ROLE_OCCUPANCY,
        },
    }
