"""Speed calibration — measure a locomotive's voltage/speed response and write
it into a `SpeedProfile` as `voltage_speed_curve` (design_new.md "Speed
calibration").

A pass commands voltages and measures the speed that comes back at each. It
runs in three parts: coarse steps from `max_voltage` down by
`CALIBRATION_VOLTAGE_STEP_V` until a step fails; narrowing, which halves the gap
between the lowest step that passed and the step that failed below it until the
gap is no more than `CALIBRATION_MIN_VOLTAGE_GAP_V` — the lowest step that
passed is the starting voltage; and filling, which measures the voltage halfway
between two neighbouring passed steps whose speeds are far apart. A step passes
when every crossing in both directions finished; a failed step gives no point.
It must work this way round: `voltage_speed_curve` is the curve that turns a
wanted speed into a voltage, and it is the very thing the pass is building, so
the pass must not ask for a target speed while building it.

**Auto profiling** times the head across one operator-named measured block, B,
between two confirmed block entries — into B and into the block after it. Timing
between two entries of the same kind measures exactly one block whatever the
start position was, and the detector's fixed lag lands on both stamps and
cancels in the difference. B's two neighbours are A and C: the run-up at one end
and the stopping room at the other, and which is which flips each leg.

The pass turns the train around at each end itself — drive to 0, wait for the
canonical stopped predicate, re-frame the span to its other head end, re-command
the step — so a plain length of track can measure a locomotive with no loop to
lap. Each step runs `CALIBRATION_ROUND_TRIPS` round trips and takes the median
of twice that many crossings, half in each direction: a stretch is rarely dead
level, and equal counts each way cancel the grade, while one crossing a
detector timed wrong lands at an end of the order and does not move the sample.

Finishing the pass does not commit it. It holds its samples until the operator
picks a target, which is what `commit(...)` writes. `CalibrationRun` is
in-memory only; only the committed result persists, through ordinary
`SpeedProfile` persistence.
"""

from dataclasses import dataclass, field, replace
from statistics import median
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from speed_profile import curve_problems
from constants import (
    CALIBRATION_CAP_RETRY_LIMIT,
    CALIBRATION_HALF_POWER_S,
    CALIBRATION_MIN_CROSSING_SPEED_KMH,
    CALIBRATION_MOVE_VOLTAGE_V,
    CALIBRATION_FILL_SPEED_FRACTION,
    CALIBRATION_MIN_VOLTAGE_GAP_V,
    CALIBRATION_OFF_B_WAIT_READS,
    CALIBRATION_ROUND_TRIPS,
    CALIBRATION_RUNOFF_MARGIN_CM,
    CALIBRATION_SAME_SPEED_FRACTION,
    CALIBRATION_STEP_TIMEOUT_S,
    CALIBRATION_VOLTAGE_STEP_V,
    OCCUPANCY_OFF_THRESHOLD,
    OCCUPANCY_ON_THRESHOLD,
)
from model_scale import cm_per_sec_to_kmh, kmh_to_cm_per_sec

# A step's sample this far outside a neighbouring step's is treated as equal,
# so float noise in the crossing median cannot trip the non-decreasing rule.
_MONOTONIC_EPSILON_KMH = 1e-6
# Two step voltages this close are the same voltage.
_VOLTAGE_EPSILON = 1e-6

# Run phases. "leg", "clearing" and "stopping" are the three parts of one leg:
# the crossing, driving on until the train is off the measured block, and coming
# to a stand. The pass cycles through them until it is finished. "resetting"
# follows a failed step: the pass waits for the train to stand, then starts the
# next step from where it stands, moving it into an outer block first when it
# is not in one (design_new.md "Speed calibration", A step passes or fails).
PHASE_MOVING = "moving"
PHASE_RESETTING = "resetting"
PHASE_LEG = "leg"
PHASE_CLEARING = "clearing"
PHASE_STOPPING = "stopping"
PHASE_MEASURED = "measured"
PHASE_FAILED = "failed"


# Which part of the sweep a step belongs to (design_new.md "Speed
# calibration": The sweep runs down from `max_voltage` in
# `CALIBRATION_VOLTAGE_STEP_V` steps; The pass narrows in on the starting
# voltage; The pass adds a step between two steps whose speeds are far apart).
STEP_COARSE = "coarse"
STEP_NARROWING = "narrowing"
STEP_FILLED = "filled"


@dataclass(frozen=True)
class CalibrationSample:
    """One measured curve point: a step that passed, at the voltage it drove,
    with the median of its crossings (or the neighbouring step's speed it
    recorded)."""

    track_voltage: float
    speed_kmh: float
    kind: str = STEP_COARSE


# What happened to a crossing, for the panel's list (design_new.md "Speed
# calibration", The panel lists every crossing).
CROSSING_USED = "used"
CROSSING_DISCARDED = "discarded"
CROSSING_REPLACED = "replaced"
CROSSING_DROPPED = "dropped"
CROSSING_FAILED_STEP = "failed_step"


@dataclass(frozen=True)
class CalibrationCrossing:
    """One crossing of the measured block the pass ran, as the panel lists it.

    `seconds` and `speed_kmh` are `None` for a crossing that failed its step:
    it never reached the far outer block, so it has no time. `attempt` counts
    the times its step was measured (a step clearly outside a neighbouring step
    is measured once more, and so is a filled step that fails)."""

    track_voltage: float
    attempt: int
    toward_block_id: str
    seconds: Optional[float]
    speed_kmh: Optional[float]
    outcome: str


@dataclass(frozen=True)
class CalibrationObservation:
    """What the world looks like to an open pass on one tick.

    Built by `SpeedCalibration.plan_tick()` from committed runtime state and
    passed into `advance(...)`, which is the whole of the pass's decision logic.
    The confirmed entries are not here: Observe records what they mean on the
    `CalibrationRun` itself (`record_detected_entries(...)`). Keeping the two
    apart is what lets the state machine be driven directly by a test with
    synthetic confirmations instead of a moving simulation.
    """

    now: float
    """`AppState.operating_time`. Wall time would count an operator's stop as
    distance covered; `operating_time` advances only while the session runs."""

    is_stopped: bool = False
    """The canonical stopped predicate, `TrainController.is_stopped()`."""

    span_blocks: Tuple[str, ...] = ()
    """The blocks the train's calculated span covers."""

    listed_blocks: Tuple[str, ...] = ()
    """Of the blocks the pass asks about — the span's blocks and the move's
    blocks behind — those the train's block list holds (`Block.occupied_by`
    names the train). With `span_blocks` this is the train's place in both
    views of occupancy (design_new.md "Power", Two views of occupancy)."""

    head_block: str = ""
    """The block the train's head is in. Arming a leg reads it to orient from
    where the train actually stands. It is the head's block and not the whole
    span, because a consist longer than a block still holds the one behind it,
    and the block the tail is trailing through is not where the train is going."""

    path_present: bool = False
    """Whether the leg's authority (the controller's `received_path_plan`)
    currently exists. A leg arms by asking for a one-off route; the route's path
    is built by the ordinary later Plan steps, so for a tick or two after arming
    the authority is not there yet. Two things hang off this flag (design_new.md
    "Speed calibration", Plan acquires leg authority all-or-nothing): the step
    timeout does not start counting until the path is present, so a busy stretch
    delays the leg instead of failing it; and once a leg has had its authority
    and then loses it, the loss means the leg route completed through ordinary
    stop completion — the train came to rest at the far outer block."""

    held_short: bool = False
    """Whether the train stands at a reservation end short of the far outer
    block: it is stopped and the last block of its `received_path_plan` is not
    the far outer block — as when another train's claim on the walk holds the
    leg short. The step's power is not what holds it there, so the time it
    stands like this counts toward neither the wait for the measured block nor
    the crossing window (design_new.md "Speed calibration", Time the train
    stands short of its reservation is not counted)."""

    outer_block_index: Optional[int] = None
    """Which outer block (0 or 1) the train stands in, or `None` when it stands
    in neither. The head's block counts first, so a train straddling both
    reads as the one its head is in. The move into an outer block ends on it."""

    measured_block_held: bool = False
    """Whether the train is still on the measured block in either view of
    occupancy (design_new.md "Power", Two views of occupancy): its calculated
    span covers it, or its block list still holds it (`Block.occupied_by` names
    the train). The list lets go of a block only once its detector has read it
    clear, a moment after the span has moved off. The next leg starts only once
    this is false, so its entry into the measured block is a fresh one."""

    measured_block_listed: bool = False
    """Whether the train's block list still holds the measured block
    (`Block.occupied_by` names the train). It turns false on the read B's
    detector confirms clear, which is what the pass times the train's length
    from (design_new.md "Speed calibration", The pass measures the train's
    length)."""


@dataclass(frozen=True)
class CalibrationCommand:
    """What the pass wants this tick, decided by `advance(...)`.

    `drive_voltage` is the run state the controller reads back through
    `get_propulsion_override(...)` and drives with; the pass never sends it as a
    command. `reverse_cab` is applied through the ordinary stopped-only cab rule.
    `arm_leg_to` and `deactivate_route` are the two authority intents Plan step
    `c9e1` carries out on the pass's behalf (design_new.md "Speed calibration",
    Each leg runs as its own one-off route): `RouteExecutor` builds and assigns a
    one-off route for `arm_leg_to`, and tears the active leg route down for
    `deactivate_route`. A leg that reaches the far block carries neither — it
    finishes through ordinary stop completion. ATC, service-stop, and EB keep the
    same authority over the pass that they hold over any manual command.
    """

    drive_voltage: Optional[float] = None
    reverse_cab: bool = False
    deactivate_route: bool = False
    """Tear the active leg route down. Set on every leg-end that did NOT reach
    the far block — a step that does not move the train or a crossing that
    stops inside the stretch (both end the sweep), an overrun, or any pass
    abandonment. A leg that reaches the far block sets nothing here: it finishes
    through ordinary stop completion, which tears its route down for it."""
    move_into_place: bool = False
    """Build the one-off route that moves the train into the nearer outer
    block, before the opening leg (design_new.md "Speed calibration", The pass
    first moves the train into an outer block). `RouteExecutor` picks the
    block, because choosing the walk needs the dispatcher."""
    arm_leg_to: Optional[str] = None
    """The far outer block to build this leg's one-off route to, before the power
    command can move the train. A leg without a route has no authority, so the
    ATC limit is `0` (`no_reservation`) and the safety-cap ordering clamps the
    commanded percent to it."""


@dataclass(frozen=True)
class CalibrationPropulsionOverride:
    """The drive an open calibration pass puts on the train this tick, read by
    the controller during Action (design_new.md "Speed calibration", Controller
    owns calibration output).

    `track_voltage` is the voltage magnitude the pass drives. Its commanded
    speed is that voltage read forward through `voltage_speed_curve`, and it
    stays under the same ATC / service-stop / EB / pending-power-off /
    output-safe-state caps as any driving source. A drive of `0` is no drive:
    the train outputs what a powered train that nothing drives outputs.

    The controller reads this through `SpeedCalibration.get_propulsion_override`.
    It is never copied into `manual.manual_power_percent` or `handle_position`,
    and no operator command carries it.
    """

    track_voltage: float


@dataclass
class CalibrationRun:
    """Transient working record of one calibration pass (in-memory only)."""

    train_id: str
    profile_id: str
    max_voltage: float = 12.0
    """The profile's `max_voltage` when the pass opened: the first coarse step,
    and where the curve's last point must sit."""
    current_step_voltage: float = 0.0
    """The `track_voltage` the step being measured drives."""
    current_step_kind: str = STEP_COARSE
    """Whether the step being measured is a coarse, narrowing or filled step."""
    stage: str = STEP_COARSE
    """The part of the sweep the pass is in: coarse, narrowing, or filling."""
    failed_voltages: List[float] = field(default_factory=list)
    """Every coarse or narrowing step that failed (design_new.md "Speed
    calibration", A step passes or fails)."""
    step_failed_once: bool = False
    """Whether the filled step being measured has already failed once
    (design_new.md "Speed calibration", A filled step that fails is measured
    once more)."""

    # The stretch. `outer_block_ids` is (A, C); `heading_to` indexes the one the
    # train is currently running toward, so it names the stopping room this leg
    # and the run-up the next.
    measured_block_id: str = ""
    outer_block_ids: Tuple[str, str] = ("", "")
    block_length_cm: float = 0.0
    heading_to: int = 1
    train_length_cm: float = 0.0
    """The train's entered length (`Train.length_cm`) when the pass opened. The
    run-off uses it until the pass has measured the length."""
    measured_length_cm: Optional[float] = None
    """The shortest train length the pass has measured, or `None` before the
    first measurement (design_new.md "Speed calibration", The pass measures the
    train's length)."""

    phase: str = PHASE_LEG
    """`PHASE_MOVING` while the pass moves the train into an outer block before
    the opening leg, then `PHASE_LEG` / `PHASE_CLEARING` / `PHASE_STOPPING`
    through each leg, and `PHASE_MEASURED` or `PHASE_FAILED` once it ends."""
    move_target_block_id: str = ""
    """The outer block the move drives the train into, written by
    `RouteExecutor` when it builds the move's route; "" before then."""
    move_slowing: bool = False
    """Whether the move drives half `CALIBRATION_MOVE_VOLTAGE_V` because
    the head has reached the outer block (design_new.md "Speed calibration",
    The move stops on the detector)."""
    move_cut: bool = False
    """Whether the move's power is cut because the train has left the block
    behind and stands only in the outer block."""
    move_blocks_behind: Tuple[str, ...] = ()
    """The blocks the span covered besides the outer block when the head first
    reached it: the blocks the move waits to see clear before it cuts."""
    leg_armed: bool = False
    """Whether this leg's one-off route and power have been commanded. Every leg
    starts unarmed, so one code path arms them all — including the pass's
    first."""
    leg_authority_seen: bool = False
    """Whether this leg's authority has actually appeared yet (the controller's
    `received_path_plan` was seen present since the leg armed). Reset every time
    a leg arms. It tells "still acquiring the route" (never seen) apart from "the
    leg route completed and gave the authority back" (seen, then gone)."""
    needs_reverse: bool = False
    """Whether arming this leg must re-frame the span to its other head end
    first. False for the opening leg, where the train has not turned around."""
    samples: List[CalibrationSample] = field(default_factory=list)
    crossings: List[float] = field(default_factory=list)
    step_remeasured: bool = False
    crossing_log: List[CalibrationCrossing] = field(default_factory=list)
    """Every crossing the pass has run, in order, with what happened to it
    (design_new.md "Speed calibration", The panel lists every crossing)."""
    attempt: int = 1
    """How many times the current step has been measured: 2 once it is being
    measured again."""

    leg_started_at: Optional[float] = None
    """When this leg's step timeout started counting — the tick its authority
    first appeared, not the tick it armed. `None` until then, so the timeout does
    not run while acquisition is still in progress (design_new.md "Speed
    calibration", Plan acquires leg authority all-or-nothing)."""
    wait_counted_s: float = 0.0
    """Counted seconds this leg has waited for the measured block to confirm,
    measured against `CALIBRATION_STEP_TIMEOUT_S`. Time the train stands short
    of its reservation is left out (`CalibrationObservation.held_short`)."""
    crossing_counted_s: float = 0.0
    """Counted seconds since the measured block confirmed, measured against the
    crossing window (`crossing_window_s`), with the same time left out."""
    counted_until: Optional[float] = None
    """The operating time the two counters above have reached, or `None` until
    the leg's authority first appears."""
    half_power_until: Optional[float] = None
    """The operating time until which the pass drives half the step's power: set
    for `CALIBRATION_HALF_POWER_S` when a leg's authority first appears and
    when the run-off after a crossing ends (design_new.md "Speed calibration",
    The pass starts and stops the train at half power first). `None` when
    neither stage is running."""
    half_power: bool = False
    """Whether this tick drives half the step's power. `advance(...)` decides it
    each Plan pass from `half_power_until`, and `get_propulsion_override(...)`
    reads it in the same tick's Action."""
    crossing_started_at: Optional[float] = None
    """When the current crossing's clock started — the moment B was confirmed
    occupied for this leg — or `None` before it starts. Written in Observe by
    `record_detected_entries(...)`, read by Plan step `c9e1`; each new leg
    resets it (design_new.md "Speed calibration", `CalibrationRun`)."""
    crossing_finished_at: Optional[float] = None
    """When the current crossing's clock stopped — the moment the far outer
    block was confirmed occupied after B — or `None` until then. Written in
    Observe, read by Plan step `c9e1`, reset by each new leg."""
    runoff_until: Optional[float] = None
    """When the run-off after this leg's crossing ends: the far outer block's
    confirmation plus the time `runoff_length_cm()` takes at the crossing's
    speed (design_new.md "Speed calibration", The run-off is timed from the
    measured speed). `None` outside the run-off."""
    runoff_started_at: Optional[float] = None
    """When this leg's crossing ended — the far outer block's confirmation — or
    `None` once the length measurement for this leg is taken or given up."""
    runoff_speed_cm_s: float = 0.0
    """This leg's crossing speed, which the run-off and the length measurement
    both use."""
    runoff_reads: int = 0
    """Plan passes since the far outer block confirmed, one per detector read,
    counted until the train's block list lets go of the measured block."""
    standing_reads: int = 0
    """Plan passes the stopped train has waited for the measured block to read
    clear, against `CALIBRATION_OFF_B_WAIT_READS`."""
    overrun_seen: bool = False
    """Set in Observe when a block past the stretch was confirmed and credited
    to the calibration train on this leg, whether the train was still crossing
    or already stopping. Plan step `c9e1` fails the pass on it; each new leg
    resets it."""
    current_crossing_tainted: bool = False
    """Whether an ATC restriction (CAUTION / SPEED_LIMIT / the leg's own
    reservation end) lowered the calibration drive during the crossing now being
    timed. Set by `taint_current_crossing(...)`, reset when a fresh leg
    (crossing) arms. A tainted crossing is discarded rather than banked and the
    step is re-measured on the next crossing, so a transient restriction never
    fails the pass (design_new.md, "A capped crossing is discarded")."""
    step_cap_retries: int = 0
    """Consecutive discarded (capped) crossings of the current step. Reset to 0
    by a clean measured crossing and at each step advance. When it reaches
    `CALIBRATION_CAP_RETRY_LIMIT` the stretch is too short to measure this step
    at its power, so the pass fails and reports (design_new.md, "A capped
    crossing is discarded")."""

    failure_reason: str = ""
    active_leg_route_id: Optional[str] = None
    """The id of this leg's one-off route while it runs, from arming the leg to
    the route being torn down (by ordinary stop completion on success, or by a
    deactivate on any other leg-end). `None` between legs and once the pass ends.
    `RouteExecutor` stores it here at Plan step `c9e1`; the pass reads it to know
    a leg is armed and does not mutate route registries itself."""

    @property
    def crossings_target(self) -> int:
        """Twice the round-trip count: one crossing of B per leg."""
        return 2 * max(1, int(CALIBRATION_ROUND_TRIPS))

    @property
    def crossing_window_s(self) -> float:
        """The time the pass allows from the measured block's confirmation to
        the far outer block's: the time `block_length_cm` takes at
        `CALIBRATION_MIN_CROSSING_SPEED_KMH` (design_new.md "Speed calibration",
        The crossing window is set by B's length)."""
        slowest_cm_s = kmh_to_cm_per_sec(CALIBRATION_MIN_CROSSING_SPEED_KMH)
        if slowest_cm_s <= 0.0:
            return float("inf")
        return self.block_length_cm / slowest_cm_s

    def runoff_length_cm(self) -> float:
        """How far the run-off carries the train past the far outer block's
        confirmation: the train's length — the measured one once there is one,
        the entered one before — plus `CALIBRATION_RUNOFF_MARGIN_CM`
        (design_new.md "Speed calibration", The run-off is timed from the
        measured speed)."""
        length = (
            self.measured_length_cm
            if self.measured_length_cm is not None
            else self.train_length_cm
        )
        return max(0.0, length) + CALIBRATION_RUNOFF_MARGIN_CM

    @property
    def current_voltage(self) -> float:
        """The `track_voltage` this step drives."""
        return self.current_step_voltage

    @property
    def is_measuring(self) -> bool:
        return self.phase in (
            PHASE_MOVING,
            PHASE_RESETTING,
            PHASE_LEG,
            PHASE_CLEARING,
            PHASE_STOPPING,
        )

    @property
    def is_measured(self) -> bool:
        """The pass is finished and waits for the operator to pick a commit
        target. It writes nothing until they do."""
        return self.phase == PHASE_MEASURED

    @property
    def heading_toward_block_id(self) -> str:
        return self.outer_block_ids[self.heading_to]

    def to_dict(self) -> Dict[str, object]:
        """Progress payload for the calibration panel."""
        return {
            "train_id": self.train_id,
            "profile_id": self.profile_id,
            "phase": self.phase,
            "measured_block_id": self.measured_block_id,
            "outer_block_ids": list(self.outer_block_ids),
            "move_target_block_id": self.move_target_block_id,
            "stage": self.stage,
            "current_voltage": self.current_voltage,
            "current_step_kind": self.current_step_kind,
            "attempt": self.attempt,
            "max_voltage": self.max_voltage,
            "crossings_done": len(self.crossings),
            "crossings_target": self.crossings_target,
            "samples": [
                {
                    "track_voltage": s.track_voltage,
                    "speed_kmh": s.speed_kmh,
                    "kind": s.kind,
                }
                for s in sorted(self.samples, key=lambda s: s.track_voltage)
            ],
            "failed_voltages": sorted(self.failed_voltages),
            "crossings": [
                {
                    "track_voltage": c.track_voltage,
                    "attempt": c.attempt,
                    "toward_block_id": c.toward_block_id,
                    "seconds": c.seconds,
                    "speed_kmh": c.speed_kmh,
                    "outcome": c.outcome,
                }
                for c in self.crossing_log
            ],
            "failure_reason": self.failure_reason,
        }


def coarse_voltages(max_voltage: float) -> List[float]:
    """The coarse steps, highest first: `max_voltage`, then every
    `CALIBRATION_VOLTAGE_STEP_V` below it while the voltage stays above `0`
    (design_new.md "Speed calibration", The sweep runs down from `max_voltage`
    in `CALIBRATION_VOLTAGE_STEP_V` steps)."""
    step = float(CALIBRATION_VOLTAGE_STEP_V)
    top = float(max_voltage)
    voltages: List[float] = []
    k = 0
    while step > 0:
        v = round(top - k * step, 6)
        if v <= _VOLTAGE_EPSILON:
            break
        voltages.append(v)
        k += 1
    return voltages


class SpeedCalibration:
    """Runs calibration passes and commits the measured curves.

    Holds the `CalibrationRun` per train — an open pass, or a finished pass
    held for the operator to commit or discard. The opening conditions
    (design_new.md "Speed calibration", Opening a run takes control of the
    train) are checked by the caller — the service layer — before `start`.
    """

    def __init__(self) -> None:
        self.runs: Dict[str, CalibrationRun] = {}
        # Named dependencies, handed in by `wire_dependencies(...)`. Unwired
        # (the unit tests drive the state machine directly), there is no
        # controller to read, the operating clock reads 0, nothing is emitted,
        # and the environment is unknown.
        self._controllers = None
        self._operating_clock = None
        self._notification_center = None
        self._block_holder = None

    def wire_dependencies(
        self,
        *,
        controllers=None,
        operating_clock=None,
        notification_center=None,
        block_holder=None,
    ) -> None:
        """Inject the named dependencies calibration needs: the controller
        registry, the operating clock every interval is measured against
        (design_new.md "Clocks"), the event-log sink, a call returning the
        active operating environment, and a call naming the train a block's
        `Block.occupied_by` holds. Called once by SimulationController at
        construction; calibration holds no SimulationController reference of
        its own."""
        self._controllers = controllers
        self._operating_clock = operating_clock
        self._notification_center = notification_center
        self._block_holder = block_holder

    # -- lifecycle --------------------------------------------------------

    def start(
        self,
        train,
        profile,
        *,
        measured_block_id: str = "",
        outer_block_ids: Sequence[str] = (),
        block_length_cm: float = 0.0,
        heading_to: Optional[int] = 1,
        now: float = 0.0,
    ) -> CalibrationRun:
        """Open a pass; its first step is the coarse step at `max_voltage`.

        The pass needs the measured block, its two resolved neighbours, and its
        length. The caller resolves the stretch, because walking the track
        through the points as they are aligned needs the network, and it passes
        `heading_to` as the index of the outer block the train is running
        *toward* — the one it is not already standing in. `heading_to=None`
        means the train stands in neither outer block, or is on the measured
        block, so the pass first moves it into the nearer one (design_new.md
        "Speed calibration", The pass first moves the train into an outer
        block).
        """
        outer = tuple(outer_block_ids)[:2]
        if len(outer) < 2:
            outer = (outer + ("", ""))[:2]
        max_voltage = float(getattr(profile, "max_voltage", 12.0) or 12.0)
        run = CalibrationRun(
            train_id=train.train_id,
            profile_id=profile.profile_id,
            max_voltage=max_voltage,
            current_step_voltage=max_voltage,
            current_step_kind=STEP_COARSE,
            stage=STEP_COARSE,
            measured_block_id=measured_block_id,
            outer_block_ids=(outer[0], outer[1]),
            block_length_cm=float(block_length_cm or 0.0),
            heading_to=1 if heading_to else 0,
            train_length_cm=float(getattr(train, "length_cm", 0.0) or 0.0),
            phase=PHASE_MOVING if heading_to is None else PHASE_LEG,
        )
        self.runs[train.train_id] = run
        return run

    def abandon(self, train_id: str, reason: str = "cancelled") -> None:
        """End a speed pass without writing, and report the reason. The samples
        go with it (design_new.md "Speed calibration", `abandon`).

        The one teardown for a speed pass: operator Stop, a panel close, Action
        step `26f5` finding the pass no longer valid, the safe-state wrapper, a
        network clear or layout load, and deleting the train all end it here.
        Each commands a drive of `0` and tears the active leg route down.
        Dropping the run withdraws the propulsion override, so the controller
        falls back to its ordinary manual source; zeroing that source here is
        what brings the train to a stand. The route deactivate's inclusive latch
        cancels any still-live leg path, and the Action consumer removes the
        RouteRun. An existing teardown is not duplicated (see
        `_deactivate_leg_route`).
        """
        run = self.runs.pop(train_id, None)
        if run is not None:
            self._stop_manual_traction(train_id)
            self._deactivate_leg_route(train_id, run)
            self._emit_command_result(
                train_id,
                operation="speed_calibration",
                success=False,
                reason=reason,
            )

    def _deactivate_leg_route(self, train_id: str, run: CalibrationRun) -> None:
        """Tear the active calibration leg route down through the ordinary
        route-deactivate latch (design_new.md "Speed calibration", Pass-
        abandonment cases; Leg completion and abandonment use ordinary route
        flows). The Action consumer cancels any live leg path and removes the
        RouteRun through the standard teardown order.

        A no-op when no leg route is armed, when the runtime/controller is absent
        (unit tests drive the state machine with no controller wired), or when a
        teardown is already pending — an existing teardown is not duplicated
        (design_new.md "Speed calibration", An existing teardown is not
        duplicated).
        """
        if run.active_leg_route_id is None:
            return
        controller = self._controller(train_id)
        if controller is None:
            return
        latches = controller.route_hooks.phase_latches
        if latches.route_deactivate:
            return
        latches.set_route_deactivate(
            reason="calibration pass abandoned",
            has_active_path=controller.path_projection.received_path_plan is not None,
        )

    def fail(self, run: CalibrationRun, reason: str) -> CalibrationCommand:
        """End a pass on a measurement fault, command a drive of `0`, and
        report the reason.

        The profile's previous curve is left in place — a pass that does not
        measure every step writes nothing. The run is removed from `self.runs`
        so a failed pass never lingers: the caller's next Plan step no longer
        finds it, and the opening check that refuses a new run while one is open
        or held (`services.train_service.calibration_readiness`) is not blocked
        by a spent, failed one. The reason has already gone out on the
        command-result stream, so the panel still learns why the pass ended. The
        `run` object the caller holds keeps its `PHASE_FAILED` phase and
        `failure_reason` for that caller's own inspection.
        """
        run.phase = PHASE_FAILED
        run.failure_reason = reason
        # A failed pass no longer drives, so the override withdraws; zero the
        # ordinary manual source the controller falls back to so the train
        # coasts to a stand instead of resuming a stale percent.
        self._stop_manual_traction(run.train_id)
        self._emit_command_result(
            run.train_id, operation="speed_calibration", success=False, reason=reason
        )
        self.runs.pop(run.train_id, None)
        return CalibrationCommand(drive_voltage=0.0, deactivate_route=True)

    def taint_current_crossing(self, train_id: str, reason: str) -> bool:
        """Mark the crossing now being timed as capped, so it is discarded
        instead of banked (design_new.md, "A capped crossing is discarded").

        Called by the controller during Action when the Propulsion cap step
        lowered the calibration drive on this tick through an external ATC
        restriction (a CAUTION block ahead, or a posted block SPEED_LIMIT). The
        speed the pass would time is then not the speed the step commanded, so
        the crossing is not a valid sample. Discarding never fails the pass: the
        step is re-measured on the next crossing. The previous curve stays in
        place. Idempotent within a crossing; a no-op returning `False` for a pass
        that is finished, already failed, or not an auto measuring pass. EB and
        power-off are not routed here — they end the whole pass through
        `run_still_valid(...)` at Action step `26f5`.
        """
        run = self.runs.get(train_id)
        if run is None or not run.is_measuring:
            return False
        # Only a cap while the clock runs touches the speed being timed. A cap in
        # the run-up — such as the tick a leg starts with its authority still
        # ending at the measured block — is not part of any crossing.
        if run.crossing_started_at is None or run.crossing_finished_at is not None:
            return False
        run.current_crossing_tainted = True
        return True

    # -- the pass ---------------------------------------------------------

    def advance(
        self, run: CalibrationRun, obs: CalibrationObservation
    ) -> Optional[CalibrationCommand]:
        """Advance one auto-profiling pass by one observation.

        Returns the command to issue this tick, or None when the pass wants
        nothing issued. This is the whole of the pass's decision logic.
        """
        if not run.is_measuring:
            return None

        if run.phase == PHASE_MOVING:
            command = self._advance_move(run, obs)
        elif run.phase == PHASE_RESETTING:
            command = self._advance_resetting(run, obs)
        elif run.phase == PHASE_LEG:
            command = self._advance_leg(run, obs)
        elif run.phase == PHASE_CLEARING:
            self._measure_length(run, obs)
            command = self._advance_clearing(run, obs)
        else:
            self._measure_length(run, obs)
            command = self._advance_stopping(run, obs)
        run.half_power = (
            run.half_power_until is not None and obs.now < run.half_power_until
        )
        return command

    def _advance_move(
        self, run: CalibrationRun, obs: CalibrationObservation
    ) -> Optional[CalibrationCommand]:
        """Move the train into the nearer outer block, then hand over to the
        opening leg (design_new.md "Speed calibration", The pass first moves
        the train into an outer block).

        The move's route finishes through ordinary stop completion like a
        leg's, so its path having appeared and then gone means it ended. The
        pass then checks where the train stopped.
        """
        in_place = obs.outer_block_index is not None and not obs.measured_block_held
        target = run.move_target_block_id or "an outer block"

        if run.leg_authority_seen and not obs.path_present:
            if not obs.is_stopped:
                return None
            if not in_place:
                return self.fail(
                    run,
                    f"the move into {target} ended with the train outside "
                    f"{run.outer_block_ids[0]} and {run.outer_block_ids[1]}, "
                    f"or still on {run.measured_block_id}",
                )
            # The opening leg runs toward the outer block the train is not in.
            # Path creation picks the span end to start from, so the train may
            # face either way (as when the pass opens with it already in place).
            run.phase = PHASE_LEG
            run.heading_to = 1 - obs.outer_block_index
            run.needs_reverse = False
            run.leg_armed = False
            return self._advance_leg(run, obs)

        if not run.leg_armed:
            run.leg_armed = True
            run.leg_authority_seen = False
            run.leg_started_at = None
            run.half_power_until = None
            run.move_slowing = False
            run.move_cut = False
            run.move_blocks_behind = ()
            return CalibrationCommand(
                drive_voltage=CALIBRATION_MOVE_VOLTAGE_V, move_into_place=True
            )

        if obs.path_present:
            if not run.leg_authority_seen:
                run.leg_authority_seen = True
                run.leg_started_at = obs.now
                run.half_power_until = obs.now + CALIBRATION_HALF_POWER_S
        # The detector, not the calculated position, stops the move: half power
        # once the head is in the outer block, nothing once the block behind
        # has confirmed clear (design_new.md "Speed calibration", The move
        # stops on the detector).
        target_block = run.move_target_block_id
        if run.leg_authority_seen and target_block:
            if not run.move_slowing and obs.head_block == target_block:
                run.move_slowing = True
                run.move_blocks_behind = tuple(
                    block_id for block_id in obs.span_blocks if block_id != target_block
                )
            behind = set(obs.span_blocks) | set(obs.listed_blocks)
            if run.move_slowing and behind <= {target_block}:
                run.move_cut = True
        if (
            run.leg_started_at is not None
            and not in_place
            and obs.now - run.leg_started_at > CALIBRATION_STEP_TIMEOUT_S
        ):
            return self.fail(
                run,
                f"the train did not reach {target} within "
                f"{CALIBRATION_STEP_TIMEOUT_S:.0f} s at "
                f"{CALIBRATION_MOVE_VOLTAGE_V:g} V",
            )
        return None

    def _advance_resetting(
        self, run: CalibrationRun, obs: CalibrationObservation
    ) -> Optional[CalibrationCommand]:
        """Start the next step after a failed one (design_new.md "Speed
        calibration", A step passes or fails).

        The failed step's leg route is being torn down. Once it is gone and the
        train stands, the next step's first leg starts from where the train is:
        toward the outer block it is not in, when it stands in one and is off
        B; otherwise the pass first moves it into the nearer outer block, as it
        does when a pass opens.
        """
        if obs.path_present or not obs.is_stopped:
            return None
        in_place = obs.outer_block_index is not None and not obs.measured_block_held
        run.leg_armed = False
        run.leg_authority_seen = False
        if in_place:
            run.phase = PHASE_LEG
            run.heading_to = 1 - obs.outer_block_index
            run.needs_reverse = False
            return self._advance_leg(run, obs)
        run.phase = PHASE_MOVING
        run.move_target_block_id = ""
        return self._advance_move(run, obs)

    def _advance_leg(
        self, run: CalibrationRun, obs: CalibrationObservation
    ) -> Optional[CalibrationCommand]:
        far_block = run.heading_toward_block_id

        # An overrun Observe recorded on this leg fails the pass — including one
        # the detector confirmed only after the train stopped, before the next
        # leg arms and resets the record. The locomotive needed more stopping
        # room than the stretch has. Nothing on the rails prevents this: the
        # block ahead of a train's head is energized as its pending-entry block
        # whichever block that turns out to be.
        if run.overrun_seen:
            return self._fail_overrun(run)

        if not run.leg_armed:
            # Every leg is armed here, the opening one included: ask for a one-off
            # route to the far end, turn around if the last leg left the train
            # facing the other way, and command this step's power.
            #
            # Orient from where the train actually stands. A leg runs toward the
            # outer block it is *not* in, and a route to the block the head
            # already occupies is refused outright — so whenever the train is
            # sitting in the block this leg was aiming at, the aim is what is
            # stale, not the position.
            if obs.head_block and far_block == obs.head_block:
                run.heading_to = 1 - run.heading_to
                run.needs_reverse = True
                far_block = run.heading_toward_block_id
            run.leg_armed = True
            run.leg_authority_seen = False
            run.leg_started_at = None
            run.wait_counted_s = 0.0
            run.crossing_counted_s = 0.0
            run.counted_until = None
            run.half_power_until = None
            run.crossing_started_at = None
            run.crossing_finished_at = None
            run.runoff_until = None
            run.runoff_started_at = None
            run.runoff_reads = 0
            run.standing_reads = 0
            run.overrun_seen = False
            # A fresh crossing begins clean: any taint from the last one is spent
            # (design_new.md, "A capped crossing is discarded").
            run.current_crossing_tainted = False
            # The head-frame reversal is one-shot per arm: the Plan translation
            # re-frames the span on each `reverse_cab=True`, so clear the flag
            # once emitted.
            # A refused-then-retried arm re-enters this branch (reconcile sets
            # leg_armed=False while active_leg_route_id is None); without the
            # clear the retry re-toggles, and an even number of toggles leaves a
            # turn-around leg facing the wrong way.
            reverse_cab = run.needs_reverse
            run.needs_reverse = False
            return CalibrationCommand(
                drive_voltage=run.current_voltage,
                reverse_cab=reverse_cab,
                arm_leg_to=far_block,
            )

        # Nothing about this leg counts until its authority actually appears. The
        # step timeout starts the tick the path first shows up, so a stretch that
        # is busy when the leg arms delays it instead of failing it (design_new.md
        # "Speed calibration", Plan acquires leg authority all-or-nothing).
        if obs.path_present:
            run.leg_authority_seen = True
            if run.leg_started_at is None:
                run.leg_started_at = obs.now
                run.counted_until = obs.now
                # The leg starts at half the step's voltage (design_new.md
                # "Speed calibration", The pass starts and stops the train at
                # half voltage first).
                run.half_power_until = obs.now + CALIBRATION_HALF_POWER_S
        self._count_leg_time(run, obs)

        if run.crossing_finished_at is not None and run.crossing_started_at is not None:
            elapsed = run.crossing_finished_at - run.crossing_started_at
            run.crossing_started_at = None
            run.crossing_finished_at = None
            speed = (
                cm_per_sec_to_kmh(run.block_length_cm / elapsed)
                if elapsed > 0.0 and run.block_length_cm > 0.0
                else None
            )
            if run.current_crossing_tainted:
                # An ATC restriction lowered the calibration drive during this
                # crossing, so the timed speed is not the speed the step
                # commanded. Discard the sample and re-measure on the next
                # crossing (the step still needs `crossings_target` of them). The
                # leg still ends through ordinary stop completion at the far
                # block. Consecutive discards of one step count against
                # CALIBRATION_CAP_RETRY_LIMIT: when a stretch caps every crossing
                # — too short for this step even at the zero reservation-end
                # standoff — the pass fails and reports rather than shuttling
                # forever (design_new.md, "A capped crossing is discarded").
                run.current_crossing_tainted = False
                self._log_crossing(run, elapsed, speed, CROSSING_DISCARDED)
                run.step_cap_retries += 1
                if run.step_cap_retries >= CALIBRATION_CAP_RETRY_LIMIT:
                    return self.fail(
                        run,
                        f"stretch too short to measure {run.current_voltage:g} V "
                        f"at block {run.measured_block_id}",
                    )
                return self._leave_measured_block(run, obs, speed)
            if elapsed <= 0.0 or run.block_length_cm <= 0.0:
                return self.fail(run, "measured block crossing timed at zero")
            # A clean crossing measured: the cap-retry streak resets.
            run.step_cap_retries = 0
            run.crossings.append(speed)
            self._log_crossing(run, elapsed, speed, CROSSING_USED)
            # The leg reached the far block, so it finishes through ordinary stop
            # completion (Plan settlement step `9d8b` completes the leg route). The pass
            # commands a stop once the train is off B and issues no route op of
            # its own — success carries no teardown (design_new.md "Speed
            # calibration", A leg finishes through ordinary stop completion).
            return self._leave_measured_block(run, obs, speed)

        # The leg had its authority and then lost it without the far block ever
        # confirming: ordinary stop completion tore the leg route down because
        # the train came to rest at its authority end short of the far block.
        # That is the same outcome as the step timing out, so resolve it the
        # same way.
        if run.leg_authority_seen and not obs.path_present:
            return self._resolve_incomplete_leg(run, obs)

        # Both limits run only once the leg's authority has appeared. The run-up
        # has `CALIBRATION_STEP_TIMEOUT_S` to reach B, and the crossing has its
        # own window from B's confirmation, sized by B's length, so a long
        # run-up never eats into the crossing and a slow crawl across B is
        # timed rather than taken for a stall.
        if run.crossing_started_at is None:
            if run.wait_counted_s < CALIBRATION_STEP_TIMEOUT_S:
                return None
        elif run.crossing_counted_s < run.crossing_window_s:
            return None
        return self._resolve_incomplete_leg(run, obs)

    @staticmethod
    def _count_leg_time(run: CalibrationRun, obs: CalibrationObservation) -> None:
        """Add the time since the last Plan pass to the leg's counted wait for
        B, or to its crossing once B has confirmed — unless the train stood
        short of its reservation (design_new.md "Speed calibration", Time the
        train stands short of its reservation is not counted)."""
        last = run.counted_until
        if last is None:
            return
        run.counted_until = obs.now
        if obs.held_short:
            return
        if run.crossing_started_at is None:
            run.wait_counted_s += max(0.0, obs.now - last)
        else:
            run.crossing_counted_s += max(
                0.0, obs.now - max(last, run.crossing_started_at)
            )

    def _leave_measured_block(
        self,
        run: CalibrationRun,
        obs: CalibrationObservation,
        crossing_kmh: Optional[float],
    ) -> Optional[CalibrationCommand]:
        """End the crossing: drive on at the step's power for the run-off time,
        then stop (design_new.md "Speed calibration", The run-off is timed from
        the measured speed; The pass turns the train around at each end).

        The run-off is not ended by B reading clear. Below
        `current_sense_min_drive_v` B holds its presence for
        `LIGHTING_CLEAR_HOLD_READS` clear reads before its clear count even
        starts — several seconds, in which the train would run off the stopping
        room. The crossing just timed gives the train's speed at this power, so
        the time its own length takes is known without the detector. A B that
        already reads clear in both views ends the run-off early."""
        speed_cm_s = kmh_to_cm_per_sec(crossing_kmh) if crossing_kmh else 0.0
        run.runoff_speed_cm_s = speed_cm_s
        run.runoff_started_at = obs.now
        run.runoff_reads = 0
        run.standing_reads = 0
        run.runoff_until = (
            obs.now + run.runoff_length_cm() / speed_cm_s if speed_cm_s > 0.0 else obs.now
        )
        run.half_power_until = None
        if obs.measured_block_held and obs.now < run.runoff_until:
            run.phase = PHASE_CLEARING
            return None
        return self._begin_stop(run, obs)

    @staticmethod
    def _begin_stop(run: CalibrationRun, obs: CalibrationObservation) -> CalibrationCommand:
        """Stop the train at the end of the run-off: half the step's power for
        `CALIBRATION_HALF_POWER_S`, then `0` (design_new.md "Speed calibration",
        The pass starts and stops the train at half power first)."""
        run.phase = PHASE_STOPPING
        run.runoff_until = None
        run.half_power_until = obs.now + CALIBRATION_HALF_POWER_S
        return CalibrationCommand(drive_voltage=0.0)

    def _advance_clearing(
        self, run: CalibrationRun, obs: CalibrationObservation
    ) -> Optional[CalibrationCommand]:
        if run.overrun_seen:
            return self._fail_overrun(run)
        until = run.runoff_until
        if not obs.measured_block_held or until is None or obs.now >= until:
            return self._begin_stop(run, obs)
        return None

    @staticmethod
    def _measure_length(run: CalibrationRun, obs: CalibrationObservation) -> None:
        """Take this leg's length measurement on the read the train's block list
        lets go of B (design_new.md "Speed calibration", The pass measures the
        train's length).

        The far outer block confirmed `OCCUPANCY_ON_THRESHOLD` reads after the
        head crossed into it, and B confirms clear `OCCUPANCY_OFF_THRESHOLD`
        reads after the tail left it. So the tail left B
        `reads - (OCCUPANCY_OFF_THRESHOLD - OCCUPANCY_ON_THRESHOLD)` reads after
        the head reached the far block, and at the crossing's speed that is the
        train's length. A clear B held through the lighting zone, or a lone
        present read pausing its clear count, only adds reads, so the pass
        keeps the shortest length it measures."""
        started = run.runoff_started_at
        if started is None:
            return
        run.runoff_reads += 1
        if obs.measured_block_listed:
            return
        run.runoff_started_at = None
        lag_reads = OCCUPANCY_OFF_THRESHOLD - OCCUPANCY_ON_THRESHOLD
        reads = run.runoff_reads
        if reads <= lag_reads or run.runoff_speed_cm_s <= 0.0:
            return
        seconds_per_read = (obs.now - started) / reads
        length = run.runoff_speed_cm_s * seconds_per_read * (reads - lag_reads)
        if run.measured_length_cm is None or length < run.measured_length_cm:
            run.measured_length_cm = length

    def _fail_overrun(self, run: CalibrationRun) -> CalibrationCommand:
        """Fail the pass for an overrun Observe recorded on this leg."""
        return self.fail(
            run, "train overran the stretch past the far outer block"
        )

    def _resolve_incomplete_leg(
        self, run: CalibrationRun, obs: CalibrationObservation
    ) -> Optional[CalibrationCommand]:
        """Resolve a leg that ran out of authority or time without reaching the
        far block: the step fails (design_new.md "Speed calibration", A step
        passes or fails).

        The locomotive never reached the measured block (A step that does not
        move the train fails), or it entered the measured block and stopped
        inside the stretch (A crossing that stops inside the stretch fails the
        step). The part-crossing is not timed — it is no measurement, and timing
        it would put a speed on the curve the locomotive never held."""
        return self._fail_step(run)

    def _fail_step(self, run: CalibrationRun) -> CalibrationCommand:
        """Drop the step's crossings, record the failure, and go on to the next
        step from wherever the train stops. The leg route never reached the far
        block, so the returned command tears it down.

        - A failed `max_voltage` step fails the pass (A pass whose top step
          fails fails the pass).
        - A filled step is measured once more; failing twice fails the pass (A
          filled step that fails is measured once more).
        - A failed coarse or narrowing step only narrows the search.
        """
        self._mark_step_crossings(run, CROSSING_DROPPED)
        self._log_crossing(run, None, None, CROSSING_FAILED_STEP)
        run.crossings = []
        run.step_remeasured = False
        run.step_cap_retries = 0
        voltage = run.current_voltage
        self._report_step(run, None, None, None, None, 0, failed=True)

        if (
            run.current_step_kind == STEP_COARSE
            and abs(voltage - run.max_voltage) <= _VOLTAGE_EPSILON
        ):
            return self.fail(
                run,
                f"the train did not cross {run.measured_block_id} even at "
                f"{voltage:g} V (max_voltage) — check the stretch is long enough "
                "for the consist and clear to run",
            )
        if run.current_step_kind == STEP_FILLED:
            if run.step_failed_once:
                return self.fail(
                    run,
                    f"{voltage:g} V did not complete its crossings twice though a "
                    "lower voltage did — check the stretch for a dirty rail or a "
                    "bad contact",
                )
            run.step_failed_once = True
            run.attempt += 1
        else:
            run.failed_voltages.append(voltage)
            finished = self._go_to_next_step(run, deactivate_route=True)
            if finished is not None:
                return finished
        run.phase = PHASE_RESETTING
        run.leg_armed = False
        return CalibrationCommand(drive_voltage=0.0, deactivate_route=True)

    def _advance_stopping(
        self, run: CalibrationRun, obs: CalibrationObservation
    ) -> Optional[CalibrationCommand]:
        # The stopping half is still this leg: a block past the stretch
        # confirming while the train brakes is an overrun too.
        if run.overrun_seen:
            return self._fail_overrun(run)
        # The half-power stop runs its course before the train counts as
        # stopped, even when a ceiling has already brought it to a stand.
        if run.half_power_until is not None and obs.now < run.half_power_until:
            return None
        if not obs.is_stopped:
            return None
        if obs.measured_block_held:
            # The next leg is timed from a fresh entry into B, so it waits until
            # the standing train is off B in both views. A standing train is
            # not driven; at a set lighting voltage B takes its lighting-zone
            # hold and its clear count to read clear (design_new.md "Speed
            # calibration", A train that cannot get off B fails the pass).
            run.standing_reads += 1
            if run.standing_reads > CALIBRATION_OFF_B_WAIT_READS:
                return self.fail(
                    run,
                    f"train stopped with its tail still on {run.measured_block_id} "
                    f"in {run.heading_toward_block_id} — check the train's length, "
                    f"or {run.heading_toward_block_id} is too short to hold it",
                )
            return None
        if len(run.crossings) >= run.crossings_target:
            outcome = self._close_step(run)
            if outcome is not None:
                return outcome
        # Whatever happened to the step, the train is standing at one end of the
        # stretch and the next leg runs back the other way. Leave it unarmed:
        # the next tick's leg branch takes the path, re-frames the span head, and
        # powers up, which is the one place any leg starts.
        run.heading_to = 1 - run.heading_to
        run.phase = PHASE_LEG
        run.leg_armed = False
        run.needs_reverse = True
        return None

    def _close_step(self, run: CalibrationRun) -> Optional[CalibrationCommand]:
        """Take the median of the step's crossings, check it against the steps
        around it, and either bank the sample or measure the step once more
        (design_new.md "Speed calibration", Both directions are measured, and a
        step's sample is their median).

        Returns a command when the pass is finished or failed, None when the
        caller should carry on to the next leg.
        """
        measured = median(run.crossings)
        crossing_count = len(run.crossings)
        run.crossings = []
        voltage = run.current_voltage
        above = self._neighbour(run, voltage, higher=True)
        below = (
            self._neighbour(run, voltage, higher=False)
            if run.current_step_kind == STEP_FILLED
            else None
        )
        above_kmh = above.speed_kmh if above is not None else None
        below_kmh = below.speed_kmh if below is not None else None

        # `voltage_speed_curve` must stay invertible, so speed must not fall as
        # voltage rises. A step close to its neighbour records the neighbour's
        # speed; a step clearly outside it is measured once more
        # (design_new.md "Speed calibration", A step that measures close to the
        # step above it records the same speed; A step that measures clearly
        # faster than the step above it is measured once more).
        fraction = CALIBRATION_SAME_SPEED_FRACTION
        step_speed = measured
        outside = False
        if above_kmh is not None:
            if above_kmh < step_speed <= above_kmh * (1.0 + fraction):
                step_speed = above_kmh
            elif step_speed > above_kmh + _MONOTONIC_EPSILON_KMH:
                outside = True
        if not outside and below_kmh is not None:
            if below_kmh * (1.0 - fraction) <= step_speed < below_kmh:
                step_speed = below_kmh
            elif step_speed < below_kmh - _MONOTONIC_EPSILON_KMH:
                outside = True
        if outside:
            self._report_step(run, measured, above_kmh, below_kmh, None, crossing_count)
            if run.step_remeasured:
                return self.fail(
                    run,
                    f"{voltage:g} V measured clearly outside the steps around it twice",
                )
            run.step_remeasured = True
            self._mark_step_crossings(run, CROSSING_REPLACED)
            run.attempt += 1
            return None

        self._report_step(run, measured, above_kmh, below_kmh, step_speed, crossing_count)
        run.samples.append(
            CalibrationSample(
                track_voltage=voltage, speed_kmh=step_speed, kind=run.current_step_kind
            )
        )
        return self._go_to_next_step(run)

    @staticmethod
    def _neighbour(
        run: CalibrationRun, voltage: float, *, higher: bool
    ) -> Optional[CalibrationSample]:
        """The nearest passed step at a higher (or lower) voltage, or None."""
        if higher:
            candidates = [s for s in run.samples if s.track_voltage > voltage + _VOLTAGE_EPSILON]
            return min(candidates, key=lambda s: s.track_voltage, default=None)
        candidates = [s for s in run.samples if s.track_voltage < voltage - _VOLTAGE_EPSILON]
        return max(candidates, key=lambda s: s.track_voltage, default=None)

    def _go_to_next_step(
        self, run: CalibrationRun, *, deactivate_route: bool = False
    ) -> Optional[CalibrationCommand]:
        """Pick the next step, or finish the sweep when none is left. Returns
        the finishing command, or None when a next step was set."""
        run.step_remeasured = False
        run.step_cap_retries = 0
        run.step_failed_once = False
        run.attempt = 1
        nxt = self._plan_next_step(run)
        if nxt is None:
            return self._finish_sweep(run, deactivate_route=deactivate_route)
        run.current_step_voltage, run.current_step_kind = nxt
        return None

    @staticmethod
    def _plan_next_step(run: CalibrationRun) -> Optional[Tuple[float, str]]:
        """The next step's voltage and kind, or None when the sweep is done.

        - Coarse: the next coarse voltage below the step just run, until a step
          fails or the coarse steps run out.
        - Narrowing: halfway between the lowest passed step and the failed step
          below it (or `0` V), while they are more than
          `CALIBRATION_MIN_VOLTAGE_GAP_V` apart.
        - Filling: from the top down, halfway between two neighbouring passed
          steps whose samples differ by more than `CALIBRATION_FILL_SPEED_FRACTION`
          of the top step's sample and whose voltages are more than twice
          `CALIBRATION_MIN_VOLTAGE_GAP_V` apart.
        """
        gap = float(CALIBRATION_MIN_VOLTAGE_GAP_V)
        passed = sorted(run.samples, key=lambda s: s.track_voltage)
        if run.stage == STEP_COARSE:
            just_failed = any(
                abs(v - run.current_voltage) <= _VOLTAGE_EPSILON for v in run.failed_voltages
            )
            if not just_failed:
                below = [
                    v
                    for v in coarse_voltages(run.max_voltage)
                    if v < run.current_voltage - _VOLTAGE_EPSILON
                ]
                if below:
                    return below[0], STEP_COARSE
            run.stage = STEP_NARROWING
        if run.stage == STEP_NARROWING:
            if passed:
                upper = passed[0].track_voltage
                lower = max(
                    (v for v in run.failed_voltages if v < upper - _VOLTAGE_EPSILON),
                    default=0.0,
                )
                if upper - lower > gap + _VOLTAGE_EPSILON:
                    return round((upper + lower) / 2.0, 6), STEP_NARROWING
            run.stage = STEP_FILLED
        if not passed:
            return None
        top = passed[-1].speed_kmh
        for low, high in reversed(list(zip(passed, passed[1:]))):
            far_apart = high.speed_kmh - low.speed_kmh > CALIBRATION_FILL_SPEED_FRACTION * top
            room = high.track_voltage - low.track_voltage > 2.0 * gap + _VOLTAGE_EPSILON
            if far_apart and room:
                return round((high.track_voltage + low.track_voltage) / 2.0, 6), STEP_FILLED
        return None

    def _log_crossing(
        self,
        run: CalibrationRun,
        seconds: Optional[float],
        speed_kmh: Optional[float],
        outcome: str,
    ) -> None:
        """Add one crossing of the current step to the panel's list, and report
        it on the event stream (design_new.md "Speed calibration", Every
        crossing and every step is reported)."""
        crossing = CalibrationCrossing(
            track_voltage=run.current_voltage,
            attempt=run.attempt,
            toward_block_id=run.heading_toward_block_id,
            seconds=seconds,
            speed_kmh=speed_kmh,
            outcome=outcome,
        )
        run.crossing_log.append(crossing)
        timing = (
            f"{seconds:.2f} s, {speed_kmh:.1f} km/h"
            if seconds is not None and speed_kmh is not None
            else "not timed"
        )
        self._emit_info(
            run.train_id,
            operation="calibration_crossing",
            message=(
                f"{run.current_voltage:g} V toward {run.heading_toward_block_id}: "
                f"{timing}, {outcome}"
            ),
            payload={
                "track_voltage": run.current_voltage,
                "attempt": run.attempt,
                "measured_block_id": run.measured_block_id,
                "toward_block_id": run.heading_toward_block_id,
                "block_length_cm": run.block_length_cm,
                "seconds": seconds,
                "speed_kmh": speed_kmh,
                "outcome": outcome,
                "measured_length_cm": run.measured_length_cm,
            },
        )

    def _report_step(
        self,
        run: CalibrationRun,
        measured_kmh: Optional[float],
        above_kmh: Optional[float],
        below_kmh: Optional[float],
        recorded_kmh: Optional[float],
        crossing_count: int,
        *,
        failed: bool = False,
    ) -> None:
        """Report how a step closed: passed with the median of its crossings,
        the samples of the steps it was checked against and the speed it
        recorded, or failed (design_new.md "Speed calibration", Every crossing
        and every step is reported)."""
        voltage = run.current_voltage
        if failed:
            result = "failed"
            message = (
                f"{voltage:g} V {run.current_step_kind} step (attempt "
                f"{run.attempt}): failed"
            )
        else:
            if recorded_kmh is None:
                result = "clearly outside the steps around it"
            elif recorded_kmh != measured_kmh:
                result = "recorded at a neighbouring step's speed"
            else:
                result = "recorded"
            message = (
                f"{voltage:g} V {run.current_step_kind} step (attempt "
                f"{run.attempt}): median {measured_kmh:.1f} km/h of "
                f"{crossing_count} crossings, {result}"
            )
        self._emit_info(
            run.train_id,
            operation="calibration_step",
            message=message,
            payload={
                "track_voltage": voltage,
                "kind": run.current_step_kind,
                "attempt": run.attempt,
                "median_kmh": measured_kmh,
                "step_above_kmh": above_kmh,
                "step_below_kmh": below_kmh,
                "recorded_kmh": recorded_kmh,
                "crossing_count": crossing_count,
                "result": result,
            },
        )

    @staticmethod
    def _mark_step_crossings(run: CalibrationRun, outcome: str) -> None:
        """Re-mark the current attempt's used crossings — when the step is
        measured again, or when a later crossing of it fails the step."""
        run.crossing_log = [
            replace(c, outcome=outcome)
            if abs(c.track_voltage - run.current_voltage) <= _VOLTAGE_EPSILON
            and c.attempt == run.attempt
            and c.outcome == CROSSING_USED
            else c
            for c in run.crossing_log
        ]

    def _finish_sweep(
        self, run: CalibrationRun, *, deactivate_route: bool = False
    ) -> CalibrationCommand:
        """Close a sweep that has no step left to run.

        When the points break any of the curve's conditions — for example only
        the `max_voltage` step passed, so the curve has one point — the pass
        fails and reports which condition, rather than offering a curve the
        commit must refuse (design_new.md "Speed calibration", A finished curve
        the commit would refuse fails the pass).
        """
        problems = curve_problems(self.measured_curve(run), run.max_voltage)
        if problems:
            return self.fail(
                run, "the measured curve cannot be stored: " + "; ".join(problems)
            )
        run.phase = PHASE_MEASURED
        # The pass has stopped driving and is waiting for a commit target, so the
        # override withdraws; zero the manual fallback so the standing train
        # stays put until the operator picks a profile.
        self._stop_manual_traction(run.train_id)
        self._emit_command_result(
            run.train_id,
            operation="speed_calibration",
            success=True,
            reason=self._measured_reason(run),
        )
        # A sweep reached here by ordinary stop completion (the final leg
        # reached the far block) needs no route op — its route already finished.
        # A sweep that ends on a failed step still holds a RUNNING leg route, so
        # that caller passes deactivate_route=True to tear it down.
        return CalibrationCommand(
            drive_voltage=0.0, deactivate_route=deactivate_route
        )

    def _measured_reason(self, run: CalibrationRun) -> str:
        """What a finished pass reports, naming the starting voltage it found."""
        curve = self.measured_curve(run)
        start_v, start_kmh = curve[0]
        return (
            f"measured {len(curve)} points; starting voltage {start_v:g} V at "
            f"{start_kmh:.1f} km/h — pick a profile to write"
        )

    # -- runtime hook -----------------------------------------------------

    def observe_detected_entries(self) -> None:
        """Read each open auto pass's detected-entry slice.

        Called inside the global detector-read window, after attribution has written every
        train's `physics_sync.pending_detected_entered_blocks` and before the
        per-train loop begins, so the pass sees a confirmation the same tick
        the global detector read credits it and before that train's own final movement callback
        folds and clears the slice (design_new.md "Speed calibration", The
        clock is two confirmed entries).

        This is a read against an existing detector-read handoff, not an additional
        one: it writes nothing into the slice (design_new.md "Stage handoff
        rule", A read is not a handoff). What it sees it records on the run
        through `record_detected_entries(...)`, which Plan step `c9e1` reads
        later that tick.
        """
        if not self.runs:
            return
        now = self._operating_time()
        for train_id, run in self.runs.items():
            if not run.is_measuring:
                continue
            controller = self._controller(train_id)
            if controller is None:
                continue
            entered = getattr(
                controller.physics_sync, "pending_detected_entered_blocks", None
            )
            if entered:
                self.record_detected_entries(run, entered, now)

    def record_detected_entries(
        self, run: CalibrationRun, entered: Iterable[str], now: float
    ) -> None:
        """Record what this tick's confirmed entries mean for the run, in
        Observe (design_new.md "Speed calibration", `CalibrationRun`; The clock
        is two confirmed entries; A leg that overruns the stretch fails the
        pass).

        `entered` holds only entries attribution credited to the calibration
        train, so another train moving elsewhere cannot start or stop the clock.
        It writes three facts that Plan step `c9e1` reads the same tick:

          * `overrun_seen` when any entry lies outside the stretch, in either
            half of the leg;
          * `crossing_started_at` when B confirms on an armed leg whose clock has
            not started;
          * `crossing_finished_at` when the far outer block confirms on a later
            read after the clock started.

        A leg that is not armed yet records no crossing: the leg arms in Plan
        after this, and arming resets all three.
        """
        if run.phase == PHASE_MOVING:
            # The move times nothing and may run through blocks outside the
            # stretch on its way in.
            return
        entered = set(entered)
        stretch = {run.measured_block_id, *run.outer_block_ids}
        if entered - stretch:
            run.overrun_seen = True
        if run.phase != PHASE_LEG or not run.leg_armed:
            return
        if run.crossing_started_at is None:
            if run.measured_block_id in entered:
                run.crossing_started_at = now
        elif run.crossing_finished_at is None and run.heading_toward_block_id in entered:
            run.crossing_finished_at = now

    def plan_tick(self, train_id: str) -> Optional[CalibrationCommand]:
        """Build this tick's observation for the train's open auto pass, advance
        the state machine, and return the leg-route command Plan step `c9e1`
        carries out (design_new.md "Speed calibration", Where it runs).

        Called from `RouteExecutor.tick_train_plan(...)` at step `c9e1`, before
        the ordinary route steps. It arms a leg, evaluates step progress, and
        updates the run state; it touches no route registry and issues no power
        command. The returned `CalibrationCommand` carries the leg-route intents
        (`arm_leg_to` / `deactivate_route`) and head-frame reversal for `RouteExecutor`
        to perform. The step's power is the run state the controller reads back
        through `get_propulsion_override(...)` during this tick's Action, so the
        pass drives without ever queuing an operator command. Returns `None` when
        no auto pass is open for the train or the pass wants nothing this tick.
        """
        run = self.runs.get(train_id)
        if run is None:
            return None
        if not run.is_measuring:
            return None
        controller = self._controller(train_id)
        if controller is None:
            return None
        is_stopped = bool(controller.is_stopped())
        plan = controller.path_projection.received_path_plan
        span_blocks = tuple(
            getattr(controller.get_current_span(), "occupied_blocks", ()) or ()
        )
        holder = self._block_holder
        listed_blocks = tuple(
            block_id
            for block_id in dict.fromkeys(span_blocks + run.move_blocks_behind)
            if holder is not None and holder(block_id) == train_id
        )
        obs = CalibrationObservation(
            now=self._operating_time(),
            is_stopped=is_stopped,
            span_blocks=span_blocks,
            listed_blocks=listed_blocks,
            head_block=self._head_block(controller),
            path_present=plan is not None,
            held_short=is_stopped
            and plan is not None
            and self._last_reserved_block(plan) != run.heading_toward_block_id,
            outer_block_index=self._outer_block_index(run, controller),
            measured_block_held=self._on_measured_block(run, controller),
            measured_block_listed=holder is not None
            and holder(run.measured_block_id) == train_id,
        )
        return self.advance(run, obs)

    @staticmethod
    def _last_reserved_block(plan) -> str:
        """The last block a received path plan reserves, or "" when it names
        none."""
        blocks = [
            entry.resource_id
            for entry in getattr(plan, "resource_sequence", ()) or ()
            if getattr(entry, "resource_type", None) == "block"
        ]
        return blocks[-1] if blocks else ""

    def _on_measured_block(self, run: CalibrationRun, controller) -> bool:
        """Whether the train is still on the measured block in either view of
        occupancy: its span covers it, or its block list holds it."""
        span = controller.get_current_span()
        if run.measured_block_id in (getattr(span, "occupied_blocks", ()) or ()):
            return True
        holder = self._block_holder
        return holder is not None and holder(run.measured_block_id) == run.train_id

    def _outer_block_index(self, run: CalibrationRun, controller) -> Optional[int]:
        """Which outer block the train stands in, the head's block first, or
        None when its span is in neither."""
        blocks = list(getattr(controller.get_current_span(), "occupied_blocks", ()) or ())
        head = self._head_block(controller)
        for block_id in [head, *blocks]:
            if block_id and block_id in run.outer_block_ids:
                return run.outer_block_ids.index(block_id)
        return None

    @staticmethod
    def _head_block(controller) -> str:
        """The block the train's head is in.

        `TrainSpan.occupied_blocks` is ordered `end_a` to `end_b`, and the framed
        head end names which physical end is the head (design_new.md
        "Reservation-side head derivation") — a span framed at `end_b` puts the head
        there, so that end's block is the last entry rather than the first.
        """
        span = controller.get_current_span()
        blocks = list(getattr(span, "occupied_blocks", ()) or ())
        if not blocks:
            return ""
        framed_head = getattr(controller, "_framed_head_end", "end_a")
        return blocks[-1] if framed_head == "end_b" else blocks[0]

    def _controller(self, train_id: str):
        controllers = self._controllers
        if not controllers:
            return None
        return controllers.get(train_id)

    def _leg_authority_present(self, train_id: str) -> bool:
        """Whether the leg's authority (the controller's `received_path_plan`)
        exists, so the pass may drive (design_new.md "Speed calibration", Plan
        acquires leg authority all-or-nothing). True with no controller wired (the
        unit tests drive the state machine directly), so those keep driving on
        `leg_armed` alone."""
        controller = self._controller(train_id)
        if controller is None:
            return True
        return controller.path_projection.received_path_plan is not None

    def _operating_time(self) -> float:
        """`AppState.operating_time`, the clock every calibration interval is
        measured against."""
        clock = self._operating_clock
        if clock is not None:
            try:
                return float(clock())
            except Exception:
                return 0.0
        return 0.0

    # -- propulsion override ----------------------------------------------

    def get_propulsion_override(
        self, train_id: str
    ) -> Optional[CalibrationPropulsionOverride]:
        """The drive an open calibration pass puts on the train this tick, or
        None.

        design_new.md "Speed calibration" (Where it runs / Controller owns
        calibration output): during Action, `TrainController.tick(...)` reads
        this and drives the returned voltage in place of the ordinary manual
        source, under every ordinary safety cap (ATC, service-stop, EB, pending
        power-off, output safe state). It returns None when no pass may drive,
        so the controller keeps its ordinary source. This is the whole of how a
        pass drives the train: it queues no operator commands.

        A pass drives the current step's voltage while a leg is armed,
        crossing, and has its authority, and `0` while the train is stopping at
        a leg's end, standing between steps or after a failed step, or still
        acquiring the leg's route. The first `CALIBRATION_HALF_POWER_S` of a leg
        and of a stop after a crossing drive half the step's voltage instead
        (design_new.md "Speed calibration", The pass starts and stops the train
        at half voltage first). Until the leg's `received_path_plan` is present
        the pass drives `0` (Plan acquires leg authority all-or-nothing): a leg
        without authority could not move anyway (the ATC limit is `0`), and
        holding the drive at `0` until the route's path exists keeps the step
        timeout and the drive in step. A finished or failed pass returns None,
        handing the train back to its ordinary manual source (which the
        abandonment/finish paths zeroed).
        """
        run = self.runs.get(train_id)
        if run is not None and run.phase == PHASE_MOVING:
            value = 0.0
            if (
                run.leg_armed
                and not run.move_cut
                and self._leg_authority_present(train_id)
            ):
                half = run.half_power or run.move_slowing
                value = CALIBRATION_MOVE_VOLTAGE_V * (0.5 if half else 1.0)
            return CalibrationPropulsionOverride(track_voltage=float(value))
        if run is not None and run.is_measuring:
            driving = run.phase in (PHASE_LEG, PHASE_CLEARING) and run.leg_armed
            if driving and not self._leg_authority_present(train_id):
                driving = False
            step_v = run.current_voltage
            if driving and run.phase == PHASE_CLEARING:
                # The run-off runs at the step's voltage, the voltage whose
                # speed timed it.
                value = step_v
            elif driving:
                value = step_v * (0.5 if run.half_power else 1.0)
            elif run.phase == PHASE_STOPPING and run.half_power:
                value = step_v * 0.5
            else:
                value = 0.0
            return CalibrationPropulsionOverride(track_voltage=float(value))
        return None

    def _stop_manual_traction(self, train_id: str) -> None:
        """Zero the controller's manual power percent as a pass ends.

        While a pass drives, the propulsion override supersedes the manual power
        source, so the pass never writes it. When the pass ends the override
        withdraws and the controller falls back to that source; zeroing it here
        is how a pass-abandonment or finish "commands a drive of `0`"
        (design_new.md "Speed calibration") without queuing an operator command —
        it is the ordinary safety reset the controller already applies under a
        braking cap. A no-op when the runtime or controller is absent (the unit
        tests drive the state machine with no controller wired).
        """
        controller = self._controller(train_id)
        if controller is None:
            return
        try:
            controller.manual.reset_power_percent_for_safety()
        except Exception:
            pass

    # -- commit -----------------------------------------------------------

    def measured_curve(self, run: CalibrationRun) -> List[Tuple[float, float]]:
        """The `voltage_speed_curve` this pass measured: one point per step that
        passed, at the voltage the step drove, in order of voltage
        (design_new.md "Speed calibration", A pass measures
        `voltage_speed_curve`). A failed step gives no point."""
        return sorted((s.track_voltage, s.speed_kmh) for s in run.samples)

    def commit(
        self, train_id: str, target_profile, *, clear_profile=None
    ) -> List[Tuple[float, float]]:
        """Write the measured curve onto `target_profile` and close the run.

        Writes two fields and no others: `voltage_speed_curve` from the pass's
        samples and `calibrated_at` from the wall clock (design_new.md "Speed
        calibration", What commit writes). It must not write `lighting_voltage`,
        a motion rate, a notch table, or `model`.

        Which records arrive here is the operator's commit-target choice,
        resolved by the caller. `target_profile` is the record the two fields are
        written onto: the train's own instance profile for **Save to this
        locomotive**, or the model profile the instance derives from for
        **Promote to model**. On promote, `clear_profile` is that instance, whose
        two measured fields are cleared in the same commit so it falls back to
        following the freshly-written model. The caller checks the lighting
        voltages of the profiles that will run on the curve (design_new.md "A
        commit keeps every lighting voltage below the starting voltage").
        """
        run = self.runs.get(train_id)
        if run is None:
            raise ValueError("No calibration pass is open for this train")
        # Commit is all or nothing. A partly replaced curve would be part
        # measured and part seeded with nothing on the profile to say which part
        # is which, and no later reading of it could tell.
        if not run.is_measured:
            raise ValueError("Calibration pass is not finished")

        curve = self.measured_curve(run)
        problems = curve_problems(curve, run.max_voltage)
        if problems:
            raise ValueError("Measured curve cannot be stored: " + "; ".join(problems))

        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        target_profile.voltage_speed_curve = curve
        target_profile.calibrated_at = stamp
        # Promote clears the instance's two measured fields so it goes back to
        # following the model curve just written.
        if clear_profile is not None and clear_profile is not target_profile:
            clear_profile.voltage_speed_curve = None
            clear_profile.calibrated_at = None
        self.runs.pop(train_id, None)
        self._emit_command_result(
            train_id,
            operation="speed_calibration",
            success=True,
            reason=f"wrote {len(curve)} measured points to {target_profile.profile_id}",
        )
        return curve

    def run_still_valid(self, train_id: str) -> bool:
        """Whether this train's open calibration pass may stay open.

        The one validity predicate (design_new.md "Speed calibration", Where it
        runs). Action step `26f5`
        (`SimulationController._reevaluate_calibration_run`) evaluates it once per
        tick from live state; when it is false the pass ends that same tick
        through `abandon(train_id, reason)`.

        Returns True when no run is open (nothing to end) and for a finished
        pass, which is a held result rather than an open run. Otherwise it is
        true when all of these hold:
          - the train's controller is still present
          - the train is powered — `controller.powered`
          - `eb.active` is False
          - no power-off is pending — `phase_latches.power_off` is False
        A measurement fault is not one of these: it ends the pass earlier, in
        Plan step `c9e1`, through `fail(...)`. `run_invalid_reason(train_id)`
        holds the checks; this is true when it names none.
        """
        return self.run_invalid_reason(train_id) is None

    def run_invalid_reason(self, train_id: str) -> Optional[str]:
        """The first condition of `run_still_valid` that no longer holds, in
        the words the panel shows, or None while the run may stay open.

        Step `26f5` passes it to `abandon(train_id, reason)`, so a speed pass
        that ends here says which condition failed (design_new.md "Speed
        calibration", Where it runs).
        """
        speed = self.runs.get(train_id)
        if speed is None or speed.is_measured:
            return None
        controller = self._controller(train_id)
        if controller is None:
            return "the train's controller is gone"
        if not controller.powered:
            return "the train was powered off"
        if controller.eb.active:
            return "the emergency brake is on"
        if controller.route_hooks.phase_latches.power_off:
            return "a power-off is pending"
        return None

    def end_open_runs(self, reason: str) -> None:
        """End every open pass, keeping held results (design_new.md "What ends
        a calibration run", The session leaves Live, or its outputs drop).

        `SimulationController.enter_output_safe_state()` and the watchdog
        trip-record consumption call this. Each open pass ends through
        `abandon(...)`. A finished pass waits for the operator, so it is left
        alone.
        """
        for train_id, run in list(self.runs.items()):
            if run.is_measuring:
                self.abandon(train_id, reason=reason)

    def discard_all(self, reason: str) -> None:
        """End every open run and discard every held result (design_new.md
        "What ends a calibration run").

        `/api/network/clear` and `load_layout` call this before they replace the
        trains, because a result measured on a train that is about to be
        replaced must not be committed to the train that replaces it.
        """
        for train_id in list(self.runs):
            self.abandon(train_id, reason=reason)

    # -- reporting --------------------------------------------------------

    def _emit_info(
        self, train_id: str, *, operation: str, message: str, payload: dict
    ) -> None:
        """One informational event about an open pass, on the same stream as
        the command results."""
        nc = self._notification_center
        if nc is None:
            return
        try:
            from notification_center import Kind, train_ref

            nc.emit(
                source="SpeedCalibration",
                operation=operation,
                kind=Kind.INFO,
                message=message,
                entities=[train_ref(train_id)],
                payload={"train_id": train_id, **payload},
            )
        except Exception:
            pass

    def _emit_command_result(
        self, train_id: str, *, operation: str, success: bool, reason: str
    ) -> None:
        """One command-result event on the stream `NotificationCenter` owns.

        Both calibration procedures report here; neither opens an event sink of
        its own (design_new.md "Speed calibration" / "Lighting-voltage
        calibration").
        """
        nc = self._notification_center
        if nc is None:
            return
        try:
            from notification_center import Kind, train_ref

            nc.emit(
                source="SpeedCalibration",
                operation=operation,
                kind=Kind.INFO if success else Kind.WARNING,
                message=reason or (
                    f"{operation} finished" if success else f"{operation} failed"
                ),
                entities=[train_ref(train_id)],
                payload={
                    "command_id": f"calibrate:{train_id}",
                    "train_id": train_id,
                    "success": success,
                    "reason": reason,
                },
            )
        except Exception:
            pass


def _clamp(value: float, low: float, high: float) -> float:
    """Hold a candidate voltage inside `[low, high]`, on a clean step grid."""
    return round(min(max(float(value), low), high), 3)
