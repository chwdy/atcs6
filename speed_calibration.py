"""Speed calibration — measure a locomotive's power/speed response and write it
into a `SpeedProfile` (design_new.md "Speed calibration").

A pass sweeps `power_percent` in `CALIBRATION_POWER_STEP_PERCENT` increments up
to `100%` and measures the speed that comes back at each. It must work this way
round: `power_speed_curve` is the curve that turns a wanted speed into a control
value, and it is the very thing the pass is building, so the pass must not ask
for a target speed while building it.

**Auto profiling** times the head across one operator-named measured block, B,
between two confirmed block entries — into B and into the block after it. Timing
between two entries of the same kind measures exactly one block whatever the
start position was, and the detector's fixed lag lands on both stamps and
cancels in the difference. B's two neighbours are A and C: the run-up at one end
and the stopping room at the other, and which is which flips each leg.

The pass turns the train around at each end itself — power to 0, wait for the
canonical stopped predicate, re-frame the span to its other head end, re-command
the step — so a plain length of track can measure a locomotive with no loop to
lap. Each
step runs `CALIBRATION_ROUND_TRIPS` round trips and averages twice that many
crossings, half in each direction: a stretch is rarely dead level, and equal
counts each way cancel the grade.

Measuring every step does not commit the run. It holds its finished samples
until the operator picks a target — replace the linked profile, or save a new
one — which is what `commit(...)` writes. `CalibrationRun` is in-memory only;
only the committed result persists, through ordinary `SpeedProfile` persistence.

This module also owns the second, smaller procedure (design_new.md
"Lighting-voltage calibration"): finding the one voltage at which a locomotive
is lit and still holds, and writing it to `lighting_voltage`. It comes first for
a newly added locomotive, because a speed pass takes its bottom step from that
anchor. It has no measurement step at all — the operator watching the locomotive
is the instrument — so `LightingCalibrationRun` carries a candidate voltage the
operator steps up and down and nothing else.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from constants import (
    CALIBRATION_CAP_RETRY_LIMIT,
    CALIBRATION_POWER_STEP_PERCENT,
    CALIBRATION_ROUND_TRIPS,
    CALIBRATION_STEP_TIMEOUT_S,
    LIGHTING_VOLTAGE_STEP_V,
)
from model_scale import cm_per_sec_to_kmh

# A measured mean this far below the previous step's is treated as equal rather
# than as a fall, so float noise in the crossing mean cannot trip the
# non-decreasing rule.
_MONOTONIC_EPSILON_KMH = 1e-6

# Run phases. "leg" and "stopping" are the two halves of one crossing; the pass
# alternates between them until every step is measured.
PHASE_LEG = "leg"
PHASE_STOPPING = "stopping"
PHASE_MEASURED = "measured"
PHASE_FAILED = "failed"


@dataclass(frozen=True)
class CalibrationSample:
    """One measured curve point: the mean of a step's crossings."""

    power_percent: float
    speed_kmh: float


@dataclass(frozen=True)
class CalibrationObservation:
    """What the world looks like to an open pass on one tick.

    Built by `SpeedCalibration.tick()` from committed runtime state and passed
    into `advance(...)`, which is the whole of the pass's decision logic. Keeping
    the two apart is what lets the state machine be driven directly by a test
    with synthetic confirmations instead of a moving simulation.
    """

    now: float
    """`AppState.operating_time`. Wall time would count an operator's stop as
    distance covered; `operating_time` advances only while the session runs."""

    newly_confirmed: Tuple[str, ...] = ()
    """Block ids that became confirmed-OCCUPIED by the calibration train since
    the last observation. Attribution has already credited these to this train,
    so another train moving elsewhere cannot start or stop the clock."""

    is_stopped: bool = False
    """The canonical stopped predicate, `TrainController.is_stopped()`."""

    head_block: str = ""
    """The block the train's head is in. Arming a leg reads it to orient from
    where the train actually stands. It is the head's block and not the whole
    span, because a consist longer than a block still holds the one behind it,
    and the block the tail is trailing through is not where the train is going."""

    safe_state_generation: int = 0
    """`SimulationController.safe_state_generation`. A change means a session
    stop or another safe-state request occurred."""

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


@dataclass(frozen=True)
class CalibrationCommand:
    """What the pass wants this tick, decided by `advance(...)`.

    `power_percent` is the run state the controller reads back through
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

    power_percent: Optional[float] = None
    reverse_cab: bool = False
    deactivate_route: bool = False
    """Tear the active leg route down. Set on every leg-end that did NOT reach
    the far block — a stall, an overrun, a never-arrives leg, or any pass
    abandonment. A leg that reaches the far block sets nothing here: it finishes
    through ordinary stop completion, which tears its route down for it."""
    arm_leg_to: Optional[str] = None
    """The far outer block to build this leg's one-off route to, before the power
    command can move the train. A leg without a route has no authority, so the
    ATC limit is `0` (`no_reservation`) and the safety-cap ordering clamps the
    commanded percent to it."""


@dataclass(frozen=True)
class CalibrationPropulsionOverride:
    """The traction an open calibration pass drives with this tick, read by the
    controller during Action (design_new.md "Speed calibration", Controller owns
    calibration output). A closed two-variant union:

    - `kind == "power_percent"`: the speed pass commands this `value` percent in
      place of the ordinary manual power source. It maps through
      `power_speed_curve` and stays under the same ATC / service-stop / EB /
      pending-power-off / output-safe-state caps as any manual percent.
    - `kind == "track_voltage"`: a lighting-voltage run substitutes this exact
      voltage magnitude. The controller reads it here and hands it to physics on
      `PhysicsCommand.voltage_source_track_voltage`, in place of the ordinary
      stopped-but-lit / speed-derived voltage; the speed pass never returns it.

    The controller reads this through `SpeedCalibration.get_propulsion_override`.
    It is never copied into `manual.manual_power_percent` or `handle_position`, and
    no operator command carries it.
    """

    kind: str
    value: float


@dataclass
class CalibrationRun:
    """Transient working record of one calibration pass (in-memory only)."""

    train_id: str
    profile_id: str
    power_steps: List[float]

    # The stretch. `outer_block_ids` is (A, C); `heading_to` indexes the one the
    # train is currently running toward, so it names the stopping room this leg
    # and the run-up the next.
    measured_block_id: str = ""
    outer_block_ids: Tuple[str, str] = ("", "")
    block_length_cm: float = 0.0
    heading_to: int = 1

    phase: str = PHASE_LEG
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
    first. False for the opening leg and after a stall, where the train never
    turned around."""
    step_index: int = 0
    samples: List[CalibrationSample] = field(default_factory=list)
    crossings: List[float] = field(default_factory=list)
    step_remeasured: bool = False

    leg_started_at: Optional[float] = None
    """When this leg's step timeout started counting — the tick its authority
    first appeared, not the tick it armed. `None` until then, so the timeout does
    not run while acquisition is still in progress (design_new.md "Speed
    calibration", Plan acquires leg authority all-or-nothing)."""
    clock_started_at: Optional[float] = None
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

    safe_state_generation: int = 0
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
    def current_power(self) -> float:
        """The `power_percent` this step commands."""
        if self.step_index >= len(self.power_steps):
            return 0.0
        return self.power_steps[self.step_index]

    @property
    def is_measuring(self) -> bool:
        return self.phase in (PHASE_LEG, PHASE_STOPPING)

    @property
    def is_measured(self) -> bool:
        """Every step measured, waiting for the operator to pick a commit
        target. The pass writes nothing until they do."""
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
            "step_index": self.step_index,
            "step_count": len(self.power_steps),
            "current_power_percent": self.current_power,
            "crossings_done": len(self.crossings),
            "crossings_target": self.crossings_target,
            "samples": [
                {"power_percent": s.power_percent, "speed_kmh": s.speed_kmh}
                for s in self.samples
            ],
            "failure_reason": self.failure_reason,
        }


@dataclass
class LightingCalibrationRun:
    """Transient working record of one lighting-voltage run (in-memory only).

    Holds the candidate voltage the operator is stepping through and the
    ceiling it may not pass. While the run is open the controller drives the
    candidate onto the rails through `get_propulsion_override(...)` and the
    per-tick `PhysicsCommand`, in place of the stopped-but-lit voltage, so each
    step is answered by the locomotive itself.
    """

    train_id: str
    profile_id: str
    candidate_voltage: float
    ceiling_voltage: float


def power_sweep() -> List[float]:
    """The commanded steps: `CALIBRATION_POWER_STEP_PERCENT` increments up to
    `100%`.

    `0%` is not measured. `0%` power is `lighting_voltage`, the voltage at which
    the train is lit and holds, so `(0%, 0 km/h)` is the curve's origin by
    definition and the commit writes it as such.
    """
    step = max(1, int(CALIBRATION_POWER_STEP_PERCENT))
    return [float(p) for p in range(step, 101, step)]


class SpeedCalibration:
    """Runs calibration passes and commits the measured curves.

    Holds the active `CalibrationRun` per train. The opening preconditions
    (placed, manual `power_percent`, `READY` and stopped, no ATO route, no
    lighting run open) are enforced by the caller — the service layer — before
    `start`.
    """

    def __init__(self, simulation_controller=None) -> None:
        self._sc = simulation_controller
        self.runs: Dict[str, CalibrationRun] = {}
        self.lighting_runs: Dict[str, LightingCalibrationRun] = {}
        # This tick's global detector read for the calibration train, stashed
        # by `observe_detected_entries(...)` and consumed by `tick_train(...)`
        # in Action (design_new.md "Speed calibration", The clock is two
        # confirmed entries). The read happens during the global detector read
        # because that is where attribution has just written the slice and
        # before the train's own final movement callback folds and clears it.
        self._step0_entries: Dict[str, Tuple[str, ...]] = {}

    # -- lifecycle --------------------------------------------------------

    def start(
        self,
        train,
        profile,
        *,
        measured_block_id: str = "",
        outer_block_ids: Sequence[str] = (),
        block_length_cm: float = 0.0,
        heading_to: int = 1,
        safe_state_generation: int = 0,
        now: float = 0.0,
    ) -> CalibrationRun:
        """Open a pass over the power sweep.

        The pass needs the measured block, its two resolved neighbours, and its
        length. The caller resolves the stretch, because walking the track
        through the points as they are aligned needs the network, and it passes
        `heading_to` as the index of the outer block the train is running
        *toward* — the one it is not already standing in.
        """
        outer = tuple(outer_block_ids)[:2]
        if len(outer) < 2:
            outer = (outer + ("", ""))[:2]
        run = CalibrationRun(
            train_id=train.train_id,
            profile_id=profile.profile_id,
            power_steps=power_sweep(),
            measured_block_id=measured_block_id,
            outer_block_ids=(outer[0], outer[1]),
            block_length_cm=float(block_length_cm or 0.0),
            heading_to=1 if int(heading_to) else 0,
            safe_state_generation=int(safe_state_generation),
        )
        self.runs[train.train_id] = run
        self._step0_entries.pop(train.train_id, None)
        return run

    def abandon(self, train_id: str, reason: str = "cancelled") -> None:
        """End a speed pass without writing, and report the reason. The samples
        go with it (design_new.md "Speed calibration", `abandon`).

        The one teardown for a speed pass: operator Stop, a mid-run panel close,
        and Action step `26f5` finding the pass no longer valid all end it here.
        Each commands `power_percent = 0` and tears the active leg route down.
        Dropping the run withdraws the propulsion override, so the controller
        falls back to its ordinary manual source; zeroing that source here is
        what brings the train to a stand. The route deactivate's inclusive latch
        cancels any still-live leg path, and the Action consumer removes the
        RouteRun. An existing teardown is not duplicated (see
        `_deactivate_leg_route`).
        """
        run = self.runs.pop(train_id, None)
        self._step0_entries.pop(train_id, None)
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
        """End a pass on a measurement fault, command `power_percent = 0`, and
        report the reason.

        The profile's previous curve is left in place — a pass that does not
        measure every step writes nothing. The run is removed from `self.runs`
        so a failed pass never lingers: the caller's next Plan step no longer
        finds it, and the readiness gate that refuses a new pass while a run is
        open (`services.train_service._require_calibration_ready`) is not blocked
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
        self._step0_entries.pop(run.train_id, None)
        return CalibrationCommand(power_percent=0.0, deactivate_route=True)

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
        if obs.safe_state_generation != run.safe_state_generation:
            return self.fail(run, "session stopped during the pass")

        if run.phase == PHASE_LEG:
            return self._advance_leg(run, obs)
        return self._advance_stopping(run, obs)

    def _advance_leg(
        self, run: CalibrationRun, obs: CalibrationObservation
    ) -> Optional[CalibrationCommand]:
        stretch = {run.measured_block_id, *run.outer_block_ids}
        far_block = run.heading_toward_block_id

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
            run.clock_started_at = None
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
                power_percent=run.current_power,
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

        for block_id in obs.newly_confirmed:
            if block_id not in stretch:
                # The locomotive needed more stopping room than the stretch
                # has. Nothing on the rails prevents this: the block ahead of a
                # train's head is energized as its pending-entry block whichever
                # block that turns out to be.
                return self.fail(
                    run, f"train overran the stretch into block {block_id}"
                )

        if run.measured_block_id in obs.newly_confirmed and run.clock_started_at is None:
            run.clock_started_at = obs.now
            return None

        if far_block in obs.newly_confirmed and run.clock_started_at is not None:
            elapsed = obs.now - run.clock_started_at
            run.clock_started_at = None
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
                run.step_cap_retries += 1
                if run.step_cap_retries >= CALIBRATION_CAP_RETRY_LIMIT:
                    return self.fail(
                        run,
                        f"stretch too short to measure {run.current_power:.0f}% "
                        f"at block {run.measured_block_id}",
                    )
                run.phase = PHASE_STOPPING
                return CalibrationCommand(power_percent=0.0)
            if elapsed <= 0.0 or run.block_length_cm <= 0.0:
                return self.fail(run, "measured block crossing timed at zero")
            # A clean crossing measured: the cap-retry streak resets.
            run.step_cap_retries = 0
            run.crossings.append(
                cm_per_sec_to_kmh(run.block_length_cm / elapsed)
            )
            run.phase = PHASE_STOPPING
            # The leg reached the far block, so it finishes through ordinary stop
            # completion (Plan settlement step `9d8b` completes the leg route). The pass
            # commands a stop and issues no route op of its own — success carries
            # no teardown (design_new.md "Speed calibration", A leg finishes
            # through ordinary stop completion).
            return CalibrationCommand(power_percent=0.0)

        # The leg had its authority and then lost it without the far block ever
        # confirming: ordinary stop completion tore the leg route down because
        # the train came to rest at its authority end short of the far block.
        # That is the same outcome as the step timing out, so resolve it the
        # same way.
        if run.leg_authority_seen and not obs.path_present:
            return self._resolve_incomplete_leg(run, obs)

        # The step timeout runs only once the leg's authority has appeared.
        if run.leg_started_at is None or obs.now - run.leg_started_at < CALIBRATION_STEP_TIMEOUT_S:
            return None
        return self._resolve_incomplete_leg(run, obs)

    def _resolve_incomplete_leg(
        self, run: CalibrationRun, obs: CalibrationObservation
    ) -> Optional[CalibrationCommand]:
        """Resolve a leg that ran out of authority or time without reaching the
        far block. Two different faults share this exit, and they are not the
        same thing."""
        if run.clock_started_at is None:
            # The locomotive never reached the measured block: the low steps may
            # not overcome its stiction at all. The flat bottom this leaves on
            # the curve is that locomotive's real stall band.
            return self._record_stall(run, obs)
        # It entered the measured block and never came out the far end, so it
        # stopped somewhere inside the stretch. A part-crossing is not a slow
        # measurement, it is no measurement, and timing it would put a speed on
        # the curve that the locomotive never held.
        return self.fail(
            run,
            f"train entered {run.measured_block_id} but never reached "
            f"{run.heading_toward_block_id}",
        )

    def _advance_stopping(
        self, run: CalibrationRun, obs: CalibrationObservation
    ) -> Optional[CalibrationCommand]:
        if not obs.is_stopped:
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
        """Average the step's crossings and either bank the sample or repeat.

        Returns a command when the pass is finished or failed, None when the
        caller should carry on to the next leg.
        """
        mean_speed = sum(run.crossings) / len(run.crossings)
        previous = run.samples[-1].speed_kmh if run.samples else 0.0
        run.crossings = []

        if mean_speed + _MONOTONIC_EPSILON_KMH < previous:
            # `power_speed_curve` must stay invertible, so samples have to rise
            # with power. One re-measure tells a noisy crossing apart from a
            # locomotive that really does run slower at more power.
            if run.step_remeasured:
                return self.fail(
                    run,
                    f"{run.current_power:.0f}% measured slower than a lower step twice",
                )
            run.step_remeasured = True
            return None

        run.samples.append(
            CalibrationSample(power_percent=run.current_power, speed_kmh=mean_speed)
        )
        run.step_remeasured = False
        run.step_cap_retries = 0
        run.step_index += 1
        if run.step_index >= len(run.power_steps):
            return self._finish_sweep(run)
        return None

    def _finish_sweep(
        self, run: CalibrationRun, *, deactivate_route: bool = False
    ) -> CalibrationCommand:
        """Close a sweep that has run every step.

        A sweep in which no step moved the locomotive is not a curve of a
        locomotive that cannot move — it is a stretch the pass could not measure
        on. Committing it would store a profile saying the locomotive stands
        still at full power, and every speed the app later asked of it would map
        back to that. So the pass reports the stretch instead.
        """
        if not any(s.speed_kmh > 0.0 for s in run.samples):
            return self.fail(
                run,
                "no step moved the train across the measured block — check the "
                "stretch is long enough for the consist and clear to run",
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
            reason=f"measured {len(run.samples)} points — pick a profile to write",
        )
        # A sweep reached here by ordinary stop completion (the final leg
        # reached the far block) needs no route op — its route already finished.
        # A sweep reached here from a last-step STALL still holds a RUNNING leg
        # route (the stall kept authority), so that caller passes
        # deactivate_route=True to tear it down.
        return CalibrationCommand(
            power_percent=0.0, deactivate_route=deactivate_route
        )

    def _record_stall(
        self, run: CalibrationRun, obs: CalibrationObservation
    ) -> Optional[CalibrationCommand]:
        """Bank a `0 km/h` sample and move on without turning the train around.

        The train never left the outer block it stood in, so the next step
        commands from where it stands, facing the way it already faces.
        """
        run.crossings = []
        run.step_remeasured = False
        run.step_cap_retries = 0
        run.samples.append(
            CalibrationSample(power_percent=run.current_power, speed_kmh=0.0)
        )
        run.step_index += 1
        if run.step_index >= len(run.power_steps):
            # A last-step stall still holds the RUNNING leg route (a stall keeps
            # authority), so tear it down here.
            return self._finish_sweep(run, deactivate_route=True)
        # The train is still standing where the leg began, facing the same way,
        # so the next step re-arms in place without turning around. The stalled
        # leg's route never reached the far block, so it is torn down.
        run.leg_armed = False
        run.needs_reverse = False
        return CalibrationCommand(power_percent=0.0, deactivate_route=True)

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
        rule", A read is not a handoff).
        """
        if not self.runs:
            return
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
                self._step0_entries[train_id] = tuple(sorted(entered))

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
        sc = self._sc
        run = self.runs.get(train_id)
        if sc is None or run is None:
            return None
        if not run.is_measuring:
            return None
        controller = self._controller(train_id)
        if controller is None:
            return None
        obs = CalibrationObservation(
            now=self._operating_time(),
            newly_confirmed=self._step0_entries.pop(train_id, ()),
            is_stopped=bool(controller.is_stopped()),
            head_block=self._head_block(controller),
            safe_state_generation=int(
                getattr(sc, "safe_state_generation", 0) or 0
            ),
            path_present=controller.path_projection.received_path_plan is not None,
        )
        return self.advance(run, obs)

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
        sc = self._sc
        controllers = getattr(sc, "controllers", None) if sc is not None else None
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
        sc = self._sc
        clock = getattr(sc, "operating_clock", None) if sc is not None else None
        if callable(clock):
            try:
                return float(clock())
            except Exception:
                return 0.0
        return 0.0

    # -- propulsion override ----------------------------------------------

    def get_propulsion_override(
        self, train_id: str
    ) -> Optional[CalibrationPropulsionOverride]:
        """The traction an open calibration pass drives with this tick, or None.

        design_new.md "Speed calibration" (Where it runs / Controller owns
        calibration output): during Action, `TrainController.tick(...)` reads
        this and drives the returned percent in place of the ordinary manual
        power source, under every ordinary safety cap (ATC, service-stop, EB,
        pending power-off, output safe state). It returns None when no pass may
        drive, so the controller keeps its ordinary source. This is the whole of
        how a pass drives the train: it queues no operator commands.

        An auto-profiling pass drives while it is measuring a leg — the current
        step's percent while that leg is armed, crossing, and has its authority,
        and `0` while the train is stopping at a leg's end, standing between
        steps, or still acquiring the leg's route. Until the leg's
        `received_path_plan` is present the pass selects zero power (design_new.md
        "Speed calibration", Plan acquires leg authority all-or-nothing): a leg
        without authority could not move anyway (the ATC limit is `0`), and
        holding the percent at `0` until the route's path exists keeps the step
        timeout and the driven power in step. A finished or failed pass returns
        None, handing the train back to its ordinary manual source (which the
        abandonment/finish paths zeroed).

        Lighting-voltage calibration is the other user of this accessor and
        returns the `track_voltage` variant of the union: while a lighting run is
        open, the controller drives its candidate as an exact voltage magnitude
        through `PhysicsCommand`, so neither the controller nor `TrainPhysics`
        reaches into the run registry (design_new.md "Lighting-voltage
        calibration", Where it lives). The two runs are mutually exclusive by
        their opening preconditions, so a speed pass is checked first and a
        lighting run only when none is measuring.
        """
        run = self.runs.get(train_id)
        if run is not None and run.is_measuring:
            driving = run.phase == PHASE_LEG and run.leg_armed
            if driving and not self._leg_authority_present(train_id):
                driving = False
            value = run.current_power if driving else 0.0
            return CalibrationPropulsionOverride(
                kind="power_percent", value=float(value)
            )
        lighting = self.lighting_runs.get(train_id)
        if lighting is not None:
            return CalibrationPropulsionOverride(
                kind="track_voltage", value=float(lighting.candidate_voltage)
            )
        return None

    def _stop_manual_traction(self, train_id: str) -> None:
        """Zero the controller's manual power percent as a pass ends.

        While a pass drives, the propulsion override supersedes the manual power
        source, so the pass never writes it. When the pass ends the override
        withdraws and the controller falls back to that source; zeroing it here
        is how a pass-abandonment or finish "commands `power_percent = 0`"
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
        """The `power_speed_curve` this pass measured, origin included."""
        return [(0.0, 0.0)] + [(s.power_percent, s.speed_kmh) for s in run.samples]

    def commit(
        self, train_id: str, target_profile, *, clear_profile=None
    ) -> List[Tuple[float, float]]:
        """Write the measured curve onto `target_profile` and close the run.

        Writes two fields and no others: `power_speed_curve` from the pass's
        samples and `calibrated_at` from the wall clock (design_new.md "Speed
        calibration", What commit writes). It must not write `voltage_power_curve`
        — **Lighting-voltage calibration** is the one procedure that moves that
        curve's anchor — and it must not write a motion rate, a notch table, or
        `model`.

        Which records arrive here is the operator's commit-target choice,
        resolved by the caller. `target_profile` is the record the two fields are
        written onto: the train's own instance profile for **Save to this
        locomotive**, or the model profile the instance derives from for
        **Promote to model**. On promote, `clear_profile` is that instance, whose
        two measured fields are cleared in the same commit so it falls back to
        following the freshly-written model.
        """
        run = self.runs.get(train_id)
        if run is None:
            raise ValueError("No calibration pass is open for this train")
        # Commit is all or nothing. A partly replaced curve would be part
        # measured and part seeded with nothing on the profile to say which part
        # is which, and no later reading of it could tell.
        if not run.is_measured:
            raise ValueError("Calibration pass has not measured every step")

        curve = self.measured_curve(run)
        speeds = [s for _p, s in curve]
        if any(b + _MONOTONIC_EPSILON_KMH < a for a, b in zip(speeds, speeds[1:])):
            raise ValueError(
                "Measured curve falls with power and could not be read backwards"
            )

        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        target_profile.power_speed_curve = curve
        target_profile.calibrated_at = stamp
        # Promote clears the instance's two measured fields so it goes back to
        # following the model curve just written.
        if clear_profile is not None and clear_profile is not target_profile:
            clear_profile.power_speed_curve = None
            clear_profile.calibrated_at = None
        self.runs.pop(train_id, None)
        self._step0_entries.pop(train_id, None)
        self._emit_command_result(
            train_id,
            operation="speed_calibration",
            success=True,
            reason=f"wrote {len(run.samples)} measured points to {target_profile.profile_id}",
        )
        return curve

    # -- lighting voltage -------------------------------------------------
    # design_new.md "Lighting-voltage calibration". No measurement step: the
    # pass puts a candidate voltage on the rails and the operator says whether
    # the locomotive moved. Nothing on the rig can answer that — at this level
    # the drive is too weak to load a current channel (that is what the
    # lighting zone is) and first movement is a creep of a few millimetres.

    def start_lighting(self, train, profile) -> LightingCalibrationRun:
        """Open a lighting-voltage run for this train, seeded from the profile.

        The caller (the service layer) enforces the state gates first.
        """
        ceiling = self.lighting_ceiling(profile, is_simulation=self._is_simulation())
        run = LightingCalibrationRun(
            train_id=train.train_id,
            profile_id=profile.profile_id,
            candidate_voltage=_clamp(
                float(getattr(profile, "lighting_voltage", 0.0)), 0.0, ceiling
            ),
            ceiling_voltage=ceiling,
        )
        self.lighting_runs[train.train_id] = run
        return run

    def _assigned_route_id(self, train_id: str) -> Optional[str]:
        """`RouteExecutor.assigned_route_id_for(train_id)`, or None when no route
        executor is wired (the state-machine unit tests drive with no runtime)."""
        sc = self._sc
        rex = getattr(sc, "route_executor", None) if sc is not None else None
        if rex is None:
            return None
        try:
            return rex.assigned_route_id_for(train_id)
        except Exception:
            return None

    def run_still_valid(self, train_id: str) -> bool:
        """Whether this train's open calibration run — a speed pass or a
        lighting-voltage run — still meets the conditions to stay open.

        The one validity predicate for both run kinds (design_new.md "Speed
        calibration", Where it runs). Action step `26f5`
        (`SimulationController._reevaluate_calibration_run`) evaluates it once per
        tick from live state; when it is false the run ends that same tick — a
        speed pass through `abandon(train_id, reason)`, a lighting run through
        `cancel_lighting(train_id)`. A run ends the moment its condition is met,
        not only on Save, Stop, or panel-close.

        Returns True when no run is open (nothing to end) and when a speed pass
        has measured every step — a finished result is committable and is never
        re-evaluated (design_new.md "Speed calibration", a measured pass is not
        evaluated). Otherwise it is false when any of these holds:
          - the runtime or controller is gone
          - the train is powered off — `not controller.powered`
            (`train_status == OFF`)
          - `eb.active`
          - a power-off is pending — `phase_latches.power_off`
          - for a lighting run: `control_mode != "manual"`, or any route is
            assigned (a lighting run has no reservation)
          - for a speed pass: a non-calibration route is assigned — the
            assigned route is neither `None` nor the leg's own
            `active_leg_route_id`. `None` is the between-legs value and the
            value on the tick a finished leg's route has been unassigned while
            `active_leg_route_id` is not yet cleared, so it does not end the pass
        There is no cab condition for either kind — the head end is derived from
        the authority (a speed pass) or the candidate drives positive (a lighting
        run).
        """
        speed = self.runs.get(train_id)
        lighting = self.lighting_runs.get(train_id)
        if speed is None and lighting is None:
            return True
        # A finished speed result is committable and is never invalidated.
        if speed is not None and speed.is_measured:
            return True
        controller = self._controller(train_id)
        if controller is None:
            return False
        # Conditions shared by both run kinds. A calibration run is valid only
        # while the train is powered; `train_status != OFF` is the sole
        # power gate (design_new.md "Speed calibration", run_still_valid).
        if not controller.powered:
            return False
        if controller.eb.active:
            return False
        if getattr(controller.route_hooks.phase_latches, "power_off", False):
            return False
        assigned = self._assigned_route_id(train_id)
        if lighting is not None:
            if getattr(controller.commands, "control_mode", None) != "manual":
                return False
            return assigned is None
        # A speed pass. The pass ends only when a non-calibration route is
        # assigned — neither `None` nor the leg's own one-off route. `None` is
        # the between-legs value, and also the value on the tick a finished
        # leg's route has been unassigned by Plan settlement while
        # `active_leg_route_id` is not yet cleared (that clear runs on the next
        # `_prepare_calibration_leg`), so the pass survives the leg boundary
        # rather than abandoning on it. A measurement fault ends the pass
        # immediately in Plan through SpeedCalibration.fail(...), which pops the
        # run, so run_still_valid never sees a PHASE_FAILED run here.
        return assigned is None or assigned == speed.active_leg_route_id

    def step_lighting(self, train_id: str, direction: int) -> Optional[LightingCalibrationRun]:
        """Move the candidate one `LIGHTING_VOLTAGE_STEP_V` up or down.

        The step control stops at the ceiling and at `0`, so the panel can
        never show a value `commit_lighting` would refuse.
        """
        run = self.lighting_runs.get(train_id)
        if run is None:
            return None
        sign = 1.0 if int(direction) >= 0 else -1.0
        run.candidate_voltage = _clamp(
            run.candidate_voltage + sign * LIGHTING_VOLTAGE_STEP_V,
            0.0,
            run.ceiling_voltage,
        )
        return run

    def commit_lighting(self, train_id: str, profile) -> Optional[float]:
        """Write the candidate to `lighting_voltage` on `profile` and close the run.

        `profile` is the record the train names — its instance profile in the
        ordinary case (design_new.md "Lighting-voltage calibration", What Save
        writes). `lighting_voltage` is also the 0%-power point of
        `voltage_power_curve`, so the anchor of the curve this train resolves
        moves with the new value. An instance profile stores no
        `voltage_power_curve` of its own, so the move shows up only in the curve
        it resolves and the model profile's stored curve is untouched; a model
        profile, which does store the curve, has its bottom anchor moved here,
        dropping any point at or below the new anchor.
        """
        run = self.lighting_runs.get(train_id)
        if run is None:
            return None
        value = _clamp(run.candidate_voltage, 0.0, run.ceiling_voltage)
        profile.lighting_voltage = value
        if getattr(profile, "derived_from", None) is None:
            profile.voltage_power_curve = self._reanchored_curve(profile, value)
        self.lighting_runs.pop(train_id, None)
        self._emit_command_result(
            train_id,
            operation="lighting_calibration",
            success=True,
            reason=f"lighting voltage set to {value:.2f} V",
        )
        return value

    def cancel_lighting(self, train_id: str) -> None:
        """Close the run without writing. The candidate is discarded and the
        train's `track_voltage` goes back to what its state ordinarily outputs."""
        self.lighting_runs.pop(train_id, None)

    def lighting_candidate_voltage(self, train_id: str) -> Optional[float]:
        """The open run's candidate voltage, or None when no run is open.

        The panel and service layer read this to show the candidate as the
        operator steps it. The drive path no longer goes through here: while a
        run is open the controller reads `get_propulsion_override(...)` and hands
        the magnitude to physics on the command (design_new.md "Lighting-voltage
        calibration", Where it lives).
        """
        run = self.lighting_runs.get(train_id)
        return None if run is None else run.candidate_voltage

    @staticmethod
    def lighting_ceiling(profile, is_simulation: bool = False) -> float:
        """The highest candidate a run may reach.

        A candidate stays below the profile's `max_voltage`. In Virtual Hardware
        and Real Hardware it also stays below the Hardware Setup's
        `current_sense_min_drive_v`, because design_new.md "Power" requires that
        level to sit above the profile's `lighting_voltage` so a lit but
        motionless train lands in the lighting zone instead of reading as driven.

        In Simulation (`is_simulation=True`) the `current_sense_min_drive_v`
        bound is NOT evaluated, because Simulation never reads Hardware Setup
        (design_new.md, "Simulation is independent of Hardware Setup"); a
        candidate saved there is bounded only by `max_voltage`. The readiness
        evaluator under "Occupancy source follows drive state" re-checks the
        stored value against `current_sense_min_drive_v` at hardware activation,
        so a Simulation candidate that would fail the hardware bound is caught
        then rather than over-capped at save time.
        """
        hi = float(getattr(profile, "max_voltage", 12.0))
        if not is_simulation:
            try:
                from hardware_config import get_hardware_config

                hi = min(hi, float(get_hardware_config().current_sense_min_drive_v))
            except Exception:
                pass
        return max(0.0, round(hi - LIGHTING_VOLTAGE_STEP_V, 3))

    def _is_simulation(self) -> bool:
        """True only when the active environment is Simulation, so lighting
        calibration skips the Hardware-Setup `current_sense_min_drive_v` bound.
        Unknown env (no simulation controller) keeps the bound conservatively."""
        from enums import OperationMode

        return getattr(self._sc, "operation_mode", None) is OperationMode.SIMULATION

    @staticmethod
    def _reanchored_curve(profile, anchor: float) -> List[Tuple[float, float]]:
        """`voltage_power_curve` with its 0%-power point moved to `anchor`.

        A point is dropped when it sits at or below the new anchor (it names a
        voltage that does not move this locomotive) and when it carries 0%
        power (it is the old anchor). The second test is what keeps the curve
        to exactly one 0% point when the anchor moves *down*: the old anchor is
        above the new one, so the first test alone would leave it in place.
        """
        kept = [
            (float(v), float(p))
            for v, p in getattr(profile, "voltage_power_curve", []) or []
            if float(v) > anchor and float(p) > 0.0
        ]
        if not kept:
            kept = [(float(getattr(profile, "max_voltage", 12.0)), 100.0)]
        return [(anchor, 0.0)] + sorted(kept)

    # -- reporting --------------------------------------------------------

    def _emit_command_result(
        self, train_id: str, *, operation: str, success: bool, reason: str
    ) -> None:
        """One command-result event on the stream `NotificationCenter` owns.

        Both calibration procedures report here; neither opens an event sink of
        its own (design_new.md "Speed calibration" / "Lighting-voltage
        calibration").
        """
        sc = self._sc
        nc = getattr(sc, "notification_center", None) if sc is not None else None
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
