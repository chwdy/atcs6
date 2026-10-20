"""Occupancy bench — a device-owning session that drives every block at one
fixed level and reports both occupancy detectors side by side.

Why this is its own session rather than a view onto the running one:

The occupancy question ("why does this block read occupied / not read at all?")
is hard to answer from a live runtime because the runtime is *part of the
question*. Occupancy and drive are a closed loop — a block's commanded voltage
picks which detector answers, and what the detector answers decides whether the
block keeps its voltage — and the runtime closes that loop twenty times a second
on top of the wiring being investigated. Concretely, with an environment active
and the simulation stopped, `SimulationController.poll_hardware_occupancy` still
runs every tick: it attributes confirmed entries (minting unidentified trains for
occupancy nobody owns) and then `_settle_after_commits` drives every block no
calculated span holds back to 0 V. A bench voltage set from a request would be
gone before the next read, and every detector that fired would spawn a train.

So the bench takes the controller instead of sharing it, exactly as commissioning
does: the caller deactivates the environment and hands over the device, and from
then on the only things touching the rig are this module's two operations —
*hold every bound block at one voltage* and *read both detectors*. No debounce,
no attribution, no de-energize sweep, no span. What the panel shows is the rig.

The two operations:

  - `drive()` holds every block with a rail binding at ±(percent% of full scale).
    Direction is per block: the caller's `flipped` set negates that block's sign.
    The write goes through the real `TrackDriver`, so the binding's own
    `reversed` flag still applies underneath — a bench flip is one more sign on
    top of the wiring, never an edit to it.
  - `read()` returns `OccupancyDriver.read_all_sources()`: the '165 bit AND the
    current channel for every bound block, plus the verdict the runtime's zone
    rule would take. Nothing is driven by a read.

`read()` also keeps a running PEAK of each channel's current since the last
reset. A single sample cannot answer the question the numbers are wanted for —
`baseline_ma` is "where does this channel sit with nothing on the block" and
`threshold_ma` is "how far above that counts", and both are decided by the
WORST reading a quiet channel produces, not by whatever the last poll caught.
An ADC that drifts a few mA shows its drift over seconds; a panel that displays
only the instant sample makes the operator eyeball a flickering number and
guess. The peak is that guess, measured.

PWM holds its duty in the chip's own registers, so one `drive()` call keeps the
rails energized until the next one — the panel polls `read()` without having to
re-command anything. Like the commissioning session this expires when a client
walks away (an abandoned browser tab must not leave a layout energized) and
de-energizes on close and at process exit.
"""

import atexit
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from block_power import MAX_TRACK_VOLTAGE

# Reclaim (and de-energize) a bench left open by an abandoned client after this
# long with no request. Longer than the commissioning session's window because a
# bench run is a watch-the-track activity: the operator is looking at the layout,
# not at the browser, and the panel polls anyway whenever it is open.
IDLE_TIMEOUT_S = 120.0

# Where the panel's drive control STARTS, in percent of full scale. Named here
# because it is a property of the bench, not of the UI: at 40% of 12 V the rails
# sit at 4.8 V, which is above `current_sense_min_drive_v` (4.0 V) and so puts
# every block in the `driven` zone — the regime where the current channel decides
# and the '165 is swamped. That is the regime a wiring check wants, and it is the
# wrong default to leave implicit, because a level chosen below the floor is
# silently testing the other detector instead.
#
# The operator can drive any level from here. Choosing one BELOW the floor is a
# supported measurement, not a mistake: the `lighting` band between the idle
# deadband and this floor is where a stopped-but-lit locomotive actually sits,
# and it is the band where both detectors are at their weakest — so it is
# precisely the one an operator calibrating `threshold_ma` or `lighting_voltage`
# needs to park in and read. The panel names the zone each level lands in so
# that choice is deliberate rather than discovered afterwards.
DEFAULT_DRIVE_PERCENT = 40.0


class OccupancyBenchSession:
    """One bench at a time, holding a controller open across requests."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._device: Optional[Any] = None
        # True when `_device` is the session's handle, borrowed in Setup.
        self._borrowed = False
        self._track: Optional[Any] = None
        self._occupancy: Optional[Any] = None
        self._block_ids: List[str] = []
        # Blocks with a rail binding — the ones `drive()` can actually command.
        # A detector-only block still gets a row; it just never energizes.
        self._track_bound: set = set()
        self._running = False
        self._percent = DEFAULT_DRIVE_PERCENT
        self._flipped: set = set()
        # One threshold applied to every current channel in place of the
        # per-block configured ones; None leaves each block on its own. The
        # bench is where an operator finds the number, `hardware_config` is
        # where it gets saved — this never writes there.
        self._threshold_ma: Optional[float] = None
        # The two drive levels that decide WHICH detector answers (see
        # `OccupancyDriver`), carried on the status so the panel can name the
        # zone a chosen percent lands in. Reported rather than recomputed
        # client-side: they are configuration, and a panel that hardcoded them
        # would keep labelling a level `driven` after the rig was retuned —
        # the one thing an operator choosing a level must not be told wrongly.
        # None until a config is opened, which is when they become knowable.
        self._idle_deadband_v: Optional[float] = None
        self._current_sense_min_drive_v: Optional[float] = None
        # Per block, the largest-magnitude current this channel has read since
        # the last reset, and how many reads carried a number at all. Keyed the
        # same as the block rows; a block with no current binding never gets an
        # entry, which is how "unwired" stays distinct from "reads 0.0".
        self._peaks: Dict[str, Dict[str, Any]] = {}
        self._last = 0.0
        # Failed zeroing writes from a close the idle timeout ran, kept until a
        # caller that can report them takes them (`take_close_failures`).
        self._unreported_close_failures: List[str] = []

    # -- lifecycle -------------------------------------------------------

    def open(
        self,
        cfg: Any,
        open_device: Optional[Callable[[], Any]] = None,
        track_polarity_of: Optional[Callable[[str], bool]] = None,
        device: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Open the controller for `cfg` and build the real track + occupancy
        interfaces on it.

        `device` borrows the session's already-open device: in Setup every tool
        reaches the rig through that one handle (design_new.md "Hardware
        setup", Setup tools own the writes; the detectors stay shared). A
        borrowed device is not closed when the bench closes; the safe state is
        still applied.

        Reopening an already-open bench is a no-op on the device but re-reads the
        configuration, so an operator who rebinds a channel in Hardware Setup and
        comes back gets the new binding without a restart.

        The watchdog is deliberately left unarmed. It exists to drop the rails
        when a *control loop* stops writing, and a bench has no control loop: the
        operator sets one level and then watches the track for as long as it takes
        to find the fault. `IDLE_TIMEOUT_S` is this session's version of the same
        protection, measured against the client rather than against a tick.
        """
        import hardware_config
        from hardware.interfaces import build_real_interfaces

        with self._lock:
            self._expire_locked()
            if self._device is None:
                if device is not None:
                    self._device = device
                    self._borrowed = True
                else:
                    opener = open_device or (lambda: self._open_real(cfg))
                    self._device = opener()
                    self._borrowed = False
            # Resolved through hardware_config's own binding helpers, not
            # hand-rolled here: they are what a real session is built from, and
            # the current-sense one carries the per-channel default fill and the
            # mA -> A conversion the interface compares against. A bench that
            # resolved its bindings differently from the runtime would be
            # answering a different question than the one being asked of it.
            occupancy, track, _switch = build_real_interfaces(
                self._device,
                cfg.chips,
                hardware_config._track_binding_tuples(cfg.block_track),
                hardware_config._binding_tuples(cfg.block_occupancy),
                {},                       # switch coils: a bench never throws points
                block_occupancy_current=hardware_config._current_binding_tuples(
                    cfg.block_occupancy_current
                ),
                idle_deadband_v=cfg.idle_deadband_v,
                current_sense_min_drive_v=cfg.current_sense_min_drive_v,
                track_polarity_of=track_polarity_of,
                watchdog_timeout_s=None,
            )
            self._track = track
            self._occupancy = occupancy
            self._idle_deadband_v = cfg.idle_deadband_v
            self._current_sense_min_drive_v = cfg.current_sense_min_drive_v
            self._track_bound = set(cfg.block_track)
            # Every block the bench can say anything about: one with rails to
            # drive, a detector to read, or both. A block bound on only one side
            # is exactly the kind of half-wired block this panel exists to find,
            # so it belongs on the list rather than being filtered off it.
            self._block_ids = sorted(
                self._track_bound | set(cfg.block_occupancy)
                | set(cfg.block_occupancy_current)
            )
            # Re-assert the drive. Reopening built a fresh TrackDriver and ran
            # `configure()` on the PWM chips, which resets every channel to zero
            # — so a bench that was running came back with dead rails while
            # still reporting itself as running, and every block fell back to
            # its idle-zone detector. The commanded state belongs to the session,
            # not to the driver object it was last written through, so it is
            # written out again here. A stopped bench writes zeros, which is the
            # right thing to reassert too.
            self._apply_drive_locked()
            # The threshold override lives on the driver object that was just
            # replaced, so it goes back on for the same reason.
            if self._occupancy is not None:
                self._occupancy.set_current_threshold_override(self._threshold_ma)
            # Peaks do NOT survive a reopen: they are readings of one wiring at
            # one drive level, and a reopen may have picked up a rebound channel.
            # Carrying a number across that would attribute one channel's peak
            # to another block's binding.
            self._reset_peaks_locked()
            self._touch()
            return self.status()

    def close(self) -> List[str]:
        """Close the bench, zeroing every channel of the chips it drives.
        Returns one message per chip whose zeroing failed, for the caller to
        report as a hardware I/O failure — including any a timed-out close left
        unreported."""
        with self._lock:
            return self._take_close_failures_locked() + self._close_locked()

    def is_holding_outputs(self) -> bool:
        """Whether the tool is open. Read without the lock: the runtime loop
        checks it on every iteration before deciding to take any lock."""
        return self._device is not None

    def close_if_idle(self) -> List[str]:
        """Close the tool when its idle timeout has passed, for the runtime loop
        (design_new.md "Power", Hardware setup, Closing a Setup tool restores
        the safe state: a timeout closes the tool on time, with no request
        needed). Never waits for the lock: a tool whose lock is held is in use,
        so it is not idle. Returns the failed zeroing writes of that close, plus
        any a request-noticed timeout left, for the caller to report."""
        if not self._lock.acquire(blocking=False):
            return []
        try:
            self._expire_locked()
            return self._take_close_failures_locked()
        finally:
            self._lock.release()

    def take_close_failures(self) -> List[str]:
        """The failed zeroing writes of a close the idle timeout ran, once.
        The caller reports them as a hardware I/O failure."""
        with self._lock:
            return self._take_close_failures_locked()

    def is_open(self) -> bool:
        with self._lock:
            self._expire_locked()
            return self._device is not None

    def status(self) -> Dict[str, Any]:
        with self._lock:
            self._expire_locked()
            return {
                "open": self._device is not None,
                "running": self._running,
                "percent": self._percent,
                "volts": self._drive_volts(),
                "flipped": sorted(self._flipped),
                "threshold_ma": self._threshold_ma,
                "block_ids": list(self._block_ids),
                "max_voltage": MAX_TRACK_VOLTAGE,
                "idle_deadband_v": self._idle_deadband_v,
                "current_sense_min_drive_v": self._current_sense_min_drive_v,
            }

    def set_current_threshold(self, threshold_ma: Optional[float]) -> Dict[str, Any]:
        """Judge every current channel against one threshold, or `None` to put
        each block back on its own configured value.

        Its own call rather than a `drive()` parameter because it changes what
        the detectors are judged AGAINST, not what the rails are doing: sweeping
        it must not require re-commanding the track, and re-commanding the track
        must not silently carry a threshold with it.

        Leaves the peaks alone, for that same reason: the threshold decides the
        verdict, never the measurement, so a sweep of it must not throw away the
        measurement history the operator is sweeping against.
        """
        with self._lock:
            self._require_open()
            self._threshold_ma = (
                None if threshold_ma is None else max(0.0, float(threshold_ma))
            )
            if self._occupancy is not None:
                self._occupancy.set_current_threshold_override(self._threshold_ma)
            self._touch()
            return self.status()

    def reset_peaks(self) -> Dict[str, Any]:
        """Start every channel's peak over from the next read.

        The peak answers "how high does this channel go under these conditions",
        so it is only meaningful within one set of conditions. `drive()` resets
        it on its own whenever it changes them; this is the manual version, for
        when what changed was on the layout rather than in the bench — a
        locomotive lifted off, a rail cleaned, a suspect block rewired.
        """
        with self._lock:
            self._require_open()
            self._reset_peaks_locked()
            self._touch()
            return self.status()

    # -- operations ------------------------------------------------------

    def drive(
        self,
        running: bool,
        percent: Optional[float] = None,
        flipped: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Command every rail-bound block, or drop them all to 0 V.

        `flipped` names the blocks whose sign is inverted; it replaces the stored
        set outright rather than merging, so the panel's per-block toggles are
        always the whole truth and a dropped request cannot leave a block flipped
        the operator can no longer see.

        Stopping still walks every block and writes 0 rather than calling the
        driver's safe state: `enter_safe_state` drops the chips wholesale, and
        the bench wants the blocks it commanded zeroed and its `_voltages`
        read-back kept coherent for the zone rule the next read applies.

        Any actual change to what the rails are doing clears the peaks. A peak
        is only readable as "the worst this channel does AT THIS DRIVE", and the
        leakage a stray-current baseline is being measured against scales with
        the applied voltage — so a peak carried from 40% into a 12% sweep would
        read as that channel's noise floor while actually being a memory of
        three times the voltage. Re-sending the same command is not a change and
        keeps the history, so a panel that re-asserts its state does not
        silently wipe a measurement in progress.
        """
        with self._lock:
            self._require_open()
            before = (self._running, self._percent, frozenset(self._flipped))
            if percent is not None:
                self._percent = max(0.0, min(100.0, float(percent)))
            if flipped is not None:
                self._flipped = {str(b) for b in flipped}
            self._running = bool(running)
            if (self._running, self._percent, frozenset(self._flipped)) != before:
                self._reset_peaks_locked()
            self._apply_drive_locked()
            self._touch()
            return self.status()

    def _apply_drive_locked(self) -> None:
        """Write the stored drive state out to every block. Caller holds the lock."""
        volts = self._drive_volts()
        for block_id in self._block_ids:
            signed = 0.0
            if self._running:
                signed = -volts if block_id in self._flipped else volts
            self._track.set_voltage(block_id, signed)

    def read(self) -> Dict[str, Any]:
        """Both detectors for every bound block, plus what is being commanded.

        `blocks` is keyed by block id; each entry carries `digital` (the '165
        bit), `current` (the sense channel), and `overall` (the verdict the
        runtime's zone rule takes from the two). A `None` on either detector means
        that block has no binding on that side.

        Each entry also carries `peak_ma` / `peak_samples` — that channel's
        largest-magnitude reading since the last reset and how many reads are
        behind it — and the payload carries a `peak` summary naming the worst
        channel of the whole layout. Every read feeds them, including reads taken
        with the drive stopped: the zero-drive noise floor is one of the numbers
        a threshold has to clear, and it is only visible with the rails quiet.
        """
        with self._lock:
            self._require_open()
            blocks: Dict[str, Any] = (
                self._occupancy.read_all_sources() if self._occupancy is not None else {}
            )
            self._observe_peaks_locked(blocks)
            for block_id in self._block_ids:
                entry = blocks.setdefault(
                    block_id, {"digital": None, "current": None, "overall": False,
                               "source": "none", "zone": "idle"}
                )
                entry["commanded_volts"] = (
                    round(self._track.get_actual_voltage(block_id), 2)
                    if self._track is not None
                    else 0.0
                )
                entry["flipped"] = block_id in self._flipped
                entry["track_bound"] = block_id in self._track_bound
                peak = self._peaks.get(block_id)
                entry["peak_ma"] = peak["peak_ma"] if peak else None
                entry["peak_samples"] = peak["samples"] if peak else 0
            self._touch()
            return {"blocks": blocks, "peak": self._peak_summary_locked(), **self.status()}

    def link_status(self) -> Optional[Dict[str, Any]]:
        """The USB bridge's own view of itself, or None for a device with no link
        to lose. Carried beside the block rows for the same reason the raw
        occupancy endpoint carries it: "every block went quiet at once" and "the
        bridge left the bus" are indistinguishable from the rows alone."""
        with self._lock:
            fn = getattr(self._device, "link_status", None)
            return fn() if callable(fn) else None

    # -- internals -------------------------------------------------------

    def _drive_volts(self) -> float:
        return round(MAX_TRACK_VOLTAGE * self._percent / 100.0, 3)

    def _reset_peaks_locked(self) -> None:
        self._peaks = {}

    def _observe_peaks_locked(self, blocks: Dict[str, Any]) -> None:
        """Fold one read's currents into the running peaks. Caller holds the lock.

        Compared on MAGNITUDE but stored SIGNED. A block driven in reverse reads
        its current negative, and a reversed channel peaking at -9 mA is the same
        finding as a forward one peaking at +9 mA — comparing raw values would
        report the reversed channel's quietest moment as its peak. Keeping the
        sign is what then tells the operator which of the two they are looking at
        rather than showing a magnitude that matches no reading on screen.
        """
        for block_id, entry in blocks.items():
            value = entry.get("current_ma")
            if value is None:
                continue
            try:
                ma = float(value)
            except (TypeError, ValueError):
                continue
            peak = self._peaks.setdefault(block_id, {"peak_ma": None, "samples": 0})
            peak["samples"] += 1
            if peak["peak_ma"] is None or abs(ma) > abs(peak["peak_ma"]):
                peak["peak_ma"] = ma

    def _peak_summary_locked(self) -> Dict[str, Any]:
        """The worst channel on the layout: `{block_id, peak_ma, samples}`.

        This is the number a single threshold has to clear. Every current
        channel on the rig is judged against the same `current_sense_min_drive_v`
        regime, and an operator setting one value for the layout needs the
        loudest quiet channel, not the average of them — a threshold set from the
        mean leaves the noisiest block reading occupied on an empty track, which
        is the fault being chased in the first place.

        `samples` is the total across all channels rather than the winner's own:
        it answers "has this been watched long enough to trust", and the read
        loop samples every channel together, so one count describes them all.
        """
        worst_id: Optional[str] = None
        worst: Optional[float] = None
        for block_id in sorted(self._peaks):
            ma = self._peaks[block_id]["peak_ma"]
            if ma is None:
                continue
            if worst is None or abs(ma) > abs(worst):
                worst, worst_id = ma, block_id
        return {
            "block_id": worst_id,
            "peak_ma": worst,
            "samples": sum(p["samples"] for p in self._peaks.values()),
        }

    @staticmethod
    def _open_real(cfg: Any) -> Any:
        from hardware.ch347_device import open_ch347_device

        return open_ch347_device(getattr(cfg.controller, "device_port", None))

    def _require_open(self) -> None:
        self._expire_locked()
        if self._device is None:
            raise RuntimeError("occupancy bench is not open")

    def _touch(self) -> None:
        self._last = time.monotonic()

    def _expire_locked(self) -> None:
        if self._device is not None and (time.monotonic() - self._last) > IDLE_TIMEOUT_S:
            # A timed-out close is a close like any other (design_new.md "Power",
            # Hardware setup, Closing a Setup tool restores the safe state): a
            # failed zeroing is a hardware I/O failure. The expiry runs inside
            # whatever call noticed it, which cannot report it, so the failures
            # wait for `take_close_failures()`.
            self._unreported_close_failures.extend(self._close_locked())

    def _take_close_failures_locked(self) -> List[str]:
        failures = self._unreported_close_failures
        self._unreported_close_failures = []
        return failures

    def _close_locked(self) -> List[str]:
        if self._device is None:
            return []
        # Closing a Setup tool restores the safe state (design_new.md "Power",
        # Hardware setup): the bench zeroes every channel of the chips it drives
        # itself, before letting go of the handle — not through `OutputSafety`,
        # and without advancing `safe_state_generation`, because the runtime's
        # power controllers do not own the outputs while the bench holds them.
        # Every chip is attempted; the failures go back to the caller.
        errors: List[str] = []
        try:
            if self._track is not None:
                errors = self._track.zero_every_channel()
        except Exception as exc:  # noqa: BLE001 - still let go of the handle
            errors = [str(exc)]
        if not self._borrowed:
            try:
                self._device.close()
            except Exception:  # noqa: BLE001 - same
                pass
        self._device = None
        self._borrowed = False
        self._track = None
        self._occupancy = None
        self._block_ids = []
        self._track_bound = set()
        self._running = False
        self._flipped = set()
        self._threshold_ma = None
        self._peaks = {}
        return errors


_session = OccupancyBenchSession()


def get_session() -> OccupancyBenchSession:
    return _session


atexit.register(_session.close)
