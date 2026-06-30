"""
Train Controller - Unified ATC + ATO + Command Handling

Definition:
This module provides the TrainController class which is the single authority for
train operational state and control decisions. It merges the functionality of:

- OnboardComputer: Power, cab, mode, handle state, command validation
- TrainATC: Clearance calculation, speed limits, braking curves

TrainController is the "cab computer" that:
- Controls power on/off and emergency stop
- Stores the current control mode (ATO vs Manual)
- Knows which cab is active and the travel direction
- Manages the throttle/brake handle position
- Calculates safe speed limits based on clearance ahead (ATC)
- In ATO mode, automatically drives toward destination
- Validates whether commands should execute
- Reports current operational state to displays

Architecture:
    SimulationController (Orchestrator / Clock)
            |
            v
    +-----------------+     +---------------+
    | TrainController |---->| TrainPhysics  |
    | (Control Logic) |     | (Movement)    |
    +-----------------+     +---------------+
            |                      |
            v                      v
        +-------+           +-------------+
        | Train |           | BlockManager|
        | (Data)|           +-------------+
        +-------+

Design Principles:
1. Single Authority: TrainController owns all train control state
2. Combined ATC+ATO: One class handles both safety limits AND automatic driving
3. Command Validation: All commands go through TrainController
4. Read-Only UI: Control panels read from TrainController, never compute locally
5. Separation from Physics: TrainController decides WHAT speed; TrainPhysics executes
"""

import logging
import math
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Tuple

from constants import (
    CAUTION_SPEED_LIMIT_KMH,
    PLATFORM_SAFETY_MARGIN_CM,
    STOPPED_SPEED_THRESHOLD_KMH,
    TRACK_END_SAFETY_MARGIN_CM,
)
from enums import AtcLimitReasonCode, BlockState, ControllerCondition, Direction, HandlePosition, SignalState, TrainState
from model_scale import get_speed_conversion_factor
from runtime_payloads import (
    Command,
    ControllerProgressReport,
    PhaseLatches,
    PhysicsSpanUpdate,
    ResourceEvent,
    TileTraversal,
)

if TYPE_CHECKING:
    from network import RailwayNetwork
    from path import Path
    from path_dispatcher import PathDispatcher
    from route_executor import RouteExecutor
    from speed_profile import SpeedProfile
    from train import Train


logger = logging.getLogger(__name__)

OPPOSITE_EDGES = {"N": "S", "S": "N", "E": "W", "W": "E"}
EDGE_TO_DIRECTION_NAME = {"N": "NORTH", "E": "EAST", "S": "SOUTH", "W": "WEST"}

# When `ATCS_LOOKAHEAD_SHADOW=1`, every lookahead memo HIT recomputes the payload
# fresh and asserts it is byte-identical to the cached one (raising on mismatch).
# This is the correctness gate for the memo: run the demo + test suite with it on
# to prove the memo key captures every input the computation reads. Off (the
# default) in production — the recompute would defeat the memo's purpose.
_LOOKAHEAD_SHADOW = os.environ.get("ATCS_LOOKAHEAD_SHADOW") == "1"


# `TRACK_END_SAFETY_MARGIN_CM`, `PLATFORM_SAFETY_MARGIN_CM`, and
# `CAUTION_SPEED_LIMIT_KMH` are imported from `constants.py` (the canonical
# home per design_new.md "Safety margin globals" and "CAUTION speed cap").
# The remaining tunables are local because they have no consumer outside
# the controller.
ATC_SIGNAL_LOOKAHEAD_BLOCKS = 3
ATC_MIN_CLEARANCE_CM = 10.0
ATC_NO_BLOCK_FALLBACK_CM = 100000.0

SPEED_TREND_EPSILON = 0.1

# Speed curve constants
CURVE_SAMPLE_RESOLUTION_CM = (
    3.0  # 3cm resolution for speed curves (balance between precision and performance)
)
ATO_SPEED_MARGIN_KMH = 3.0  # ATO stays this far km/h below ATC limit

# ATC overspeed escalation thresholds (km/h above the ATC limit). Named here
# (not inline literals) so they are tunable via the Advanced settings panel
# through `runtime_config`. EB escalation at `> OVERSPEED_EB_THRESHOLD_KMH`;
# a diagnostic warning at `> OVERSPEED_WARN_THRESHOLD_KMH`.
OVERSPEED_EB_THRESHOLD_KMH = 10.0
OVERSPEED_WARN_THRESHOLD_KMH = 5.0


# =============================================================================
# Data Structures for Lookahead and ATO
# =============================================================================


# `LookaheadPoint` and `ATOInfo` live in `train_controller_types` so the
# helper modules (`lookahead_builder`, `atc_calculator`, `ato_navigator`)
# can import them without a circular dep on this file. They are re-exported
# at this module path so other parts of the system keep a single import home.
from train_controller_types import (  # noqa: E402,F401
    ATOInfo,
    LookaheadPoint,
    PhysicsCommand,
    PHYSICS_KIND_EMERGENCY_BRAKE,
    PHYSICS_KIND_HANDLE,
    PHYSICS_KIND_TARGET_SPEED,
)


# =============================================================================
# Speed Calculation Helpers
# =============================================================================
#
# The two pure braking-curve helpers and the `SpeedCurveCalculator` (which
# owns what `TrainController` used to inline as `calculate_speed_curves`) live
# in `train_controller.speed_curves`. They are re-exported here so existing
# importers keep their single home: `atc.py` does
# `from train_controller import calculate_max_safe_speed`, and the curve
# builder's internal calls resolve `calculate_max_safe_speed` /
# `calculate_speed_for_target` in their own module.
from .speed_curves import (  # noqa: E402
    SpeedCurveCalculator,
    calculate_max_safe_speed,
    calculate_speed_for_target,
)


@dataclass
class TrainController:
    """
    Combined ATC + ATO + command handling for one train.

    TrainController is the single authority for:
    - Power state and emergency stop
    - Active cab selection
    - Throttle/brake handle position
    - Control mode (ATO/Manual)
    - Travel direction
    - ATC speed limit calculation
    - ATO automatic driving decisions
    - Command validation

    It does NOT own:
    - Train position (owned by Train, updated by TrainPhysics)
    - Train current_speed (owned by Train, updated by TrainPhysics)
    - Block state (owned by BlockManager, updated by TrainPhysics)
    - Simulation time (owned by SimulationController)
    """

    train_id: str
    network: Optional["RailwayNetwork"] = None
    path_dispatcher: Optional["PathDispatcher"] = None
    route_executor: Optional["RouteExecutor"] = None

    # -------------------------------------------------------------------------
    # Power & Safety State
    # -------------------------------------------------------------------------
    # `power_on` is derived from `state != OFF` per design_new.md. It is
    # exposed as a @property defined later in the class.
    #
    # EB latch and operator-EB activate / release live on the
    # `EBWorkflow` sub-module attached as `self.eb` in `__post_init__`.
    # External reads are `controller.eb.active`; operator calls are
    # `controller.eb.activate()` / `controller.eb.release()`.

    # -------------------------------------------------------------------------
    # Notifications (for frontend toast messages via API)
    # -------------------------------------------------------------------------
    #
    # Toast queue lives on the `Notifications` sub-module attached as
    # `self.notifications` in `__post_init__`. External callers read via
    # `controller.notifications.pending` or drain via
    # `controller.notifications.get_and_clear()`.

    # -------------------------------------------------------------------------
    # Cab & Mode State
    # -------------------------------------------------------------------------
    # `cab` and `control_mode` live on the `OperatorCommands` sub-module
    # at `self.commands`. External readers use `controller.commands.cab`
    # and `controller.commands.control_mode`.
    # `manual_mode_type` lives on the `ManualDriving` sub-module at
    # `self.manual` in `__post_init__`. External readers use
    # `controller.manual.manual_mode_type`.

    # -------------------------------------------------------------------------
    # Operational State
    # -------------------------------------------------------------------------
    # `state` and `handle_position` live on the `OperationalStateMachine`
    # sub-module at `self.ops`. External readers use `controller.ops.state`
    # and `controller.ops.handle_position`.
    # `manual_power_percent` lives on the `ManualDriving` sub-module at
    # `self.manual` in `__post_init__`. External readers use
    # `controller.manual.manual_power_percent`.

    # -------------------------------------------------------------------------
    # ATO State
    # -------------------------------------------------------------------------
    #
    # ATO run lifecycle (`running`), the destination snapshot (`info`),
    # the per-tick debug payload (`debug`), and the diagnostic
    # `_previous_speed` tracker live on the `ATOComputer` sub-module
    # attached as `self.ato` in `__post_init__`. External callers read
    # `controller.ato.running`, `controller.ato.info`,
    # `controller.ato.debug`; the run-lifecycle methods are
    # `controller.ato.go()` and `controller.ato.stop()`.
    #
    # `path_backed_run_active` was a stored field in earlier code; per the
    # design's "Latch Table" canonical home it is not part of the schema. It
    # is now a derived property (see below) that reads
    # `_has_path_backed_run_context(...)` live on every access. Kept on the
    # class as a `@property` so existing readers continue to work.

    # -------------------------------------------------------------------------
    # ATC State (calculated each tick)
    # -------------------------------------------------------------------------
    #
    # ATC caps, the cached reason code, and the safety-margin / signal-
    # lookahead tunables live on the ATC Computer sub-module attached as
    # `self.atc` in `__post_init__`. External callers read via
    # `controller.atc.speed_limit`, `controller.atc.clearance_ahead_cm`,
    # `controller.atc.current_block_speed_limit`,
    # `controller.atc.safety_margin_cm`,
    # `controller.atc.signal_lookahead_blocks`. Note: target_speed and
    # speed_limit_reason remain derived from `self.lookahead[0]` per
    # design_new.md; they are not stored.

    # -------------------------------------------------------------------------
    # Handle reason (will move to ops sub-module in a later PR)
    # -------------------------------------------------------------------------
    # `handle_reason` lives on the `OperationalStateMachine` sub-module
    # at `self.ops`. External readers use `controller.ops.handle_reason`.

    # -------------------------------------------------------------------------
    # Phase 3 Mailbox (per design_new.md)
    # -------------------------------------------------------------------------
    #
    # `queued_progress_reports` lives on the `PhysicsSync` sub-module
    # attached as `self.physics_sync` in `__post_init__`. External
    # callers read via `controller.physics_sync.queued_progress_reports`.
    #
    # The four Phase-3 latches (`path_cancel_request`, `route_abort`,
    # `power_off`, `route_deactivate`) are bundled into `PhaseLatches` and
    # live on the `RouteHooks` sub-module attached as `self.route_hooks` in
    # `__post_init__`. External callers reach them through
    # `controller.route_hooks.phase_latches`. Writers go through the
    # `set_*` helpers on that aggregate; consumers (`simulation.py`) call
    # the matching `consume_*()` methods. See runtime_payloads.PhaseLatches.

    # -------------------------------------------------------------------------
    # Operator Command Inbox (per design_new.md "Command queue + drain")
    # -------------------------------------------------------------------------
    # Operator commands enter the controller via the public command methods
    # (power_on_cmd, set_cab, set_mode, etc.). Each command is enqueued into
    # `command_inbox` and then drained by `_drain_command_inbox(...)` at the
    # very top of `tick(...)`. Commands are processed in the canonical kind
    # order (power_on -> set_mode -> set_cab -> set_manual_mode_type ->
    # set_handle/set_manual_power_percent -> activate_emergency_cmd ->
    # release_emergency_cmd -> power_off), with FIFO order within each kind
    # bucket.
    # `command_inbox` and the canonical drain order live on the
    # `OperatorCommands` sub-module at `self.commands`. External readers
    # use `controller.commands.command_inbox` and
    # `controller.commands.drain_order`.

    # -------------------------------------------------------------------------
    # Controller-owned live span / speed (per design_new.md)
    # -------------------------------------------------------------------------
    #
    # `current_span` (canonical TrainSpan storage) and `current_speed_kmh`
    # (canonical live speed) live on the `PhysicsSync` sub-module attached
    # as `self.physics_sync` in `__post_init__`. The orchestrator
    # `on_physics_updated(...)` (still flat on the façade) writes them
    # through `self.physics_sync.current_span = ...` etc. External readers
    # use `controller.physics_sync.current_span` and
    # `controller.physics_sync.current_speed_kmh`.

    # -------------------------------------------------------------------------
    # Helper modules (lookahead / ATC / ATO computation)
    # -------------------------------------------------------------------------
    #
    # These hold a back-reference to `self` and own the actual logic that the
    # controller used to inline. Lazy-initialised so the dataclass doesn't
    # need a custom `__post_init__`.
    _lookahead_builder: Any = field(default=None, repr=False, compare=False)
    _speed_curve_calculator: Any = field(default=None, repr=False, compare=False)

    # -------------------------------------------------------------------------
    # Sub-modules (created in __post_init__)
    # -------------------------------------------------------------------------
    #
    # Each sub-module owns its own fields and exposes its own methods.
    # External callers reach state through the sub-module namespace
    # (e.g. `controller.eb.active`, `controller.eb.activate()`).
    eb: Any = field(init=False, default=None, repr=False, compare=False)
    notifications: Any = field(init=False, default=None, repr=False, compare=False)
    atc: Any = field(init=False, default=None, repr=False, compare=False)
    physics_sync: Any = field(init=False, default=None, repr=False, compare=False)
    ato: Any = field(init=False, default=None, repr=False, compare=False)
    path_projection: Any = field(init=False, default=None, repr=False, compare=False)
    route_hooks: Any = field(init=False, default=None, repr=False, compare=False)
    manual: Any = field(init=False, default=None, repr=False, compare=False)
    commands: Any = field(init=False, default=None, repr=False, compare=False)
    ops: Any = field(init=False, default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Instantiate sub-modules that own their own state.

        Dataclass-style ``field(init=False, default=None)`` declares the
        attribute; ``__post_init__`` replaces the sentinel with the real
        sub-module instance, which holds its own state and a
        back-reference to this controller.
        """
        from train_controller.atc import ATCComputer
        from train_controller.ato import ATOComputer
        from train_controller.commands import OperatorCommands
        from train_controller.eb import EBWorkflow
        from train_controller.manual import ManualDriving
        from train_controller.notifications import Notifications
        from train_controller.ops import OperationalStateMachine
        from train_controller.path_projection import PathProjection
        from train_controller.physics_sync import PhysicsSync
        from train_controller.progress_report import ProgressReportBuilder
        from train_controller.route_hooks import RouteHooks

        self.eb = EBWorkflow(self)
        self.notifications = Notifications(self)
        self.atc = ATCComputer(self)
        self.physics_sync = PhysicsSync(self)
        self.ato = ATOComputer(self)
        self.path_projection = PathProjection(self)
        self.route_hooks = RouteHooks(self)
        self.manual = ManualDriving(self)
        self.commands = OperatorCommands(self)
        self.ops = OperationalStateMachine(self)
        self.progress_report = ProgressReportBuilder(self)

        # Per-tick memo for `_get_effective_path_tiles()` — see that method.
        # Keyed on the active path's tile-list content so it self-invalidates
        # when the dispatcher reroutes / extends / truncates the path.
        self._effective_path_tiles_cache = None

        # Tick-local memo for `_compute_canonical_lookahead()` — see that method
        # and the wrapper just above its `_impl`. Stores `(key, payload)`.
        self._lookahead_memo = None

    def _get_lookahead_builder(self):
        if self._lookahead_builder is None:
            from lookahead_builder import LookaheadBuilder

            self._lookahead_builder = LookaheadBuilder(self)
        return self._lookahead_builder

    def _get_speed_curve_calculator(self):
        if self._speed_curve_calculator is None:
            self._speed_curve_calculator = SpeedCurveCalculator(self)
        return self._speed_curve_calculator

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    def _get_train(self) -> Optional["Train"]:
        """Get the Train entity this controller controls."""
        if self.network is None:
            return None
        return self.network.trains.get(self.train_id)

    def _get_speed_profile(self) -> "SpeedProfile":
        """Get the SpeedProfile for this train."""
        from speed_profile import DEFAULT_SPEED_PROFILE

        train = self._get_train()
        if not train or not self.network:
            return DEFAULT_SPEED_PROFILE

        profile_id = train.speed_profile_id
        if profile_id and hasattr(self.network, "speed_profiles"):
            profile = self.network.speed_profiles.get(profile_id)
            if profile:
                return profile

        # No assigned/resolvable profile: anchor the default power curve to THIS
        # train's max_speed instead of the module-global DEFAULT_SPEED_PROFILE
        # (which maps 100% -> 100 km/h regardless of the train). Covers trains
        # created by paths that do not seed a profile (from_dict load, step
        # replay, editor add-without-place). The memoized fallback lives in one
        # shared home so the controller and physics read paths cannot drift.
        from speed_profile import resolve_memoized_default_profile

        return resolve_memoized_default_profile(self, train)

    # -------------------------------------------------------------------------
    # Derived properties (per design_new.md)
    #
    # These are read-only properties; nothing in the controller writes them.
    # They recompute from the controller's storage (cab, current_span,
    # received_path_plan, etc.) on every read.
    # -------------------------------------------------------------------------

    # `received_path_plan`, `assigned_path_id`, `ato_path_id`,
    # `ato_destination_block_id`, `path_backed_run_active`, and
    # `get_live_path_data()` live on the `PathProjection` sub-module at
    # `self.path_projection`. External callers use
    # `controller.path_projection.<name>` directly.

    @property
    def state(self) -> TrainState:
        """Operational state, owned by the `ops` sub-state-machine.

        Exposed at the controller level so external readers (e.g.
        `TrainPhysics.compute_track_voltage`) and test doubles can read
        `controller.state` uniformly. The canonical store remains
        `self.ops.state`; this is a read-only projection of it.
        """
        return self.ops.state

    @property
    def travel_direction(self) -> Optional[str]:
        """Read-only derived value computed each read from (self.commands.cab, self.physics_sync.current_span).

        Per design_new.md: the cab's physical end is the head, and travel_direction
        is the network-frame direction pointing from the opposite physical end
        through the cab end and onward. When no next span resource exists ahead,
        it is the direction outward through the cab edge of the cab end's tile.
        Returns None when no cab is selected.

        Resolution order:
        1. From `current_span` (canonical) when wired — the direction from the
           tail-side end to the head-side end (head derived from the cab via
           `_head_span_locator`), or for single-tile spans the outward
           direction through the head's exit edge.
        2. None when there is no live span or no cab is selected.
        """
        if self.commands.cab is None:
            return None

        # Canonical path: derive the head/tail side from (cab, current_span)
        # and read the network-frame direction from tail tile -> head tile.
        head = self._head_span_locator()
        if head is not None:
            tail = self._tail_span_locator()
            head_tile = getattr(head, "tile", None)
            tail_tile = getattr(tail, "tile", None) if tail is not None else None
            if head_tile is not None and tail_tile is not None and tuple(head_tile) != tuple(tail_tile):
                direction = self._direction_from_tiles(tail_tile, head_tile)
                if isinstance(direction, Direction):
                    return self._direction_to_edge(direction) or direction.name
            head_entry = getattr(head, "entry_edge", None)
            if isinstance(head_entry, str) and head_entry:
                # Single-tile span (or no tail): outward direction is the
                # opposite of the entry edge through the head.
                return OPPOSITE_EDGES.get(head_entry.upper(), head_entry)

        # No usable span (no live span yet): no travel direction.
        return None

    @property
    def lookahead(self) -> List["LookaheadPoint"]:
        """Pure derived value: recomputes on every read.

        Inputs: (current_span, cab, received_path_plan, network_state, signals,
        current_speed_kmh, eb_active). No caching; no stored field; no
        invalidation rule. Per design_new.md, the canonical lookahead payload
        is exported via `get_lookahead()`.
        """
        return self._calculate_lookahead()

    @property
    def target_speed(self) -> float:
        """Live read: `operational_limit` at distance 0 from the canonical
        lookahead payload (per design_new.md ~line 161:
        "Target speed and speed-limit reason MUST NOT be stored as
        standalone fields anywhere; they are the value and reason metadata
        at position 0 of the lookahead series").

        Returns `0.0` when the lookahead's `speed_curves` is empty (no cab,
        no usable authority, etc.). There is no fallback to a stored field.
        """
        payload = self._compute_canonical_lookahead()
        curves = (payload or {}).get("lookahead", {}).get("speed_curves") or []
        if not curves:
            return 0.0
        value = curves[0].get("operational_limit")
        if value is None:
            return 0.0
        speed = float(value)
        if speed == float("inf"):
            return 0.0
        return speed

    @property
    def speed_limit_reason(self) -> Optional[str]:
        """Live read of the canonical ATC speed-limit reason at distance 0.

        Per design_new.md ~line 161, the canonical source is
        `lookahead.speed_curves[0].atc_limit_reason_detail` (with
        `atc_limit_reason_code` as the structured tag). There is no
        controller-owned cache; the reason is recomputed from the live
        lookahead on every read.
        """
        payload = self._compute_canonical_lookahead()
        curves = (payload or {}).get("lookahead", {}).get("speed_curves") or []
        if not curves:
            return None
        detail = curves[0].get("atc_limit_reason_detail")
        if detail:
            return detail
        code = curves[0].get("atc_limit_reason_code")
        if code:
            return str(code)
        return None

    # -------------------------------------------------------------------------
    # Tick Method (Called Each Simulation Tick)
    # -------------------------------------------------------------------------

    def tick(self, delta: float) -> None:
        """
        Called each simulation tick by SimulationController.

        Workflow:
        0. Drain operator command inbox (per design_new.md)
        0a. Power check
        1. Emergency brake (EB) check
        2. Lookahead calculation (shared)
        3. ATC info collection
        4. ATC action (warning/EB on overspeed)
        5. ATO info
        6. ATO action (cab correction, handle calculation)
        7. Effective limit & target speed
        8. State transitions

        Does NOT move the train - that's TrainPhysics.tick().

        Args:
            delta: Time elapsed since last tick (seconds)
        """
        # ===== 0. DRAIN OPERATOR COMMAND INBOX =====
        # Process any commands enqueued by power_on_cmd / set_cab / etc. in the
        # canonical drain order before running the rest of the tick. Without
        # this, queued commands would never take effect.
        try:
            self.commands._drain_command_inbox(self.commands._estimate_event_time())
        except Exception as exc:
            # Defensive: do not let a single bad command crash the tick.
            # Surface via NotificationCenter so operators see the failure;
            # fall back to logger only if the canonical sink is itself broken.
            import traceback as _tb

            event_time = self.commands._estimate_event_time()
            # Reach the NotificationCenter through the controller's own
            # `simulation_controller` back-reference (wired by the runtime in
            # SimulationController._get_or_create_controller); fully guarded so
            # this error handler never raises a second error.
            sc = getattr(self, "simulation_controller", None)
            nc = (
                getattr(sc, "notification_center", None) if sc is not None else None
            )
            emitted = False
            if nc is not None:
                try:
                    from notification_center import Severity as _Severity

                    nc.emit(
                        source="TrainController",
                        kind="command_drain_failed",
                        severity=_Severity.ERROR,
                        event_time=event_time,
                        train_id=self.train_id,
                        payload={
                            "reason": str(exc),
                            "traceback": _tb.format_exc(),
                        },
                    )
                    emitted = True
                except Exception:
                    pass
            if not emitted:
                logger.exception(
                    "command-drain raised for train %s; continuing tick "
                    "(NotificationCenter unavailable)",
                    self.train_id,
                )

        train = self._get_train()
        if not train:
            return

        logger.debug(
            "[TrainController %s] tick start: power=%s eb=%s mode=%s state=%s speed=%.2f",
            self.train_id,
            self.power_on,
            self.eb.active,
            self.commands.control_mode,
            self.ops.state.value,
            self.physics_sync.current_speed_kmh,
        )

        # ===== 0. POWER CHECK =====
        if not self.power_on:
            self.ops.state = TrainState.OFF
            logger.debug(
                "[TrainController %s] power off -> state=off",
                self.train_id,
            )
            return

        # ===== 1. EMERGENCY BRAKE CHECK =====
        if self.eb.active:
            self.ops.handle_position = HandlePosition.EB
            self.atc.speed_limit = 0.0

            logger.debug(
                "[TrainController %s] EB active -> handle=EB speed=%.2f",
                self.train_id,
                self.physics_sync.current_speed_kmh,
            )

            # While EB is latched, controller does not run post-EB restart
            # logic. Explicit release_emergency_cmd() is required to clear
            # eb_active. This early-exit holds until the operator releases.
            return

        # Per design_new.md tick step `293e`, the reactive speed-based state
        # evaluation is a no-op in `tick()` — it now fires synchronously
        # inside `on_physics_updated()` after the new speed is written.

        # ===== 2. LOOKAHEAD (computed live; no caching) =====
        # Per design_new.md, lookahead is a pure derived value. We trigger one
        # local computation here so the ATC info collection below uses the same
        # live values; downstream consumers re-read it via the property.
        live_lookahead = self._calculate_lookahead()

        # ===== 3. ATC INFO COLLECTION =====
        if self.commands.cab is None:
            self.atc.speed_limit = 0.0
            self.atc._last_limit_reason_code = AtcLimitReasonCode.NO_CAB
            logger.debug(
                "[TrainController %s] ATC blocked: no cab selected",
                self.train_id,
            )
        elif self._is_cab_wrong_direction():
            self.atc.speed_limit = 0.0
            self.atc._last_limit_reason_code = AtcLimitReasonCode.WRONG_DIRECTION_CAB
            logger.debug(
                "[TrainController %s] ATC blocked: cab opposite active authority",
                self.train_id,
            )
        else:
            # Get block speed limit for current block
            self.atc.current_block_speed_limit = self._get_current_block_speed_limit()

            # `clearance_ahead_cm` is still published for diagnostics and as
            # the no-curve fallback inside `_distance_zero_atc_anchor()`;
            # refresh it first so that fallback reads a current value.
            self.atc.clearance_ahead_cm = self.atc.clearance_ahead()
            # Publish the ATC limit + reason from the canonical lookahead's
            # distance-0 anchor (single source of truth). Per design_new.md,
            # lookahead is the pure derived authority value: its `speed_curves`
            # walk the path's tile geometry, so they measure real forward
            # clearance even where a block's `length_cm` metadata is 0. The
            # older block-walk clearance (`clearance_ahead()`, which sums
            # `length_cm`) under-counted authority through zero-length blocks
            # and published a phantom-low limit that braked the train and
            # tripped a spurious overspeed EB; the curve anchor has no such
            # failure mode, and its `atc_limit_reason_code` is the canonical
            # reason the monitor/journey-harness already read.
            self.atc.speed_limit, self.atc._last_limit_reason_code = (
                self._distance_zero_atc_anchor()
            )
            logger.debug(
                "[TrainController %s] ATC: clearance=%.2f limit=%.2f",
                self.train_id,
                self.atc.clearance_ahead_cm,
                self.atc.speed_limit,
            )

        # ===== 4. ATC ACTION =====
        overspeed = self.physics_sync.current_speed_kmh - self.atc.speed_limit

        # Per design_new.md step `826a`: a rolling train with no path authority
        # has nothing to brake against, so EB — not a service-brake notch — is
        # the only stopping protocol. With no authority ATC sets atc_limit = 0,
        # so any motion is overspeed at distance 0; we escalate here instead of
        # letting the ATO/manual branches below pick a service notch (which
        # would override the operator's handle with a confusing brake step).
        no_authority_rolling = (
            self.path_projection.received_path_plan is None
            and not self.is_stopped()
        )

        if overspeed > OVERSPEED_EB_THRESHOLD_KMH or no_authority_rolling:
            # Emergency brake. Per design_new.md the EB controller-local
            # effects are: set eb_active = True, force handle to EB, clear
            # ato_running, and (when an active path exists) write
            # pending_path_cancel_request(EB).
            self.eb.active = True
            self.ops.handle_position = HandlePosition.EB
            self.ato.running = False
            if self.path_projection.received_path_plan is not None:
                self.route_hooks.phase_latches.set_path_cancel_request(
                    "eb", "ATC overspeed escalation"
                )
            self.notifications.emit(
                "ATC triggered emergency brake: overspeed"
                if overspeed > OVERSPEED_EB_THRESHOLD_KMH
                else "No movement authority: emergency brake"
            )
            logger.debug(
                "[TrainController %s] ATC EB: overspeed=%.2f no_authority_rolling=%s",
                self.train_id,
                overspeed,
                no_authority_rolling,
            )
            return
        elif overspeed > OVERSPEED_WARN_THRESHOLD_KMH:
            logger.debug(
                "[TrainController %s] ATC warning: overspeed=%.2f",
                self.train_id,
                overspeed,
            )

        # ===== 5. ATO INFO =====
        self.ato.info = self.ato.compute_info()

        # ===== 6. ATO ACTION (skip if manual) =====
        # Per design_new.md line 1637: if ato_running is True but the path
        # authority has been revoked mid-motion, clear ato_running before the
        # ATO/manual branch so the branch uses the correct run state.
        if self.ato.running and self.path_projection.received_path_plan is None:
            self.ato.running = False

        # Compute the effective_limit locally; consumers read this via the
        # `target_speed` property when needed.
        effective_limit = min(
            train.max_speed,
            self.atc.speed_limit,
            self.atc.current_block_speed_limit,
        )

        if self.commands.control_mode == "ato":
            # NOTE: We no longer auto-correct cab during tick().
            # Previously, this would partially update cab (leading_end only) without
            # flipping facing_direction and entry edges, causing state corruption.
            # Cab selection should be done:
            # 1. Manually by the operator before starting ATO
            # 2. Automatically in ato_go() when cab is None (using required_cab)
            # The operator can freely change cab when ato_running=False.

            # Calculate handle position based on ato_running
            if self.ato.running:
                # ATO is running - calculate handle to reach target speed
                self.ops.handle_position = self.ato.compute_handle(delta)
                logger.debug(
                    "[TrainController %s] ATO handle=%s target=%.2f",
                    self.train_id,
                    self.ops.handle_position.value,
                    effective_limit,
                )
            else:
                # ATO not running - brake to stop. Transition to the
                # state-machine-derived stopped ready state once stopped.
                # RUNNING_AT_STATION (dwell) is also a powered-stopped state
                # and is owned by Phase 1's dwell-complete check; preserve it
                # here so a single tick doesn't clobber dwell entry.
                self.ops.handle_position = self.ato.calculate_braking_notch_for_speed(
                    self.physics_sync.current_speed_kmh, 0.0
                )
                if self.physics_sync.current_speed_kmh <= STOPPED_SPEED_THRESHOLD_KMH:
                    if self.ops.state != TrainState.RUNNING_AT_STATION:
                        self.transition_to_ready()
                    self.ops.handle_position = HandlePosition.N
        else:
            # Manual mode - ATC still enforces braking if overspeed
            if self.physics_sync.current_speed_kmh > self.atc.speed_limit:
                self.ops.handle_position = self.ato.calculate_braking_notch_for_speed(
                    self.physics_sync.current_speed_kmh, self.atc.speed_limit
                )
                logger.debug(
                    "[TrainController %s] Manual ATC brake: handle=%s",
                    self.train_id,
                    self.ops.handle_position.value,
                )

        # ===== 7. EFFECTIVE LIMIT (target_speed property reads from lookahead) =====
        # Per design_new.md, target_speed and speed_limit_reason are derived
        # from lookahead at distance 0. We do NOT mirror them onto self/train.
        logger.debug(
            "[TrainController %s] effective_limit=%.2f",
            self.train_id,
            effective_limit,
        )

        # `path_backed_run_active` is now a derived `@property`; no write here.

        # ===== 8. STATE TRANSITIONS =====
        # While RUNNING_MOVING, Phase 2 (tick()) makes no state transition here:
        #
        # - Only RouteExecutor (Phase 1) writes RUNNING_AT_STATION, via
        #   TrainController.enter_station_dwell() (design_new.md "Per-train tick
        #   transaction"). Phase 2 must not transition to RUNNING_AT_STATION
        #   here, even when the stopped train sits in a scheduled-stop station
        #   block — the path-completion gate is_stopped_at_authority_end()
        #   requires state == RUNNING_MOVING, and Phase 3's
        #   try_complete_active_path(...) runs after this method returns. Dwell
        #   entry happens on the next eligible Phase 1 tick once the dispatcher
        #   has completed the path and recorded the stop's finished_at.
        # - A train at zero ATC clearance with live authority stays in
        #   RUNNING_MOVING with current_speed_kmh == 0. The reactive speed-based
        #   evaluation in on_physics_updated() handles the no-authority no-speed
        #   case (RUNNING_MOVING -> READY once stopped with no live authority and
        #   EB clear). Per design_new.md steps `89ba` / `32dd` / `90d3`, tick()
        #   must not latch path completion or call route callbacks itself: all
        #   completion flows through Phase 3's try_complete_active_path(...).
        #
        # Other states (READY, RUNNING_AT_STATION, OFF) handled by commands.

    def produce_physics_command(self) -> PhysicsCommand:
        """Compute the per-tick command physics will execute.

        Decision tree:
          - power off              → handle N (train coasts; physics
                                     reads no controller mode fields)
          - eb active              → emergency brake
          - manual power_percent   → snap to target_speed_kmh, where
                                     target = clamp(power_speed_curve(pct),
                                     [0, atc_speed_limit])
          - ATO / manual simulation→ integrate from previous speed via the
                                     controller-set handle position

        `voltage_source_speed_kmh` is the scalar the voltage emitter uses
        as the train's "current speed" when computing the per-tick voltage:
          - power_percent          → calibrated target speed
          - manual simulation      → integrated current speed
          - ATO                    → live target speed from lookahead
          - everything else        → 0.0 (READY/AT_STATION/OFF use lighting
                                     or zero voltage and ignore this field)
        """
        if not self.power_on:
            # Per design_new.md line 1755: clear manual_power_percent on
            # power-off so the set-and-forget contract holds.
            self.manual.manual_power_percent = 0.0
            return PhysicsCommand(
                kind=PHYSICS_KIND_HANDLE,
                handle=HandlePosition.N,
                voltage_source_speed_kmh=0.0,
            )
        if self.eb.active:
            # Per design_new.md line 1755: clear manual_power_percent on EB.
            self.manual.manual_power_percent = 0.0
            return PhysicsCommand(
                kind=PHYSICS_KIND_EMERGENCY_BRAKE,
                handle=HandlePosition.EB,
                voltage_source_speed_kmh=0.0,
            )
        if (
            self.commands.control_mode == "manual"
            and self.manual.manual_mode_type == "power_percent"
        ):
            profile = self._get_speed_profile()
            pct = float(self.manual.manual_power_percent or 0.0)
            target = profile.power_percent_to_speed(pct) if profile else 0.0
            atc = float(self.atc.speed_limit or 0.0)
            if target > atc:
                # Per design_new.md: when ATC intervenes — including a zero
                # authority cap (no cab, wrong-direction cab, or no/at-end
                # authority, all of which set atc.speed_limit = 0 earlier this
                # tick) — the controller clamps the target to the ATC limit and
                # forces manual_power_percent = 0 for the tick (set-and-forget).
                # A zero cap therefore holds the train at rest instead of letting
                # the stored percent command motion.
                target = atc
                self.manual.manual_power_percent = 0.0
            target = max(0.0, target)
            return PhysicsCommand(
                kind=PHYSICS_KIND_TARGET_SPEED,
                target_speed_kmh=target,
                voltage_source_speed_kmh=target,
            )
        # ATO and manual `simulation` submode: handle integration.
        if self.commands.control_mode == "ato":
            voltage_speed = float(self.target_speed or 0.0)
        else:
            voltage_speed = float(self.physics_sync.current_speed_kmh or 0.0)
        return PhysicsCommand(
            kind=PHYSICS_KIND_HANDLE,
            handle=self.ops.handle_position,
            voltage_source_speed_kmh=voltage_speed,
        )

    # -------------------------------------------------------------------------
    # ATC (Automatic Train Control) Logic
    # -------------------------------------------------------------------------

    # ATC clearance / speed-limit / predicate computation lives on the
    # `ATCComputer` sub-module at `self.atc`. The thin forwarders that
    # used to live here have been retired; call `self.atc.clearance_ahead()`,
    # `self.atc.speed_limit_now()`, `self.atc.is_caution_ahead()`,
    # `self.atc.is_approaching_yellow_signal()`,
    # `self.atc.effective_safety_margin()`, or
    # `self.atc.is_signal_red_for_block(block_id)` directly.

    def _calculate_clearance_to_destination(self, base_clearance: float) -> float:
        """
        Calculate clearance to ato_destination_block_id.

        Handles two scenarios:
        1. Train has current_path: uses path to trace ahead to destination block
        2. Train already IN destination block (path cleared): calculates remaining
           distance to end of current block, considering station position if present

        If the destination block contains a station, stops at the end of station.

        Args:
            base_clearance: Already calculated clearance from current block

        Returns:
            Total clearance to destination stopping point
        """
        train = self._get_train()
        if not train or not self.network:
            return base_clearance

        if not self.path_projection.ato_destination_block_id:
            return base_clearance

        plan = self.path_projection.received_path_plan
        resource_sequence = (
            getattr(plan, "resource_sequence", None) if plan else None
        )
        dest_block_id = self.path_projection.ato_destination_block_id
        current_block_id = self._get_current_block_id()

        # Check if we're already in the destination block
        if current_block_id == dest_block_id:
            return self._calculate_clearance_to_end_of_current_block()

        # Not in destination block yet - need the live authority's
        # resource_sequence to trace forward.
        if not resource_sequence:
            return base_clearance

        # Check if destination block has a station (for precise stopping)
        station_id = self._get_station_in_block(dest_block_id)
        station_positions: set = set()
        if station_id:
            station = self.network.platforms.get(station_id)
            if station:
                station_positions = set(station.positions)

        head_tile = self._get_head_tile()
        if not head_tile:
            return base_clearance

        head_resource_index = -1
        head_tile_index = -1
        for r_idx, resource in enumerate(resource_sequence):
            for t_idx, tile_entry in enumerate(resource.path_tiles):
                if (tile_entry.x, tile_entry.y) == head_tile:
                    head_resource_index = r_idx
                    head_tile_index = t_idx
                    break
            if head_resource_index >= 0:
                break

        if head_resource_index < 0:
            return base_clearance

        # Flatten the tail→authority tile slice ahead of the head so the
        # station-end check can peek forward without nested indexing.
        ahead_entries: List[Tuple[int, int]] = []
        for r_idx in range(head_resource_index, len(resource_sequence)):
            resource = resource_sequence[r_idx]
            tile_start = (
                head_tile_index + 1 if r_idx == head_resource_index else 0
            )
            for t_idx in range(tile_start, len(resource.path_tiles)):
                tile_entry = resource.path_tiles[t_idx]
                ahead_entries.append((tile_entry.x, tile_entry.y))

        clearance = base_clearance
        in_dest_block = False

        for entry_idx, (tile_x, tile_y) in enumerate(ahead_entries):
            tile_pos = (tile_x, tile_y)

            track = self.network.get_track(tile_x, tile_y)
            if not track:
                break

            # Check if this tile's block is obstructed (occupied by another train)
            if track.block_section_id and track.block_section_id != current_block_id:
                block = self.network.blocks.get(track.block_section_id)
                if block and self._is_block_obstructed(block, track.block_section_id):
                    break
                current_block_id = track.block_section_id

            if track.block_section_id == dest_block_id:
                in_dest_block = True

            tile_length = self.network._get_tile_length_cm(tile_x, tile_y)
            clearance += tile_length

            if station_positions and tile_pos in station_positions:
                remaining_ahead = ahead_entries[entry_idx + 1 :]
                has_more_station_tiles = any(
                    p in station_positions for p in remaining_ahead
                )
                if not has_more_station_tiles:
                    break
            elif in_dest_block and track.block_section_id != dest_block_id:
                break

        return clearance

    def _calculate_clearance_to_end_of_current_block(self) -> float:
        """
        Calculate remaining distance to end of current block.

        Used when train is already in the destination block. Considers station
        position for precise stopping at end of station tiles.

        Returns:
            Remaining distance in cm to the stopping point
        """
        train = self._get_train()
        if not train or not self.network:
            return 0.0

        current_block_id = self._get_current_block_id()
        if not current_block_id:
            return 0.0

        block = self.network.blocks.get(current_block_id)
        if not block:
            return 0.0

        head_tile = self._get_head_tile()
        if not head_tile:
            return block.length_cm if block.length_cm else 0.0

        # Check if this block has a station
        station_id = self._get_station_in_block(current_block_id)
        station_positions: set = set()
        if station_id:
            station = self.network.platforms.get(station_id)
            if station:
                station_positions = set(station.positions)

        head_distance_cm = self._get_head_distance_cm()

        block_positions = self.network.get_block_positions(current_block_id)
        if not block_positions:
            # Fall back to simple remaining calculation
            tile_length = self.network._get_tile_length_cm(*head_tile)
            return max(0.0, tile_length - head_distance_cm)

        # Sort block positions by travel direction
        # This is critical for multi-tile stations to ensure we calculate
        # remaining distance in the correct order
        travel_direction = self._get_travel_direction()
        if travel_direction:
            block_positions = self._sort_positions_by_direction(
                block_positions, travel_direction
            )

        # Find current position within block
        try:
            current_idx = block_positions.index(head_tile)
        except ValueError:
            current_idx = -1

        if current_idx < 0:
            # Head tile not in block positions - use fallback
            tile_length = self.network._get_tile_length_cm(*head_tile)
            return max(0.0, tile_length - head_distance_cm)

        # Calculate remaining distance
        remaining = 0.0

        for i in range(current_idx, len(block_positions)):
            tile = block_positions[i]
            tile_length = self.network._get_tile_length_cm(*tile)

            if i == current_idx:
                # Current tile: remaining from current position to end
                remaining += max(0.0, tile_length - head_distance_cm)
            else:
                # Subsequent tiles: full length
                remaining += tile_length

            # Check if this is the last station tile (for station blocks)
            if station_positions and tile in station_positions:
                # Check if there are more station tiles after this
                has_more_station_tiles = any(
                    block_positions[j] in station_positions
                    for j in range(i + 1, len(block_positions))
                )
                if not has_more_station_tiles:
                    # Stop at end of this tile (last station tile)
                    break

        return remaining

    def _get_travel_direction(self) -> Optional[Direction]:
        """Get the train's current travel direction from path or facing direction."""
        train = self._get_train()
        if not train:
            return None

        # First, try to get direction from current_path
        path_tiles = self._get_effective_path_tiles()
        if path_tiles and len(path_tiles) > 0:
            head_tile = self._get_head_tile()
            if head_tile:
                # Find current position in path
                for i, path_entry in enumerate(path_tiles):
                    if (path_entry[0], path_entry[1]) == head_tile:
                        # Return direction from this path entry
                        if len(path_entry) > 2:
                            return path_entry[2]
                        # Or infer from next tile
                        if i + 1 < len(path_tiles):
                            next_tile = path_tiles[i + 1]
                            dx = next_tile[0] - path_entry[0]
                            dy = next_tile[1] - path_entry[1]
                            if dy > 0:
                                return Direction.NORTH
                            elif dy < 0:
                                return Direction.SOUTH
                            elif dx > 0:
                                return Direction.EAST
                            elif dx < 0:
                                return Direction.WEST
                        break

        # Fall back to facing direction (derived from cab + current_span).
        return self._facing_direction()

    def _sort_positions_by_direction(
        self,
        positions: List[Tuple[int, int]],
        direction: Direction,
    ) -> List[Tuple[int, int]]:
        """Sort positions in the given travel direction.

        For multi-tile blocks, we need to process tiles in the order
        the train will encounter them, not in arbitrary insertion order.

        Args:
            positions: List of (x, y) positions
            direction: Travel direction

        Returns:
            Positions sorted by travel direction
        """
        if not positions:
            return positions

        # Create a copy to avoid mutating the original
        positions = list(positions)

        if direction == Direction.NORTH:
            # Traveling north: sort by y ascending
            positions.sort(key=lambda p: p[1])
        elif direction == Direction.SOUTH:
            # Traveling south: sort by y descending
            positions.sort(key=lambda p: -p[1])
        elif direction == Direction.EAST:
            # Traveling east: sort by x ascending
            positions.sort(key=lambda p: p[0])
        elif direction == Direction.WEST:
            # Traveling west: sort by x descending
            positions.sort(key=lambda p: -p[0])

        return positions

    def _is_cab_wrong_direction(self) -> bool:
        """Return True when the active cab points away from active authority.

        v1 only flags wrong-direction cab when there is active path authority
        (no active authority falls under the `no_authority` reason instead).
        Compares the current cab's required-cab projection from the active
        path with the active cab and returns True on mismatch.
        """
        if self.commands.cab not in ("A", "B"):
            return False
        if self.path_projection.received_path_plan is None:
            return False
        required = self._required_cab_for_path()
        if required is None:
            return False
        return required != self.commands.cab

    def _get_current_block_id(self) -> Optional[str]:
        """Get the block ID where the train's head is located."""
        head_tile = self._get_head_tile()
        if not head_tile or not self.network:
            return None

        track = self.network.tracks.get(head_tile)
        if track:
            return track.block_section_id

        return None

    # --- Cab-derived head/tail/orientation helpers ---------------------------
    # Per design_new.md: head/tail and travel_direction derive from
    # (cab, current_span). Cab "A" -> end_a, cab "B" -> end_b; head defaults to
    # end_a when no cab is selected (matching the placement default). `end_a` /
    # `end_b` on the span are cab-independent physical ends and never carry
    # head/tail labels themselves.

    def _head_end_label(self) -> str:
        """Which physical span end ("end_a"/"end_b") is the head for the
        selected cab. Cab "B" -> end_b; cab "A" or None -> end_a."""
        return "end_b" if self.commands.cab == "B" else "end_a"

    def _head_span_locator(self):
        """`SpanEndLocator` for the head end of `current_span`, or None."""
        span = getattr(self.physics_sync, "current_span", None)
        if span is None:
            return None
        return span.end_b if self._head_end_label() == "end_b" else span.end_a

    def _tail_span_locator(self):
        """`SpanEndLocator` for the tail end of `current_span`, or None."""
        span = getattr(self.physics_sync, "current_span", None)
        if span is None:
            return None
        return span.end_a if self._head_end_label() == "end_b" else span.end_b

    def _orientation_for_head_end(self, head_end: str) -> Optional[Direction]:
        """Direction the train points when `head_end` ("end_a"/"end_b") is the
        head, derived purely from `current_span` geometry. Cab-independent —
        the caller chooses which end is the head. Returns None when there is no
        span or the geometry is degenerate."""
        span = getattr(self.physics_sync, "current_span", None)
        if span is None:
            return None
        if head_end == "end_a":
            head, tail = span.end_a, span.end_b
        else:
            head, tail = span.end_b, span.end_a
        head_tile = tuple(head.tile) if head.tile is not None else None
        tail_tile = tuple(tail.tile) if tail.tile is not None else None
        if head_tile is None:
            return None
        occupied = [tuple(t) for t in (span.occupied_tiles or [])]
        # Multi-tile: vector from the adjacent body tile to the head tile.
        if tail_tile is not None and head_tile != tail_tile:
            adjacent = tail_tile
            if len(occupied) >= 2 and head_tile in occupied:
                hidx = occupied.index(head_tile)
                adjacent = occupied[1] if hidx == 0 else occupied[hidx - 1]
            dx = head_tile[0] - adjacent[0]
            dy = head_tile[1] - adjacent[1]
            for direction in Direction:
                if direction.value == (dx, dy):
                    return direction
            return None
        # Single-tile: derive from the head entry edge and the two end offsets.
        head_entry = head.entry_edge
        if not head_entry:
            return None
        exit_edge = OPPOSITE_EDGES.get(head_entry.upper(), head_entry)
        edge_to_dir = {
            "N": Direction.NORTH,
            "E": Direction.EAST,
            "S": Direction.SOUTH,
            "W": Direction.WEST,
        }
        if float(head.distance_cm) >= float(tail.distance_cm):
            return edge_to_dir.get(exit_edge)
        return edge_to_dir.get(head_entry.upper())

    def _facing_direction(self) -> Optional[Direction]:
        """Train facing direction (a `Direction`) derived from
        (cab, current_span). Replaces the removed `Train.facing_direction`. Not
        cab-gated: head defaults to end_a when no cab. Returns None when there
        is no live span."""
        return self._orientation_for_head_end(self._head_end_label())

    def get_span_end_exits(self) -> List[Tuple[Tuple[int, int], str]]:
        """Both physical span ends as ``(tile, outward_edge)`` pairs.

        ``outward_edge`` is the network edge pointing away from the body at
        that end — the direction the train would move if it left through that
        end (forward out of one end, reverse out of the other). Cab-independent:
        both ends are always reported. Returns an empty list when there is no
        live span. Used by ``PathDispatcher.get_reachable_endpoints_for_train``
        to seed span-aware reachability for the Quick Route tool.

        The end tile's outward edge is read from its real track edges, not from
        the straight-line body heading: on a curved end tile the heading points
        at an edge the tile does not have (the rail bends away), so the seed has
        to follow the curve. See ``_outward_edge_for_end_tile``.
        """
        span = getattr(self.physics_sync, "current_span", None)
        if span is None:
            return []
        occupied = {
            (int(x), int(y))
            for x, y in (getattr(span, "occupied_tiles", None) or [])
        }
        seeds: List[Tuple[Tuple[int, int], str]] = []
        for label, loc in (("end_a", span.end_a), ("end_b", span.end_b)):
            if loc is None or getattr(loc, "tile", None) is None:
                continue
            tile = (int(loc.tile[0]), int(loc.tile[1]))
            direction = self._orientation_for_head_end(label)
            straight = (
                self._direction_to_edge(direction) if direction is not None else None
            )
            edge = self._outward_edge_for_end_tile(tile, straight, occupied)
            if edge:
                seeds.append((tile, edge))
        return seeds

    def _outward_edge_for_end_tile(
        self,
        tile: Tuple[int, int],
        straight_edge: Optional[str],
        occupied: set,
    ) -> Optional[str]:
        """The track edge a span end actually exits through.

        ``straight_edge`` is the straight-line heading derived from the body's
        grid vector. On a straight or switch end tile that heading is a real
        track edge, so we keep it — a train sitting across a switch leaves along
        its straight-through route, never the diverge its own body is blocking.
        On a curved end tile the heading points at an edge the tile does not
        have (the rail bends), so the real exit is the tile's other track edge:
        the one whose neighbour is not part of the train's own body. Falls back
        to ``straight_edge`` when there is no track or the choice is ambiguous.
        """
        if self.network is None:
            return straight_edge
        track = self.network.get_track(tile[0], tile[1])
        if track is None:
            return straight_edge
        candidates: List[str] = []
        for e in getattr(track, "edges", None) or ():
            nb = self.network.get_neighbor(tile[0], tile[1], e)
            if nb is not None and (nb.x, nb.y) not in occupied:
                candidates.append(e)
        if straight_edge in candidates:
            return straight_edge
        if len(candidates) == 1:
            return candidates[0]
        return straight_edge

    def _get_head_tile(self) -> Optional[Tuple[int, int]]:
        """Head tile, derived from (cab, current_span), or None when no span."""
        loc = self._head_span_locator()
        if loc is not None and loc.tile is not None:
            return (loc.tile[0], loc.tile[1])
        return None

    def _get_head_distance_cm(self) -> float:
        """Head in-tile distance, derived from (cab, current_span)."""
        loc = self._head_span_locator()
        if loc is not None:
            return float(loc.distance_cm)
        return 0.0

    # `get_live_path_data()` lives on the `PathProjection` sub-module at
    # `self.path_projection`. External callers use
    # `controller.path_projection.<method>()` directly.

    def _get_head_path_index(self, head_tile: Tuple[int, int]) -> int:
        """Get the path index for the train head tile.

        v1 path geometry forbids tile re-visits in a single direction of
        travel, so the first match is always the correct one.
        """
        path_tiles = self._get_effective_path_tiles()
        if not path_tiles:
            return -1
        for i, (x, y, _) in enumerate(path_tiles):
            if (x, y) == head_tile:
                return i
        return -1

    def _direction_to_edge(self, direction: Direction) -> Optional[str]:
        """Convert Direction enum to edge token (N/E/S/W)."""
        if direction == Direction.NORTH:
            return "N"
        if direction == Direction.EAST:
            return "E"
        if direction == Direction.SOUTH:
            return "S"
        if direction == Direction.WEST:
            return "W"
        return None

    def _get_head_entry_edge(self) -> Optional[str]:
        """Head tile entry edge, derived from (cab, current_span)."""
        loc = self._head_span_locator()
        if loc is not None:
            return loc.entry_edge
        return None

    def _get_remaining_distance_in_current_block(self) -> float:
        """
        Get remaining distance in the current block.

        This is the distance from train's head to the block boundary.
        """
        train = self._get_train()
        if not train or not self.network:
            return 0.0

        current_block_id = self._get_current_block_id()
        if not current_block_id:
            return 0.0

        block = self.network.blocks.get(current_block_id)
        if not block or not block.length_cm:
            return 0.0

        head_tile = self._get_head_tile()
        if not head_tile:
            return block.length_cm

        plan = self.path_projection.received_path_plan
        resource_sequence = (
            getattr(plan, "resource_sequence", None) if plan else None
        )
        if resource_sequence:
            head_resource_index = -1
            head_tile_index = -1
            for r_idx, resource in enumerate(resource_sequence):
                for t_idx, tile_entry in enumerate(resource.path_tiles):
                    if (tile_entry.x, tile_entry.y) == head_tile:
                        head_resource_index = r_idx
                        head_tile_index = t_idx
                        break
                if head_resource_index >= 0:
                    break

            if head_resource_index < 0:
                return block.length_cm

            head_tile_length = self.network._get_tile_length_cm(*head_tile)
            remaining = max(0.0, head_tile_length - self._get_head_distance_cm())

            # Walk forward through resource_sequence (always tail→authority
            # order per the schema) starting one tile past the head.
            done = False
            for r_idx in range(head_resource_index, len(resource_sequence)):
                resource = resource_sequence[r_idx]
                tile_start = (
                    head_tile_index + 1 if r_idx == head_resource_index else 0
                )
                for t_idx in range(tile_start, len(resource.path_tiles)):
                    tile_entry = resource.path_tiles[t_idx]
                    track = self.network.get_track(tile_entry.x, tile_entry.y)
                    if not track or track.block_section_id != current_block_id:
                        done = True
                        break
                    remaining += self.network._get_tile_length_cm(
                        tile_entry.x, tile_entry.y
                    )
                if done:
                    break

            return remaining

        block_positions = self.network.get_block_positions(current_block_id)
        if not block_positions:
            return block.length_cm

        # Sort block positions by travel direction to ensure correct calculation
        # for multi-tile stations regardless of insertion order
        travel_direction = self._get_travel_direction()
        if travel_direction:
            block_positions = self._sort_positions_by_direction(
                block_positions, travel_direction
            )

        tile_count = len(block_positions)
        if tile_count <= 0:
            return block.length_cm

        per_tile_length = block.length_cm / tile_count

        current_tile_index = -1
        for i, (bx, by) in enumerate(block_positions):
            if (bx, by) == head_tile:
                current_tile_index = i
                break

        if current_tile_index < 0:
            return block.length_cm

        remaining_tiles_after_current = tile_count - current_tile_index - 1

        current_track = self.network.get_track(*head_tile)
        fraction_through_tile = 0.0
        if current_track:
            track_length_cm = self.network._get_tile_length_cm(*head_tile)
            if track_length_cm > 0:
                head_distance_cm = self._get_head_distance_cm()
                fraction_through_tile = head_distance_cm / track_length_cm
                fraction_through_tile = max(0.0, min(1.0, fraction_through_tile))

        remaining_in_current_tile = (1.0 - fraction_through_tile) * per_tile_length
        remaining_in_later_tiles = remaining_tiles_after_current * per_tile_length

        return remaining_in_current_tile + remaining_in_later_tiles

    def _get_blocks_ahead(self) -> List[str]:
        """Forward to `LookaheadBuilder.blocks_ahead()`."""
        return self._get_lookahead_builder().blocks_ahead()

    def _is_block_obstructed(self, block: Any, block_id: str) -> bool:
        """
        Check if a block is obstructed (cannot be entered).

        A block is obstructed if:
        - It's occupied by another train
        - It's reserved for a different path
        - It's disabled

        Per design_new.md lines 703 and 1751, CAUTION is NOT an obstruction:
        its only operational effect on lookahead is the CAUTION_SPEED_LIMIT_KMH
        cap applied in `ATCCalculator.speed_limit()`. A CAUTION block that is
        otherwise free / reserved-for-this-train must remain enterable so a
        train can re-approach a block it (or another train) recently vacated.
        """
        train = self._get_train()
        if not train:
            return True

        # Disabled blocks are obstructed
        if hasattr(block, "disabled") and block.disabled:
            return True

        # Occupied by another train
        if block.state == BlockState.OCCUPIED:
            if block.occupied_by != train.train_id:
                return True

        # Reserved for a different path
        if block.state == BlockState.RESERVED:
            if hasattr(block, "reserved_by_path") and block.reserved_by_path:
                assigned_path_id = self.path_projection.assigned_path_id
                if assigned_path_id:
                    if block.reserved_by_path != assigned_path_id:
                        return True
                else:
                    # Block reserved but train has no path
                    return True

        return False

    # `is_signal_red_for_block(block_id)` and
    # `is_approaching_yellow_signal()` live on the `ATCComputer` sub-module
    # at `self.atc`. The bodies moved there; callers use
    # `self.atc.is_signal_red_for_block(block_id)` and
    # `self.atc.is_approaching_yellow_signal()`.

    def _get_current_block_speed_limit(self) -> float:
        """Forward to `LookaheadBuilder.current_block_speed_limit()`."""
        return self._get_lookahead_builder().current_block_speed_limit()

    @property
    def power_on(self) -> bool:
        """Convenience read derived from `state != OFF` per design_new.md."""
        return self.ops.state != TrainState.OFF

    def is_stopped(self) -> bool:
        """Canonical stopped predicate per design_new.md.

        Reads controller-owned speed (current_speed_kmh). The threshold is
        STOPPED_SPEED_THRESHOLD_KMH per the units contract.
        """
        speed_kmh = getattr(self.physics_sync, "current_speed_kmh", 0.0) or 0.0
        return speed_kmh <= STOPPED_SPEED_THRESHOLD_KMH

    @property
    def active_conditions(self) -> FrozenSet[ControllerCondition]:
        """Canonical projection of recovery-relevant controller flags.

        Per design_new.md "Controller condition set" canonical home: derived
        each read from controller-stored fields. Predicates such as
        is_recovery_clean and RouteExecutor.is_clean_for_create read subsets
        of this set; readers must not duplicate the underlying field checks.
        """
        conditions: set[ControllerCondition] = set()
        if self.path_projection.received_path_plan is not None:
            conditions.add(ControllerCondition.HAS_AUTHORITY)
        if self.eb.active:
            conditions.add(ControllerCondition.EB_LATCHED)
        latches = self.route_hooks.phase_latches
        if latches.power_off:
            conditions.add(ControllerCondition.POWER_OFF_PENDING)
        if latches.path_cancel_request is not None:
            conditions.add(ControllerCondition.PATH_CANCEL_PENDING)
        if latches.route_abort:
            conditions.add(ControllerCondition.ROUTE_ABORT_PENDING)
        if latches.route_deactivate:
            conditions.add(ControllerCondition.ROUTE_DEACTIVATE_PENDING)
        return frozenset(conditions)

    @property
    def is_recovery_clean(self) -> bool:
        """Canonical "controller has reached the stopped recovery state" predicate.

        Per design_new.md (Controller state machine subsection): true when
        ``state == READY`` and none of the active conditions
        ``{HAS_AUTHORITY, EB_LATCHED, POWER_OFF_PENDING}`` are set. Phase-1
        route-deviation cleanup and Phase-1 dwell-entry both gate on this
        predicate; other call sites cross-reference it by name and must not
        restate the conjuncts.
        """
        if self.ops.state != TrainState.READY:
            return False
        return not (
            self.active_conditions
            & {
                ControllerCondition.HAS_AUTHORITY,
                ControllerCondition.EB_LATCHED,
                ControllerCondition.POWER_OFF_PENDING,
            }
        )

    def is_stopped_at_authority_end(self) -> bool:
        """True when the train is stopped at the usable end of active authority.

        Per design_new.md step `8df5`, this is the post-physics usable-
        authority stop check used by Phase 3 path completion.

        The spec lists two satisfying cases: the train has either "physically
        progressed through the final `resource_sequence` entry" (its head
        occupies the authority-end block) or "the remaining projected authority
        distance is too short to grant additional positive clearance after
        applying the controller safety margin" (it braked to rest at the
        safety-margin boundary short of that block — common when the
        authority-end block is very short or zero-length). Both cases surface
        identically at distance 0: the lookahead curve's dominant restriction
        becomes the path-end stop, so its reason code is `AUTHORITY_STOP`. The
        literal `atc_limit == 0` the spec mentions is unreachable under
        forward-Euler integration (a step cannot land exactly on the boundary,
        so the value reads a few km/h); the `AUTHORITY_STOP` reason code is the
        reachable expression of the same intent.

        The gate therefore fires on `is_stopped()` + reason code
        `AUTHORITY_STOP` and does NOT require the head to occupy the
        authority-end block. Requiring head occupancy would strand a train that
        stopped just short of a very short final block — it satisfies the
        spec's second case yet would never complete. The dispatcher's
        `try_complete_active_path(...)` separately verifies the live authority
        actually reached the requested destination block (and, for multi-tile
        blocks, its direction), so this controller-side gate only answers
        "stopped at my current usable authority end?".
        """
        if not self.is_stopped():
            return False
        if self.ops.state != TrainState.RUNNING_MOVING:
            return False
        if self.eb.active:
            return False
        plan = self.path_projection.received_path_plan
        if plan is None:
            return False
        # The ATO curve has collapsed to the path-end stop at distance 0: the
        # dominant restriction's reason code is AUTHORITY_STOP. This holds in
        # both spec cases (head in the authority-end block, or stopped short of
        # a very short final block by the safety margin), so no head-occupancy
        # test is needed here — the dispatcher's try_complete_active_path
        # verifies the authority actually reached the requested destination.
        payload = self._compute_canonical_lookahead()
        curves = (payload or {}).get("lookahead", {}).get("speed_curves") or []
        if not curves:
            return False
        return (
            curves[0].get("atc_limit_reason_code")
            == AtcLimitReasonCode.AUTHORITY_STOP.value
        )

    # -------------------------------------------------------------------------
    # Physics callback — Phase 2 storage / Phase 3 enqueue (per design_new.md)
    # -------------------------------------------------------------------------

    def on_physics_updated(
        self,
        event_time: float,
        span_update: PhysicsSpanUpdate,
    ) -> None:
        """Apply a TrainPhysics span_update and enqueue a Phase 3 progress
        report. Delegates to ``ProgressReportBuilder``
        (train_controller/progress_report.py); see there for the full
        Phase-2 storage / Phase-3 enqueue procedure.
        """
        self.progress_report.on_physics_updated(event_time, span_update)

    # -------------------------------------------------------------------------
    # Lookahead Calculation (shared by ATC and ATO)
    # -------------------------------------------------------------------------

    def _calculate_lookahead(self) -> List[LookaheadPoint]:
        """Build the lookahead point list ahead of the train.

        Forwards to `LookaheadBuilder.build()`. Kept as an instance method so
        existing callers and `monkeypatch.setattr(controller, ...)` test
        fixtures continue to work.
        """
        return self._get_lookahead_builder().build()

    def _get_station_in_block(self, block_id: str) -> Optional[str]:
        """Forward to `LookaheadBuilder.station_in_block(block_id)`."""
        return self._get_lookahead_builder().station_in_block(block_id)

    # -------------------------------------------------------------------------
    # ATO Info Calculation lives on the `ATOComputer` sub-module at `self.ato`.
    # Callers use `self.ato.compute_info()` directly.

    # -------------------------------------------------------------------------
    # Path Direction Detection
    # -------------------------------------------------------------------------

    def _get_effective_path_tiles(self):
        """Active path tiles for control decisions, as `(x, y, Direction)` tuples.

        Derived live from `received_path_plan.resource_sequence` — the
        dispatcher-owned active `Path` is the single source of truth for path
        geometry (design_new.md "received_path_plan schema"). The ordered tile
        list comes from `Path.get_path_tiles()` (the `resource_sequence[*].
        path_tiles` slices concatenated in `sequence_index` order) and is
        converted with the network so the per-tile direction stays correct
        across tunnel hops, where consecutive tiles are not grid-adjacent and
        raw `(dx, dy)` would mis-project the cardinal direction. Returns `None`
        when the train has no `ACTIVE` path.
        """
        plan = self.path_projection.received_path_plan
        if plan is None:
            return None
        path_tiles = plan.get_path_tiles()
        if not path_tiles:
            return None

        # The conversion is a pure function of (path_tiles, network), and the
        # network geometry is fixed for the controller's lifetime, so the result
        # only changes when the path's tile list changes. `_get_effective_path_
        # tiles()` is called many times per tick (cab-direction checks, authority
        # probes, path-projection), each re-walking the whole path through
        # per-tile network lookups, so memoize on the tile-list *content*:
        # `PathTileEntry` is a value dataclass, so the `==` compare is cheap and
        # detects any reroute / extend / truncate the dispatcher applies in place
        # (where the plan object identity would stay the same).
        cache = self._effective_path_tiles_cache
        if cache is not None and cache[0] == path_tiles:
            return cache[1]

        from path_utils import convert_tiles_to_train_path

        result = convert_tiles_to_train_path(path_tiles, network=self.network)
        self._effective_path_tiles_cache = (path_tiles, result)
        return result

    def _has_active_path_authority(self, train: "Train") -> bool:
        """Return True while the train still has movement authority to follow.

        Live path authority surfaces through the controller-owned projections
        (`_get_effective_path_tiles()` / `path_projection.ato_path_id`). There is
        no `Train.assigned_path_id` to read — `assigned_path_id` is a read-only
        derived property of `received_path_plan.path_id` on the controller
        (design_new.md "Train Entity"), already reflected by those projections.
        """
        if self._get_effective_path_tiles():
            return True
        if self.path_projection.ato_path_id:
            return True
        return False

    def _has_path_backed_run_context(self, train: "Train") -> bool:
        """Return True when the current run is tied to live path authority or an
        ATO destination.

        This projection means "has live authority" for its consumers — the
        `path_error` badge suppresses while it is True, so a bare route
        assignment with no live path must NOT make it True (or a stuck,
        no-path train would hide its own error). There is correspondingly no
        active-route branch here: `assigned_route_id` is not a `Train` field
        (the canonical store is `RouteExecutor.assigned_route_id_for(...)`,
        design_new.md "Train Entity").
        """
        if self._has_active_path_authority(train):
            return True
        if self.path_projection.ato_destination_block_id:
            return True
        return False

    def _path_needs_cab_flip(self) -> bool:
        """Check if the current path requires the train to reverse direction.

        Uses track connectivity: compares the head tile's entry edge with
        the edge leading to the path's next tile.  If they match, the path
        goes backward through the head (back the way it came), so the cab
        must flip.

        This is more reliable than comparing cardinal directions, which can
        be misleading on zigzag / diagonal staircase tracks where the
        per-tile direction differs from the overall travel direction.

        Tunnel-aware: the next tile in the path may be a tunnel-far tile
        rather than a grid neighbour, in which case the raw (dx, dy) is not
        a unit vector. We delegate edge resolution to
        ``RailwayNetwork.get_connection_edges(...)``, which already handles
        both grid adjacency and tunnel adjacency per design_new.md
        "Pathfinding home" / "Tunnels".

        Returns:
            True if cab should be flipped, False otherwise or if the check
            cannot be performed.
        """
        train = self._get_train()
        if not train:
            return False

        path_tiles = self._get_effective_path_tiles()
        if not path_tiles or len(path_tiles) < 2:
            return False

        head_tile = self._get_head_tile()
        head_entry_edge = self._get_head_entry_edge()
        if not head_tile or not head_entry_edge:
            return False

        # Find head tile in path
        head_idx = -1
        for i, entry in enumerate(path_tiles):
            if (entry[0], entry[1]) == head_tile:
                head_idx = i
                break

        if head_idx < 0 or head_idx + 1 >= len(path_tiles):
            return False

        # Which edge of the head tile leads to the path's next tile?
        # ``get_connection_edges`` resolves both ordinary grid adjacency and
        # tunnel adjacency (where ``next_entry`` is the tunnel-far tile, not
        # a grid neighbour). Falling back to raw ``(dx, dy)`` math would
        # silently return ``None`` on every tunneled hop, which made
        # ``_path_needs_cab_flip()`` return False regardless of the actual
        # head-edge geometry and let ``_required_cab_for_path()`` fall
        # through to the cardinal-direction fallback (which then
        # mis-projects across the tunnel).
        next_entry = path_tiles[head_idx + 1]
        network = self.network
        needed_edge: Optional[str] = None
        if network is not None:
            edges = network.get_connection_edges(
                (head_tile[0], head_tile[1]),
                (next_entry[0], next_entry[1]),
            )
            if edges is not None:
                needed_edge = edges[0]

        if not needed_edge:
            return False

        # Entry edge = the edge the head entered from (backward direction).
        # If the path's next tile is through the same edge, the path goes
        # backward → the cab must flip.
        return needed_edge == head_entry_edge

    def _required_cab_for_path(self) -> Optional[str]:
        """Determine the required cab for the current path.

        Strategy (in priority order):
        1. **Track connectivity** — compare the head tile's entry edge with
           the edge toward the path's next tile.  Reliable on zigzag /
           diagonal staircase tracks where cardinal directions are misleading.
        2. **Direction fallback** — when the head tile isn't in the path (e.g.
           the path was prepended from a different tile), compare the
           ``facing_direction`` with the path's overall direction vector.

        Returns:
            ``"A"`` or ``"B"``, or ``None`` if the check cannot be performed.
        """
        train = self._get_train()
        if not train:
            return None

        current_cab = self.commands.cab or "A"

        # --- Approach 1: track connectivity (preferred) ---
        flip = self._path_needs_cab_flip()
        if flip:
            return "B" if current_cab == "A" else "A"

        # When the path contains a tunnel hop (a tile pair that is not
        # grid-adjacent), the cardinal-direction fallback below cannot be
        # trusted: it projects a straight-line displacement across the
        # tunnel, which has no physical meaning. Trust Approach 1's
        # "no flip" verdict in that case as long as the connectivity check
        # was conclusive (head tile in path with a next tile). For ordinary
        # grid-contiguous paths we still fall through to Approach 2 to
        # preserve the diagonal-staircase return-trip case, where the
        # per-tile direction is 90° from facing but the overall vector is
        # the true reversal. See design_new.md "Tunnels": tile-pair
        # adjacency can be non-grid.
        if (
            self._connectivity_check_was_conclusive(train)
            and self._path_has_tunnel_hop()
        ):
            return current_cab

        # --- Approach 2: direction fallback ---
        # Compare the path's travel direction with the train's facing
        # direction. This also resolves the "90 degrees at the head but overall
        # reverse" diagonal case after the direct backtracking check above.
        facing = self._facing_direction()
        if facing is None:
            return (
                current_cab if self._connectivity_check_was_conclusive(train) else None
            )

        path_direction = self._get_path_direction_at_head(train)
        if path_direction is not None:
            if path_direction == facing:
                return current_cab
            if path_direction == facing.opposite():
                return "B" if current_cab == "A" else "A"

        # 90 degrees or path direction unknown — the overall start->end vector
        # heuristic. This only makes sense at the START of a path: it picks the
        # initial cab for a fresh return trip whose first move is 90° from
        # facing (the diagonal-staircase reversal). Once the train is already
        # moving ALONG the path (head past the first tile) the cab was fixed at
        # the start, and v1 paths are monotonic forward (a path never revisits a
        # resource), so a 90° turn is just a bend, not a reversal. Applying the
        # gross-displacement heuristic mid-path would flip the cab at every bend
        # of a winding approach (e.g. the depot lead climbing north then the
        # ladder doubling back), spuriously tripping WRONG_DIRECTION_CAB. A real
        # mid-path wrong cab (operator flipped to the wrong end) reverses the
        # per-tile direction at the head and is already caught by the opposite
        # check above, which runs regardless of position. So restrict the gross
        # vector flip to the path-start (head-at-first-tile) case.
        path_tiles = self._get_effective_path_tiles()
        if path_tiles and len(path_tiles) >= 2:
            head_tile = self._get_head_tile()
            head_index = -1
            if head_tile is not None:
                for i, entry in enumerate(path_tiles):
                    if (entry[0], entry[1]) == head_tile:
                        head_index = i
                        break
            # head_index <= 0 means the head is at the path's first tile (fresh
            # path) or could not be located (path prepended from elsewhere —
            # keep the original conservative behaviour). head_index > 0 means
            # the train is mid-path, so the bend is not a reversal.
            if head_index <= 0:
                overall = self._dominant_direction_from_tiles(
                    path_tiles[0], path_tiles[-1]
                )
                if overall is not None and overall == facing.opposite():
                    return "B" if current_cab == "A" else "A"

        return current_cab

    def _path_has_tunnel_hop(self) -> bool:
        """Return True when the current path's tile sequence contains a hop
        between two tiles that are not grid-adjacent — i.e. the hop crosses
        a tunnel edge. Used by ``_required_cab_for_path`` to decide whether
        the cardinal-direction fallback is safe to consult: across a tunnel
        the dominant-vector heuristic projects a meaningless straight-line
        displacement, so we have to trust the connectivity-based verdict.
        """
        path_tiles = self._get_effective_path_tiles()
        if not path_tiles or len(path_tiles) < 2:
            return False
        for i in range(len(path_tiles) - 1):
            a = path_tiles[i]
            b = path_tiles[i + 1]
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            # Grid-adjacent tiles differ by exactly one cardinal unit. Any
            # other delta (zero, diagonal, or non-unit) signals a tunnel hop
            # for v1 (paths never repeat a tile in one direction of travel,
            # per design_new.md "v1 path geometry forbids tile re-visits in
            # a single direction of travel" — train_controller.py:1281).
            if (abs(dx), abs(dy)) != (1, 0) and (abs(dx), abs(dy)) != (0, 1):
                return True
        return False

    def _connectivity_check_was_conclusive(self, train) -> bool:
        """Return True if _path_needs_cab_flip had enough data to decide.

        Specifically: the head tile was found in the path and there was a
        next tile to compare against.  If the head wasn't found, the
        connectivity check was inconclusive and the caller should fall back.
        """
        if not train:
            return False
        path_tiles = self._get_effective_path_tiles()
        if not path_tiles or len(path_tiles) < 2:
            return False
        head_tile = self._get_head_tile()
        if not head_tile:
            return False
        for i, entry in enumerate(path_tiles):
            if (entry[0], entry[1]) == head_tile:
                return i + 1 < len(path_tiles)  # has a next tile
        return False  # head not in path

    def _get_path_direction_at_head(self, train: "Train") -> Optional[Direction]:
        """Determine the path's travel direction at or near the train's head tile.

        Looks up the head tile in current_path. If the path entry has a stored
        direction (3rd element), uses that. Otherwise infers direction from the
        vector to the next tile in the path.

        If the head tile is the last tile in the path, looks backward to infer
        the direction from the previous tile.

        Falls back to using the first two tiles of the path if the head tile
        is not found in the path (e.g., during initial placement).

        Args:
            train: Train with current_path and position set.

        Returns:
            Direction the path is heading at the train's head, or None.
        """
        path_tiles = self._get_effective_path_tiles()
        if not path_tiles or len(path_tiles) < 2:
            return None

        head_tile = self._get_head_tile()
        if not head_tile:
            # Fallback: use first two tiles
            return self._direction_from_tiles(path_tiles[0], path_tiles[1])

        # Find head tile in path
        head_index = -1
        for i, entry in enumerate(path_tiles):
            if (entry[0], entry[1]) == head_tile:
                head_index = i
                break

        if head_index >= 0:
            # Found head in path - get direction from this entry or next tile
            entry = path_tiles[head_index]
            if len(entry) > 2 and entry[2] is not None:
                return entry[2]
            # Infer from next tile
            if head_index + 1 < len(path_tiles):
                return self._direction_from_tiles(entry, path_tiles[head_index + 1])
            # Head is last tile - infer from previous tile (same direction)
            if head_index > 0:
                return self._direction_from_tiles(path_tiles[head_index - 1], entry)

        # Head tile not found in path - fallback to first two tiles
        return self._direction_from_tiles(path_tiles[0], path_tiles[1])

    @staticmethod
    def _direction_from_tiles(tile_a: Tuple, tile_b: Tuple) -> Optional[Direction]:
        """Compute direction from tile_a to tile_b."""
        dx = tile_b[0] - tile_a[0]
        dy = tile_b[1] - tile_a[1]
        if dx > 0:
            return Direction.EAST
        elif dx < 0:
            return Direction.WEST
        elif dy > 0:
            return Direction.NORTH
        elif dy < 0:
            return Direction.SOUTH
        return None

    @staticmethod
    def _dominant_direction_from_tiles(
        tile_a: Tuple, tile_b: Tuple
    ) -> Optional[Direction]:
        """Compute the dominant cardinal direction from tile_a to tile_b.

        Unlike ``_direction_from_tiles`` (which prioritises dx), this method
        picks the axis with the larger absolute displacement.  This is useful
        for computing the *overall* direction of a multi-tile path where the
        start→end vector may be diagonal.

        Args:
            tile_a: Starting tile (x, y, ...).
            tile_b: Ending tile (x, y, ...).

        Returns:
            The cardinal Direction along the axis with greater displacement,
            or None if both displacements are zero.
        """
        dx = tile_b[0] - tile_a[0]
        dy = tile_b[1] - tile_a[1]
        if dx == 0 and dy == 0:
            return None
        if abs(dx) >= abs(dy):
            return Direction.EAST if dx > 0 else Direction.WEST
        else:
            return Direction.NORTH if dy > 0 else Direction.SOUTH

    # -------------------------------------------------------------------------
    # ATO (Automatic Train Operation) Logic
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # Future-Aware ATO Helpers
    # -------------------------------------------------------------------------

    # ATO curve sampling, kinematic forward-simulation, handle-selection,
    # and notch helpers live on the `ATOComputer` sub-module at `self.ato`.
    # Callers use `self.ato.sample_curve_at(...)`, `self.ato.simulate_forward(...)`,
    # `self.ato.compute_handle(...)`, `self.ato.calculate_power_notch(...)`,
    # and `self.ato.calculate_braking_notch_for_speed(...)` directly.

    def calculate_speed_curves(
        self,
        max_distance_cm: float = 500.0,
        lookahead_blocks: Optional[List[Dict]] = None,
        lookahead_signals: Optional[List[Dict]] = None,
        lookahead_stations: Optional[List[Dict]] = None,
        target_stop_cm: Optional[float] = None,
        override_reason_code: Optional[AtcLimitReasonCode] = None,
        override_reason_detail: Optional[str] = None,
        path_end_reason_code: Optional[AtcLimitReasonCode] = None,
    ) -> List[Dict]:
        """Build the ATC/ATO speed-limit curve for this controller.

        Thin forwarding shim: the computation lives on the
        `SpeedCurveCalculator` sub-module (see `train_controller.speed_curves`),
        constructed lazily via `_get_speed_curve_calculator()`. The full
        argument contract is documented on
        `SpeedCurveCalculator.calculate_speed_curves`.
        """
        return self._get_speed_curve_calculator().calculate_speed_curves(
            max_distance_cm=max_distance_cm,
            lookahead_blocks=lookahead_blocks,
            lookahead_signals=lookahead_signals,
            lookahead_stations=lookahead_stations,
            target_stop_cm=target_stop_cm,
            override_reason_code=override_reason_code,
            override_reason_detail=override_reason_detail,
            path_end_reason_code=path_end_reason_code,
        )

    # Toast emission and drain live on the `Notifications` sub-module
    # attached as `self.notifications`. External callers use
    # `controller.notifications.get_and_clear()`; controller-internal
    # writers use `self.notifications.emit(msg)`.

    # Operator commands (power_on_cmd, power_off_cmd, set_cab, set_mode,
    # set_handle, set_manual_mode_type, set_manual_power_percent, enqueue_command)
    # and the per-tick command inbox / drain live on the `OperatorCommands`
    # sub-module attached as `self.commands`. External callers use
    # `controller.commands.<method>(...)`. The drain itself is invoked at the
    # top of `tick(...)` as `self.commands._drain_command_inbox(...)`.

    # ATO run lifecycle (`go`, `stop`) lives on the `ATOComputer` sub-module
    # at `self.ato`. Callers use `controller.ato.go()` / `controller.ato.stop()`
    # directly.

    # -------------------------------------------------------------------------
    # State Transitions
    # -------------------------------------------------------------------------

    def transition_to_ready(self) -> None:
        """Transition to the READY state (powered, stopped, not dwelling).

        Per design_new.md, READY is the unified powered-stopped state
        regardless of cab selection. Cab selection and route assignment are
        orthogonal fields and are not cleared here; their lifecycle is owned
        by `set_cab(...)` and `RouteExecutor` respectively.
        """
        self.ops.state = TrainState.READY
        self.ato.running = False
        # path_backed_run_active is derived from path/route/destination context.

    # -------------------------------------------------------------------------
    # Route Executor Commands (for RouteExecutor integration)
    # -------------------------------------------------------------------------

    def depart(self) -> Tuple[bool, Optional[str]]:
        """
        Depart-acceptance entry point called by `RouteExecutor` when live
        authority exists.

        Per design_new.md line 1812 "TrainController.depart() canonical
        contract", the accepted entry states are `{READY, RUNNING_MOVING}`:
        the second case admits the reactive speed-based evaluation having
        already advanced state ahead of this call. From RUNNING_MOVING the
        call is an idempotent success that does not change state but still
        applies side effects (in ATO, invokes `ato_go()` so `ato_running = True`
        is written if the reactive transition has not already done so).

        Returns:
            Tuple[bool, Optional[str]]: (success, diagnostic_reason).
            On success: (True, None). On rejection: (False, "<reason>").
        """
        # Per design_new.md line 1812: precondition checks before state guards.
        if self.path_projection.received_path_plan is None:
            return (False, "no active path")
        if self.eb.active:
            return (False, "EB active")
        if self.route_hooks.phase_latches.power_off:
            return (False, "pending power-off")

        train = self._get_train()
        if train is None:
            return (False, "train not found")

        if self.ops.state not in (TrainState.READY, TrainState.RUNNING_MOVING):
            return (False, f"invalid state: {self.ops.state.value}")

        # design_new.md "Event log": a TRAIN_DEPARTED event marks the
        # READY -> RUNNING_MOVING transition only, not the idempotent re-calls
        # depart() accepts while already moving.
        from notification_center import EventKind as _EventKind

        _was_ready = self.ops.state == TrainState.READY

        # ATO mode: delegate to ato_go() which is the canonical writer of
        # ato_running and handles both READY (full path) and RUNNING_MOVING
        # (idempotent) entries.
        if self.commands.control_mode == "ato":
            result = self.ato.go()
            if result.success:
                if _was_ready:
                    self._emit_event(_EventKind.TRAIN_DEPARTED)
                return (True, None)
            return (False, result.reason if hasattr(result, "reason") else "ato_go rejected")

        # Manual mode: requires a cab and facing direction. From
        # RUNNING_MOVING the call is an idempotent success.
        # path_backed_run_active is now derived from path/route/destination
        # context; no field write needed here.
        if self.commands.cab not in ("A", "B"):
            return (False, "no cab selected")
        if self._facing_direction() is None:
            return (False, "facing direction not set")
        if self.ops.state == TrainState.READY:
            self.ops.state = TrainState.RUNNING_MOVING
            self._emit_event(_EventKind.TRAIN_DEPARTED)
        return (True, None)

    def _emit_dwell_diagnostic(self, message: str) -> None:
        """Emit a dwell-transition rejection diagnostic through
        NotificationCenter (design_new.md: rejected dwell transitions leave
        state unchanged and emit a diagnostic). Best-effort: resolves the sink
        defensively and no-ops when no NotificationCenter is wired (isolated
        unit-test controllers), and never raises into the caller."""
        try:
            sc = getattr(self, "simulation_controller", None)
            nc = getattr(sc, "notification_center", None) if sc is not None else None
            if nc is None:
                return
            from notification_center import Severity as _Severity

            nc.emit(
                source="TrainController",
                kind="diagnostic",
                severity=_Severity.WARNING,
                event_time=self.commands._estimate_event_time(),
                train_id=self.train_id,
                payload={"message": message},
            )
        except Exception:
            pass

    def _emit_event(self, kind: str, payload=None, severity=None, path_id=None) -> None:
        """Emit an operational event-log notification through NotificationCenter
        (design_new.md "Event log"). Best-effort: resolves the sink defensively,
        no-ops when none is wired (isolated unit-test controllers), and never
        raises into the caller."""
        try:
            sc = getattr(self, "simulation_controller", None)
            nc = getattr(sc, "notification_center", None) if sc is not None else None
            if nc is None:
                return
            from notification_center import Severity as _Severity

            nc.emit(
                source="TrainController",
                kind=kind,
                severity=severity if severity is not None else _Severity.INFO,
                event_time=self.commands._estimate_event_time(),
                train_id=self.train_id,
                path_id=path_id,
                payload=payload or {},
            )
        except Exception:
            pass

    def enter_station_dwell(self) -> Tuple[bool, Optional[str]]:
        """Dwell-entry transition called by Phase 1 dwell-entry step.

        Per design_new.md, RouteExecutor invokes this directly when its
        preconditions are met. Acceptance preconditions (design_new.md:1901):
        `state == READY` and `eb.active == False`. On acceptance, transitions
        controller state to RUNNING_AT_STATION and clears ato_running because
        dwell entry is a run-ending event. On rejection, leaves controller
        state unchanged and emits a NotificationCenter diagnostic.

        Returns:
            Tuple[bool, Optional[str]]: (True, None) on success;
            (False, reason) on rejection.
        """
        if self.ops.state != TrainState.READY:
            reason = f"enter_station_dwell requires READY, state is {self.ops.state.value}"
            self._emit_dwell_diagnostic(reason)
            return (False, reason)
        if self.eb.active:
            reason = "enter_station_dwell rejected: EB latched"
            self._emit_dwell_diagnostic(reason)
            return (False, reason)
        self.ops.state = TrainState.RUNNING_AT_STATION
        self.ato.running = False
        from notification_center import EventKind

        self._emit_event(EventKind.ENTERED_DWELL)
        return (True, None)

    def finish_dwell(self) -> Tuple[bool, Optional[str]]:
        """Dwell-end transition called by Phase 1 dwell-complete step.

        Acceptance precondition (design_new.md:1902): `state ==
        RUNNING_AT_STATION`. EB latch state is NOT a precondition — the
        Phase-3 teardown caller `RouteExecutor.remove_train_route_run(...)`
        may invoke this with `eb.active == True` (the OFF cleanup that
        follows clears the latch). On acceptance, lands in READY (powered,
        stopped, not dwelling — cab selection is preserved as an orthogonal
        field); does not clear received_path_plan and does not start
        movement. On rejection, leaves controller state unchanged and emits a
        NotificationCenter diagnostic.

        Returns:
            Tuple[bool, Optional[str]]: (True, None) on success;
            (False, reason) on rejection.
        """
        if self.ops.state != TrainState.RUNNING_AT_STATION:
            reason = (
                f"finish_dwell requires RUNNING_AT_STATION, state is "
                f"{self.ops.state.value}"
            )
            self._emit_dwell_diagnostic(reason)
            return (False, reason)
        self.ops.state = TrainState.READY
        return (True, None)

    def on_path_complete(self) -> None:
        """Called by PathDispatcher.try_complete_active_path on accepted completion.

        Per design_new.md: transition to READY. Does not clear
        received_path_plan (the dispatcher has already transitioned
        Path.status), does not enter dwell itself.
        """
        self.ato.running = False
        # path_backed_run_active is derived from path/route/destination context.
        self.ops.state = TrainState.READY

    def complete_power_off(self) -> None:
        """Controller-owned OFF-cleanup entry point (design_new.md:282, :1808).

        Invoked by the Phase-3 ``pending_power_off`` consumer (step ``886a`` of
        the Power-off procedure). It is the only ``OFF``-writing site besides the
        startup default, so the Operational state machine stays the single writer
        of ``state``. Callable only while ``power_off`` is latched; on commit it
        transitions ``state`` to ``OFF``, clears the active cab selection, clears
        ``eb.active``, clears ``ato.running``, and consumes ``pending_power_off``.
        """
        if not self.route_hooks.phase_latches.power_off:
            return
        self.ops.state = TrainState.OFF
        self.commands.cab = None
        # design_new.md:1895 — clear the EB latch through the EB-owned helper so
        # the EB Workflow stays the single writer of `eb.active`.
        self.eb.clear()
        self.ato.running = False
        self.route_hooks.phase_latches.consume_power_off()

    # -------------------------------------------------------------------------
    # Serialization
    # -------------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Convert controller state to dictionary for JSON serialization.

        Live runtime state for JSON output (per design_new.md).
        Persistent/configuration data lives on `Train.to_dict()`.
        """
        # Helper to convert infinity to None for JSON serialization
        def safe_float(value: float) -> Optional[float]:
            if value == float("inf") or value == float("-inf"):
                return None
            return value

        # Live snapshot of the derived lookahead (computed once for this dict)
        live_lookahead = self._calculate_lookahead()

        span_dict = self._span_to_dict()

        return {
            "train_id": self.train_id,
            "power_on": self.power_on,
            "eb_active": self.eb.active,
            "pending_notifications": self.notifications.pending.copy(),
            "cab": self.commands.cab,
            "control_mode": self.commands.control_mode,
            "manual_mode_type": self.manual.manual_mode_type,
            "manual_power_percent": self.manual.manual_power_percent,
            "travel_direction": self.travel_direction,
            # Train facing (a `Direction` name) derived from (cab, current_span);
            # replaces the removed `Train.facing_direction` on the wire so the
            # client reads facing from the controller payload.
            "facing_direction": (lambda d: d.name if d is not None else None)(
                self._facing_direction()
            ),
            "state": self.ops.state.value,
            "target_speed": self.target_speed,
            "handle_position": self.ops.handle_position.value,
            "ato_running": self.ato.running,
            "ato_destination_block_id": self.path_projection.ato_destination_block_id,
            "ato_path_id": self.path_projection.ato_path_id,
            "assigned_path_id": self.path_projection.assigned_path_id,
            "path_backed_run_active": self.path_projection.path_backed_run_active,
            # Export the live received_path_plan using the canonical
            # received_path_plan schema (design_new.md:1740, 2187): exactly
            # path_id, resource_sequence, authority_end_block_id. Dispatcher-
            # only requested-target / lifecycle / progress fields stay on the
            # Path record and must not leak into the controller projection.
            "received_path_plan": self.path_projection.received_path_plan_dict(),
            "clearance_ahead_cm": safe_float(self.atc.clearance_ahead_cm),
            "atc_speed_limit": safe_float(self.atc.speed_limit),
            "current_block_speed_limit": safe_float(self.atc.current_block_speed_limit),
            "speed_limit_reason": self.speed_limit_reason,
            # Current physical span
            "span": span_dict,
            # Include lookahead for display (live computed here)
            "lookahead": [
                {
                    "distance_cm": lp.distance_cm,
                    "speed_limit_kmh": safe_float(lp.speed_limit_kmh),
                    "block_id": lp.block_id,
                    "signal_state": lp.signal_state.value if lp.signal_state else None,
                    "station_id": lp.station_id,
                    "is_path_end": lp.is_path_end,
                }
                for lp in live_lookahead
            ],
            # Include ATO info
            "ato_info": {
                "path_id": self.ato.info.path_id,
                "destination_block_id": self.ato.info.destination_block_id,
                "destination_station_name": self.ato.info.destination_station_name,
                "path_heading": self.ato.info.path_heading,
                "required_cab": self.ato.info.required_cab,
            }
            if self.ato.info
            else None,
            # Live speed and stopped predicate are controller-owned per
            # design_new.md "Units contract".
            "current_speed": float(getattr(self.physics_sync, "current_speed_kmh", 0.0) or 0.0),
            "is_stopped": self.is_stopped(),
            # Diagnostic: reason for current handle position
            "handle_reason": self.ops.handle_reason,
            # ATO ETA: integrates the operational_limit speed curve to the
            # current target stop. None when no path/target/curves.
            "time_to_target_sec": self._calculate_time_to_target_sec(),
            # Model-scale factor (model cm/s per prototype km/h). Clients use
            # this to convert lookahead distances (model cm) and prototype
            # speeds (km/h) into ETA seconds when time_to_target_sec is None.
            "speed_conversion_factor": get_speed_conversion_factor(),
            # Run instance identifier — populated when a RouteRun is active.
            # Composed as `${route_id}#${run_number}` so the cab display can
            # tell apart multiple runs of the same route.
            "route_run_id": (
                f"{self.route_executor.execution_state_for(self.train_id).route_id}"
                f"#{self.route_executor.execution_state_for(self.train_id).run_number}"
                if self.route_executor is not None
                and self.route_executor.execution_state_for(self.train_id) is not None
                else None
            ),
            # UI projection: True while an operator route teardown (abort or
            # deactivate) is latched but not yet consumed in Phase 3. The layout
            # renderer tints the train light purple during this window. Excludes
            # power_off (ends in OFF, not a route teardown).
            "route_teardown_pending": (
                self.route_hooks.phase_latches.is_route_teardown_pending()
            ),
            # Live "current path error" projection for the train panel badge
            # and the Quick Route no-path toast. Surfaces the active
            # RouteRun.last_error ONLY while the train currently has no live
            # authority (path_backed_run_active is False). A successful
            # acquisition clears last_error (RouteExecutor), and a live path
            # suppresses it here, so this reflects a CURRENT inability to route
            # rather than a stale historical error.
            "path_error": (
                (lambda es: (
                    es.last_error.to_dict()
                    if es is not None
                    and getattr(es, "last_error", None) is not None
                    and not self.path_projection.path_backed_run_active
                    else None
                ))(
                    self.route_executor.execution_state_for(self.train_id)
                    if self.route_executor is not None
                    else None
                )
            ),
        }

    def _span_to_dict(self) -> Optional[Dict[str, Any]]:
        """Serialize `current_span` into the live-position payload, or None."""
        span = getattr(self.physics_sync, "current_span", None)
        if span is None:
            return None
        return {
            "end_a": {
                "tile": list(span.end_a.tile),
                "distance_cm": span.end_a.distance_cm,
                "entry_edge": span.end_a.entry_edge,
            },
            "end_b": {
                "tile": list(span.end_b.tile),
                "distance_cm": span.end_b.distance_cm,
                "entry_edge": span.end_b.entry_edge,
            },
            "occupied_tiles": [list(t) for t in span.occupied_tiles],
            "occupied_blocks": list(span.occupied_blocks),
            # leading_end is the cab-derived head end ("a"/"b"), or None when no
            # cab is selected (a no-cab train has no designated head; the span
            # itself carries no head/tail label per the design_new.md TrainSpan
            # schema). Clients render dual end-markers when it is null.
            "leading_end": (
                None
                if self.commands.cab is None
                else ("a" if self._head_end_label() == "end_a" else "b")
            ),
        }

    def _calculate_time_to_target_sec(self) -> Optional[float]:
        """Estimate seconds until the train reaches its current ATO stop target.

        Integrates the operational_limit speed curve from the train head
        (distance_cm == 0) up to target_stop_cm. For each segment we track
        a running ``v_actual`` so the integration reflects the train's real
        motion, not just the curve ceiling: at the start of a segment the
        train is at ``v_actual``; over the segment it accelerates toward
        the segment's curve endpoint, bounded by ``_ETA_ACCEL_CM_PER_S2``.
        The segment's average speed is ``(v_actual_start + v_actual_end)/2``.

        Falls back to a coarse kinematic estimate when the curve is too
        sparse (single sample or no segments before target) but a real
        target distance is still published.

        Returns None when there is no useful target — train unpowered or
        target_stop_cm is too close to register as meaningful (<10 cm).
        """
        # Gate: only meaningful when the train can actually move toward something.
        if not self.power_on:
            return None
        try:
            payload = self._compute_canonical_lookahead()
        except Exception:
            return None
        la = (payload or {}).get("lookahead") or {}
        curves = la.get("speed_curves") or []
        target_cm = la.get("target_stop_cm") or 0.0
        if target_cm < 10:
            return None

        cm_per_second_per_kmh = get_speed_conversion_factor()  # model cm/s per prototype km/h
        current_kmh = float(getattr(self.physics_sync, "current_speed_kmh", 0.0) or 0.0)

        if len(curves) < 2:
            return self._kinematic_time_to_target_sec(target_cm, current_kmh)

        total_sec = 0.0
        v_actual_kmh = current_kmh  # running actual speed at d_prev
        prev = curves[0]
        for cur in curves[1:]:
            d_prev = prev.get("distance_cm", 0.0) or 0.0
            d_cur = cur.get("distance_cm", 0.0) or 0.0
            if d_cur > target_cm:
                break
            delta_cm = d_cur - d_prev
            if delta_cm <= 0:
                prev = cur
                continue

            v_curve_cur = cur.get("operational_limit", 0.0) or 0.0
            v_start_kmh = v_actual_kmh
            # Acceleration-bounded end-of-segment speed: starting from
            # v_start_kmh, how fast can the train be at the end of this
            # segment if it accelerates constantly at _ETA_ACCEL_CM_PER_S2?
            # v_end_cms = sqrt(v_start_cms^2 + 2*a*delta_cm). Then clamp
            # by the curve ceiling (the train cannot exceed it).
            v_start_cms = v_start_kmh * cm_per_second_per_kmh
            v_end_unbounded_cms_sq = (
                v_start_cms * v_start_cms
                + 2.0 * self._ETA_ACCEL_CM_PER_S2 * delta_cm
            )
            v_end_unbounded_kmh = (
                math.sqrt(max(0.0, v_end_unbounded_cms_sq))
                / cm_per_second_per_kmh
            )
            v_end_kmh = min(v_curve_cur, v_end_unbounded_kmh)
            v_avg_kmh = (v_start_kmh + v_end_kmh) / 2.0
            if v_avg_kmh < 0.5:
                prev = cur
                continue
            total_sec += delta_cm / (v_avg_kmh * cm_per_second_per_kmh)
            v_actual_kmh = v_end_kmh
            prev = cur

        return total_sec if total_sec > 0 else None

    # Default acceleration used by the ETA estimator when the curve does not
    # constrain the train.  Value is in **model** cm/s² (not real-world cm/s²).
    # 0.5 model cm/s² ≈ 0.8 real m/s² at 1:160 N-scale, matching a typical
    # metro service rate.  Intentionally conservative so ETA runs slightly
    # long rather than misleadingly short.
    _ETA_ACCEL_CM_PER_S2 = 0.5

    def _kinematic_time_to_target_sec(
        self, target_cm: float, current_kmh: float
    ) -> Optional[float]:
        """Coarse ETA when the speed curve is too sparse to integrate.

        Assumes the train accelerates from ``current_kmh`` at
        ``_ETA_ACCEL_CM_PER_S2`` until covering ``target_cm``. Used as a
        fallback when the lookahead curve has fewer than two samples but
        a real target distance is still published.
        """
        if target_cm <= 0:
            return None
        cm_per_second_per_kmh = get_speed_conversion_factor()  # model cm/s per prototype km/h
        v0_cms = max(0.0, current_kmh) * cm_per_second_per_kmh
        a = self._ETA_ACCEL_CM_PER_S2
        # v² = v0² + 2*a*d → v = sqrt(v0² + 2*a*d). Time = (v - v0) / a.
        v_end_cms = math.sqrt(v0_cms * v0_cms + 2.0 * a * target_cm)
        time_sec = (v_end_cms - v0_cms) / a if a > 0 else None
        if time_sec is None or time_sec <= 0:
            return None
        return time_sec

    def get_current_span(self) -> Optional[Any]:
        """Return the controller's canonical live span (or None).

        Per design_new.md "Data Flow To UI" — the doc-canonical replacement
        read interface for current physical span. ``current_span`` remains
        the canonical TrainSpan storage written by
        ``on_physics_updated(...)``; this accessor is the public read used
        by ``RouteExecutor.tick_train_phase1(...)`` and
        ``PathDispatcher.create_path(...)`` to discover the train's current
        span without reaching into the dataclass field directly.
        """
        return self.physics_sync.current_span

    def _distance_zero_atc_anchor(
        self,
    ) -> Tuple[float, Optional[AtcLimitReasonCode]]:
        """The published ATC speed limit + reason at the train head.

        Primary source is the canonical lookahead's first `speed_curves`
        anchor (distance 0) — the single source of truth for forward authority
        per design_new.md "Lookahead is a pure derived value". The curve walks
        the path's tile geometry, so it measures real clearance even where a
        block's `length_cm` metadata is 0; this is why the ATC limit is sourced
        here rather than from the `length_cm`-summing block walk in
        `clearance_ahead()`. The no-authority / no-cab / head-off-path payloads
        stamp their own distance-0 anchor (with a structured reason code), so
        those reasons flow through unchanged.

        Only when the payload carries no `speed_curves` anchor at all — a
        degenerate path with no usable tile geometry — fall back to the
        clearance-based `speed_limit_with_code()` recompute (which reads the
        `clearance_ahead_cm` refreshed just before this call) so the published
        limit still has a defined value.
        """
        payload = self._compute_canonical_lookahead()
        curves = (payload.get("lookahead") or {}).get("speed_curves") or []
        if curves:
            anchor = curves[0]
            limit = float(anchor.get("atc_limit", 0.0) or 0.0)
            code_str = anchor.get("atc_limit_reason_code")
            code = AtcLimitReasonCode(code_str) if code_str else None
            return limit, code
        limit, _reason, code = self.atc.speed_limit_with_code()
        return limit, code

    def get_lookahead(self) -> Dict[str, Any]:
        """Return the canonical lookahead payload per design_new.md.

        Live recompute on every call (no cache). The payload shape is:

        - top level: `train_id`, `head_tile`, `head_distance_cm`,
          `travel_direction`, `cab`, `speed_limit_reason`, `lookahead`
        - nested `lookahead`: `blocks`, `signals`, `switches`, `stations`,
          `clearance_cm`, `safety_margin_cm`, `authority_boundary_delta_cm`,
          `speed_curves`

        See the schema described in the design doc for each nested
        entry's keys.
        """
        return self._compute_canonical_lookahead()

    def _lookahead_memo_key(self):
        """Cheap, COMPLETE key for the lookahead memo, or None to skip caching.

        Returns a tuple of every input the payload depends on that is not
        already pinned by one `_lookahead_world_version`:
          - the network world version — bumped at every tick phase boundary, so
            within one version every network / path / config input the
            computation reads (block state, signal state, switch position +
            lock, active-path geometry, speed profile, max_speed,
            safety margin) is constant;
          - the controller-local inputs that can change between calls inside a
            single version: head tile + head distance (physics moves the head),
            active cab, and EB state.
        `current_speed_kmh` is deliberately absent — the braking-curve envelope
        is purely position-based and never reads it (verified in
        `calculate_speed_curves`). Returns None when there is no network.
        """
        net = self.network
        if net is None:
            return None
        return (
            getattr(net, "_lookahead_world_version", 0),
            self._get_head_tile(),
            self._get_head_distance_cm(),
            self.commands.cab,
            self.eb.active,
        )

    def _compute_canonical_lookahead(self) -> Dict[str, Any]:
        """Canonical lookahead payload (design_new.md "Lookahead is a pure
        derived value"). The computation itself lives in
        `_get_lookahead_builder().compute_payload()`; this wrapper adds a tick-local
        memo.

        The lookahead is recomputed many times per tick (cab-direction / ATC /
        ATO probes) with identical inputs, so while a tick is running
        (`network._lookahead_memo_active`) the payload is cached on the
        controller keyed by `_lookahead_memo_key()` and reused for the duration
        of one `(version, head, cab, eb)`.

        This is a backend recompute-avoidance, never a frontend cache: the memo
        is consulted ONLY mid-tick, so every between-tick / paused / UI / test
        caller of `get_lookahead()` runs the live computation and can never
        observe a stale payload. Within a tick the world version (bumped at each
        phase boundary) plus the head/cab/eb key guarantees freshness;
        `ATCS_LOOKAHEAD_SHADOW=1` proves it by recomputing on every memo hit and
        asserting byte-equality with the cached payload.
        """
        net = self.network
        if net is None or not getattr(net, "_lookahead_memo_active", False):
            # Outside a tick: always recompute live; never serve/store a cache.
            return self._get_lookahead_builder().compute_payload()

        key = self._lookahead_memo_key()
        memo = self._lookahead_memo
        if key is not None and memo is not None and memo[0] == key:
            if not _LOOKAHEAD_SHADOW:
                return memo[1]
            fresh = self._get_lookahead_builder().compute_payload()
            if fresh != memo[1]:
                raise AssertionError(
                    f"Stale lookahead memo for train {self.train_id!r}: "
                    f"cached payload != fresh recompute at key={key!r}. The "
                    "memo key is missing an input the computation reads."
                )
            return fresh

        result = self._get_lookahead_builder().compute_payload()
        if key is not None:
            self._lookahead_memo = (key, result)
        return result

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], network: "RailwayNetwork"
    ) -> "TrainController":
        """Reconstruct from dictionary after deserialization."""
        handle_str = data.get("handle_position", "N")
        try:
            handle = HandlePosition(handle_str)
        except ValueError:
            handle = HandlePosition.N

        state_str = data.get("state", "off")
        try:
            state = TrainState(state_str)
        except ValueError:
            state = TrainState.OFF

        # Per design_new.md, travel_direction / target_speed / speed_limit_reason
        # / ato_destination_block_id / ato_path_id are derived properties; we
        # do not seed them on construction. ato_destination_block_id / ato_path_id
        # come back through `received_path_plan` once a path is registered with
        # the dispatcher.
        controller = cls(
            train_id=data["train_id"],
            network=network,
            ato_running=data.get("ato_running", False),
            # path_backed_run_active is now a derived @property; no
            # constructor arg accepted. Any persisted value is ignored
            # because the property recomputes from path/route context.
        )
        # `state` and `handle_position` live on the `OperationalStateMachine`
        # sub-module; restore after construction.
        controller.ops.state = state
        controller.ops.handle_position = handle
        # Sub-module fields are created in `__post_init__`; restore
        # persisted values after construction.
        controller.eb.active = data.get("eb_active", False)
        controller.manual.manual_mode_type = data.get("manual_mode_type", "simulation")
        controller.manual.manual_power_percent = int(
            data.get("manual_power_percent", 0) or 0
        )
        controller.commands.cab = data.get("cab")
        controller.commands.control_mode = data.get("control_mode", "ato")
        return controller


# =============================================================================
# Speed Calculation Functions (from train_onboard.py)
# =============================================================================


# =============================================================================
# Factory function
# =============================================================================


def create_controller_for_train(
    train_id: str,
    network: "RailwayNetwork",
    path_dispatcher: Optional["PathDispatcher"] = None,
    route_executor: Optional["RouteExecutor"] = None,
) -> TrainController:
    """
    Create a TrainController instance for a train.

    Args:
        train_id: ID of the train to control
        network: Railway network
        path_dispatcher: PathDispatcher for reading path data (single source of truth)
        route_executor: RouteExecutor that owns the canonical route_registry

    Returns:
        TrainController instance
    """
    return TrainController(
        train_id=train_id,
        network=network,
        path_dispatcher=path_dispatcher,
        route_executor=route_executor,
    )
