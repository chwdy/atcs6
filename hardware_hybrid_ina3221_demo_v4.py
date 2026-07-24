#!/usr/bin/env python3
"""Hybrid N-scale track occupancy demo: LM339 when idle, INA3221 when driving.

Idea
----
A single voltage-comparator detector is not reliable while the H-bridge is
actively driving the rails, and it is also polluted by flywheel/back-EMF while a
train is coasting. So this demo uses two different occupancy sources:

  * IDLE / no-output / skip-output:
      briefly open the PCA9685 /OE coast window, let the LM339 detector settle,
      latch 74HC165, and use the LM339 digital input as occupancy.

  * DRIVE / nonzero output:
      do NOT trust the LM339 rail-voltage state. Instead read INA3221 current on
      the 12 V supply feed into this block's DRV8871 VM pin. Current in either
      direction above threshold means the block is occupied. This also treats
      regeneration/back-EMF as occupancy because abs(current) is used.

Recommended INA3221 wiring for one block
---------------------------------------

    12V supply  -> INA3221 CH1 IN+
    INA3221 CH1 IN- -> DRV8871 VM / motor supply
    GND shared  -> INA3221 GND, DRV GND, logic GND
    SDA/SCL     -> CH347 I2C bus

Do NOT put INA3221 channel 1 in series with Rail A or Rail B. Those are H-bridge
nodes whose polarity/PWM/recovery paths change.

Recommended LM339 detector wiring for idle detection
----------------------------------------------------

Four current paths, one +5V rail:

    1)  Rail B --10k-- GND                       bleed, so an empty block sits
                                                 at a known level, not floating
    2)  5V --47k--|<|-- Rail A -> LM339+         sense
    3)  5V --10k-- LM339- --10k-- GND            fixed threshold at half of 5V
    4)  5V --10k-- LM339 OUT -> 74HC165 input    open-collector pullup

The LM339 output drives the HC165 input directly; there is no 74HC14 between
them, so bit polarity is set by the comparator alone.

For the current rig this demo defaults to HC165 port 3 only, active-low:
    no train -> HC165 bit 3 = 1 -> clear
    train    -> HC165 bit 3 = 0 -> occupied
Use --lm339-port and --hc165-active-high/--hc165-active-low to change this.

Run examples
------------

    conda run -n general --no-capture-output python hardware_hybrid_ina3221_demo.py

    # Tune the current threshold and print faster
    conda run -n general --no-capture-output python hardware_hybrid_ina3221_demo.py \
        --current-threshold-ma 15 --read-interval 0.25

    # Only test LM339 idle detector; no track drive
    conda run -n general --no-capture-output python hardware_hybrid_ina3221_demo.py \
        --skip-output --settle-ms 10

    # If the first seconds are guaranteed empty track, estimate idle current
    conda run -n general --no-capture-output python hardware_hybrid_ina3221_demo.py \
        --auto-baseline --baseline-samples 30
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from block_power import MAX_TRACK_VOLTAGE  # expected 12.0
from hardware.chips.hc165 import HC165Chain
from hardware.chips.hc595 import HC595Chain
from hardware.chips.pca9685 import PCA9685
from hardware.interfaces import TrackDriver


# ---- rig parameters --------------------------------------------------------
PCA9685_ADDR = 0x54
TRACK_CHIP_ID = "track0"
TRACK_BLOCK_ID = "B1"
TRACK_PORT = 0                  # port 0 -> PCA channels 0/1 -> DRV IN1/IN2

HC165_SELECT = "cs1"
HC165_CHAIN = 1

HC595_SELECT = "cs0"
HC595_CHAIN = 3
OE_595_PORT = 2                 # PCA9685 /OE via 74HC595 Q2, high=disable/coast

INA3221_ADDR = 0x40              # common INA3221 default; change with --ina-addr if needed

PEAK_VOLTAGE = MAX_TRACK_VOLTAGE
RAMP_STEP_V = 0.5
STEP_DELAY_S = 0.05
DEFAULT_SPEED_SCALE = 0.90      # 10% slower by default: command 90% of the old voltage
DEFAULT_ZERO_HOLD_S = 5.0       # hold at 0V for stopped-state LM339 occupancy detection
DEFAULT_READ_INTERVAL_S = 1.0
DEFAULT_SETTLE_MS = 10.0
DEFAULT_LM339_PORT = 3

# Flip to True only if your CH347 cannot interleave I2C/SPI reliably.
FORCE_REINIT_EACH_OP = False


class PCAOutputEnableController:
    """Own the 74HC595 bit that drives PCA9685 /OE.

    Hardware convention on this rig:
        74HC595 Q2 -> PCA9685 /OE
        /OE LOW  = PCA9685 outputs enabled, DRV inputs receive PWM
        /OE HIGH = PCA9685 outputs disabled, used only for LM339 idle-sense window

    The important part is that every disable operation is paired with enable() in
    a finally block. A previous version could leave /OE high if an HC165/SPI read
    failed or the process was interrupted inside the detection window, making the
    PCA9685 look like it had stopped outputting PWM.
    """

    def __init__(self, chain: HC595Chain, oe_port: int, *, debug: bool = False):
        self.chain = chain
        self.oe_port = oe_port
        self.debug = debug
        self.disabled = False

    def _write(self, disabled: bool) -> None:
        self.chain.set_output(self.oe_port, disabled)
        self.disabled = disabled
        if self.debug:
            print(
                f"    [OE] PCA9685 /OE={'HIGH disabled' if disabled else 'LOW enabled'} "
                f"via 74HC595 Q{self.oe_port}"
            )

    def enable(self) -> None:
        """Force PCA9685 output enabled: /OE LOW."""
        self._write(False)

    def disable(self) -> None:
        """Force PCA9685 output disabled: /OE HIGH."""
        self._write(True)

    def reset_chain_and_enable(self) -> None:
        """Set all 595 outputs low, then explicitly keep /OE low.

        This is useful at startup and shutdown. It also avoids the common failure
        mode where a stale HC595 state leaves Q2 high across reruns.
        """
        self.chain.all_off()
        self.disabled = False
        if self.debug:
            print(f"    [OE] 74HC595 all_off(); PCA9685 /OE=LOW enabled")


# ============================================================
# INA3221 —— 3-channel current / voltage monitor
# ============================================================
class INA3221:
    """Minimal INA3221 driver for CH347 I2C.

    The INA3221 does not have INA219-style current/power calibration registers.
    It reports shunt voltage directly. Current is computed as:

        current = shunt_voltage / shunt_ohms

    Register layout used here:
      ch1 shunt = 0x01, ch1 bus = 0x02
      ch2 shunt = 0x03, ch2 bus = 0x04
      ch3 shunt = 0x05, ch3 bus = 0x06

    Conversion:
      shunt register: signed 13-bit value in bits 15..3, LSB = 40 uV
      bus register:   13-bit value in bits 15..3, LSB = 8 mV
    """

    REG_CONFIG = 0x00
    REG_SHUNT_V = {1: 0x01, 2: 0x03, 3: 0x05}
    REG_BUS_V   = {1: 0x02, 2: 0x04, 3: 0x06}

    RESET = 0x8000
    CH_ENABLE = {1: 0x4000, 2: 0x2000, 3: 0x1000}

    # Config bits. 0x7127 is the common power-up style: all channels enabled,
    # 1.1 ms bus + 1.1 ms shunt conversion, continuous shunt+bus mode.
    AVG_1 = 0x0000
    AVG_4 = 0x0200
    AVG_16 = 0x0400
    VBUS_CT_1100US = 0x0100
    VSH_CT_1100US = 0x0020
    MODE_CONT_SHUNT_BUS = 0x0007

    SHUNT_LSB_V = 40e-6
    BUS_LSB_V = 8e-3

    def __init__(self, dev: Any, addr: int = 0x40, shunt_ohms: float = 0.1,
                 channel: int = 1, enable_all_channels: bool = False):
        if channel not in (1, 2, 3):
            raise ValueError("INA3221 channel must be 1, 2, or 3")
        if shunt_ohms <= 0:
            raise ValueError("shunt_ohms must be positive")
        self.dev = dev
        self.addr = addr
        self.shunt_ohms = shunt_ohms
        self.channel = channel
        self.enable_all_channels = enable_all_channels
        self._configure()

    def _write_reg16(self, reg: int, value: int) -> None:
        high = (value >> 8) & 0xFF
        low = value & 0xFF
        ok = self.dev.i2c_write(self.addr, bytes([reg, high, low]))
        if not ok:
            raise IOError(f"INA3221 write register 0x{reg:02X} failed: no ACK")
        time.sleep(0.00001)

    def _read_reg16(self, reg: int) -> int:
        # Same CH347-safe pattern as the previous INA219 code: write pointer with STOP, then read.
        ok = self.dev.i2c_write(self.addr, bytes([reg]))
        if not ok:
            raise IOError(f"INA3221 write register pointer 0x{reg:02X} failed: no ACK")
        status, raw = self.dev.i2c_read(self.addr, 2)
        if not status:
            raise IOError(f"INA3221 read register 0x{reg:02X} failed: no ACK")
        return int.from_bytes(raw, "big", signed=False)

    @staticmethod
    def _sign_extend(value: int, bits: int) -> int:
        sign = 1 << (bits - 1)
        return (value ^ sign) - sign

    def _configure(self) -> None:
        self._write_reg16(self.REG_CONFIG, self.RESET)
        time.sleep(0.01)

        if self.enable_all_channels:
            channel_bits = self.CH_ENABLE[1] | self.CH_ENABLE[2] | self.CH_ENABLE[3]
        else:
            channel_bits = self.CH_ENABLE[self.channel]

        config = (
            channel_bits
            | self.AVG_1
            | self.VBUS_CT_1100US
            | self.VSH_CT_1100US
            | self.MODE_CONT_SHUNT_BUS
        )
        self._write_reg16(self.REG_CONFIG, config)

    def read_bus_voltage(self, channel: Optional[int] = None) -> float:
        ch = self.channel if channel is None else channel
        raw = self._read_reg16(self.REG_BUS_V[ch])
        value = (raw >> 3) & 0x1FFF
        return value * self.BUS_LSB_V

    def read_shunt_voltage(self, channel: Optional[int] = None) -> float:
        """Return shunt voltage in mV, signed."""
        ch = self.channel if channel is None else channel
        raw = self._read_reg16(self.REG_SHUNT_V[ch])
        value = self._sign_extend((raw >> 3) & 0x1FFF, 13)
        return value * self.SHUNT_LSB_V * 1000.0

    def read_current(self, channel: Optional[int] = None) -> float:
        """Return current in A, signed. Sign depends on IN+ -> IN- wiring."""
        shunt_mv = self.read_shunt_voltage(channel)
        return (shunt_mv / 1000.0) / self.shunt_ohms

    def read_power(self, channel: Optional[int] = None) -> float:
        return self.read_bus_voltage(channel) * self.read_current(channel)


@dataclass
class InaSample:
    current_a: float
    bus_v: float
    shunt_mv: float
    occupied: bool


@dataclass
class Lm339Sample:
    raw: bytes
    active_ports: List[int]
    monitored_port: int
    monitored_level_high: bool
    occupied: bool


def build_triangle(peak: float, step: float) -> List[float]:
    steps = max(1, round(peak / step))
    unit = peak / steps
    ramp_up = [round(i * unit, 3) for i in range(0, steps + 1)]
    ramp_down = [round(i * unit, 3) for i in range(steps - 1, -steps - 1, -1)]
    ramp_back = [round(i * unit, 3) for i in range(-steps + 1, 0)]
    return ramp_up + ramp_down + ramp_back


def bit_is_active(data: bytes, port: int, active_low: bool) -> bool:
    bit = bool(data[port // 8] & (1 << (port % 8)))
    return (not bit) if active_low else bit


def active_ports(data: bytes, active_low: bool) -> List[int]:
    return [p for p in range(len(data) * 8) if bit_is_active(data, p, active_low)]


def raw_bit_high(data: bytes, port: int) -> bool:
    return bool(data[port // 8] & (1 << (port % 8)))


def format_hc165(data: bytes, active_low: bool) -> str:
    raw = " ".join(format(b, "08b") for b in data)
    active = active_ports(data, active_low)
    return f"[{raw}] active ports: {active if active else '—'}"


def read_lm339_window(oe_ctrl: PCAOutputEnableController, hc165: HC165Chain, settle_s: float,
                      active_low: bool, monitored_port: int) -> Lm339Sample:
    """Disable PCA outputs, wait for the idle LM339 detector, then read one HC165 input.

    Only the monitored LM339 port is allowed to decide occupancy. This matters when
    most HC165 inputs are floating or tied to default levels.

    /OE is ALWAYS restored low in finally, even if HC165 read fails. This prevents
    74HC595 Q2 from getting stuck high and silently disabling all PCA9685 outputs.
    """
    data: Optional[bytes] = None
    try:
        oe_ctrl.disable()      # PCA /OE high -> outputs disabled / DRV inputs coast
        time.sleep(settle_s)
        data = hc165.read_all()
    finally:
        oe_ctrl.enable()       # PCA /OE low -> restore PWM outputs no matter what

    assert data is not None
    ports = active_ports(data, active_low)
    level_high = raw_bit_high(data, monitored_port)
    occupied = bit_is_active(data, monitored_port, active_low)
    return Lm339Sample(
        raw=data,
        active_ports=ports,
        monitored_port=monitored_port,
        monitored_level_high=level_high,
        occupied=occupied,
    )


def read_ina_sample(ina: INA3221, baseline_a: float, threshold_a: float) -> InaSample:
    # Read shunt first, current second, bus last. Any order is OK; this gives useful diagnostics.
    shunt_mv = ina.read_shunt_voltage()
    current_a = ina.read_current()
    bus_v = ina.read_bus_voltage()
    occupied = abs(current_a - baseline_a) >= threshold_a
    return InaSample(current_a=current_a, bus_v=bus_v, shunt_mv=shunt_mv, occupied=occupied)


def estimate_current_baseline(ina: INA3221, samples: int, delay_s: float) -> float:
    values: List[float] = []
    for _ in range(max(1, samples)):
        values.append(ina.read_current())
        time.sleep(delay_s)
    return sum(values) / len(values)


def mode_for_voltage(voltage: float, idle_deadband_v: float, skip_output: bool) -> str:
    if skip_output or abs(voltage) <= idle_deadband_v:
        return "IDLE_LM339"
    return "DRIVE_INA3221"


def run_demo(
    track: Optional[TrackDriver],
    oe_ctrl: PCAOutputEnableController,
    hc165: HC165Chain,
    ina: Optional[INA3221],
    *,
    settle_s: float,
    skip_output: bool,
    read_interval_s: float,
    current_threshold_a: float,
    current_baseline_a: float,
    idle_deadband_v: float,
    hc165_active_low: bool,
    lm339_port: int,
    warn_low_bus_v: float,
    fixed_voltage: Optional[float],
    speed_scale: float,
    zero_hold_s: float,
) -> None:
    settle_ms = settle_s * 1000.0
    threshold_ma = current_threshold_a * 1000.0
    baseline_ma = current_baseline_a * 1000.0

    print(
        "Hybrid occupancy demo\n"
        f"  LM339/HC165 used only when idle/skip-output; settle={settle_ms:g} ms; "
        f"HC165 port={lm339_port}; active_low={hc165_active_low} "
        f"(no train=1, train=0 when active_low=True)\n"
        f"  INA3221 ch{ina.channel if ina is not None else '?'} used when driving; baseline={baseline_ma:+.2f} mA; "
        f"threshold=±{threshold_ma:.2f} mA; idle_deadband=±{idle_deadband_v:g} V\n"
        f"  Drive voltage scale={speed_scale:.2f}; zero-voltage hold={zero_hold_s:g}s for stopped-state occupancy detection\n"
        "  Ctrl-C to stop.\n"
    )

    if fixed_voltage is not None:
        period = [round(fixed_voltage * speed_scale, 3)]
    else:
        period = [round(v * speed_scale, 3) for v in build_triangle(PEAK_VOLTAGE, RAMP_STEP_V)]
    t0 = time.monotonic()
    last_read = 0.0
    step = 0
    voltage = 0.0

    while True:
        if not skip_output:
            voltage = period[step % len(period)]
            assert track is not None
            oe_ctrl.enable()  # make sure 74HC595 Q2 keeps PCA9685 /OE low before driving
            track.set_voltage(TRACK_BLOCK_ID, voltage)
        else:
            voltage = 0.0

        now = time.monotonic()
        if step == 0 or now - last_read >= read_interval_s:
            mode = mode_for_voltage(voltage, idle_deadband_v, skip_output)
            elapsed = now - t0

            if mode == "DRIVE_INA3221":
                if ina is None:
                    print(f"t+{elapsed:6.1f}s track={voltage:+6.2f} V DRIVE but INA3221 disabled/unavailable")
                else:
                    sample = read_ina_sample(ina, current_baseline_a, current_threshold_a)
                    state = "OCC" if sample.occupied else "clear"
                    bus_warn = ""
                    if abs(voltage) > idle_deadband_v and sample.bus_v < warn_low_bus_v:
                        bus_warn = "  [WARN: INA bus low; check VIN+/VIN- in 12V->DRV VM path]"
                    print(
                        f"t+{elapsed:6.1f}s track={voltage:+6.2f} V  DRIVE/INA3221  "
                        f"I={sample.current_a * 1000:+8.2f} mA  "
                        f"shunt={sample.shunt_mv:+7.3f} mV  bus={sample.bus_v:5.2f} V  => {state}{bus_warn}"
                    )
            else:
                # Only use LM339 as an idle detector. During active drive it is not trusted for clear/occupied.
                lm = read_lm339_window(oe_ctrl, hc165, settle_s, hc165_active_low, lm339_port)
                state = "OCC" if lm.occupied else "clear"
                level = "HIGH" if lm.monitored_level_high else "LOW"
                drive = "skip-output" if skip_output else f"track={voltage:+6.2f} V"
                print(
                    f"t+{elapsed:6.1f}s {drive:>15}  IDLE/LM339   "
                    f"port{lm.monitored_port}={level:<4}  raw={' '.join(format(b, '08b') for b in lm.raw)}  => {state}"
                )

            last_read = now

        # When the commanded voltage is exactly 0 V, intentionally hold the block
        # stopped for a few seconds and keep reading only the selected LM339/HC165
        # port. This gives the flywheel train time to stop before stopped-state
        # occupancy detection is used as the source of truth.
        if (not skip_output) and abs(voltage) <= idle_deadband_v and zero_hold_s > 0:
            hold_start = time.monotonic()
            while time.monotonic() - hold_start < zero_hold_s:
                now = time.monotonic()
                if now - last_read >= read_interval_s:
                    lm = read_lm339_window(oe_ctrl, hc165, settle_s, hc165_active_low, lm339_port)
                    state = "OCC" if lm.occupied else "clear"
                    level = "HIGH" if lm.monitored_level_high else "LOW"
                    elapsed = now - t0
                    print(
                        f"t+{elapsed:6.1f}s track={0.0:+6.2f} V  ZERO-HOLD/LM339  "
                        f"port{lm.monitored_port}={level:<4}  raw={' '.join(format(b, '08b') for b in lm.raw)}  => {state}"
                    )
                    last_read = now
                time.sleep(min(STEP_DELAY_S, 0.05))
        else:
            time.sleep(STEP_DELAY_S)
        step += 1


def parse_int_auto(s: str) -> int:
    return int(s, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-output", action="store_true",
                        help="do not drive track; only use LM339/HC165 idle detection")
    parser.add_argument("--settle-ms", type=float, default=DEFAULT_SETTLE_MS,
                        help="LM339 idle detector settle time before HC165 latch")
    parser.add_argument("--read-interval", type=float, default=DEFAULT_READ_INTERVAL_S,
                        help="seconds between printed occupancy reads")
    parser.add_argument("--speed-scale", type=float, default=DEFAULT_SPEED_SCALE,
                        help="multiply commanded track voltage by this factor; default 0.90 means 10%% slower")
    parser.add_argument("--zero-hold-s", type=float, default=DEFAULT_ZERO_HOLD_S,
                        help="seconds to hold at 0V for stopped-state LM339 occupancy detection")
    parser.add_argument("--fixed-voltage", type=float, default=None,
                        help="drive a fixed signed voltage instead of triangle ramp, e.g. 6 or -6")
    parser.add_argument("--idle-deadband-v", type=float, default=0.25,
                        help="abs(track voltage) at/below this is treated as idle and uses LM339")

    parser.add_argument("--ina-addr", type=parse_int_auto, default=INA3221_ADDR,
                        help="INA3221 I2C address, commonly 0x40; use i2c scan if unsure")
    parser.add_argument("--ina-channel", type=int, default=1, choices=[1, 2, 3],
                        help="INA3221 channel used for this block; default: 1")
    parser.add_argument("--ina-enable-all-channels", action="store_true",
                        help="enable all INA3221 channels instead of only --ina-channel")
    parser.add_argument("--disable-ina", action="store_true",
                        help="do not initialize/read INA3221")
    parser.add_argument("--shunt-ohms", type=float, default=0.1)
    parser.add_argument("--current-threshold-ma", type=float, default=15.0,
                        help="abs(current-baseline) above this means occupied while driving")
    parser.add_argument("--current-baseline-ma", type=float, default=0.0,
                        help="manual INA3221 baseline current in mA")
    parser.add_argument("--auto-baseline", action="store_true",
                        help="estimate current baseline at startup; use only with empty track")
    parser.add_argument("--baseline-samples", type=int, default=20)
    parser.add_argument("--baseline-delay-ms", type=float, default=20.0)

    level_group = parser.add_mutually_exclusive_group()
    level_group.add_argument("--hc165-active-low", dest="hc165_active_low", action="store_true",
                             help="treat 0 on the selected HC165 input as occupied")
    level_group.add_argument("--hc165-active-high", dest="hc165_active_low", action="store_false",
                             help="treat 1 on the selected HC165 input as occupied")
    parser.set_defaults(hc165_active_low=True)
    parser.add_argument("--lm339-port", type=int, default=DEFAULT_LM339_PORT,
                        help="HC165 input port connected to the LM339 output; default: 3")
    parser.add_argument("--warn-low-bus-v", type=float, default=6.0,
                        help="warn while driving if INA3221 bus voltage is below this value")
    parser.add_argument("--debug-oe", action="store_true",
                        help="print every 74HC595 Q2 / PCA9685 /OE enable/disable operation")
    args = parser.parse_args()

    settle_s = max(0.0, args.settle_ms) / 1000.0
    read_interval_s = max(0.01, args.read_interval)
    threshold_a = max(0.0, args.current_threshold_ma) / 1000.0
    baseline_a = args.current_baseline_ma / 1000.0
    if not (0 <= args.lm339_port < HC165_CHAIN * 8):
        raise SystemExit(f"--lm339-port must be 0..{HC165_CHAIN * 8 - 1}")

    import hardware.ch347_device as ch347_device
    from hardware.ch347_device import open_ch347_device

    if FORCE_REINIT_EACH_OP:
        ch347_device.REINIT_EACH_OP = True

    device = open_ch347_device()
    track: Optional[TrackDriver] = None

    try:
        oe_raw = HC595Chain(device, select=HC595_SELECT, chain_length=HC595_CHAIN)
        oe_ctrl = PCAOutputEnableController(oe_raw, OE_595_PORT, debug=args.debug_oe)
        oe_ctrl.reset_chain_and_enable()  # /OE low -> PCA outputs enabled

        hc165 = HC165Chain(device, select=HC165_SELECT, chain_length=HC165_CHAIN)

        ina: Optional[INA3221] = None
        if not args.disable_ina:
            ina = INA3221(
                device,
                addr=args.ina_addr,
                shunt_ohms=args.shunt_ohms,
                channel=args.ina_channel,
                enable_all_channels=args.ina_enable_all_channels,
            )

        if not args.skip_output:
            pca = PCA9685(device, address=PCA9685_ADDR)
            pca.configure()
            oe_ctrl.enable()  # guard against stale 595 /OE state before first PWM command
            track = TrackDriver(
                {TRACK_CHIP_ID: pca},
                {TRACK_BLOCK_ID: (TRACK_CHIP_ID, TRACK_PORT)},
            )
            if not track.set_voltage(TRACK_BLOCK_ID, 0.0):
                raise SystemExit(
                    f"could not drive {TRACK_BLOCK_ID!r} on chip {TRACK_CHIP_ID!r}; "
                    f"check PCA9685_ADDR=0x{PCA9685_ADDR:02x} and wiring"
                )

        if args.auto_baseline:
            if ina is None:
                raise SystemExit("--auto-baseline requires INA3221; remove --disable-ina")
            if track is not None:
                track.set_voltage(TRACK_BLOCK_ID, 0.0)
            print("Estimating INA3221 baseline. Use this only when the driven block is empty...")
            baseline_a = estimate_current_baseline(
                ina,
                samples=args.baseline_samples,
                delay_s=max(0.0, args.baseline_delay_ms) / 1000.0,
            )
            print(f"Estimated baseline: {baseline_a * 1000:+.2f} mA\n")

        run_demo(
            track,
            oe_ctrl,
            hc165,
            ina,
            settle_s=settle_s,
            skip_output=args.skip_output,
            read_interval_s=read_interval_s,
            current_threshold_a=threshold_a,
            current_baseline_a=baseline_a,
            idle_deadband_v=max(0.0, args.idle_deadband_v),
            hc165_active_low=args.hc165_active_low,
            lm339_port=args.lm339_port,
            warn_low_bus_v=max(0.0, args.warn_low_bus_v),
            fixed_voltage=args.fixed_voltage,
            speed_scale=max(0.0, args.speed_scale),
            zero_hold_s=max(0.0, args.zero_hold_s),
        )

    except KeyboardInterrupt:
        print("\nstopping — de-energizing track.")
    finally:
        if track is not None:
            track.enter_safe_state()
        try:
            oe_ctrl.reset_chain_and_enable()
        except Exception:
            pass
        device.close()


if __name__ == "__main__":
    main()
