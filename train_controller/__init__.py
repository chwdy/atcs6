"""
Train Controller - Unified ATC + ATO + Command Handling

Definition:
This module provides the TrainController class which is the single authority for
train operational state and control decisions. It merges the functionality of:

- OnboardComputer: Power, mode, handle state, command validation
- TrainATC: Clearance calculation, speed limits, braking curves

TrainController is the onboard control computer that:
- Controls power on/off and emergency stop
- Stores the current control mode (ATO vs Manual)
- Derives the travel direction from the live authority (there is no operator cab)
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
from typing import TYPE_CHECKING, Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from constants import (
    BRAKING_LOCK_ACCEL_HEADROOM_KMH,
    BRAKING_LOCK_LIMIT_MARGIN_KMH,
    CAUTION_SPEED_LIMIT_KMH,
    PLATFORM_SAFETY_MARGIN_CM,
    STOPPED_SPEED_THRESHOLD_KMH,
    TRACK_END_SAFETY_MARGIN_CM,
)
from runtime_payloads import ErrorRecord as _ErrorRecord
from enums import AtcLimitReasonCode, BlockState, ControllerCondition, Direction, HandlePosition, SignalState, TrainStatus
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
ATC_NO_BLOCK_FALLBACK_CM = 100000.0

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
    - Throttle/brake handle position
    - Control mode (ATO/Manual)
    - Travel direction (derived from the live authority; no operator cab)
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

    # Edge trigger for the ATC diagnostic overspeed warning: True while the
    # train sits above `OVERSPEED_WARN_THRESHOLD_KMH`, so one excursion emits
    # one warning instead of one per tick. A plain transient flag, not a latch.
    _overspeed_warned: bool = False

    # The distance-0 ATC reason code this controller last reported, so a change
    # is reported once and a steady code is silent. Compared against what was
    # emitted rather than against last tick, so a code flickering between two
    # values still reports each real change. A plain transient flag, not a latch.
    _last_emitted_atc_reason_code: Optional[str] = None

    # True while every wired block this train could show up on — its span
    # blocks and its held blocks — reads confirmed clear, so no physical
    # detection backs the calculated position and the train may be gone
    # (design_new.md "Power", Presence unknown). Written each tick by
    # `SimulationController._detect_block_occupancy`; stays False in a session
    # that detects nothing physical.
    presence_unknown: bool = False

    # -------------------------------------------------------------------------
    # Power & Safety State
    # -------------------------------------------------------------------------
    # `powered` is the single stored power flag (design_new.md "Controller
    # state machine"): one of the three stored inputs the derived
    # `train_status` reads. Written by `power_on_cmd()` / `complete_power_off()`
    # (and by `set_mode(...)` powering an OFF train up as a side effect).
    # `power_on` is a read-only alias for it, exposed as a @property below.
    powered: bool = False
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
    # the one `NotificationCenter` stream, filtered by this train's entity ref.

    # -------------------------------------------------------------------------
    # Mode State
    # -------------------------------------------------------------------------
    # `control_mode` lives on the `OperatorCommands` sub-module at
    # `self.commands`. External readers use `controller.commands.control_mode`.
    # There is no operator cab: the head end is derived from the live authority
    # (design_new.md "Authority-side head derivation") and framed as
    # `controller._framed_head_end`.
    # `manual_mode_type` lives on the `ManualDriving` sub-module at
    # `self.manual` in `__post_init__`. External readers use
    # `controller.manual.manual_mode_type`.

    # -------------------------------------------------------------------------
    # Operational State
    # -------------------------------------------------------------------------
    # The derived `train_status` read and `handle_position` live on the
    # `OperationalStateMachine` sub-module at `self.ops`. External readers use
    # `controller.ops.train_status` and `controller.ops.handle_position`.
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
    # `controller.ato.engaged`, `controller.ato.info`,
    # `controller.ato.debug`; the intent methods are
    # `controller.ato.engage()` and `controller.ato.disengage()`.
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
    # Controller mailbox (progress reports + latches; per design_new.md)
    # -------------------------------------------------------------------------
    #
    # `queued_progress_reports` lives on the `PhysicsSync` sub-module
    # attached as `self.physics_sync` in `__post_init__`. External
    # callers read via `controller.physics_sync.queued_progress_reports`.
    #
    # The four Action latches (`path_cancel_request`, `route_abort`,
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
    # (power_on_cmd, set_mode, etc.). Each command is enqueued into
    # `command_inbox` and then drained by `_drain_command_inbox(...)` at the
    # very top of `tick(...)`. Commands are processed in the canonical kind
    # order (power_on -> set_mode -> set_manual_mode_type ->
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
        from train_controller.cab_policy import CabFlipPolicy
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
        self.cab_policy = CabFlipPolicy(self)

        # Which physical span end ("end_a"/"end_b") the span is currently framed
        # with as the head. The span keeps its `end_a`-head framing default until
        # a new authority moves the derived head to the other end, when the
        # driving tick re-expresses the frame (design_new.md "Authority-side head
        # derivation" / "Reversed-frame re-expression"). Bookkeeping only; the
        # head itself is derived from the live authority.
        self._framed_head_end: str = "end_a"

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
            # resolve(...) applies the model/instance overlay so a partial
            # instance profile is never driven directly (design_new.md
            # "SpeedProfile", Model profiles and instance profiles). For a model
            # profile it returns the profile unchanged.
            profile = self.network.speed_profiles.resolve(profile_id)
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

    def braking_distance_cm(self, delta_seconds: float = 0.0) -> float:
        """Forward distance the braking-distance authority lock reserves ahead of
        this train (design_new.md "Braking-distance lock").

        It is the stopping distance at the service rate
        (`SpeedProfile.deceleration_rate`) with the armed target's safety margin
        intact (`atc.effective_safety_margin()`), computed by
        `speed_curves.calculate_braking_distance(...)`, plus one tick of travel at
        the current speed as a cushion for tick and detector-confirmation lag.

        The stopping-distance speed is not the raw current speed but a little
        above it, so the lock always leaves room for the ATC/ATO curve to
        accelerate into: `current_speed + BRAKING_LOCK_ACCEL_HEADROOM_KMH`, capped
        at `atc.speed_limit + BRAKING_LOCK_LIMIT_MARGIN_KMH` so a train at line
        speed reserves only its real braking distance (the small margin keeps the
        posted limit, not the reservation, the thing holding it at top speed), and
        floored at the current speed so an overspeeding train still reserves its
        full stopping distance. From a stand the ATC limit is low (short
        clearance), so the lock starts small and ratchets forward as the train
        rolls; the one-block floor in the cap-block walk covers the case where
        even this rounds under a single block (design_new.md "Braking-distance
        lock")."""
        from train_controller.speed_curves import calculate_braking_distance

        speed_kmh = float(self.physics_sync.current_speed_kmh)
        profile = self._get_speed_profile()
        decel = float(getattr(profile, "deceleration_rate", 0.0) or 0.0)
        margin = self.atc.effective_safety_margin()
        atc_limit_kmh = float(self.atc.speed_limit)
        lock_speed_kmh = max(
            speed_kmh,
            min(
                speed_kmh + BRAKING_LOCK_ACCEL_HEADROOM_KMH,
                atc_limit_kmh + BRAKING_LOCK_LIMIT_MARGIN_KMH,
            ),
        )
        distance = calculate_braking_distance(lock_speed_kmh, decel, margin)
        cushion = (
            max(0.0, speed_kmh)
            * get_speed_conversion_factor()
            * max(0.0, float(delta_seconds))
        )
        return distance + cushion

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
    def train_status(self) -> TrainStatus:
        """The train's overall status (`OFF` / `MANUAL` / `ATO` / `EB`).

        Shorthand for the derived `self.ops.train_status` read (design_new.md
        "Controller state machine"): computed from `powered`, `eb.active`, and
        `commands.control_mode`. Not stored; no writer.
        """
        return self.ops.train_status

    @property
    def travel_direction(self) -> Optional[str]:
        """Read-only derived value computed each read from
        (received_path_plan, current_span) per design_new.md:2283.

        The head is the physical span end that faces into the live authority
        (**Authority-side head derivation**); travel_direction is the
        network-frame direction pointing from the opposite physical end through
        the head end and onward. When no next span resource exists ahead, it is
        the direction outward through the head end's tile edge.

        Returns None when there is no live authority (`received_path_plan is
        None`): with no authority there is no defined direction of travel, and
        the span keeps its `end_a`-head framing default. There is no operator
        cab — reversing means a new authority the other way.

        Resolution order:
        1. From `current_span` (canonical) when wired — the direction from the
           tail-side end to the head-side end (head derived from the authority
           via `_head_span_locator`), or for single-tile spans the outward
           direction through the head's exit edge.
        2. None when there is no live authority or no live span.
        """
        if self.path_projection.received_path_plan is None:
            return None

        # Canonical path: derive the head/tail side from the authority-framed
        # span and read the network-frame direction from tail tile -> head tile.
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

    def _report_atc_reason_change(self) -> None:
        """Report a change in the distance-0 ATC reason code.

        The reason code is recomputed every tick and stored nowhere, so a train
        that held at zero for a minute leaves no trace of why unless the change
        itself is an event (design_new.md "canonical lookahead payload", A
        changed reason code is an event).

        Compared against the last code *emitted*, not the last tick's: a code
        that flickers between two values emits on each real change, and a steady
        code emits nothing — the **Emit on change, not per tick** rule. `info`
        for the two codes that hold the train at zero, because an operator wants
        them in the stream, and `debug` for the rest, which are ordinary driving
        restrictions a developer reads back rather than faults. Both stay below
        `warning`, so this event never reaches `latest_error_for` — the
        no-authority banner still answers with a real failure (design_new.md
        "A changed reason code is an event", Kind selection).
        """
        code = self.atc._last_limit_reason_code
        code_str = code.value if hasattr(code, "value") else (str(code) if code else None)
        if code_str == self._last_emitted_atc_reason_code:
            return
        previous = self._last_emitted_atc_reason_code
        self._last_emitted_atc_reason_code = code_str
        if code_str is None:
            return

        from notification_center import Kind as _Kind

        # The canonical distance-0 detail, read through the same property every
        # other consumer uses. It falls back to the code when there is no prose,
        # so drop it in that case rather than repeating the code twice. Computed
        # only on a change, which is why the recompute is affordable here.
        detail = self.speed_limit_reason
        if detail == code_str:
            detail = None
        holding = code_str in ("deviation", "no_reservation")
        self.notifications.emit(
            f"ATC limit reason {previous or 'none'} -> {code_str}",
            operation="tick",
            kind=_Kind.INFO if holding else _Kind.DEBUG,
            payload={
                "previous_reason_code": previous,
                "reason_code": code_str,
                "atc_limit_kmh": self.atc.speed_limit,
                "atc_limit_reason_detail": detail,
            },
        )

    # -------------------------------------------------------------------------
    # Tick Method (Called Each Simulation Tick)
    # -------------------------------------------------------------------------

    def tick(
        self, delta: float, after_command_drain: Optional[Callable[[], None]] = None
    ) -> None:
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
        # Process any commands enqueued by power_on_cmd / set_mode / etc. in the
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
                    from notification_center import Kind as _Kind, train_ref as _tref
                    from runtime_payloads import ErrorRecord as _ErrRec

                    nc.emit(
                        source="TrainController",
                        operation="_drain_command_inbox",
                        kind=_Kind.WARNING,
                        message=f"command drain raised for {self.train_id}",
                        entities=[_tref(self.train_id)],
                        error=_ErrRec(str(exc), "command_drain_failed"),
                        event_time=event_time,
                        payload={"traceback": _tb.format_exc()},
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

        # design_new.md Action step `7b40`: immediately after the command drain,
        # SimulationController's narrow callback consumes the stopped-gated
        # `pending_path_cancel_request`, so the control decision below evaluates
        # the post-cancel state. The callback exposes no dispatcher object to the
        # controller.
        if after_command_drain is not None:
            after_command_drain()

        # design_new.md "Controller state machine": `train_status` is a pure
        # derived read from `powered` / `eb.active` / `control_mode`; there is
        # no reactive state machine to run and no `state` field to re-sync here.

        # ===== 0. POWER CHECK =====
        if not self.powered:
            # An OFF train computes no limit, so it has no reason code to
            # report. Forget the last one reported instead of holding it: the
            # code the train comes back on with is news whether or not it
            # matches what it went down with.
            self._last_emitted_atc_reason_code = None
            return

        # ===== 1. EMERGENCY BRAKE CHECK =====
        if self.eb.active:
            self.ops.handle_position = HandlePosition.EB
            self.atc.speed_limit = 0.0
            # EB is a supervisory brake-only override: it springs both driver
            # inputs to their safe rest every latched tick (design_new.md
            # "ato.engaged" / EB-hold step `d483`). The handle is pinned to EB
            # above; the power notch is re-zeroed here, so a percentage
            # re-commanded under EB is accepted by the tick-entry drain but
            # wiped back to 0 before the tick ends. This is what keeps a
            # retained percentage from driving the train the moment EB lifts.
            self.manual.reset_power_percent_for_safety()
            # Reported before the early return, because a latched EB is exactly
            # when an operator asks why the limit is zero. The branches below
            # are never reached while EB holds, so this is the only place the
            # `eb` code can be published.
            self.atc._last_limit_reason_code = AtcLimitReasonCode.EB
            self._report_atc_reason_change()

            # While EB is latched, controller does not run post-EB restart
            # logic. Explicit release_emergency_cmd() is required to clear
            # eb_active. This early-exit holds until the operator releases.
            return

        # The reactive condition evaluation already ran above (Action step
        # `71ee`), right after the command drain and the early departure /
        # cancellation consumers, and also fires each Observe pass inside
        # on_physics_updated(). Nothing below re-runs it.

        # ===== 2. LOOKAHEAD (computed live; no caching) =====
        # Per design_new.md, lookahead is a pure derived value. We trigger one
        # local computation here so the ATC info collection below uses the same
        # live values; downstream consumers re-read it via the property.
        live_lookahead = self._calculate_lookahead()

        # ===== 3. ATC INFO COLLECTION =====
        # The head is derived from the live authority, so there is no cabless or
        # wrong-direction-cab zero-limit case (design_new.md "Authority-side head
        # derivation"): the ATC limit comes straight from the canonical anchor.
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

        # Placed after every branch above has settled the code, so no branch is
        # missed: the reason code answers "why is this train limited right now",
        # and this is where that answer is finished for the tick.
        self._report_atc_reason_change()

        # ===== 4. ATC ACTION =====
        overspeed = self.physics_sync.current_speed_kmh - self.atc.speed_limit

        # Per design_new.md step `914f` / `826a`: a rolling train whose
        # authority can no longer be used has nothing to brake against, so EB —
        # not a service-brake notch — is the only stopping protocol. Both
        # zero-limit reasons trigger this fail-safe override: `no_reservation`
        # (no live path at all) and `deviation` (the path is still ACTIVE but
        # the head is unmappable, so the distance-0 limit is 0 with reason
        # `deviation`). In either case any motion is overspeed at distance 0, so
        # `not is_stopped()` escalates to EB regardless of the ordinary margin,
        # rather than letting the ATO/manual branches pick a service notch that
        # would override the operator's handle with a confusing brake step. All
        # other reason codes use the ordinary overspeed margin above.
        zero_limit_loss = (
            self.atc.speed_limit <= 0.0
            and self.atc._last_limit_reason_code
            in (AtcLimitReasonCode.NO_RESERVATION, AtcLimitReasonCode.DEVIATION)
            and not self.is_stopped()
        )

        if overspeed > OVERSPEED_EB_THRESHOLD_KMH or zero_limit_loss:
            # Raise EB through the canonical activation path (design_new.md "EB
            # has two trigger paths" / step `826a`): `eb.activate()` performs
            # every canonical EB effect — force handle to EB, disengage ATO
            # (`ato.engaged = False`), and (when a live path exists) latch the
            # `eb` cancel. ATC must not assign `eb.active` itself: no site
            # outside `train_controller/eb.py` assigns `eb.active`.
            self.eb.activate()
            self.notifications.emit(
                "ATC triggered emergency brake: overspeed"
                if overspeed > OVERSPEED_EB_THRESHOLD_KMH
                else "Unusable authority (zero limit): emergency brake",
                operation="_run_atc",
                error=_ErrorRecord(
                    "ATC escalated to emergency brake", "eb"
                ),
            )
            return

        # ATC diagnostic overspeed warning, per the `overspeed_warn_threshold_kmh`
        # runtime setting. Edge-triggered on entry into the warn band, so one
        # excursion emits one warning rather than one per tick.
        if overspeed > OVERSPEED_WARN_THRESHOLD_KMH:
            if not self._overspeed_warned:
                self._overspeed_warned = True
                self.notifications.emit(
                    f"ATC overspeed warning: {overspeed:.1f} km/h over the limit",
                    operation="tick",
                )
        else:
            self._overspeed_warned = False

        # ===== 5. ATO INFO =====
        self.ato.info = self.ato.compute_info()

        # ===== 6. ATO ACTION (skip if manual) =====
        # `ato.engaged` is operator intent, not a run-in-progress flag: losing
        # path authority mid-motion does not clear it (design_new.md
        # "ato.engaged"). An engaged train with no live authority holds at a
        # stand and resumes on its own when authority returns, so nothing is
        # cleared here.

        # Re-express the span frame to the authority-derived head before the
        # driving decision reads it, so a new authority the other way (a
        # turnaround) points the train the right way this tick. Only fires while
        # stopped, so it is safe in both modes.
        self._reframe_span_to_authority_head()

        # Compute the effective_limit locally; consumers read this via the
        # `target_speed` property when needed.
        effective_limit = min(
            train.max_speed,
            self.atc.speed_limit,
            self.atc.current_block_speed_limit,
        )

        if self.commands.control_mode == "ato":
            # The head end is derived from the live authority (design_new.md
            # "Authority-side head derivation"), so ATO always drives toward the
            # authority end — no cab to align.

            # Drive while ATO intent is engaged; otherwise brake to a stand.
            if self.ato.engaged:
                # Engaged - calculate handle to reach target speed. compute_handle
                # returns neutral / a brake when there is no live authority, so an
                # engaged train with no path simply holds at a stand.
                self.ops.handle_position = self.ato.compute_handle(delta)
            else:
                # Not engaged - brake to stop, then land in the stopped standby
                # state. An ATO-mode controller reads `engaged == False` only
                # after an EB trip that has been released (release does not
                # re-engage) or before the operator's first set_mode("ato"); it
                # holds stopped until the operator re-engages.
                self.ops.handle_position = self.ato.calculate_braking_notch_for_speed(
                    self.physics_sync.current_speed_kmh, 0.0
                )
                if self.physics_sync.current_speed_kmh <= STOPPED_SPEED_THRESHOLD_KMH:
                    # Braked to a stand while not engaged (`ato_status == READY`):
                    # the ATO output is `0` and the handle rests at neutral. There
                    # is no operational-state write — `train_status` is derived
                    # (design_new.md "ATO driving rule", Not engaged).
                    self.ops.handle_position = HandlePosition.N
        else:
            # Manual mode - ATC still enforces braking if overspeed
            if self.physics_sync.current_speed_kmh > self.atc.speed_limit:
                self.ops.handle_position = self.ato.calculate_braking_notch_for_speed(
                    self.physics_sync.current_speed_kmh, self.atc.speed_limit
                )

        # ===== 7. EFFECTIVE LIMIT (target_speed property reads from lookahead) =====
        # Per design_new.md, target_speed and speed_limit_reason are derived
        # from lookahead at distance 0. We do NOT mirror them onto self/train.

        # `path_backed_run_active` is now a derived `@property`; no write here.

        # ===== 8. NO STATE TRANSITIONS =====
        # `train_status` is derived from `powered` / `eb.active` /
        # `control_mode`; there is no stored operational state for tick() to
        # transition. A train at zero ATC clearance with live reservation simply
        # stands with `current_speed_kmh == 0` and keeps its reservation. Per
        # design_new.md steps `89ba` / `32dd` / `90d3`, tick() must not latch
        # path completion or call route callbacks itself: all completion flows
        # through Plan settlement's try_complete_active_path(...).

    def produce_physics_command(self) -> PhysicsCommand:
        """Compute the per-tick command physics will execute — the **Propulsion
        cap step** (design_new.md).

        The selected driving source produces one commanded operating point, and
        the safety layer only ever lowers it under the active ceilings — EB
        (zero, at `emergency_brake_rate`), pending power-off (zero, at the
        service rate), and ATC (`atc_speed_limit`) — springing the retained
        power percentage to `0` whenever a ceiling intervenes. The `power_on`
        check below is the device de-energize (OFF), not a ceiling. The ceilings
        appear as ordered branches here because each source emits its own
        command kind (handle / target-speed / track-voltage):
          - not power_on           → handle N (OFF; train coasts)
          - eb active              → emergency brake / zero target speed
          - pending power-off      → handle N
          - manual power_percent   → clamp(power_speed_curve(pct), [0, atc])
          - ATO / manual simulation→ the handle; physics caps it at the live
                                     distance-0 atc_limit

        `voltage_source_speed_kmh` is the scalar the voltage emitter uses
        as the train's "current speed" when computing the per-tick voltage:
          - power_percent          → calibrated target speed
          - manual simulation      → integrated current speed
          - ATO                    → live target speed from lookahead
          - everything else        → 0.0 (READY/AT_STATION/OFF use lighting
                                     or zero voltage and ignore this field)
        """
        # design_new.md:3047 (A capped crossing is discarded): when the
        # Propulsion cap step lowers the calibration drive through an external
        # ATC restriction during a measured crossing, taint that crossing so it
        # is discarded and re-measured rather than banked. EB and power-off end
        # the whole pass separately, through run_still_valid at step `26f5`.
        self._taint_calibration_crossing_on_cap()
        if not self.power_on:
            # Per design_new.md line 1755: clear manual_power_percent on
            # power-off so the set-and-forget contract holds.
            self.manual.reset_power_percent_for_safety()
            return PhysicsCommand(
                kind=PHYSICS_KIND_HANDLE,
                handle=HandlePosition.N,
                voltage_source_speed_kmh=0.0,
            )
        # Propulsion cap step, EB ceiling (design_new.md "Propulsion cap step").
        # EB is a zero ceiling at `emergency_brake_rate`: it lowers the command
        # to a stop and springs the retained percentage to `0` (the cap step's
        # retained-field reset, no exception list), symmetric with the per-tick
        # EB hold. Nothing drives under EB and no retained percentage survives.
        if self.eb.active:
            self.manual.reset_power_percent_for_safety()
            if (
                self.commands.control_mode == "manual"
                and self.manual.manual_mode_type == "power_percent"
            ):
                # Manual `power_percent` is the momentum-free model: the zero
                # ceiling writes the capped zero speed THIS tick rather than
                # decelerating at `emergency_brake_rate` (design_new.md row
                # `e67d`).
                return PhysicsCommand(
                    kind=PHYSICS_KIND_TARGET_SPEED,
                    handle=HandlePosition.EB,
                    target_speed_kmh=0.0,
                    voltage_source_speed_kmh=0.0,
                )
            return PhysicsCommand(
                kind=PHYSICS_KIND_EMERGENCY_BRAKE,
                handle=HandlePosition.EB,
                voltage_source_speed_kmh=0.0,
            )
        # Propulsion cap step, power-off ceiling (design_new.md "Propulsion cap
        # step"). A pending `phase_latches.power_off` (EB returned above) is a
        # zero ceiling at the service rate: the command is the neutral notch and
        # the retained percentage springs to `0`. This holds a still-powered
        # rolling ghost train at neutral every tick until the latch is consumed;
        # it is a no-op for the stopped-only operator writer, which already
        # neutralized once through `_apply_power_off_request()`.
        if self.route_hooks.phase_latches.power_off:
            self.ops.reset_handle_to_neutral()
            self.manual.reset_power_percent_for_safety()
            return PhysicsCommand(
                kind=PHYSICS_KIND_HANDLE,
                handle=HandlePosition.N,
                voltage_source_speed_kmh=0.0,
            )
        # design_new.md "Lighting-voltage calibration", What the run drives: an
        # open lighting-voltage run substitutes an exact track-voltage magnitude
        # for the ordinary manual-notch / manual-power / ATO traction request,
        # read through the shared `SpeedCalibration.get_propulsion_override`
        # accessor's `track_voltage` variant. It is reached only after pending
        # power-off and EB (both returned above), so those outrank it. The
        # candidate does not feed the speed solution — the command carries no
        # motion (handle N), only the magnitude physics will emit — so it never
        # moves the modelled train by itself.
        lighting_track_voltage = self._lighting_track_voltage_override()
        if lighting_track_voltage is not None:
            return PhysicsCommand(
                kind=PHYSICS_KIND_HANDLE,
                handle=HandlePosition.N,
                voltage_source_speed_kmh=0.0,
                voltage_source_track_voltage=lighting_track_voltage,
            )
        if (
            self.commands.control_mode == "manual"
            and self.manual.manual_mode_type == "power_percent"
        ):
            profile = self._get_speed_profile()
            # design_new.md "Speed calibration", Controller owns calibration
            # output: an open speed-calibration pass supplies the commanded
            # percent through get_propulsion_override, replacing the ordinary
            # manual power source. It reaches this branch already below EB and
            # pending power-off (both returned earlier) and stays below the
            # ATC / service-stop cap applied just below, and it is never copied
            # into manual.manual_power_percent. A pass runs in manual
            # power_percent submode, so it lands here.
            calibration_pct = self._calibration_power_percent_override()
            driven_by_calibration = calibration_pct is not None
            pct = (
                float(calibration_pct)
                if driven_by_calibration
                else float(self.manual.manual_power_percent or 0.0)
            )
            target = profile.power_percent_to_speed(pct) if profile else 0.0
            # Propulsion cap step, ATC ceiling (design_new.md "Propulsion cap
            # step"): the commanded speed is lowered under `atc.speed_limit`
            # (already zero when no cab, wrong-direction cab, or no/at-end
            # authority set it to 0 earlier this tick; EB and power-off returned
            # above as their own zero ceilings).
            cap = float(self.atc.speed_limit or 0.0)
            # The commanded percent is the authoritative operating point for
            # this tick, so physics maps it straight to track_voltage through
            # voltage_power_curve (design_new.md:2424, 2431). A zero percent
            # keeps the existing dead-rail outcome rather than the curve's
            # 0%-power lighting anchor.
            voltage_pct: Optional[float] = pct if pct > 0.0 else None
            if target > cap:
                # Per design_new.md: when the cap intervenes, the controller
                # clamps the target to it. For an ordinary manual command it also
                # resets manual_power_percent for this tick (set-and-forget); a
                # calibration override is not a stored manual command — it is
                # re-supplied each tick and never written into that field — so
                # the field is left untouched when the pass drives. A zero cap
                # therefore holds the train at rest instead of letting the
                # stored percent command motion.
                target = cap
                if not driven_by_calibration:
                    self.manual.reset_power_percent_for_safety()
                # The cap outranks the commanded percent, so the capped speed —
                # not the discarded percent — is this tick's authoritative
                # operating point, and voltage derives from it.
                voltage_pct = None
            target = max(0.0, target)
            return PhysicsCommand(
                kind=PHYSICS_KIND_TARGET_SPEED,
                target_speed_kmh=target,
                voltage_source_speed_kmh=target,
                voltage_source_power_percent=voltage_pct,
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

    def _taint_calibration_crossing_on_cap(self) -> None:
        """Taint an open speed-calibration crossing when the Propulsion cap step
        lowers the calibration drive through an external ATC restriction
        (design_new.md:3047, "A capped crossing is discarded").

        The calibration drive goes through the cap step like every driving
        source. When that step lowers it on a tick, the speed the pass would time
        is not the speed the step commanded, so the crossing being timed is
        marked capped; it is then discarded and re-measured on the next crossing
        rather than banked, and the pass goes on. Only a pass actively driving a
        crossing is affected.

        An ATC `CAUTION` block ahead and a posted block `SPEED_LIMIT` are the two
        external restrictions that lower the drive without ending the pass.
        `RESERVATION_STOP` / `NO_RESERVATION` are the leg's own reservation-end
        approach and lifecycle, not an external cap, so they are excluded. EB and
        power-off are not handled here: they end the whole pass through
        `SpeedCalibration.run_still_valid(...)` at Action step `26f5`.
        """
        sc = getattr(self, "simulation_controller", None)
        calibration = getattr(sc, "speed_calibration", None) if sc is not None else None
        if calibration is None:
            return
        driving_pct = self._calibration_power_percent_override()
        if driving_pct is None or driving_pct <= 0.0:
            return
        reason_code = self.atc._last_limit_reason_code
        if reason_code == AtcLimitReasonCode.CAUTION:
            reason = "CAUTION restriction during a measured crossing"
        elif reason_code == AtcLimitReasonCode.SPEED_LIMIT:
            reason = "block speed limit during a measured crossing"
        else:
            return
        calibration.taint_current_crossing(self.train_id, reason)

    def _calibration_power_percent_override(self) -> Optional[float]:
        """The percent an open speed-calibration pass commands this tick, or None.

        design_new.md "Speed calibration", Controller owns calibration output: a
        calibration pass drives through `SpeedCalibration.get_propulsion_override`
        rather than by queuing operator commands. Reached through the
        `simulation_controller` back-reference — the same channel the command-drain
        error path and `TrainPhysics`'s lighting-voltage read already use. Returns
        None off-pass, when the runtime is absent (unit-test controllers), or for
        the union's `track_voltage` variant (a lighting run, which still drives
        through its own physics read), so the ordinary manual source drives in
        every one of those cases.
        """
        sc = getattr(self, "simulation_controller", None)
        calibration = getattr(sc, "speed_calibration", None) if sc is not None else None
        if calibration is None:
            return None
        override = calibration.get_propulsion_override(self.train_id)
        if override is None or getattr(override, "kind", None) != "power_percent":
            return None
        return float(getattr(override, "value", 0.0))

    def _lighting_track_voltage_override(self) -> Optional[float]:
        """The exact track-voltage magnitude an open lighting-voltage run drives
        this tick, or None.

        design_new.md "Lighting-voltage calibration", Where it lives: a lighting
        run drives through the shared `SpeedCalibration.get_propulsion_override`
        accessor's `track_voltage` variant, so neither the controller nor
        `TrainPhysics` reaches into the run registry. Reached through the same
        `simulation_controller` back-reference the speed-calibration override
        read above already uses. Returns None off-run, when the runtime is absent
        (unit-test controllers), or for the union's `power_percent` variant (a
        speed pass, handled in the manual branch), so the ordinary voltage source
        drives in every one of those cases.
        """
        sc = getattr(self, "simulation_controller", None)
        calibration = getattr(sc, "speed_calibration", None) if sc is not None else None
        if calibration is None:
            return None
        override = calibration.get_propulsion_override(self.train_id)
        if override is None or getattr(override, "kind", None) != "track_voltage":
            return None
        return float(getattr(override, "value", 0.0))

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
            if track.block_id and track.block_id != current_block_id:
                block = self.network.blocks.get(track.block_id)
                if block and self._is_block_obstructed(block, track.block_id):
                    break
                current_block_id = track.block_id

            if track.block_id == dest_block_id:
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
            elif in_dest_block and track.block_id != dest_block_id:
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

    def _get_current_block_id(self) -> Optional[str]:
        """Get the block ID where the train's head is located."""
        head_tile = self._get_head_tile()
        if not head_tile or not self.network:
            return None

        track = self.network.tracks.get(head_tile)
        if track:
            return track.block_id

        return None

    # --- Authority-framed head/tail/orientation helpers ----------------------
    # Per design_new.md "Authority-side head derivation": head/tail and
    # travel_direction derive from (`_framed_head_end`, current_span). The framed
    # head follows the live authority, defaulting to end_a with no authority
    # (matching the placement default). `end_a` / `end_b` on the span are
    # frame-independent physical ends and never carry head/tail labels themselves.

    def _head_end_label(self) -> str:
        """Which physical span end ("end_a"/"end_b") the span is currently framed
        with as the head. Kept in sync with the authority-derived head by the
        driving tick's reframe (design_new.md "Authority-side head derivation");
        span re-framing is driven by a live authority, so a train that loses its
        authority keeps the framing it last had until a new authority moves it
        (power-off resets it to the ``end_a`` default). There is no operator cab:
        direction comes from the authority the route granted."""
        return self._framed_head_end

    def _apply_head_frame(self, end: str) -> Optional[str]:
        """Frame the span head-forward for physical end ``end`` ("end_a"/"end_b").

        The single writer of ``_framed_head_end``: it re-expresses both span end
        locators into the new head-forward frame (design_new.md "Reversed-frame
        re-expression") and, only if that succeeds, records the new framed head.
        A no-op returning ``None`` when the frame is already ``end``. Returns a
        user-facing reason (and leaves the frame untouched) when the span cannot
        be re-expressed. Callers: placement, the between-legs calibration
        reversal, the authority-driven reframe, and OFF-cleanup reset."""
        if end == self._framed_head_end:
            return None
        reframe_error = self._reframe_span_for_head_change(self._framed_head_end, end)
        if reframe_error is None:
            self._framed_head_end = end
        return reframe_error

    def _reframe_span_to_authority_head(self) -> None:
        """Re-express the span's end frames when a new authority has moved the
        derived head to the other physical end (design_new.md "Reversed-frame
        re-expression"). Runs only while the train is stopped, so re-expressing
        the two end locators can never disagree with a rolling frame. A no-op
        when the framed head already matches the authority head or there is no
        live authority to move it."""
        if not self.is_stopped():
            return
        self._apply_head_frame(self._authority_head_end())

    def _reset_head_frame_to_default(self) -> None:
        """Reset the span framing to its ``end_a`` head default, re-expressing the
        two end locators when the head moves off ``end_b`` (design_new.md
        "Reversed-frame re-expression"). Called by `complete_power_off()` during
        OFF cleanup: with power off there is no live authority, so the head is
        the ``end_a`` default. A no-op when the frame is already ``end_a`` or the
        span cannot be re-expressed."""
        self._apply_head_frame("end_a")

    def _authority_head_end(self) -> str:
        """The physical span end ("end_a"/"end_b") that faces into the live
        authority (design_new.md "Authority-side head derivation"): the end whose
        resource sits later in ``received_path_plan.resource_sequence``, with the
        track-connectivity / path-direction geometry resolving a train whose span
        covers one resource. Returns the currently-framed head when there is no
        live authority or the geometry cannot separate the ends, so the frame is
        left unchanged then: span re-framing is driven by a live authority
        (design_new.md "Span re-framing" — it happens on the first tick the
        controller drives with a given authority), and a train that loses its
        authority keeps the framing it last had until a new authority moves it."""
        if self.path_projection.received_path_plan is None:
            return self._framed_head_end
        side = self.cab_policy.required_cab_for_path()
        if side == "B":
            return "end_b"
        if side == "A":
            return "end_a"
        return self._framed_head_end

    def _reframe_span_for_head_change(
        self, old_head: str, new_head: str
    ) -> Optional[str]:
        """Atomically re-express both span locators when the head end changes.

        Moving the head to the other span end does not move the physical train,
        but it reverses the head-forward traversal frame used by each
        ``SpanEndLocator``.  Compute both replacements before writing either one
        so a malformed endpoint can never leave the head frame and span
        describing opposite directions.

        Returns ``None`` on success, otherwise a user-facing reason.  On
        failure the span is untouched.
        """
        if old_head == new_head:
            return None
        span = getattr(self.physics_sync, "current_span", None)
        if span is None:
            return None
        if self.network is None:
            return "Cannot re-frame the span head without network geometry"

        replacements = []
        for label, end in (("end_a", span.end_a), ("end_b", span.end_b)):
            if not end.tile or not end.entry_edge:
                return f"Cannot re-frame the span head: {label} has no traversal frame"
            track = self.network.get_track(*end.tile)
            if track is None:
                return f"Cannot re-frame the span head: {label} is not on track"
            exits = track.get_exits(end.entry_edge, use_switch_state=True)
            if not exits:
                return f"Cannot re-frame the span head: {label} has no reverse traversal"
            tile_length = self.network._get_tile_length_cm(
                end.tile[0], end.tile[1]
            )
            replacements.append(
                (end, exits[0], float(tile_length) - float(end.distance_cm))
            )

        for end, entry_edge, distance_cm in replacements:
            end.entry_edge = entry_edge
            end.distance_cm = distance_cm
        return None

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
        """Train facing direction (a `Direction`) derived from the
        authority-side head and `current_span`. Replaces the removed
        `Train.facing_direction`. Head defaults to end_a with no live authority.
        Returns None when there is no live span."""
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
        """Head tile, derived from the authority-framed span, or None when no span."""
        loc = self._head_span_locator()
        if loc is not None and loc.tile is not None:
            return (loc.tile[0], loc.tile[1])
        return None

    def _get_head_distance_cm(self) -> float:
        """Head in-tile distance, derived from the authority-framed span."""
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
        """Head tile entry edge, derived from the authority-framed span."""
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
                    if not track or track.block_id != current_block_id:
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
        """Read-only alias for the stored `powered` flag (design_new.md
        "TrainController" owns the stored `powered` flag; `power_on` is its
        convenience read)."""
        return self.powered

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
        each read from controller-stored fields. RouteExecutor.is_clean_for_create reads a subset of this set; readers must not duplicate the underlying field checks.
        """
        conditions: set[ControllerCondition] = set()
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

    def is_stopped_at_reservation_end(self) -> bool:
        """True when the train is stopped at the usable end of active authority.

        Per design_new.md step `8df5`, this is the post-physics usable-
        authority stop check used by Observe path completion.

        The spec lists two satisfying cases: the train has either "physically
        progressed through the final `resource_sequence` entry" (its head
        occupies the authority-end block) or "the remaining projected authority
        distance is too short to grant additional positive clearance after
        applying the controller safety margin" (it braked to rest at the
        safety-margin boundary short of that block — common when the
        authority-end block is very short or zero-length). Both cases surface
        identically at distance 0: the lookahead curve's dominant restriction
        becomes the path-end stop, so its reason code is `RESERVATION_STOP`. The
        literal `atc_limit == 0` the spec mentions is unreachable under
        forward-Euler integration (a step cannot land exactly on the boundary,
        so the value reads a few km/h); the `RESERVATION_STOP` reason code is the
        reachable expression of the same intent.

        The gate fires on `is_stopped()` plus **either** of the step-`8df5`
        arrival conditions:
          - **4a** — the distance-0 lookahead's dominant restriction is the
            authority-end stop (reason code `RESERVATION_STOP`). This covers a
            train braked to rest at the safety-margin boundary short of a very
            short or zero-length final block; requiring head occupancy alone
            would strand it, since it never enters that block.
          - **4b** — the head is physically in the authority-end (terminal)
            block. This covers a train held at rest inside its target block by
            something other than the authority end, such as a manual driver's
            voluntary stop under positive clearance (its dominant distance-0
            restriction is then not `RESERVATION_STOP`, so 4a alone would never
            complete it).
        The dispatcher's `try_complete_active_path(...)` separately verifies the
        live authority's terminal block is the served stop's target, so this
        controller-side gate only answers "stopped at my current usable
        authority end?".
        """
        if not self.is_stopped():
            return False
        if self.eb.active:
            return False
        plan = self.path_projection.received_path_plan
        if plan is None:
            return False
        # 4a: the ATO curve has collapsed to the path-end stop at distance 0, so
        # the dominant restriction's reason code is RESERVATION_STOP.
        payload = self._compute_canonical_lookahead()
        curves = (payload or {}).get("lookahead", {}).get("speed_curves") or []
        reason_4a = bool(curves) and (
            curves[0].get("atc_limit_reason_code")
            == AtcLimitReasonCode.RESERVATION_STOP.value
        )
        # 4b: the head is physically in the authority-end (terminal) block.
        # Derive the head tile from (cab, span) via `_get_head_tile()` and map it
        # to its block; the span's `occupied_blocks` ordering is not head-
        # relative (it is geometric end_a->end_b, not cab-forward), so the head
        # block must come from the head tile, not from an occupied_blocks end.
        head_4b = False
        authority_end_block = plan.reservation_end_block_id
        if authority_end_block and self.network is not None:
            head_tile = self._get_head_tile()
            if head_tile is not None:
                head_block = self.network.tile_to_block_id(
                    head_tile[0], head_tile[1]
                )
                head_4b = head_block == authority_end_block
        return reason_4a or head_4b

    # -------------------------------------------------------------------------
    # Physics callback — span storage + progress enqueue (Observe; per design_new.md)
    # -------------------------------------------------------------------------

    def on_physics_updated(
        self,
        event_time: float,
        span_update: PhysicsSpanUpdate,
    ) -> None:
        """Apply a TrainPhysics span_update and enqueue a progress report for
        Observe. Delegates to ``ProgressReportBuilder``
        (train_controller/progress_report.py); see there for the full
        span-storage and progress-enqueue procedure.
        """
        self.progress_report.on_physics_updated(event_time, span_update)

    def build_detected_progress_report(self, event_time: float) -> None:
        """Queue a progress report carrying only this tick's detector-confirmed
        block events. Delegates to ``ProgressReportBuilder``.

        The stopped-simulation hardware poll's stand-in for the Observe physics
        callback: that poll integrates no motion, so there is no span update to
        apply and nothing else the callback would contribute — but the detected
        blocks still have to reach Observe, which is the only path that commits
        them (see `SimulationController.poll_hardware_occupancy`).
        """
        self.progress_report.build_detected_progress_report(event_time)

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

    # Event emission lives on the `Notifications` sub-module attached as
    # `self.notifications` — the thin adapter onto the one `NotificationCenter`
    # stream. It holds no queue: operator-facing per-train reads are filters
    # over that stream (design_new.md "Runtime notifications").

    # Operator commands (power_on_cmd, power_off_cmd, set_mode,
    # set_handle, set_manual_mode_type, set_manual_power_percent, enqueue_command)
    # and the per-tick command inbox / drain live on the `OperatorCommands`
    # sub-module attached as `self.commands`. External callers use
    # `controller.commands.<method>(...)`. The drain itself is invoked at the
    # top of `tick(...)` as `self.commands._drain_command_inbox(...)`.

    # ATO intent (`engage`, `disengage`) lives on the `ATOComputer` sub-module
    # at `self.ato`. Callers use `controller.ato.engage()` /
    # `controller.ato.disengage()` directly.

    # -------------------------------------------------------------------------
    # Route Executor Commands (for RouteExecutor integration)
    # -------------------------------------------------------------------------

    def _emit_event(
        self, operation: str, message: str, payload=None, kind=None, path_id=None
    ) -> None:
        """Emit an operational event-log notification through NotificationCenter
        (design_new.md "Event log"). Best-effort: resolves the sink defensively,
        no-ops when none is wired (isolated unit-test controllers), and never
        raises into the caller."""
        try:
            sc = getattr(self, "simulation_controller", None)
            nc = getattr(sc, "notification_center", None) if sc is not None else None
            if nc is None:
                return
            from notification_center import (
                Kind as _Kind,
                path_ref as _pref,
                train_ref as _tref,
            )

            entities = [_tref(self.train_id)]
            if path_id:
                entities.append(_pref(path_id))
            nc.emit(
                source="TrainController",
                operation=operation,
                kind=kind if kind is not None else _Kind.INFO,
                message=message,
                entities=entities,
                event_time=self.commands._estimate_event_time(),
                payload=payload or {},
                # `_emit_event` callers are latch-consume / event-log events, so
                # per the Latch consumption events table (design_new.md:156) it
                # passes latch_event=True and is never dropped by suppression.
                latch_event=True,
            )
        except Exception:
            pass

    def on_path_complete(self) -> None:
        """Called by PathDispatcher.try_complete_active_path on accepted completion.

        Per design_new.md step `09b0` the terminal teardown writes no controller
        state: `train_status` and `ato_status` are derived, so once
        `received_path_plan` reads `None` the train simply holds no live
        reservation (a paused or idle ATO train reads `ato_status == READY`) or
        stays held under EB (`train_status == EB`) until `eb.release()`. There is
        nothing for this callback to write, so it is a no-op kept only so the
        dispatcher's optional-callback lookup finds a callable. It does not clear
        `received_path_plan` and does not enter dwell.
        """
        return None

    def complete_power_off(self) -> None:
        """Controller-owned OFF-cleanup entry point (design_new.md:282, :1808).

        Invoked by the Action ``pending_power_off`` consumer (step ``886a`` of
        the Power-off procedure). It is the only ``OFF``-writing site besides the
        startup default, so the Operational state machine stays the single writer
        of ``powered``. Callable only while ``power_off`` is latched; on commit it
        sets ``powered = False`` (so ``train_status`` derives to ``OFF``), resets
        the span framing to its ``end_a`` head default, clears ``eb.active``,
        sets ``control_mode = "manual"`` (which clears ``commands.engaged``), and
        consumes ``pending_power_off``.

        Per design_new.md "Latch Table" (Clear/consume access) this method is
        the one place `phase_latches.power_off` is cleared — not the Action
        power-off consumer step that invokes it — so the field is cleared
        whichever of its two callers ran: that consumer step in the ordinary
        case, or `SimulationController.clear_failed_runtime()`'s step-`7c41`
        fan-out across every controller, which is not an Action step at all. It
        consumes the latch *when present* rather than requiring it, because the
        fan-out reaches controllers that never latched one — every entry to
        `OFF` except the startup default goes through here (Controller state
        machine, the `OFF` row). It is also that latch's consume-side emitter
        (Latch consumption events), which is why the event below passes
        `latch_event=True` — and why it is emitted only when a latch was
        actually consumed.
        """
        had_latch = self.route_hooks.phase_latches.power_off
        self.powered = False
        self._reset_head_frame_to_default()
        self.ops.reset_handle_to_neutral()
        # design_new.md:1895 — clear the EB latch through the EB-owned helper so
        # the EB Workflow stays the single writer of `eb.active`.
        self.eb.clear()
        # OFF cleanup sets control_mode = "manual" (which clears
        # commands.engaged), so a powered-on train reads MANUAL until the
        # operator re-selects ATO (design_new.md "complete_power_off").
        self.commands._write_control_mode("manual")
        if had_latch:
            self.route_hooks.phase_latches.clear("power_off")
            self.notifications.emit(
                f"power off committed for {self.train_id}",
                operation="complete_power_off",
                kind="info",
                payload={"latch": "power_off"},
                latch_event=True,
            )

    # -------------------------------------------------------------------------
    # Serialization
    # -------------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Convert controller state to a JSON-serializable dict.

        Delegates to ``train_controller.serialization.controller_to_dict``.
        """
        from train_controller.serialization import controller_to_dict

        return controller_to_dict(self)

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
        by ``RouteExecutor.tick_train_plan(...)`` and
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
          `clearance_cm`, `safety_margin_cm`, `reservation_boundary_delta_cm`,
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
            self._framed_head_end,
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
        """Reconstruct from a dict; delegates to serialization.controller_from_dict."""
        from train_controller.serialization import controller_from_dict

        return controller_from_dict(data, network)


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
