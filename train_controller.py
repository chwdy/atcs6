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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Tuple

from constants import (
    CAUTION_SPEED_LIMIT_KMH,
    PLATFORM_SAFETY_MARGIN_CM,
    STOPPED_SPEED_THRESHOLD_KMH,
    TRACK_END_SAFETY_MARGIN_CM,
)
from enums import (
    BRAKE_MULTIPLIERS,
    HANDLE_POSITION_INDEX,
    INDEX_TO_HANDLE_POSITION,
    POWER_MULTIPLIERS,
    AtcLimitReasonCode,
    BlockState,
    CommandResult,
    ControllerCondition,
    Direction,
    HandlePosition,
    LeadingEnd,
    SignalState,
    TrainState,
)
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


# `TRACK_END_SAFETY_MARGIN_CM`, `PLATFORM_SAFETY_MARGIN_CM`, and
# `CAUTION_SPEED_LIMIT_KMH` are imported from `constants.py` (the canonical
# home per design_new.md "Safety margin globals" and "CAUTION speed cap").
# The remaining tunables are local because they have no consumer outside
# the controller.
ATC_SIGNAL_LOOKAHEAD_BLOCKS = 3
ATC_MIN_CLEARANCE_CM = 10.0
ATC_NO_BLOCK_FALLBACK_CM = 100000.0
YELLOW_SIGNAL_SPEED_KMH = 40.0

SPEED_TREND_EPSILON = 0.1

# Speed curve constants
CURVE_SAMPLE_RESOLUTION_CM = (
    3.0  # 3cm resolution for speed curves (balance between precision and performance)
)
ATO_SPEED_MARGIN_KMH = 3.0  # ATO stays this far km/h below ATC limit


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
# Speed Calculation Helper
# =============================================================================


def calculate_max_safe_speed(
    clearance_cm: float,
    deceleration_rate: float,
    safety_margin_cm: float = TRACK_END_SAFETY_MARGIN_CM,
) -> float:
    """
    Calculate maximum speed that allows stopping within clearance.

    Uses physics formula: v^2 = 2 * a * d
    Where:
        v = velocity (converted between km/h and cm/s)
        a = deceleration (km/h/s -> cm/s^2)
        d = distance (cm)

    Args:
        clearance_cm: Available distance before obstruction
        deceleration_rate: Braking rate in km/h per second
        safety_margin_cm: Buffer distance (default 25cm)

    Returns:
        Maximum safe speed in km/h
    """
    effective_distance = max(0, clearance_cm - safety_margin_cm)

    if effective_distance <= 0:
        return 0.0

    if deceleration_rate <= 0:
        return 0.0

    km_h_to_cm_s = get_speed_conversion_factor()
    decel_cm_s2 = deceleration_rate * km_h_to_cm_s

    # v^2 = 2 * a * d
    v_cm_s_squared = 2 * decel_cm_s2 * effective_distance
    v_cm_s = math.sqrt(v_cm_s_squared)

    # Convert back to km/h
    v_kmh = v_cm_s / km_h_to_cm_s

    return v_kmh


def calculate_speed_for_target(
    distance_cm: float,
    target_speed_kmh: float,
    deceleration_rate: float,
) -> float:
    """
    Calculate maximum speed that allows decelerating to target speed within distance.

    Uses physics formula: v_final^2 = v_initial^2 - 2 * a * d
    Rearranged: v_initial = sqrt(v_final^2 + 2 * a * d)

    Args:
        distance_cm: Available distance before reaching target
        target_speed_kmh: Required speed at end of distance
        deceleration_rate: Braking rate in km/h per second

    Returns:
        Maximum initial speed in km/h that allows reaching target_speed_kmh
    """
    if distance_cm <= 0:
        return target_speed_kmh

    if deceleration_rate <= 0:
        return target_speed_kmh

    km_h_to_cm_s = get_speed_conversion_factor()
    decel_cm_s2 = deceleration_rate * km_h_to_cm_s
    target_cm_s = target_speed_kmh * km_h_to_cm_s

    # v_initial^2 = v_final^2 + 2 * a * d
    v_initial_squared = target_cm_s * target_cm_s + 2 * decel_cm_s2 * distance_cm
    v_initial_cm_s = math.sqrt(v_initial_squared)

    # Convert back to km/h
    v_kmh = v_initial_cm_s / km_h_to_cm_s

    return v_kmh


def _rate_limit_handle(
    current: HandlePosition,
    target: HandlePosition,
    allow_fast: bool = False,
) -> HandlePosition:
    """Module-level forward to `ato_navigator.rate_limit_handle(...)`.

    Kept under the original name so `tests/test_trains_ato.py` and any other
    callers that already import it from `train_controller` keep working.
    """
    from ato_navigator import rate_limit_handle

    return rate_limit_handle(current, target, allow_fast)


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
    eb_active: bool = False  # Emergency brake active

    # -------------------------------------------------------------------------
    # Notifications (for frontend toast messages via API)
    # -------------------------------------------------------------------------
    pending_notifications: List[str] = field(default_factory=list)

    # -------------------------------------------------------------------------
    # Cab & Mode State
    # -------------------------------------------------------------------------
    cab: Optional[str] = None  # 'A', 'B', or None
    control_mode: str = "ato"  # 'ato' or 'manual'
    manual_mode_type: str = "simulation"  # 'simulation' or 'power_percent'

    # -------------------------------------------------------------------------
    # Operational State
    # -------------------------------------------------------------------------
    state: TrainState = field(default=TrainState.OFF)
    handle_position: HandlePosition = field(default=HandlePosition.N)
    manual_power_percent: int = 0  # 0..100 voltage-style command for manual power_percent mode

    # -------------------------------------------------------------------------
    # ATO State
    # -------------------------------------------------------------------------
    ato_running: bool = False
    # `path_backed_run_active` was a stored field in earlier code; per the
    # design's "Latch table" canonical home it is not part of the schema. It
    # is now a derived property (see below) that reads
    # `_has_path_backed_run_context(...)` live on every access. Kept on the
    # class as a `@property` so existing readers continue to work.

    # -------------------------------------------------------------------------
    # ATC State (calculated each tick)
    # -------------------------------------------------------------------------
    # Note: target_speed and speed_limit_reason are read live from
    # `self.lookahead[0]` per design_new.md; they are not stored.
    # `_last_atc_limit_reason_code` mirrors the structured
    # `AtcLimitReasonCode` from `speed_curves[0]` so consumers can read the
    # enum directly without parsing the free-form string detail.
    clearance_ahead_cm: float = 0.0
    atc_speed_limit: float = 0.0
    current_block_speed_limit: float = float("inf")
    _last_atc_limit_reason_code: Optional["AtcLimitReasonCode"] = None

    # Configuration
    safety_margin_cm: float = TRACK_END_SAFETY_MARGIN_CM
    signal_lookahead_blocks: int = ATC_SIGNAL_LOOKAHEAD_BLOCKS

    # -------------------------------------------------------------------------
    # ATO Info (calculated each tick; lookahead is a derived property below)
    # -------------------------------------------------------------------------
    ato_info: Optional[ATOInfo] = None

    _previous_speed: float = 0.0
    handle_reason: str = ""  # Diagnostic: reason for current handle position
    # Per-tick ATO debug snapshot populated by `ATONavigator.compute_handle`.
    # Read by `routes/inspector.py` "ATO Debug" group. Reset/replaced each tick.
    # Schema (all keys present after the first ATO tick):
    #   mode: "stopping" | "stopped" | "no_curves" | "no_train" | "coast" |
    #         "power_up" | "brake"
    #   delta_seconds, op_decel, planning_horizon_cm, ceiling_at_zero,
    #   curve_at_zero, current_speed_kmh, samples (list of dicts),
    #   binding_distance_cm, binding_target_kmh, required_decel_kmh_s,
    #   multiplier_needed, target_handle, final_handle.
    ato_debug: Optional[Dict[str, Any]] = None

    # -------------------------------------------------------------------------
    # Phase 3 Mailbox (per design_new.md)
    # -------------------------------------------------------------------------
    queued_progress_reports: List[ControllerProgressReport] = field(default_factory=list)
    # The four Phase-3 latches (`path_cancel_request`, `route_abort`,
    # `power_off`, `route_deactivate`) are bundled into `PhaseLatches`. Writers
    # go through the `set_*` helpers; consumers (`simulation.py`) call the
    # matching `consume_*()` methods. See runtime_payloads.PhaseLatches.
    phase_latches: PhaseLatches = field(default_factory=PhaseLatches)

    # -------------------------------------------------------------------------
    # Operator Command Inbox (per design_new.md "Command queue + drain")
    # -------------------------------------------------------------------------
    # Operator commands enter the controller via the public command methods
    # (power_on_cmd, set_cab, set_mode, etc.). Each command is enqueued into
    # `command_inbox` and then drained by `_drain_command_inbox(...)` at the
    # very top of `tick(...)`. Commands are processed in the canonical kind
    # order (power_on -> set_cab -> set_mode -> set_manual_mode_type ->
    # set_handle/set_manual_power_percent -> activate_emergency_cmd ->
    # release_emergency_cmd -> power_off), with FIFO order within each kind
    # bucket.
    command_inbox: List[Any] = field(default_factory=list)
    # Canonical drain order from design_new.md "Phase 2 — Execute" /
    # "Driver/operator inputs": operator commands only. `ato_go` / `ato_stop`
    # are internal helpers (called by depart() and by the reactive transition
    # in on_physics_updated), not operator-issued commands, so they are not
    # part of the drain order.
    _command_drain_order: tuple = (
        "power_on",
        "set_cab",
        "set_mode",
        "set_manual_mode_type",
        "set_handle",
        "set_manual_power_percent",
        "activate_emergency_cmd",
        "release_emergency_cmd",
        "power_off",
    )

    # -------------------------------------------------------------------------
    # Controller-owned live span / speed (per design_new.md)
    #
    # `current_span` is the canonical TrainSpan storage, written by
    # `on_physics_updated(...)`. The doc-owned TrainSpan schema is being
    # introduced incrementally; until it is, this field carries whichever
    # concrete shape the producer (TrainPhysics) currently emits.
    # `current_speed_kmh` is the canonical live speed.
    # -------------------------------------------------------------------------
    current_span: Optional[Any] = None
    current_speed_kmh: float = 0.0

    # -------------------------------------------------------------------------
    # Helper modules (lookahead / ATC / ATO computation)
    # -------------------------------------------------------------------------
    #
    # These hold a back-reference to `self` and own the actual logic that the
    # controller used to inline. Lazy-initialised so the dataclass doesn't
    # need a custom `__post_init__`.
    _lookahead_builder: Any = field(default=None, repr=False, compare=False)
    _atc_calculator: Any = field(default=None, repr=False, compare=False)
    _ato_navigator: Any = field(default=None, repr=False, compare=False)

    def _get_lookahead_builder(self):
        if self._lookahead_builder is None:
            from lookahead_builder import LookaheadBuilder

            self._lookahead_builder = LookaheadBuilder(self)
        return self._lookahead_builder

    def _get_atc_calculator(self):
        if self._atc_calculator is None:
            from atc_calculator import ATCCalculator

            self._atc_calculator = ATCCalculator(self)
        return self._atc_calculator

    def _get_ato_navigator(self):
        if self._ato_navigator is None:
            from ato_navigator import ATONavigator

            self._ato_navigator = ATONavigator(self)
        return self._ato_navigator

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

        return DEFAULT_SPEED_PROFILE

    # -------------------------------------------------------------------------
    # Derived properties (per design_new.md)
    #
    # These are read-only properties; nothing in the controller writes them.
    # They recompute from the controller's storage (cab, current_span,
    # received_path_plan, etc.) on every read.
    # -------------------------------------------------------------------------

    @property
    def received_path_plan(self) -> Optional["Path"]:
        """Live read of the controller's active authority.

        Resolves to PathDispatcher.get_active_path(self.train_id) on every read.
        There is no setter and no stored field. When the dispatcher transitions
        the path's status away from ACTIVE, this property returns None on the
        next read automatically.
        """
        if self.path_dispatcher is None:
            return None
        getter = getattr(self.path_dispatcher, "get_active_path", None)
        if getter is None:
            return None
        return getter(self.train_id)

    @property
    def assigned_path_id(self) -> Optional[str]:
        """Computed read derived from received_path_plan.path_id."""
        plan = self.received_path_plan
        if plan is None:
            return None
        return getattr(plan, "path_id", None) or getattr(plan, "id", None)

    @property
    def path_backed_run_active(self) -> bool:
        """Derived read: does this controller currently have a path/route/
        destination context backing the run?

        Per design_new.md "Latch table" canonical home, this is not a stored
        latch field — it is a derived projection of `received_path_plan`,
        the active route execution state, and `ato_destination_block_id`.
        Returns the same boolean that `_has_path_backed_run_context(...)`
        used to compute when this was a stored field.
        """
        train = self._get_train()
        if train is None:
            return False
        return self._has_path_backed_run_context(train)

    @property
    def ato_path_id(self) -> Optional[str]:
        """Computed read derived from received_path_plan.path_id."""
        return self.assigned_path_id

    @property
    def ato_destination_block_id(self) -> Optional[str]:
        """Computed read derived from `received_path_plan.authority_end_block_id`.

        Per design_new.md ~line 80, `Path` carries `authority_end_block_id`
        as a derived read over `resource_sequence`; this property simply
        forwards it.
        """
        plan = self.received_path_plan
        if plan is None:
            return None
        return plan.get_authority_end_block_id() or None

    @property
    def travel_direction(self) -> Optional[str]:
        """Read-only derived value computed each read from (self.cab, self.current_span).

        Per design_new.md: the cab's physical end is the head, and travel_direction
        is the network-frame direction pointing from the opposite physical end
        through the cab end and onward. When no next span resource exists ahead,
        it is the direction outward through the cab edge of the cab end's tile.
        Returns None when no cab is selected.

        Resolution order:
        1. From `current_span` (canonical) when wired — the direction from
           `span.tail.tile` to `span.head.tile`, or for single-tile spans
           the outward direction through the head's exit edge.
        2. Fall back to `train.position.facing_direction` when the canonical
           `current_span` is not yet populated (e.g. before the first
           physics tick).
        """
        if self.cab is None:
            return None

        # Canonical path: derive from current_span when it carries head/tail.
        span = getattr(self, "current_span", None)
        if span is not None:
            head = getattr(span, "head", None)
            tail = getattr(span, "tail", None)
            if head is not None:
                head_tile = getattr(head, "tile", None)
                tail_tile = getattr(tail, "tile", None) if tail is not None else None
                if head_tile is not None and tail_tile is not None and head_tile != tail_tile:
                    direction = self._direction_from_tiles(tail_tile, head_tile)
                    if isinstance(direction, Direction):
                        return self._direction_to_edge(direction) or direction.name
                head_entry = getattr(head, "entry_edge", None)
                if isinstance(head_entry, str) and head_entry:
                    # Single-tile span (or no tail): outward direction is the
                    # opposite of the entry edge through the head.
                    return OPPOSITE_EDGES.get(head_entry.upper(), head_entry)

        # Fallback: existing TrainPosition-derived direction.
        train = self._get_train()
        if not train or not getattr(train, "position", None):
            return None
        position = train.position
        facing = getattr(position, "facing_direction", None)
        if isinstance(facing, Direction):
            edge = self._direction_to_edge(facing)
            if edge:
                return edge
            return facing.name
        if isinstance(facing, str) and facing:
            return facing
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
            self._drain_command_inbox(self._estimate_event_time())
        except Exception as exc:
            # Defensive: do not let a single bad command crash the tick.
            # Surface via NotificationCenter so operators see the failure;
            # fall back to logger only if the canonical sink is itself broken.
            import traceback as _tb

            event_time = self._estimate_event_time()
            sc = self.simulation_controller
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
            self.eb_active,
            self.control_mode,
            self.state.value,
            self.current_speed_kmh,
        )

        # ===== 0. POWER CHECK =====
        if not self.power_on:
            self.state = TrainState.OFF
            logger.debug(
                "[TrainController %s] power off -> state=off",
                self.train_id,
            )
            return

        # ===== 1. EMERGENCY BRAKE CHECK =====
        if self.eb_active:
            self.handle_position = HandlePosition.EB
            self.atc_speed_limit = 0.0

            logger.debug(
                "[TrainController %s] EB active -> handle=EB speed=%.2f",
                self.train_id,
                self.current_speed_kmh,
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
        if self.cab is None:
            self.atc_speed_limit = 0.0
            self._last_atc_limit_reason_code = AtcLimitReasonCode.NO_CAB
            logger.debug(
                "[TrainController %s] ATC blocked: no cab selected",
                self.train_id,
            )
        elif self._is_cab_wrong_direction():
            self.atc_speed_limit = 0.0
            self._last_atc_limit_reason_code = AtcLimitReasonCode.WRONG_DIRECTION_CAB
            logger.debug(
                "[TrainController %s] ATC blocked: cab opposite active authority",
                self.train_id,
            )
        else:
            # Get block speed limit for current block
            self.current_block_speed_limit = self._get_current_block_speed_limit()

            # Calculate clearance and ATC speed. Per design_new.md, the
            # canonical reason for the published ATC limit is the
            # `atc_limit_reason_*` fields at distance 0 of the lookahead;
            # the structured code is mirrored onto `_last_atc_limit_reason_code`
            # so consumers (journey harness, monitor) can read the enum
            # without parsing the free-form string.
            self.clearance_ahead_cm = self._calculate_clearance_ahead()
            (
                self.atc_speed_limit,
                _,
                self._last_atc_limit_reason_code,
            ) = self._calculate_atc_speed_limit_with_code()
            logger.debug(
                "[TrainController %s] ATC: clearance=%.2f limit=%.2f",
                self.train_id,
                self.clearance_ahead_cm,
                self.atc_speed_limit,
            )

        # ===== 4. ATC ACTION =====
        overspeed = self.current_speed_kmh - self.atc_speed_limit

        if overspeed > 10:
            # Emergency brake - overspeed > 10 km/h. Per design_new.md the
            # EB controller-local effects are: set eb_active = True,
            # force handle to EB, clear ato_running, and (when an active path
            # exists) write pending_path_cancel_request(EB).
            self.eb_active = True
            self.handle_position = HandlePosition.EB
            self.ato_running = False
            if self.received_path_plan is not None:
                self.phase_latches.set_path_cancel_request(
                    "eb", "ATC overspeed escalation"
                )
            self.pending_notifications.append(
                "ATC triggered emergency brake: overspeed"
            )
            logger.debug(
                "[TrainController %s] ATC EB: overspeed=%.2f",
                self.train_id,
                overspeed,
            )
            return
        elif overspeed > 5:
            logger.debug(
                "[TrainController %s] ATC warning: overspeed=%.2f",
                self.train_id,
                overspeed,
            )

        # ===== 5. ATO INFO =====
        self.ato_info = self._calculate_ato_info()

        # ===== 6. ATO ACTION (skip if manual) =====
        # Compute the effective_limit locally; consumers read this via the
        # `target_speed` property when needed.
        effective_limit = min(
            train.max_speed,
            self.atc_speed_limit,
            self.current_block_speed_limit,
        )

        if self.control_mode == "ato":
            # NOTE: We no longer auto-correct cab during tick().
            # Previously, this would partially update cab (leading_end only) without
            # flipping facing_direction and entry edges, causing state corruption.
            # Cab selection should be done:
            # 1. Manually by the operator before starting ATO
            # 2. Automatically in ato_go() when cab is None (using required_cab)
            # The operator can freely change cab when ato_running=False.

            # Calculate handle position based on ato_running
            if self.ato_running:
                # ATO is running - calculate handle to reach target speed
                self.handle_position = self._ato_calculate_handle(delta)
                logger.debug(
                    "[TrainController %s] ATO handle=%s target=%.2f",
                    self.train_id,
                    self.handle_position.value,
                    effective_limit,
                )
            else:
                # ATO not running - brake to stop. Transition to the
                # state-machine-derived stopped ready state once stopped.
                self.handle_position = self._calculate_braking_notch_for_speed(
                    self.current_speed_kmh, 0.0
                )
                if self.current_speed_kmh <= STOPPED_SPEED_THRESHOLD_KMH:
                    self.transition_to_ready()
                    self.handle_position = HandlePosition.N
        else:
            # Manual mode - ATC still enforces braking if overspeed
            if self.current_speed_kmh > self.atc_speed_limit:
                self.handle_position = self._calculate_braking_notch_for_speed(
                    self.current_speed_kmh, self.atc_speed_limit
                )
                logger.debug(
                    "[TrainController %s] Manual ATC brake: handle=%s",
                    self.train_id,
                    self.handle_position.value,
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
        if self.state == TrainState.RUNNING_MOVING:
            # Per design_new.md "Per-train tick transaction" (~line 196,
            # 282, 297): only RouteExecutor (Phase 1) writes
            # RUNNING_AT_STATION, via TrainController.enter_station_dwell().
            # Phase 2 must not transition to RUNNING_AT_STATION here, even
            # when the stopped train sits in a scheduled-stop station
            # block — the path-completion gate is_stopped_at_authority_end()
            # requires state == RUNNING_MOVING, and Phase 3's
            # try_complete_active_path(...) runs after this method
            # returns. Dwell entry happens on the next eligible Phase 1
            # tick once the dispatcher has completed the path and recorded
            # the stop's finished_at.

            if self._is_standalone_path_complete(train):
                self.transition_to_ready()
                logger.debug(
                    "[TrainController %s] standalone path complete -> READY",
                    self.train_id,
                )

            # Per design_new.md, a train at zero ATC clearance with live
            # authority stays in RUNNING_MOVING with current_speed_kmh == 0.
            # The reactive speed-based evaluation at the top of tick() handles
            # the no-authority no-speed case. There is nothing to do here.

        # Other states (READY, RUNNING_AT_STATION, OFF) handled by commands

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
            return PhysicsCommand(
                kind=PHYSICS_KIND_HANDLE,
                handle=HandlePosition.N,
                voltage_source_speed_kmh=0.0,
            )
        if self.eb_active:
            return PhysicsCommand(
                kind=PHYSICS_KIND_EMERGENCY_BRAKE,
                handle=HandlePosition.EB,
                voltage_source_speed_kmh=0.0,
            )
        if (
            self.control_mode == "manual"
            and self.manual_mode_type == "power_percent"
        ):
            profile = self._get_speed_profile()
            pct = float(self.manual_power_percent or 0.0)
            target = profile.power_percent_to_speed(pct) if profile else 0.0
            atc = float(self.atc_speed_limit or 0.0)
            if atc > 0 and target > atc:
                target = atc
            target = max(0.0, target)
            return PhysicsCommand(
                kind=PHYSICS_KIND_TARGET_SPEED,
                target_speed_kmh=target,
                voltage_source_speed_kmh=target,
            )
        # ATO and manual `simulation` submode: handle integration.
        if self.control_mode == "ato":
            voltage_speed = float(self.target_speed or 0.0)
        else:
            voltage_speed = float(self.current_speed_kmh or 0.0)
        return PhysicsCommand(
            kind=PHYSICS_KIND_HANDLE,
            handle=self.handle_position,
            voltage_source_speed_kmh=voltage_speed,
        )

    # -------------------------------------------------------------------------
    # ATC (Automatic Train Control) Logic
    # -------------------------------------------------------------------------

    def _calculate_clearance_ahead(self) -> float:
        """Forward to `ATCCalculator.clearance_ahead()`."""
        return self._get_atc_calculator().clearance_ahead()

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

        if not self.ato_destination_block_id:
            return base_clearance

        plan = self.received_path_plan
        resource_sequence = (
            getattr(plan, "resource_sequence", None) if plan else None
        )
        dest_block_id = self.ato_destination_block_id
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

        head_distance_cm = train.position.head_distance_cm if train.position else 0.0

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
        if not train or not train.position:
            return None

        # First, try to get direction from current_path
        path_tiles = self.get_path_tiles()
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

        # Fall back to facing direction
        return train.position.facing_direction

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

    def _calculate_atc_speed_limit(self) -> Tuple[float, Optional[str]]:
        """Forward to `ATCCalculator.speed_limit()`."""
        return self._get_atc_calculator().speed_limit()

    def _calculate_atc_speed_limit_with_code(
        self,
    ) -> Tuple[float, Optional[str], Optional[AtcLimitReasonCode]]:
        """Forward to `ATCCalculator.speed_limit_with_code()`."""
        return self._get_atc_calculator().speed_limit_with_code()

    def _is_caution_ahead(self) -> bool:
        """Forward to `ATCCalculator.is_caution_ahead()`."""
        return self._get_atc_calculator().is_caution_ahead()

    def _is_cab_wrong_direction(self) -> bool:
        """Return True when the active cab points away from active authority.

        v1 only flags wrong-direction cab when there is active path authority
        (no active authority falls under the `no_authority` reason instead).
        Compares the current cab's required-cab projection from the active
        path with the active cab and returns True on mismatch.
        """
        if self.cab not in ("A", "B"):
            return False
        if self.received_path_plan is None:
            return False
        required = self._required_cab_for_path()
        if required is None:
            return False
        return required != self.cab

    def _get_effective_safety_margin(self) -> float:
        """Forward to `ATCCalculator.effective_safety_margin()`."""
        return self._get_atc_calculator().effective_safety_margin()

    def _get_current_block_id(self) -> Optional[str]:
        """Get the block ID where the train's head is located."""
        head_tile = self._get_head_tile()
        if not head_tile or not self.network:
            return None

        track = self.network.tracks.get(head_tile)
        if track:
            return track.block_section_id

        return None

    def _get_head_tile(self) -> Optional[Tuple[int, int]]:
        """Get the head tile for the train."""
        train = self._get_train()
        if not train:
            return None

        if train.position:
            return train.position.head_tile

        return None

    def _get_head_distance_cm(self) -> float:
        """Get head distance within the current tile in cm."""
        train = self._get_train()
        if not train:
            return 0.0

        if train.position:
            return train.position.head_distance_cm

        return 0.0

    def get_path_tiles(self) -> Optional[List[Tuple[int, int, "Direction"]]]:
        """Get path tiles from PathDispatcher (single source of truth).

        Reads `Path.path_tiles` from the dispatcher's currently active path
        for this train and converts it to `(x, y, Direction)` format.
        Returns `None` if no active path exists.
        """
        if not self.path_dispatcher:
            return None
        path = self.path_dispatcher.get_active_path(self.train_id)
        if not path:
            return None
        path_tiles = path.get_path_tiles()
        if not path_tiles:
            return None
        from path_utils import convert_tiles_to_train_path
        return convert_tiles_to_train_path(path_tiles)

    def get_live_path_data(self) -> Optional[Dict[str, Any]]:
        """Get live path data for serialization (Train.to_dict).

        Returns path_tiles and the current head path_index, both derived
        from the PathDispatcher (source of truth). Returns None if the
        train has no active path.

        Returns:
            Dict with 'path_tiles' (List[Tuple[int, int, Direction]]) and
            'path_index' (int), or None if no active path.
        """
        train = self._get_train()
        if not train:
            return None

        path_tiles = self.get_path_tiles()
        if path_tiles is None:
            return None

        # Derive head path index from train's physical position
        head_tile = self._get_head_tile()
        if head_tile:
            path_index = self._get_head_path_index(head_tile)
        else:
            path_index = -1

        return {
            "path_tiles": path_tiles,
            "path_index": path_index,
        }

    def _get_head_path_index(self, head_tile: Tuple[int, int]) -> int:
        """Get the path index for the train head tile.

        v1 path geometry forbids tile re-visits in a single direction of
        travel, so the first match is always the correct one.
        """
        path_tiles = self.get_path_tiles()
        if not path_tiles:
            return -1
        for i, (x, y, _) in enumerate(path_tiles):
            if (x, y) == head_tile:
                return i
        return -1

    def _direction_between_tiles(
        self, from_tile: Tuple[int, int], to_tile: Tuple[int, int]
    ) -> Optional[Direction]:
        """Convert an adjacent tile-to-tile delta into a Direction."""
        dx = to_tile[0] - from_tile[0]
        dy = to_tile[1] - from_tile[1]

        if dx == 1 and dy == 0:
            return Direction.EAST
        if dx == -1 and dy == 0:
            return Direction.WEST
        if dx == 0 and dy == 1:
            return Direction.NORTH
        if dx == 0 and dy == -1:
            return Direction.SOUTH
        return None

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

    def _get_head_entry_edge(self, train: "Train") -> Optional[str]:
        """Get the current head tile entry edge based on leading end."""
        if not train.position:
            return None

        if train.position.leading_end == "a":
            return train.position.end_a_entry_edge
        if train.position.leading_end == "b":
            return train.position.end_b_entry_edge
        return None

    def _can_traverse_path_step(
        self,
        train: "Train",
        from_tile: Tuple[int, int],
        to_tile: Tuple[int, int],
        entry_edge_into_from: Optional[str],
    ) -> Tuple[bool, Optional[str]]:
        """
        Check whether traversal from one path tile to the next is currently usable.

        Returns:
            (is_traversable, entry_edge_into_to_tile)
        """
        if not self.network:
            return False, None

        from_track = self.network.get_track(from_tile[0], from_tile[1])
        to_track = self.network.get_track(to_tile[0], to_tile[1])
        if not from_track or not to_track:
            return False, None

        direction = self._direction_between_tiles(from_tile, to_tile)
        if direction is None:
            return False, None

        exit_edge = self._direction_to_edge(direction)
        if not exit_edge:
            return False, None

        to_entry_edge = OPPOSITE_EDGES.get(exit_edge)
        if not to_entry_edge:
            return False, None

        assigned_path_id = self.assigned_path_id

        for tile_track in (from_track, to_track):
            if tile_track.is_switch and tile_track.is_locked_by_path():
                if (
                    not assigned_path_id
                    or tile_track.locked_by_path != assigned_path_id
                ):
                    return False, None

        if (
            to_track.block_section_id
            and self.network
            and to_track.block_section_id in self.network.blocks
        ):
            target_block = self.network.blocks[to_track.block_section_id]
            if target_block.reserved_by_path:
                if (
                    not assigned_path_id
                    or target_block.reserved_by_path != assigned_path_id
                ):
                    return False, None

        if entry_edge_into_from:
            from_exits = from_track.get_exits(
                entry_edge_into_from, use_switch_state=True
            )
            if exit_edge not in from_exits:
                return False, None
        elif exit_edge not in from_track.edges:
            return False, None

        to_exits = to_track.get_exits(to_entry_edge, use_switch_state=True)
        if not to_exits:
            return False, None

        return True, to_entry_edge

    def _build_empty_monitor_lookahead(
        self,
        *,
        head_tile: Optional[Tuple[int, int]] = None,
        head_distance_cm: float = 0.0,
        travel_direction: Optional[str] = None,
        speed_limit_reason: Optional[str] = None,
        reason_code: Optional[AtcLimitReasonCode] = None,
    ) -> Dict[str, Any]:
        """Build a consistent empty lookahead payload for Train Monitor.

        Per design_new.md "canonical lookahead payload", the empty form
        preserves the full top-level and nested key shape with empty lists and
        zero distances. When `reason_code` is provided, a single distance-0
        anchor is stamped onto `speed_curves` so consumers reading the
        canonical reason at distance 0 (target_speed / speed_limit_reason
        properties) see the structured `AtcLimitReasonCode` without a separate
        controller-owned cache. Used for the no-cab, no-authority (no path),
        and head-off-path cases.
        """
        payload: Dict[str, Any] = {
            "train_id": self.train_id,
            "head_tile": list(head_tile) if head_tile is not None else None,
            "head_distance_cm": head_distance_cm,
            "travel_direction": travel_direction,
            "cab": self.cab,
            "speed_limit_reason": speed_limit_reason,
            "lookahead": {
                "blocks": [],
                "signals": [],
                "switches": [],
                "stations": [],
                "clearance_cm": 0.0,
                "safety_margin_cm": 0.0,
                "authority_boundary_delta_cm": 0.0,
                "speed_curves": [],
            },
        }
        if reason_code is not None:
            payload["lookahead"]["speed_curves"] = [
                {
                    "distance_cm": 0.0,
                    "atc_limit": 0.0,
                    "operational_limit": 0.0,
                    "atc_limit_reason_code": reason_code.value,
                    "atc_limit_reason_detail": speed_limit_reason,
                }
            ]
        return payload

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

        plan = self.received_path_plan
        resource_sequence = (
            getattr(plan, "resource_sequence", None) if plan else None
        )
        if train.position and resource_sequence:
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
            remaining = max(0.0, head_tile_length - train.position.head_distance_cm)

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
                assigned_path_id = self.assigned_path_id
                if assigned_path_id:
                    if block.reserved_by_path != assigned_path_id:
                        return True
                else:
                    # Block reserved but train has no path
                    return True

        # CAUTION state means train just left
        if block.state == BlockState.CAUTION:
            return True

        return False

    def _is_signal_red_for_block(self, block_id: str) -> bool:
        """
        Check if there's a red signal protecting this block for the train's
        direction of travel.

        Only considers signals whose detection_direction matches the train's
        facing direction. A signal with detection_direction='E' monitors
        eastbound traffic and should not block a westbound train.
        """
        if not self.network:
            return False

        # Get the train's travel direction as an edge string ("N"/"E"/"S"/"W")
        train = self._get_train()
        travel_edge = None
        if train and train.facing_direction:
            travel_edge = self._direction_to_edge(train.facing_direction)

        for signal_id, signal in self.network.signals.items():
            if (
                hasattr(signal, "monitored_block_id")
                and signal.monitored_block_id == block_id
            ):
                if signal.state == SignalState.RED:
                    # If we know the train's direction, only consider signals
                    # that monitor traffic in that direction
                    if travel_edge and hasattr(signal, "detection_direction"):
                        if signal.detection_direction != travel_edge:
                            continue
                    return True

        return False

    def _is_approaching_yellow_signal(self) -> bool:
        """Forward to `ATCCalculator.is_approaching_yellow_signal()`."""
        return self._get_atc_calculator().is_approaching_yellow_signal()

    def _get_current_block_speed_limit(self) -> float:
        """Forward to `LookaheadBuilder.current_block_speed_limit()`."""
        return self._get_lookahead_builder().current_block_speed_limit()

    def is_safe_to_move(self) -> bool:
        """
        Check if it's safe for the train to move.
        """
        return self.clearance_ahead_cm > ATC_MIN_CLEARANCE_CM

    @property
    def power_on(self) -> bool:
        """Convenience read derived from `state != OFF` per design_new.md."""
        return self.state != TrainState.OFF

    @power_on.setter
    def power_on(self, value: bool) -> None:
        """Convenience setter that maps to the canonical `state` field.

        ``True`` selects READY (cab selection is orthogonal per design_new.md).
        ``False`` forces OFF. Production code should write `self.state`
        directly (or call `power_on_cmd()` / `power_off_cmd()`).
        """
        if value:
            if self.state == TrainState.OFF:
                self.state = TrainState.READY
        else:
            self.state = TrainState.OFF

    def is_stopped(self) -> bool:
        """Canonical stopped predicate per design_new.md.

        Reads controller-owned speed (current_speed_kmh). The threshold is
        STOPPED_SPEED_THRESHOLD_KMH per the units contract.
        """
        speed_kmh = getattr(self, "current_speed_kmh", 0.0) or 0.0
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
        if self.received_path_plan is not None:
            conditions.add(ControllerCondition.HAS_AUTHORITY)
        if self.eb_active:
            conditions.add(ControllerCondition.EB_LATCHED)
        latches = self.phase_latches
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
        if self.state != TrainState.READY:
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

        The spec lists two satisfying cases for this gate: the train has
        either "physically progressed through the final `resource_sequence`
        entry" (i.e. its head occupies the authority-end block), or "the
        remaining projected authority distance is too short to grant
        additional positive clearance after applying the controller safety
        margin" (i.e. `atc_limit` at distance 0 has fallen to zero). The
        first case is the natural commit point under discrete physics:
        ATO brakes the train to rest somewhere inside the destination
        block, and forward-Euler integration cannot land exactly on the
        safety-margin boundary, so `atc_limit` at d=0 generally still
        reads a few km/h. Requiring `atc_limit == 0` (or the operational-
        limit equivalent) would leave the gate failing in normal cases
        and trap ATO in a low-speed creep loop. Using head-in-destination
        + reason-code AUTHORITY_STOP captures the canonical intent without
        depending on a precision the integrator cannot deliver.
        """
        if not self.is_stopped():
            return False
        if self.state != TrainState.RUNNING_MOVING:
            return False
        if self.eb_active:
            return False
        plan = self.received_path_plan
        if plan is None:
            return False
        # ATO curve has terminated at the authority end: distance-0
        # lookahead's dominant restriction is the path-end stop.
        payload = self._compute_canonical_lookahead()
        curves = (payload or {}).get("lookahead", {}).get("speed_curves") or []
        if not curves:
            return False
        if (
            curves[0].get("atc_limit_reason_code")
            != AtcLimitReasonCode.AUTHORITY_STOP.value
        ):
            return False
        # Train head is at the destination (authority-end) block — the
        # spec's first satisfying case.
        head_block_id = self._get_current_block_id()
        if head_block_id is None:
            return False
        return head_block_id == plan.get_authority_end_block_id()

    # -------------------------------------------------------------------------
    # Physics callback — Phase 2 storage / Phase 3 enqueue (per design_new.md)
    # -------------------------------------------------------------------------

    def on_physics_updated(
        self,
        event_time: float,
        span_update: PhysicsSpanUpdate,
    ) -> None:
        """Apply a TrainPhysics span_update and enqueue a Phase 3 progress report.

        Per design_new.md "Physical progress / deviation procedure" and
        "TrainController" sections:
        1. Atomically write current_span and current_speed_kmh from the update.
        2. Build a ControllerProgressReport from the tile delta (added/removed
           tiles -> entered/tail-vacated resource events).
        3. Append the report to self.queued_progress_reports for SimulationController
           to drain in Phase 3.

        For powered-off transactions, the controller still applies span/speed
        but must not enqueue dispatcher/route progress reports because Phase 3
        is skipped. This method honors that rule: if power is off and the
        state is OFF, the queue write is skipped.
        """
        # 1. Storage update (canonical span + speed). The doc's TrainSpan is
        # owned by the train/controller side; until that schema lands here we
        # store the span on the controller (current_span) and let consumers
        # access it via the existing helpers.
        self.current_span = span_update.new_span
        self.current_speed_kmh = float(span_update.current_speed_kmh)

        # 1a. Reactive speed-based state evaluation (per design_new.md
        # "Reactive speed-based state evaluation"). Fires synchronously after
        # the new speed has been written and before building the progress
        # report, so any READY <-> RUNNING_MOVING transition lands in the same
        # tick that physics produced the speed change.
        if self.state == TrainState.READY and not self.is_stopped():
            self.state = TrainState.RUNNING_MOVING
            if self.control_mode == "ato" and self.received_path_plan is not None:
                # ato_go() is the canonical writer of ato_running = True.
                # From RUNNING_MOVING it is an idempotent success that skips
                # cab auto-selection.
                self.ato_go()
        elif (
            self.state == TrainState.RUNNING_MOVING
            and self.is_stopped()
            and self.received_path_plan is None
        ):
            # No-authority braking completion: zero speed reached without
            # live authority. transition_to_ready() clears ato_running and
            # path-run bookkeeping in lockstep.
            self.transition_to_ready()

        # 2/3. Build progress report from tile delta and enqueue for Phase 3.
        if self.state == TrainState.OFF:
            # Powered-off transactions: storage updated, but Phase 3 is
            # skipped — do not enqueue a report.
            return

        entered_resources: List[ResourceEvent] = []
        seen_entered: set = set()
        for tile in span_update.added_tiles:
            res_id = tile.resource_id
            res_type = tile.resource_type
            if res_id is None or res_type is None:
                continue
            key = (res_type, res_id)
            if key in seen_entered:
                continue
            seen_entered.add(key)
            entered_resources.append(
                ResourceEvent(
                    sequence_index=None,
                    resource_type=res_type,
                    resource_id=res_id,
                    exit_edge=None,
                    block_id=tile.block_id,
                )
            )

        # Compute the blocks still occupied by the train post-move so we
        # only emit "tail vacated R" events when R is fully vacated. Without
        # this guard, removing a single tile from a multi-tile block emits a
        # spurious tail-vacate for the block (the train still occupies other
        # tiles of the same block), which downstream causes BlockManager to
        # release the block reservation while the train is still on it —
        # which in turn flips the protecting signal to RED, truncates ATC
        # clearance, and fires the spurious overspeed EB.
        new_span = span_update.new_span
        still_occupied_block_ids: set = set(
            new_span.occupied_blocks if new_span and new_span.occupied_blocks else []
        )

        tail_vacated_resources: List[ResourceEvent] = []
        seen_vacated: set = set()
        for tile in span_update.removed_tiles:
            res_id = tile.resource_id
            res_type = tile.resource_type
            if res_id is None or res_type is None:
                continue
            key = (res_type, res_id)
            if key in seen_vacated:
                continue
            if res_type == "block" and res_id in still_occupied_block_ids:
                continue
            seen_vacated.add(key)
            exit_edge = tile.exit_edge if res_type == "block" else None
            tail_vacated_resources.append(
                ResourceEvent(
                    sequence_index=None,
                    resource_type=res_type,
                    resource_id=res_id,
                    exit_edge=exit_edge,
                    block_id=tile.block_id,
                )
            )

        report = ControllerProgressReport(
            train_id=self.train_id,
            event_time=float(event_time),
            entered_resources=entered_resources,
            tail_vacated_resources=tail_vacated_resources,
            current_head_sequence_index=None,
            deviation=False,
            deviation_kind=None,
            deviation_reason=None,
        )
        self.queued_progress_reports.append(report)

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

    def _get_signal_state_for_block(self, block_id: str) -> Optional[SignalState]:
        """Forward to `LookaheadBuilder.signal_state_for_block(block_id)`."""
        return self._get_lookahead_builder().signal_state_for_block(block_id)

    def _get_station_in_block(self, block_id: str) -> Optional[str]:
        """Forward to `LookaheadBuilder.station_in_block(block_id)`."""
        return self._get_lookahead_builder().station_in_block(block_id)

    # -------------------------------------------------------------------------
    # ATO Info Calculation
    # -------------------------------------------------------------------------

    def _calculate_ato_info(self) -> Optional[ATOInfo]:
        """Forward to `ATONavigator.compute_ato_info()`."""
        return self._get_ato_navigator().compute_ato_info()

    # -------------------------------------------------------------------------
    # Path Direction Detection
    # -------------------------------------------------------------------------

    def _get_effective_path_tiles(self):
        """Get active path tiles for control decisions.

        Per design_new.md ~line 1292, the dispatcher's `Path` is the single
        source of truth for active path geometry. This delegates to
        `get_path_tiles()` which reads `Path.path_tiles` from the active
        dispatcher path.
        """
        return self.get_path_tiles()

    def _has_active_route_context(self, train: "Train") -> bool:
        """Return True when the train still belongs to an active route."""
        route_id = getattr(train, "assigned_route_id", None)
        if not route_id:
            return False

        routes = getattr(self.network, "routes", None)
        if not routes:
            return False

        route = routes.get(route_id)
        return bool(route and route.is_active)

    def _has_active_path_authority(self, train: "Train") -> bool:
        """Return True while the train still has movement authority to follow."""
        if getattr(train, "assigned_path_id", None):
            return True
        if self._get_effective_path_tiles():
            return True
        if self.ato_path_id:
            return True
        return False

    def _has_path_backed_run_context(self, train: "Train") -> bool:
        """Return True when the current run is tied to a path, route, or destination."""
        if self._has_active_path_authority(train):
            return True
        if self._has_active_route_context(train):
            return True
        if self.ato_destination_block_id:
            return True
        return False

    def _is_standalone_path_complete(self, train: "Train") -> bool:
        """Return True when a stopped running train has no remaining run context."""
        if self.current_speed_kmh > STOPPED_SPEED_THRESHOLD_KMH:
            return False
        # ato_running=True is acceptable if there is no surviving path context —
        # it is a stale flag left by ATC braking the train to 0.  Trust
        # _has_path_backed_run_context() to confirm the path is truly gone.
        if self.ato_running and self._has_path_backed_run_context(train):
            return False
        if self._has_path_backed_run_context(train):
            return False
        return self.cab in ("A", "B")

    def _align_cab_to_required_path_direction(self) -> bool:
        """Ensure the stopped train's active cab matches the required path direction."""
        train = self._get_train()
        if not train:
            return False

        required_cab = self._required_cab_for_path()
        if required_cab is None or self.cab == required_cab:
            return True

        logger.info(
            "[TrainController %s] auto-correcting cab %s -> %s for path direction",
            self.train_id,
            self.cab,
            required_cab,
        )

        if self.current_speed_kmh > 0:
            self.current_speed_kmh = 0.0

        result = self.set_cab(required_cab)
        return result.success

    def _path_needs_cab_flip(self) -> bool:
        """Check if the current path requires the train to reverse direction.

        Uses track connectivity: compares the head tile's entry edge with
        the edge leading to the path's next tile.  If they match, the path
        goes backward through the head (back the way it came), so the cab
        must flip.

        This is more reliable than comparing cardinal directions, which can
        be misleading on zigzag / diagonal staircase tracks where the
        per-tile direction differs from the overall travel direction.

        Returns:
            True if cab should be flipped, False otherwise or if the check
            cannot be performed.
        """
        train = self._get_train()
        if not train or not train.position or not train.position.leading_end:
            return False

        path_tiles = self._get_effective_path_tiles()
        if not path_tiles or len(path_tiles) < 2:
            return False

        head_tile = train.position.head_tile
        head_entry_edge = train.position.head_entry_edge
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
        next_entry = path_tiles[head_idx + 1]
        dx = next_entry[0] - head_tile[0]
        dy = next_entry[1] - head_tile[1]
        _EDGE_MAP = {(1, 0): "E", (-1, 0): "W", (0, 1): "N", (0, -1): "S"}
        needed_edge = _EDGE_MAP.get((dx, dy))

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
        if not train or not train.position:
            return None

        current_cab = self.cab or "A"

        # --- Approach 1: track connectivity (preferred) ---
        flip = self._path_needs_cab_flip()
        if flip:
            return "B" if current_cab == "A" else "A"

        # --- Approach 2: direction fallback ---
        # Compare the path's travel direction with the train's facing
        # direction. This also resolves the "90 degrees at the head but overall
        # reverse" diagonal case after the direct backtracking check above.
        facing = train.facing_direction
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

        # 90 degrees or path direction unknown — use overall path vector
        path_tiles = self._get_effective_path_tiles()
        if path_tiles and len(path_tiles) >= 2:
            overall = self._dominant_direction_from_tiles(path_tiles[0], path_tiles[-1])
            if overall is not None and overall == facing.opposite():
                return "B" if current_cab == "A" else "A"

        return current_cab

    def _connectivity_check_was_conclusive(self, train) -> bool:
        """Return True if _path_needs_cab_flip had enough data to decide.

        Specifically: the head tile was found in the path and there was a
        next tile to compare against.  If the head wasn't found, the
        connectivity check was inconclusive and the caller should fall back.
        """
        if not train.position or not train.position.leading_end:
            return False
        path_tiles = self._get_effective_path_tiles()
        if not path_tiles or len(path_tiles) < 2:
            return False
        head_tile = train.position.head_tile
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
        if not train.position:
            return None

        head_tile = train.position.head_tile
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
    # Station Detection
    # -------------------------------------------------------------------------

    def _is_at_station(self) -> bool:
        """
        Check if the train is currently at a station (for state transition).

        A train is "at station" if:
        - It's in a block that contains a station
        - It's stopped (speed near zero)
        - The station is a scheduled stop on its route OR is the ATO destination

        When ato_destination_block_id is set (manual path), the train should
        only stop at the destination block, not at intermediate or departure
        stations. This prevents a false-positive "arrival" at the departure
        station on the first tick after depart() when speed is still 0.

        Returns:
            True if train is at a scheduled station stop
        """
        train = self._get_train()
        if not train or not self.network:
            return False

        # Must be stopped or nearly stopped
        if self.current_speed_kmh > STOPPED_SPEED_THRESHOLD_KMH:
            return False

        # Check if current block has a station
        current_block_id = self._get_current_block_id()
        if not current_block_id:
            return False

        station_id = self._get_station_in_block(current_block_id)
        if not station_id:
            return False

        # Check if this station is a scheduled stop on the train's route.
        # Per design_new.md "RouteTask data", v1 routes hold an ordered
        # `tasks` list whose `stop` and `dwell` entries imply a stop at
        # `task_location` (BlockTarget / PlatformTarget / StationTarget);
        # `waypoint` entries are pass-through and do not imply stopping.
        if self.route_executor is not None:
            route_id = self.route_executor.assigned_route_id_for(self.train_id)
            if route_id is not None:
                route = self.route_executor.route_registry.get(route_id)
                if route:
                    from train_route import PlatformTarget, StationTarget

                    station_group_id: Optional[str] = None
                    if hasattr(self.network, "get_station_for_platform"):
                        try:
                            station_group_id = self.network.get_station_for_platform(
                                station_id
                            )
                        except Exception:
                            station_group_id = None
                    for task in route.tasks:
                        if task.task_kind not in ("stop", "dwell"):
                            continue
                        loc = task.task_location
                        if isinstance(loc, PlatformTarget):
                            if loc.platform_id == station_id:
                                return True
                        elif isinstance(loc, StationTarget):
                            if station_group_id and loc.station_id == station_group_id:
                                return True

        # If ato_destination_block_id is set (manual path), only stop at the
        # destination block. This prevents stopping at the departure station
        # or at intermediate stations along the path.
        if self.ato_destination_block_id:
            return current_block_id == self.ato_destination_block_id

        # If no route and no destination, consider any station as a potential stop
        return True

    # -------------------------------------------------------------------------
    # ATO (Automatic Train Operation) Logic
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # Future-Aware ATO Helpers
    # -------------------------------------------------------------------------

    def _get_brake_level(self, handle: HandlePosition) -> int:
        """Convert brake handle to level number (1-8)."""
        brake_levels = {
            HandlePosition.B1: 1,
            HandlePosition.B2: 2,
            HandlePosition.B3: 3,
            HandlePosition.B4: 4,
            HandlePosition.B5: 5,
            HandlePosition.B6: 6,
            HandlePosition.B7: 7,
            HandlePosition.B8: 8,
        }
        return brake_levels.get(handle, 0)

    def _brake_level_to_handle(self, level: int) -> HandlePosition:
        """Convert level number (1-8) to brake handle."""
        handles = {
            1: HandlePosition.B1,
            2: HandlePosition.B2,
            3: HandlePosition.B3,
            4: HandlePosition.B4,
            5: HandlePosition.B5,
            6: HandlePosition.B6,
            7: HandlePosition.B7,
            8: HandlePosition.B8,
        }
        return handles.get(level, HandlePosition.B1)

    def _sample_curve_at(
        self, curve: List[Dict], distance_cm: float, key: str = "operational_limit"
    ) -> float:
        """Forward to `ATONavigator.sample_curve_at(curve, distance_cm, key)`."""
        from ato_navigator import ATONavigator

        return ATONavigator.sample_curve_at(curve, distance_cm, key)

    def _simulate_forward(
        self,
        current_speed_kmh: float,
        handle: HandlePosition,
        distance_cm: float,
        dt: float = 0.02,
    ) -> float:
        """Forward to `ATONavigator.simulate_forward(...)`."""
        return self._get_ato_navigator().simulate_forward(
            current_speed_kmh, handle, distance_cm, dt
        )

    def _ato_calculate_handle(self, delta_seconds: float = 0.05) -> HandlePosition:
        """Forward to `ATONavigator.compute_handle(delta_seconds)`."""
        return self._get_ato_navigator().compute_handle(delta_seconds)

    def _calculate_power_notch(
        self, current_speed: float, target_speed: float
    ) -> HandlePosition:
        """Forward to `ATONavigator.calculate_power_notch(...)`."""
        from ato_navigator import ATONavigator

        return ATONavigator.calculate_power_notch(current_speed, target_speed)

    def _calculate_braking_notch_for_speed(
        self, current_speed: float, target_speed: float
    ) -> HandlePosition:
        """Forward to `ATONavigator.calculate_braking_notch_for_speed(...)`."""
        from ato_navigator import ATONavigator

        return ATONavigator.calculate_braking_notch_for_speed(
            current_speed, target_speed
        )

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
        """
        Calculate ATC and ATO speed limit curves at 3cm resolution.

        For each sample point at distance d from train head, calculate:
        - Braking curve to path end (stop at max_distance_cm)
        - Braking curves to each speed restriction ahead
        - Take the minimum of all curves

        This creates the characteristic "speed envelope" where the curve
        shows what speed you can be at each point while still being able
        to stop or slow down before any restriction ahead.

        ATC Curve: Uses 100% of service braking (B8), NOT emergency brake.
                   This ensures EB can always stop the train if ATC is exceeded.

        ATO Curve: Uses 70% braking for comfort + stays 5 km/h below ATC.

        Args:
            max_distance_cm: Total clearance/path end distance (cm)
            lookahead_blocks: Block info with speed limits
            lookahead_signals: Signal info
            lookahead_stations: Station info (for tighter safety margin)
            target_stop_cm: Target stopping distance (clearance - safety_margin) for explicit stop
                point

        Returns:
            List of dicts with distance_cm, atc_limit, operational_limit
        """
        train = self._get_train()
        if not train:
            return []

        if override_reason_code is not None:
            # No cab / EB / wrong direction / no authority: ATC limit is zero
            # regardless of geometry. Build a flat zero curve with the reason
            # metadata stamped on every sample.
            override_curves: List[Dict[str, Any]] = []
            step = CURVE_SAMPLE_RESOLUTION_CM
            sample_distance = 0.0
            limit = max(0.0, max_distance_cm)

            def _override_point(distance: float) -> Dict[str, Any]:
                point: Dict[str, Any] = {
                    "distance_cm": distance,
                    "atc_limit": 0.0,
                    "operational_limit": 0.0,
                    "atc_limit_reason_code": override_reason_code.value,
                }
                if override_reason_detail:
                    point["atc_limit_reason_detail"] = override_reason_detail
                return point

            while sample_distance <= limit:
                override_curves.append(_override_point(sample_distance))
                sample_distance += step
            if not override_curves or override_curves[-1]["distance_cm"] < limit:
                override_curves.append(_override_point(limit))
            return override_curves

        profile = self._get_speed_profile()
        max_speed = train.max_speed
        decel_rate = profile.deceleration_rate
        operational_decel = decel_rate * profile.operational_decel_ratio

        # Build list of speed restrictions ahead. Each restriction tracks
        # distance, speed limit, safety margin, and a canonical reason code +
        # optional plain-text detail so the dominant constraint at each
        # sample stamps reason metadata into the canonical lookahead payload.
        restrictions: List[
            Tuple[float, float, float, AtcLimitReasonCode, Optional[str]]
        ] = []

        # Add path end as a stop (speed 0)
        # Use effective safety margin based on destination type:
        # - 5cm if ato_destination_id is a station
        # - 25cm otherwise
        # This ensures the curve matches what _get_effective_safety_margin() returns
        path_end_safety_margin = self._get_effective_safety_margin()
        # The path end stop is the safety-margin-adjusted authority boundary.
        # `path_end_reason_code` lets callers override the reason — the
        # fallback (no active path) builder uses `NO_AUTHORITY` here, while
        # the path-based builder uses `AUTHORITY_STOP`.
        path_end_code = path_end_reason_code or AtcLimitReasonCode.AUTHORITY_STOP
        restrictions.append(
            (max_distance_cm, 0.0, path_end_safety_margin, path_end_code, None)
        )

        # Add block speed limits as restrictions (for braking curve calculation)
        # This creates braking curves to slow down BEFORE entering speed-limited blocks
        if lookahead_blocks:
            for block in lookahead_blocks:
                block_start = block.get("start_cm", 0)
                block_speed = block.get("speed_limit_kmh")  # None if no limit
                if block_speed is not None and block_speed < max_speed:
                    block_state = block.get("state")
                    block_id = block.get("id")
                    if block_state == BlockState.CAUTION.value:
                        block_code = AtcLimitReasonCode.CAUTION
                    else:
                        block_code = AtcLimitReasonCode.OBSTRUCTION
                    detail = (
                        f"Block speed limit at {block_id}" if block_id else None
                    )
                    restrictions.append(
                        (block_start, block_speed, 0.0, block_code, detail)
                    )

        # Signal restrictions are intentionally NOT used for ATC/ATO speed calculation.
        # This is a CBTC block-based system where:
        # - Speed control comes from BLOCKS (clearance distance, speed_limit_kmh)
        # - Signals are visual indicators of block state only (for display in UI)
        # - Train stopping is enforced by block reservation, not signal aspects
        # See docs/ARCHITECTURE.md "CBTC Block-Based Signaling System" for details.
        #
        # if lookahead_signals:
        #     for signal in lookahead_signals:
        #         signal_dist = signal.get("distance_cm", float("inf"))
        #         signal_state = signal.get("state", "green")
        #         if signal_state == "red":
        #             restrictions.append((signal_dist, 0.0, path_end_safety_margin))
        #         elif signal_state in ("yellow", "double_yellow"):
        #             restrictions.append((signal_dist, YELLOW_SIGNAL_SPEED_KMH, 0.0))

        # NOTE: Station speed limits are NOT added as restrictions here.
        # ATC/ATO only cares about BLOCKS, not platforms. Station speed limits
        # are applied through the block's speed_limit_kmh property, not the station's
        # top_speed_kmh which is for visualization/informational purposes only.

        curves = []

        # Use 5cm resolution for smooth curves
        step = CURVE_SAMPLE_RESOLUTION_CM
        sample_distance = 0.0
        while sample_distance <= max_distance_cm:
            # For this sample point, find the minimum speed limit considering
            # all restrictions ahead, and remember which restriction's reason
            # code dominates so the curve point can carry it forward.

            # Each entry: (limit_kmh, reason_code, reason_detail)
            atc_candidates: List[Tuple[float, Optional[AtcLimitReasonCode], Optional[str]]] = [
                (max_speed, None, None)
            ]
            operational_limits: List[float] = [max_speed]

            for (
                restriction_dist,
                restriction_speed,
                restriction_safety,
                restriction_code,
                restriction_detail,
            ) in restrictions:
                # Distance from sample point to this restriction
                dist_to_restriction = restriction_dist - sample_distance

                # Handle being AT or PAST a stop restriction (speed=0)
                # At or past the path end, speed must be 0
                if dist_to_restriction <= 0 and restriction_speed <= 0:
                    atc_candidates.append((0.0, restriction_code, restriction_detail))
                    operational_limits.append(0.0)
                    continue

                # Apply braking curve when BEFORE the restriction point
                # Once past non-stop restrictions, the speed limit comes from block/station
                # position checks below
                if dist_to_restriction > 0:
                    # Sample point is before restriction
                    # Calculate braking curve back from restriction

                    if restriction_speed <= 0:
                        # Must stop: use stopping distance formula
                        effective_dist = dist_to_restriction - restriction_safety
                        if effective_dist <= 0:
                            atc_candidates.append(
                                (0.0, restriction_code, restriction_detail)
                            )
                            operational_limits.append(0.0)
                        else:
                            atc_limit = calculate_max_safe_speed(
                                effective_dist + restriction_safety,
                                decel_rate,
                                restriction_safety,
                            )
                            operational_limit = calculate_max_safe_speed(
                                effective_dist + restriction_safety,
                                operational_decel,
                                restriction_safety,
                            )
                            atc_candidates.append(
                                (atc_limit, restriction_code, restriction_detail)
                            )
                            operational_limits.append(operational_limit)
                    else:
                        # Must slow to target speed: use decel to target formula
                        atc_limit = calculate_speed_for_target(
                            dist_to_restriction, restriction_speed, decel_rate
                        )
                        operational_limit = calculate_speed_for_target(
                            dist_to_restriction, restriction_speed, operational_decel
                        )
                        atc_candidates.append(
                            (atc_limit, restriction_code, restriction_detail)
                        )
                        operational_limits.append(operational_limit)

            # Also apply block speed limit at current position
            if lookahead_blocks:
                for block in lookahead_blocks:
                    block_start = block.get("start_cm", 0)
                    block_end = block.get("end_cm", 0)
                    if block_start <= sample_distance < block_end:
                        block_speed = block.get("speed_limit_kmh")  # None if no limit
                        if block_speed is not None:
                            block_state = block.get("state")
                            block_id = block.get("id")
                            if block_state == BlockState.CAUTION.value:
                                inside_code = AtcLimitReasonCode.CAUTION
                            else:
                                inside_code = AtcLimitReasonCode.OBSTRUCTION
                            inside_detail = (
                                f"Block speed limit at {block_id}"
                                if block_id
                                else None
                            )
                            atc_candidates.append(
                                (block_speed, inside_code, inside_detail)
                            )
                            operational_limits.append(block_speed)
                        break

            # NOTE: Station speed limits are NOT applied here.
            # ATC/ATO only cares about BLOCKS. Station's top_speed_kmh is for
            # visualization only. Block speed limits (if any) are enforced above.

            # Take minimum of all ATC candidates and remember its reason
            atc_limit, dominant_code, dominant_detail = min(
                atc_candidates, key=lambda c: c[0]
            )
            operational_limit = min(operational_limits)

            # Cap at train max speed
            atc_limit = min(atc_limit, max_speed)
            operational_limit = min(operational_limit, max_speed)

            # Ensure ATO limit stays ATO_SPEED_MARGIN_KMH below ATC
            operational_limit = min(
                operational_limit, max(0.0, atc_limit - ATO_SPEED_MARGIN_KMH)
            )

            point: Dict[str, Any] = {
                "distance_cm": sample_distance,
                "atc_limit": atc_limit,
                "operational_limit": operational_limit,
            }
            if dominant_code is not None:
                point["atc_limit_reason_code"] = dominant_code.value
                if dominant_detail:
                    point["atc_limit_reason_detail"] = dominant_detail
            curves.append(point)

            sample_distance += step

        # Add explicit curve points for cleaner visualization. Both anchor
        # points carry the path-end reason code so consumers can key on it
        # (`AUTHORITY_STOP` with an active path, `NO_AUTHORITY` without).
        anchor_code = path_end_reason_code or AtcLimitReasonCode.AUTHORITY_STOP

        # 1. Target stop point (where train actually stops)
        if target_stop_cm is not None and target_stop_cm > 0:
            has_target_point = any(
                abs(point["distance_cm"] - target_stop_cm) < step / 2
                for point in curves
            )
            if not has_target_point:
                curves.append(
                    {
                        "distance_cm": target_stop_cm,
                        "atc_limit": 0.0,
                        "operational_limit": 0.0,
                        "atc_limit_reason_code": anchor_code.value,
                    }
                )

        # 2. Clearance end point (end of available distance) — always emit
        # this to show the clearance boundary.
        has_clearance_point = any(
            abs(point["distance_cm"] - max_distance_cm) < step / 2 for point in curves
        )
        if not has_clearance_point:
            curves.append(
                {
                    "distance_cm": max_distance_cm,
                    "atc_limit": 0.0,
                    "operational_limit": 0.0,
                    "atc_limit_reason_code": anchor_code.value,
                }
            )

        # Sort by distance to ensure proper curve drawing
        curves.sort(key=lambda p: p["distance_cm"])

        return curves

    # -------------------------------------------------------------------------
    # Notification Methods
    # -------------------------------------------------------------------------

    def get_and_clear_notifications(self) -> List[str]:
        """
        Get pending notifications and clear them.

        Called by frontend to retrieve toast messages.

        Returns:
            List of notification messages
        """
        notifications = self.pending_notifications.copy()
        self.pending_notifications.clear()
        return notifications

    # -------------------------------------------------------------------------
    # Operator Command Inbox (drain helpers)
    # -------------------------------------------------------------------------

    def _estimate_event_time(self) -> float:
        """Best-effort current event time for drain bookkeeping."""
        sc = getattr(self, "simulation_controller", None)
        getter = getattr(sc, "current_event_time", None) if sc is not None else None
        if callable(getter):
            try:
                return float(getter())
            except Exception:
                pass
        try:
            import time as _time
            return _time.time()
        except Exception:
            return 0.0

    def _enqueue_command(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> Command:
        """Push a Command onto command_inbox and return it."""
        import uuid as _uuid
        cmd = Command(
            kind=kind,
            payload=dict(payload or {}),
            command_id=_uuid.uuid4().hex,
            accepted_at=self._estimate_event_time(),
        )
        self.command_inbox.append(cmd)
        return cmd

    def enqueue_command(self, kind: str, payload: Optional[Dict[str, Any]] = None) -> Command:
        """Public alias for `_enqueue_command` (used by bootstrap and external callers)."""
        return self._enqueue_command(kind, payload)

    def _drain_command_inbox(self, event_time: float) -> None:
        """Drain the command inbox in canonical kind order, FIFO within each.

        Per design_new.md "Phase 2 — Execute" / "Driver/operator inputs",
        commands are drained in the order: power_on -> set_cab -> set_mode
        -> set_manual_mode_type -> set_handle / set_manual_power_percent ->
        activate_emergency_cmd -> release_emergency_cmd -> power_off.
        `ato_go` / `ato_stop` are internal helpers and are not part of the
        operator-command drain.

        Each drained command re-checks acceptance gates by delegating to the
        existing inline command method. Rejected commands are silently dropped
        (their inline method's CommandResult.success=False is observed via
        notifications, not by the caller).
        """
        if not self.command_inbox:
            return

        # Bucket commands by kind preserving FIFO within each bucket.
        buckets: Dict[str, List[Command]] = {}
        for cmd in self.command_inbox:
            buckets.setdefault(cmd.kind, []).append(cmd)
        self.command_inbox.clear()

        for kind in self._command_drain_order:
            cmds = buckets.pop(kind, [])
            for cmd in cmds:
                self._execute_drained_command(cmd, event_time)
        # Any unknown kinds left over: drop with a notification.
        for kind, cmds in buckets.items():
            for cmd in cmds:
                self.pending_notifications.append(
                    f"Unknown command kind: {kind}"
                )

    def _execute_drained_command(self, cmd: Command, event_time: float) -> None:
        """Dispatch one drained command to its inline executor.

        Re-checks acceptance gates inside the inline method. Notifies the
        operator on failure via pending_notifications. When wired, also emits
        a `command_result` notification through the simulation controller's
        notification center.
        """
        kind = cmd.kind
        payload = cmd.payload or {}
        result: Optional[CommandResult] = None
        try:
            if kind == "power_on":
                result = self._execute_power_on(payload, event_time)
            elif kind == "power_off":
                result = self._execute_power_off(payload, event_time)
            elif kind == "activate_emergency_cmd":
                result = self._execute_activate_emergency_cmd(payload, event_time)
            elif kind == "release_emergency_cmd":
                result = self._execute_release_emergency_cmd(payload, event_time)
            elif kind == "set_cab":
                result = self._execute_set_cab(payload, event_time)
            elif kind == "set_mode":
                result = self._execute_set_mode(payload, event_time)
            elif kind == "set_manual_mode_type":
                result = self._execute_set_manual_mode_type(payload, event_time)
            elif kind == "set_handle":
                result = self._execute_set_handle(payload, event_time)
            elif kind == "set_manual_power_percent":
                result = self._execute_set_manual_power_percent(payload, event_time)
            # ato_go / ato_stop are not operator commands; they are internal
            # helpers invoked directly by depart() and by the reactive
            # transition in on_physics_updated. They do not appear in the
            # canonical drain order. Any leftover bucket of them gets caught
            # by the "unknown kind" notification in _drain_command_inbox.
        except Exception as exc:
            self.pending_notifications.append(
                f"Command {kind} raised: {exc}"
            )
            return

        if result is None:
            return

        # Emit a command_result notification through the simulation
        # controller's notification center.
        sc = getattr(self, "simulation_controller", None)
        nc = getattr(sc, "notification_center", None) if sc is not None else None
        if nc is not None:
            try:
                from notification_center import Severity as _Severity
                severity = _Severity.INFO if result.success else _Severity.WARNING
                nc.emit(
                    source="TrainController",
                    kind="command_result",
                    severity=severity,
                    event_time=event_time,
                    train_id=self.train_id,
                    payload={
                        "command_id": cmd.command_id,
                        "command_name": kind,
                        "success": result.success,
                        "reason": result.message,
                    },
                )
            except Exception:
                logger.warning(
                    "NotificationCenter.emit() failed for command_result "
                    "(train=%s, kind=%s); the command itself completed",
                    self.train_id,
                    kind,
                    exc_info=True,
                )

        if not result.success:
            self.pending_notifications.append(result.message)

    # Inline executors — these contain the actual command bodies. The public
    # methods below (`power_on_cmd` etc.) call them after enqueuing so the
    # behaviour is identical when no drain is needed (e.g. tests that call
    # the command directly without ever ticking).

    def _execute_power_on(self, payload, event_time) -> CommandResult:
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")
        if self.state != TrainState.OFF:
            return CommandResult(False, "Power already on")
        self.state = TrainState.READY
        self.handle_position = HandlePosition.N
        return CommandResult(True, "Power on")

    def _execute_power_off(self, payload, event_time) -> CommandResult:
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")
        if not self.is_stopped():
            return CommandResult(False, "Cannot power off while moving")
        self._apply_power_off_request()
        return CommandResult(True, "Power off accepted")

    def _execute_activate_emergency_cmd(self, payload, event_time) -> CommandResult:
        if self.eb_active:
            return CommandResult(False, "Emergency brake already active")
        train = self._get_train()
        self.eb_active = True
        self.handle_position = HandlePosition.EB
        self.ato_running = False
        if (
            self.state != TrainState.RUNNING_AT_STATION
            and self.received_path_plan is not None
        ):
            self.phase_latches.set_path_cancel_request("eb", "operator EB")
        return CommandResult(True, "Emergency stop activated")

    def _execute_release_emergency_cmd(self, payload, event_time) -> CommandResult:
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")
        if self.state == TrainState.OFF:
            return CommandResult(False, "Power is off")
        if not self.eb_active:
            return CommandResult(False, "Emergency brake not active")
        if not self.is_stopped():
            return CommandResult(False, "Cannot release emergency while moving")
        if self.phase_latches.path_cancel_request is not None:
            return CommandResult(False, "EB cancellation has not committed yet")
        if self.received_path_plan is not None:
            return CommandResult(False, "Pre-EB path authority has not cleared yet")
        self.eb_active = False
        self.handle_position = HandlePosition.N
        if self.state != TrainState.RUNNING_AT_STATION:
            self.state = TrainState.READY
        return CommandResult(True, "Emergency stop released")

    def _execute_set_cab(self, payload, event_time) -> CommandResult:
        return self.set_cab(payload.get("cab"))

    def _execute_set_mode(self, payload, event_time) -> CommandResult:
        return self.set_mode(payload.get("mode"))

    def _execute_set_manual_mode_type(self, payload, event_time) -> CommandResult:
        return self.set_manual_mode_type(payload.get("mode_type"))

    def _execute_set_handle(self, payload, event_time) -> CommandResult:
        return self.set_handle(payload.get("position"))

    def _execute_set_manual_power_percent(self, payload, event_time) -> CommandResult:
        return self.set_manual_power_percent(payload.get("percent"))

    # -------------------------------------------------------------------------
    # Power Commands
    # -------------------------------------------------------------------------

    def power_on_cmd(self) -> CommandResult:
        """
        Turn on train power.

        Per design_new.md: valid only when state == OFF. On accept, transitions
        to READY regardless of cab selection (cab is orthogonal in the v1 state
        machine).
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if self.state != TrainState.OFF:
            return CommandResult(False, "Power already on")

        self.state = TrainState.READY
        self.handle_position = HandlePosition.N

        return CommandResult(True, "Power on")

    def power_off_cmd(self) -> CommandResult:
        """
        Turn off train power.

        Per design_new.md: stopped-only at acceptance, valid even when
        eb_active == True. On acceptance, immediately disengages any active
        ATO run, sets pending_power_off = True, and (when an active path
        exists) also writes pending_path_cancel_request with initiator =
        "power_off" (inclusive-latch rule). The actual dispatcher cancellation,
        route teardown, and OFF transition happen in the later eligible
        Phase 3 cleanup.
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not self.is_stopped():
            return CommandResult(False, "Cannot power off while moving")

        self._apply_power_off_request()

        return CommandResult(True, "Power off accepted; shutdown deferred to Phase 3")

    def activate_emergency_cmd(self) -> CommandResult:
        """
        Operator-issued emergency brake.

        Per design_new.md: only acceptance gate is `eb_active == False`
        (idempotent rejection of a duplicate activation). Valid in any motion
        state, any operational state including OFF, with or without a cab,
        with or without an active path. On acceptance: set eb_active = True,
        force handle to EB, clear ato_running, and (when an active path
        exists) write pending_path_cancel_request(EB). Exception while
        state == RUNNING_AT_STATION: the controller is dwelling and has no
        active path, so no pending_path_cancel_request is written; state and
        dwell timer are preserved.
        """
        if self.eb_active:
            return CommandResult(False, "Emergency brake already active")

        train = self._get_train()

        self.eb_active = True
        self.handle_position = HandlePosition.EB
        self.ato_running = False

        if (
            self.state != TrainState.RUNNING_AT_STATION
            and self.received_path_plan is not None
        ):
            self.phase_latches.set_path_cancel_request("eb", "operator EB")

        return CommandResult(True, "Emergency stop activated")

    def release_emergency_cmd(self) -> CommandResult:
        """
        Release emergency stop.

        Per design_new.md line 1385: accepts only when state != OFF,
        eb_active == True, is_stopped() == True, the controller's own
        pending_path_cancel_request is None (the EB cancel latch has been
        consumed by Phase 3), and the controller's own received_path_plan is
        None (the pre-EB authority has been cleared by PathDispatcher).
        Otherwise return success=False with controller state unchanged.
        On acceptance from RUNNING_AT_STATION (dwell EB cycle), state stays
        RUNNING_AT_STATION and only eb_active is cleared; dwell timing is
        unaffected. Otherwise lands in the READY state (powered, stopped, not
        dwelling — cab selection is preserved as an orthogonal field).
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if self.state == TrainState.OFF:
            return CommandResult(False, "Power is off")
        if not self.eb_active:
            return CommandResult(False, "Emergency brake not active")
        if not self.is_stopped():
            return CommandResult(False, "Cannot release emergency while moving")
        if self.phase_latches.path_cancel_request is not None:
            return CommandResult(
                False, "EB cancellation has not committed yet"
            )
        if self.received_path_plan is not None:
            return CommandResult(
                False, "Pre-EB path authority has not cleared yet"
            )

        self.eb_active = False
        self.handle_position = HandlePosition.N

        # Land in READY (powered, stopped, not dwelling), except during dwell
        # (RUNNING_AT_STATION) where the dwell is preserved.
        if self.state != TrainState.RUNNING_AT_STATION:
            self.state = TrainState.READY
        return CommandResult(True, "Emergency stop released")

    # -------------------------------------------------------------------------
    # Power-off request side effects
    # -------------------------------------------------------------------------
    #
    # The four Phase-3 latches themselves live on `self.phase_latches`
    # (see `runtime_payloads.PhaseLatches`). Setting `power_off` also has the
    # cross-field side effect of clearing `ato_running`; that side effect is
    # owned by the controller and bundled into `_apply_power_off_request(...)`
    # so every power-off code path stays consistent.

    def _apply_power_off_request(self, reason: str = "power off") -> None:
        """Set the `power_off` latch and disengage any active ATO run.

        Wraps `phase_latches.set_power_off(...)` together with the
        controller-local side effect of clearing `ato_running`. The inclusive
        `path_cancel_request` fan-out is applied automatically when a live
        path exists, per design_new.md "Latch Table".
        """
        self.ato_running = False
        self.phase_latches.set_power_off(
            reason=reason,
            has_active_path=self.received_path_plan is not None,
        )

    # -------------------------------------------------------------------------
    # Handle Commands
    # -------------------------------------------------------------------------

    def set_handle(self, position: HandlePosition) -> CommandResult:
        """
        Set throttle/brake handle position.

        Rules:
        - Must be powered on
        - In manual mode: directly controls acceleration/braking
        - In ATO mode: handle is controlled by computer (read-only to user)
        - EB always allowed (safety override)
        """
        if not self.power_on:
            return CommandResult(False, "Power is off")

        if position == HandlePosition.EB:
            return self.activate_emergency_cmd()

        if self.eb_active:
            brake_positions = [
                HandlePosition.N,
                HandlePosition.B1,
                HandlePosition.B2,
                HandlePosition.B3,
                HandlePosition.B4,
                HandlePosition.B5,
                HandlePosition.B6,
                HandlePosition.B7,
                HandlePosition.B8,
            ]
            if position in brake_positions:
                return self.release_emergency_cmd()
            return CommandResult(False, "Release emergency stop first")

        if self.control_mode == "ato" and self.ato_running:
            return CommandResult(False, "Handle controlled by ATO while running")

        self.handle_position = position
        return CommandResult(True, f"Handle set to {position.value}")

    # -------------------------------------------------------------------------
    # Cab & Mode Commands
    # -------------------------------------------------------------------------

    def set_cab(self, cab: Optional[str]) -> CommandResult:
        """
        Set active cab. Flips facing_direction when switching cabs.

        Rules:
        - Train must be stopped (speed = 0)
        - Setting cab to 'A' or 'B' transitions to STANDBY
        - Setting cab to None transitions to IDLE
        - Switching from A to B (or vice versa) flips facing_direction
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not self.is_stopped():
            return CommandResult(False, "Cannot switch cab while moving")

        old_cab = self.cab

        # Per design_new.md: same-cab no-op success — does not change state,
        # does not disengage ato_running, no notification.
        if cab == old_cab:
            return CommandResult(True, f"Cab already {cab}")

        # Per design_new.md line 1630 + Allowed (state, cab) pairs table
        # (line 1589): `(RUNNING_AT_STATION, None)` is not a legal pair.
        # Reject `set_cab(None)` from RUNNING_AT_STATION; the operator must
        # finish dwell or pick a concrete cab letter instead.
        if cab is None and self.state == TrainState.RUNNING_AT_STATION:
            return CommandResult(False, "cannot deselect cab while dwelling")

        self.cab = cab

        # Per design_new.md: changing cab is part of the depart-acceptance
        # contract; it disengages any active ATO run. set_cab from
        # RUNNING_AT_STATION preserves the dwell state.
        self.ato_running = False

        prev_state = self.state
        if cab is None:
            if prev_state != TrainState.RUNNING_AT_STATION:
                self.state = TrainState.READY
            train.update_position_for_cab(None)
        else:
            if prev_state != TrainState.RUNNING_AT_STATION:
                self.state = TrainState.READY
            train.update_position_for_cab(cab)

            # When switching cabs (A <-> B), the physical train doesn't move.
            # The driver walks to the other end. What changes:
            # 1. leading_end flips (done by update_position_for_cab)
            # 2. facing_direction reverses (computed from leading_end for multi-tile trains)
            # 3. entry_edge for BOTH ends flips (since train will travel opposite direction)
            # 4. distance_cm for BOTH ends flips (10cm from W = tile_length - 10cm from E)
            if old_cab in ("A", "B") and old_cab != cab:
                # Sync facing_direction from the computed property.
                # For multi-tile trains, the computed property derives direction from
                # leading_end + tile positions, so changing leading_end automatically
                # gives the correct (reversed) direction.
                # For single-tile trains (end_a_tile == end_b_tile), the computed
                # property falls back to the stored `facing_direction`, so we
                # must explicitly flip it here.
                if train.position:
                    is_multi_tile = (
                        train.position.end_a_tile is not None
                        and train.position.end_b_tile is not None
                        and train.position.end_a_tile != train.position.end_b_tile
                    )
                    # For multi-tile trains, facing_direction is automatically recalculated
                    # from the already-updated leading_end. No manual sync needed.
                    # For single-tile trains, the direction is calculated from entry_edge
                    # and distance_cm, which are being flipped below.
                    # Therefore, no direction sync is needed at all - it's automatic.

                # Flip entry edges and distances for BOTH ends
                if train.position and self.network:
                    # Flip end_a entry edge
                    if train.position.end_a_tile and train.position.end_a_entry_edge:
                        track_a = self.network.get_track(*train.position.end_a_tile)
                        if track_a:
                            exits_a = track_a.get_exits(
                                train.position.end_a_entry_edge, use_switch_state=True
                            )
                            if exits_a:
                                train.position.end_a_entry_edge = exits_a[0]
                            # Flip distance: new_distance = tile_length - old_distance
                            tile_length = self.network._get_tile_length_cm(
                                train.position.end_a_tile[0],
                                train.position.end_a_tile[1],
                            )
                            train.position.end_a_distance_cm = (
                                tile_length - train.position.end_a_distance_cm
                            )

                    # Flip end_b entry edge
                    if train.position.end_b_tile and train.position.end_b_entry_edge:
                        track_b = self.network.get_track(*train.position.end_b_tile)
                        if track_b:
                            exits_b = track_b.get_exits(
                                train.position.end_b_entry_edge, use_switch_state=True
                            )
                            if exits_b:
                                train.position.end_b_entry_edge = exits_b[0]
                            # Flip distance
                            tile_length = self.network._get_tile_length_cm(
                                train.position.end_b_tile[0],
                                train.position.end_b_tile[1],
                            )
                            train.position.end_b_distance_cm = (
                                tile_length - train.position.end_b_distance_cm
                            )

                    # Per design_new.md ~line 1292 the head index is derived
                    # from `train.position` against the dispatcher's path
                    # tiles each read; nothing on Train is mirrored here.
            # No need to sync direction - it's calculated automatically from position

        return CommandResult(True, f"Cab set to {cab}")

    def set_mode(self, mode: str) -> CommandResult:
        """
        Set control mode (ATO or Manual).

        Per design_new.md, manual is a control mode, not a separate
        operational state. Stopped manual trains sit in STANDBY/IDLE just
        like ATO trains. From RUNNING_AT_STATION, the dwell state is
        preserved; only the side effects (ato_running cleared) apply.

        Rules:
        - Train must be stopped
        - 'ato' / 'manual' both leave the state at STANDBY (cab) or IDLE
          (no cab), unless coming from RUNNING_AT_STATION which is preserved.
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not self.is_stopped():
            return CommandResult(False, "Cannot switch mode while moving")

        if mode not in ("ato", "manual"):
            return CommandResult(False, f"Invalid mode: {mode}")

        self.control_mode = mode
        # Per design_new.md, set_mode disengages any active ATO run.
        self.ato_running = False

        # Preserve dwell state if currently dwelling; otherwise land in READY.
        if self.state != TrainState.RUNNING_AT_STATION:
            self.state = TrainState.READY
        return CommandResult(True, f"Mode set to {mode}")

    def set_manual_mode_type(self, mode_type: str) -> CommandResult:
        """
        Set manual-mode submode ('simulation' or 'power_percent').

        Per design_new.md, valid only while the train is stopped, powered,
        and control_mode == manual. Side effects: entering 'power_percent'
        resets handle_position to neutral; entering 'simulation' resets
        manual_power_percent to 0. Preserves cab selection and
        received_path_plan; does not start movement.
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")
        if not self.power_on:
            return CommandResult(False, "Power is off")
        if self.control_mode != "manual":
            return CommandResult(False, "Not in manual mode")
        if not self.is_stopped():
            return CommandResult(False, "Cannot change manual mode type while moving")
        if mode_type not in ("simulation", "power_percent"):
            return CommandResult(False, f"Invalid manual mode type: {mode_type}")

        self.manual_mode_type = mode_type
        if mode_type == "power_percent":
            self.handle_position = HandlePosition.N
        else:
            self.manual_power_percent = 0

        return CommandResult(True, f"Manual mode type set to {mode_type}")

    def set_manual_power_percent(self, percent: int) -> CommandResult:
        """
        Set manual power-percent command (0..100).

        Per design_new.md: accepted only while manual mode is active,
        manual_mode_type == power_percent, the train is powered, a cab is
        selected, and EB is not latched. The command does not start movement
        by itself; it just writes the controller-owned field.
        """
        if not self.power_on:
            return CommandResult(False, "Power is off")
        if self.control_mode != "manual":
            return CommandResult(False, "Not in manual mode")
        if self.manual_mode_type != "power_percent":
            return CommandResult(False, "Manual mode type is not power_percent")
        if self.cab not in ("A", "B"):
            return CommandResult(False, "No cab selected")
        if self.eb_active:
            return CommandResult(False, "Emergency brake latched")

        try:
            value = int(percent)
        except (TypeError, ValueError):
            return CommandResult(False, f"Invalid power percent: {percent!r}")

        clamped = max(0, min(100, value))
        self.manual_power_percent = clamped
        return CommandResult(True, f"Manual power percent set to {clamped}")

    # -------------------------------------------------------------------------
    # ATO Commands
    # -------------------------------------------------------------------------

    def ato_go(self) -> CommandResult:
        """
        Canonical helper that arms an ATO run by setting `ato_running = True`.

        Per design_new.md "ato_running meaning and lifecycle", both transitions
        into RUNNING_MOVING (the depart-acceptance path and the reactive
        speed-based evaluation path) converge on this writer so callers do not
        duplicate the precondition check.

        Accepted entry states:
        - READY: full depart-acceptance path with cab auto-selection and
          state transition to RUNNING_MOVING.
        - RUNNING_MOVING: idempotent success (the reactive transition has
          already moved state forward); skip cab auto-selection because the
          train is already moving with an ATC-accepted cab.
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if self.control_mode != "ato":
            return CommandResult(False, "Not in ATO mode")

        if not self.power_on:
            return CommandResult(False, "Power is off")

        if self.state not in (TrainState.READY, TrainState.RUNNING_MOVING):
            return CommandResult(
                False, f"Train not in READY (is {self.state.value})"
            )

        # Reactive-transition entry: train is already moving under live
        # authority with an ATC-accepted cab. Just write ato_running;
        # path_backed_run_active is derived.
        if self.state == TrainState.RUNNING_MOVING:
            self.ato_running = True
            return CommandResult(True, "ATO armed (already moving)")

        # READY entry: full depart-acceptance path.
        # Recalculate ATO info fresh to ensure cab direction matches
        # the current path (path may have been assigned since last tick)
        self.ato_info = self._calculate_ato_info()

        # ATO Auto-selection/correction: determine the required cab from path
        # direction and select or switch if needed. This handles both:
        #   1. cab=None: auto-select cab from path
        #   2. cab=A/B but wrong: auto-correct cab to match path direction
        if self.ato_info and self.ato_info.required_cab:
            required_cab = self.ato_info.required_cab
            if self.cab != required_cab:
                old_cab = self.cab
                logger.info(
                    "[TrainController %s] ATO auto-%s cab: %s -> %s",
                    self.train_id,
                    "selecting" if old_cab is None else "correcting",
                    old_cab,
                    required_cab,
                )
                # Ensure train is physically stopped before switching cab.
                if self.current_speed_kmh > 0:
                    self.current_speed_kmh = 0.0
                result = self.set_cab(required_cab)
                if not result.success:
                    return CommandResult(
                        False,
                        f"ATO cannot start: cab auto-{'selection' if old_cab is None else 'correction'} failed - {result.message}",
                    )
                self.pending_notifications.append(
                    f"ATO auto-{'selected' if old_cab is None else 'switched'} cab to {required_cab} for path direction"
                )
        elif self.cab is None:
            # No ATO info available and no cab - cannot determine direction
            return CommandResult(
                False,
                "No cab selected and no path available to auto-select direction",
            )

        # After auto-selection, verify we're still in READY (set_cab keeps
        # state == READY for non-dwelling controllers).
        if self.state != TrainState.READY:
            return CommandResult(False, f"Train not in READY (is {self.state.value})")
        if self.cab not in ("A", "B"):
            return CommandResult(False, "ATO requires a cab to be selected")

        self.state = TrainState.RUNNING_MOVING
        self.ato_running = True
        # path_backed_run_active is derived from path/route/destination context.
        return CommandResult(True, "ATO started")

    def ato_stop(self) -> CommandResult:
        """
        Stop ATO movement (service stop, not emergency).

        Sets ato_running=False. The tick() will handle braking and
        transitioning to READY when stopped.
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if not self.ato_running:
            return CommandResult(False, "ATO not running")

        # Just set ato_running=False - tick() handles the rest
        self.ato_running = False

        return CommandResult(True, "ATO stopping")

    # -------------------------------------------------------------------------
    # State Transitions
    # -------------------------------------------------------------------------

    def transition_to_moving(self) -> None:
        """Transition to RUNNING_MOVING state."""
        self.state = TrainState.RUNNING_MOVING

    def transition_to_at_station(self) -> None:
        """Transition to RUNNING_AT_STATION state."""
        self.state = TrainState.RUNNING_AT_STATION

    def transition_to_ready(self) -> None:
        """Transition to the READY state (powered, stopped, not dwelling).

        Per design_new.md, READY is the unified powered-stopped state
        regardless of cab selection. Cab selection and route assignment are
        orthogonal fields and are not cleared here; their lifecycle is owned
        by `set_cab(...)` and `RouteExecutor` respectively.
        """
        self.state = TrainState.READY
        self.ato_running = False
        # path_backed_run_active is derived from path/route/destination context.

    # -------------------------------------------------------------------------
    # Route Executor Commands (for RouteExecutor integration)
    # -------------------------------------------------------------------------

    def depart(self) -> bool:
        """
        Depart-acceptance entry point called by `RouteExecutor` when live
        authority exists.

        Per design_new.md "TrainController.depart() canonical contract", the
        accepted entry states are `{READY, RUNNING_MOVING}`: the second case
        admits the reactive speed-based evaluation having already advanced
        state ahead of this call. From RUNNING_MOVING the call is an
        idempotent success that does not change state but still applies side
        effects (in ATO, invokes `ato_go()` so `ato_running = True` is
        written if the reactive transition has not already done so).

        Returns:
            True if successful, False if preconditions not met
        """
        train = self._get_train()
        if train is None:
            return False

        if self.state not in (TrainState.READY, TrainState.RUNNING_MOVING):
            return False

        # ATO mode: delegate to ato_go() which is the canonical writer of
        # ato_running and handles both READY (full path) and RUNNING_MOVING
        # (idempotent) entries.
        if self.control_mode == "ato":
            result = self.ato_go()
            return result.success

        # Manual mode: requires a cab and facing direction. From
        # RUNNING_MOVING the call is an idempotent success.
        # path_backed_run_active is now derived from path/route/destination
        # context; no field write needed here.
        if self.cab not in ("A", "B"):
            return False
        if train.facing_direction is None:
            return False
        if self.state == TrainState.READY:
            self.state = TrainState.RUNNING_MOVING
        return True

    def enter_station_dwell(self) -> None:
        """Dwell-entry transition called by Phase 1 dwell-entry step.

        Per design_new.md, RouteExecutor invokes this directly when its
        preconditions are met. Transitions controller state to
        RUNNING_AT_STATION; clears ato_running because dwell entry is a
        run-ending event.
        """
        self.state = TrainState.RUNNING_AT_STATION
        self.ato_running = False

    def finish_dwell(self) -> None:
        """Dwell-end transition called by Phase 1 dwell-complete step.

        Per design_new.md: lands in READY (powered, stopped, not dwelling —
        cab selection is preserved as an orthogonal field). Does not clear
        received_path_plan and does not start movement.
        """
        self.state = TrainState.READY

    def on_path_complete(self) -> None:
        """Called by PathDispatcher.try_complete_active_path on accepted completion.

        Per design_new.md: transition to READY. Does not clear
        received_path_plan (the dispatcher has already transitioned
        Path.status), does not enter dwell itself.
        """
        self.ato_running = False
        # path_backed_run_active is derived from path/route/destination context.
        self.state = TrainState.READY

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
            "eb_active": self.eb_active,
            "pending_notifications": self.pending_notifications.copy(),
            "cab": self.cab,
            "control_mode": self.control_mode,
            "manual_mode_type": self.manual_mode_type,
            "manual_power_percent": self.manual_power_percent,
            "travel_direction": self.travel_direction,
            "state": self.state.value,
            "target_speed": self.target_speed,
            "handle_position": self.handle_position.value,
            "ato_running": self.ato_running,
            "ato_destination_block_id": self.ato_destination_block_id,
            "ato_path_id": self.ato_path_id,
            "assigned_path_id": self.assigned_path_id,
            "path_backed_run_active": self.path_backed_run_active,
            "clearance_ahead_cm": safe_float(self.clearance_ahead_cm),
            "atc_speed_limit": safe_float(self.atc_speed_limit),
            "current_block_speed_limit": safe_float(self.current_block_speed_limit),
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
                "path_id": self.ato_info.path_id,
                "destination_block_id": self.ato_info.destination_block_id,
                "destination_station_name": self.ato_info.destination_station_name,
                "path_heading": self.ato_info.path_heading,
                "required_cab": self.ato_info.required_cab,
            }
            if self.ato_info
            else None,
            # Live speed and stopped predicate are controller-owned per
            # design_new.md "Units contract".
            "current_speed": float(getattr(self, "current_speed_kmh", 0.0) or 0.0),
            "is_stopped": self.is_stopped(),
            # Diagnostic: reason for current handle position
            "handle_reason": self.handle_reason,
            # ATO ETA: integrates the operational_limit speed curve to the
            # current target stop. None when no path/target/curves.
            "time_to_target_sec": self._calculate_time_to_target_sec(),
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
        }

    def _span_to_dict(self) -> Optional[Dict[str, Any]]:
        """Serialize `current_span` into the live-position payload, or None."""
        span = getattr(self, "current_span", None)
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
            "leading_end": span.leading_end.value if span.leading_end else None,
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

        cm_per_second_per_kmh = 100000.0 / 3600.0  # 27.78
        current_kmh = float(getattr(self, "current_speed_kmh", 0.0) or 0.0)

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

    # Default acceleration used by the ETA estimator when the curve does
    # not constrain the train. ~0.5 m/s² matches a typical metro service
    # rate; it is intentionally conservative so ETA is slightly long
    # rather than misleadingly short.
    _ETA_ACCEL_CM_PER_S2 = 50.0

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
        cm_per_second_per_kmh = 100000.0 / 3600.0
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
        return self.current_span

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

    def _compute_canonical_lookahead(self) -> Dict[str, Any]:
        train = self._get_train()
        if not train or not self.network:
            # No reason to publish — the lookahead is empty because the train
            # / network haven't been wired yet, not because of any ATC state.
            return self._build_empty_monitor_lookahead(speed_limit_reason=None)

        if self.cab not in ("A", "B"):
            return self._build_empty_monitor_lookahead(
                speed_limit_reason="No cab selected",
                reason_code=AtcLimitReasonCode.NO_CAB,
            )

        head_tile = self._get_head_tile()
        head_distance_cm = self._get_head_distance_cm()
        travel_direction = (
            train.position.facing_direction.name
            if train.position and train.position.facing_direction
            else None
        )

        if not head_tile:
            # No reason published — head_tile being None is a missing-position
            # case, not an ATC condition.
            return self._build_empty_monitor_lookahead(
                head_distance_cm=head_distance_cm,
                travel_direction=travel_direction,
                speed_limit_reason=None,
            )

        plan = self.received_path_plan
        resource_sequence = (
            getattr(plan, "resource_sequence", None) if plan else None
        )
        if not plan or not resource_sequence:
            # Per design_new.md step `22a9` (canonical lookahead), without a
            # path plan the canonical payload is the no-authority empty form:
            # a single distance-0 anchor with atc_limit/operational_limit=0
            # and reason `no_authority`. There is no block-bounded fallback;
            # movement authority is binary on `received_path_plan`.
            # EB is a higher-priority safety state — when EB is latched the
            # reason is stamped as `eb` even though authority is also absent
            # (the cancellation-during-EB sequence drops authority too, and
            # operators should see the safety state, not the latent
            # "no_authority" condition).
            if self.eb_active:
                reason_code = AtcLimitReasonCode.EB
                reason_detail = "Emergency brake active"
            else:
                reason_code = AtcLimitReasonCode.NO_AUTHORITY
                reason_detail = "no_authority"
            return self._build_empty_monitor_lookahead(
                head_tile=head_tile,
                head_distance_cm=head_distance_cm,
                travel_direction=travel_direction,
                speed_limit_reason=reason_detail,
                reason_code=reason_code,
            )

        # Locate the head tile inside the resource_sequence (tail→authority
        # order per the canonical resource_sequence schema). The head must
        # live in some resource's path_tiles slice.
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

        head_track = self.network.get_track(head_tile[0], head_tile[1])
        head_block_id: Optional[str] = (
            head_track.block_section_id if head_track else None
        )

        # Recovery for the freshly-departed case: a Path's resource_sequence
        # describes only the authority *forward* of the head, so when the
        # head sits behind the first path tile (e.g. just after `depart()`)
        # its tile is not in any `path_tiles` slice. When the head's block
        # matches the first authority block we anchor the loop at path[0]'s
        # first tile, then below add the in-block portion behind that first
        # path tile to `block_back_length_cm` so the head's block still
        # reports its true length and the chart frame stays aligned to the
        # head's actual position. When the head's block doesn't match
        # path[0]'s block either, the path is structurally disconnected from
        # the head and the function publishes the no-authority empty payload
        # (per step `22a9`) so the train doesn't drive under fabricated
        # block-bounded authority.
        recovery_extra_back_cm = 0.0
        recovery_extra_back_tiles = 0
        if head_resource_index < 0:
            if not (
                head_block_id is not None
                and resource_sequence[0].resource_type == "block"
                and resource_sequence[0].resource_id == head_block_id
                and resource_sequence[0].path_tiles
            ):
                # Path exists but the head's tile is not in its
                # resource_sequence and the head's block doesn't match the
                # first authority block either — the path is structurally
                # disconnected from the head. Per design_new.md step `22a9`,
                # we publish the no-authority empty payload rather than
                # silently fabricating block-bounded authority. If the train
                # is rolling, the no-authority overspeed branch in step
                # `826a` will escalate to EB.
                return self._build_empty_monitor_lookahead(
                    head_tile=head_tile,
                    head_distance_cm=head_distance_cm,
                    travel_direction=travel_direction,
                    speed_limit_reason="no_authority",
                    reason_code=AtcLimitReasonCode.NO_AUTHORITY,
                )
            head_resource_index = 0
            head_tile_index = 0
            # Compute the head's position within its block and the position
            # of path[0]'s first tile within the same block. The gap between
            # them is the distance the head still has to travel before
            # reaching the path; the in-block prefix before path[0] becomes
            # extra `block_back` content the loop wouldn't otherwise see.
            first_pt = resource_sequence[0].path_tiles[0]
            first_pt_tile = (first_pt.x, first_pt.y)
            block_positions = (
                self.network.get_block_positions(head_block_id) or []
            )
            try:
                head_idx = block_positions.index(head_tile)
                first_pt_idx = block_positions.index(first_pt_tile)
            except ValueError:
                head_idx = -1
                first_pt_idx = -1
            if 0 <= head_idx < first_pt_idx:
                # Sum lengths of every block tile up to (but not including)
                # path[0]'s first tile — that is the back portion of the
                # head's block that the path doesn't enumerate.
                for prev_tile in block_positions[:first_pt_idx]:
                    recovery_extra_back_cm += self.network._get_tile_length_cm(
                        prev_tile[0], prev_tile[1]
                    )
                    recovery_extra_back_tiles += 1
                # Distance from head's actual position to start of path[0]'s
                # first tile (= "gap"). Equals back-in-block-up-to-path[0]
                # minus the head's already-traversed portion of the block.
                head_in_block_cm = sum(
                    self.network._get_tile_length_cm(t[0], t[1])
                    for t in block_positions[:head_idx]
                ) + head_distance_cm
                gap_to_first_path_tile_cm = max(
                    0.0, recovery_extra_back_cm - head_in_block_cm
                )
                # Set head_distance_cm so that the loop's
                # `cumulative_distance_cm = -head_distance_cm` initialization
                # places path[0]'s first tile at exactly `gap_to_first_path_tile_cm`.
                head_distance_cm = -gap_to_first_path_tile_cm
            else:
                # Couldn't resolve positions; fall back to placing path[0]
                # at the lookahead origin (chart shifted by ~one tile).
                head_distance_cm = 0.0

        blocks: List[Dict[str, Any]] = []
        signals: List[Dict[str, Any]] = []
        switches: List[Dict[str, Any]] = []
        stations: List[Dict[str, Any]] = []

        cumulative_distance_cm = -head_distance_cm

        current_block_id: Optional[str] = head_block_id

        # Sum the tail length of the head's block — tiles that share the
        # same block_id and lie before the head in tail→authority order. The
        # walk extends backward through the head resource, then through any
        # earlier resources that still belong to the same block.
        block_back_length_cm = 0.0
        block_back_tile_count = 0
        if current_block_id:
            stop = False
            for prev_tile in reversed(
                resource_sequence[head_resource_index].path_tiles[:head_tile_index]
            ):
                prev_track = self.network.get_track(prev_tile.x, prev_tile.y)
                prev_block_id = prev_track.block_section_id if prev_track else None
                if prev_block_id != current_block_id:
                    stop = True
                    break
                block_back_length_cm += self.network._get_tile_length_cm(
                    prev_tile.x, prev_tile.y
                )
                block_back_tile_count += 1
            if not stop:
                for r_idx in range(head_resource_index - 1, -1, -1):
                    earlier = resource_sequence[r_idx]
                    for prev_tile in reversed(earlier.path_tiles):
                        prev_track = self.network.get_track(prev_tile.x, prev_tile.y)
                        prev_block_id = (
                            prev_track.block_section_id if prev_track else None
                        )
                        if prev_block_id != current_block_id:
                            stop = True
                            break
                        block_back_length_cm += self.network._get_tile_length_cm(
                            prev_tile.x, prev_tile.y
                        )
                        block_back_tile_count += 1
                    if stop:
                        break

        # When recovery picked up a same-block prefix that the path doesn't
        # enumerate (head sits behind path[0] in the same block), fold that
        # prefix into the back-walk totals so the first block's reported
        # length, tile_count and start_cm cover the entire block.
        if recovery_extra_back_cm > 0.0:
            block_back_length_cm += recovery_extra_back_cm
            block_back_tile_count += recovery_extra_back_tiles

        current_block_start_cm = (
            cumulative_distance_cm - block_back_length_cm
            if current_block_id
            else cumulative_distance_cm
        )

        seen_blocks: set[str] = set()
        seen_signals: set[str] = set()
        seen_switches: set[Tuple[int, int]] = set()
        seen_stations: set[str] = set()

        is_first_step = True

        for r_idx in range(head_resource_index, len(resource_sequence)):
            resource = resource_sequence[r_idx]
            tile_start = head_tile_index if r_idx == head_resource_index else 0

            for t_idx in range(tile_start, len(resource.path_tiles)):
                tile_entry = resource.path_tiles[t_idx]
                x, y = tile_entry.x, tile_entry.y
                track = self.network.get_track(x, y)
                tile_length = (
                    self.network._get_tile_length_cm(x, y) if track else 50.0
                )
                tile_block_id = track.block_section_id if track else None
                tile_travel_direction = EDGE_TO_DIRECTION_NAME.get(
                    tile_entry.exit_edge
                )

                point_element_start = max(0.0, cumulative_distance_cm)

                if tile_block_id and tile_block_id != current_block_id:
                    current_block_id = tile_block_id
                    current_block_start_cm = cumulative_distance_cm

                if tile_block_id and tile_block_id not in seen_blocks:
                    seen_blocks.add(tile_block_id)
                    block_length = 0.0
                    tile_count = 0
                    done = False
                    for fr_idx in range(r_idx, len(resource_sequence)):
                        forward_resource = resource_sequence[fr_idx]
                        ft_start = t_idx if fr_idx == r_idx else 0
                        for ft_idx in range(
                            ft_start, len(forward_resource.path_tiles)
                        ):
                            ft_entry = forward_resource.path_tiles[ft_idx]
                            ft_track = self.network.get_track(
                                ft_entry.x, ft_entry.y
                            )
                            ft_block_id = (
                                ft_track.block_section_id if ft_track else None
                            )
                            if ft_block_id != tile_block_id:
                                done = True
                                break
                            block_length += self.network._get_tile_length_cm(
                                ft_entry.x, ft_entry.y
                            )
                            tile_count += 1
                        if done:
                            break

                    if (
                        tile_block_id == current_block_id
                        and is_first_step
                        and block_back_length_cm > 0.0
                    ):
                        block_length += block_back_length_cm
                        tile_count += block_back_tile_count

                    block_state = None
                    block_speed_limit: Optional[float] = None
                    block = self.network.blocks.get(tile_block_id)
                    if block:
                        if block.state:
                            block_state = block.state.value
                        if (
                            block.speed_limit_kmh
                            and block.speed_limit_kmh < float("inf")
                        ):
                            block_speed_limit = block.speed_limit_kmh

                    blocks.append(
                        {
                            "id": tile_block_id,
                            "name": tile_block_id,
                            "state": block_state or BlockState.FREE.value,
                            "start_cm": current_block_start_cm,
                            "end_cm": current_block_start_cm + block_length,
                            "length_cm": block_length,
                            "tile_count": tile_count,
                            "speed_limit_kmh": block_speed_limit,
                        }
                    )

                if track and track.signal_ids and tile_travel_direction:
                    train_dir = tile_travel_direction.upper()
                    for signal_id in track.signal_ids:
                        if signal_id in seen_signals:
                            continue
                        signal = self.network.signals.get(signal_id)
                        if not signal:
                            continue
                        sig_dir = (signal.detection_direction or "").upper()
                        dir_matches = (
                            sig_dir == train_dir
                            or sig_dir == train_dir[:1]
                            or sig_dir[:1] == train_dir[:1]
                        )
                        if not dir_matches:
                            continue
                        seen_signals.add(signal_id)
                        signal_distance = (
                            cumulative_distance_cm
                            if is_first_step
                            else point_element_start
                        )
                        signals.append(
                            {
                                "id": signal_id,
                                "name": signal.name or signal_id,
                                "state": signal.state.value
                                if signal.state
                                else "unknown",
                                "distance_cm": signal_distance,
                            }
                        )

                if track and track.is_switch:
                    switch_key = (x, y)
                    if switch_key not in seen_switches:
                        seen_switches.add(switch_key)
                        switch_entity = (
                            self.network.get_switch_at(x, y)
                            if hasattr(self.network, "get_switch_at")
                            else None
                        )
                        switches.append(
                            {
                                "x": x,
                                "y": y,
                                "name": switch_entity.name
                                if switch_entity and switch_entity.name
                                else f"Switch ({x},{y})",
                                "position": "main"
                                if track.switch_state == 0
                                else "diverging",
                                "distance_cm": point_element_start,
                                "locked": track.is_locked_by_path()
                                if hasattr(track, "is_locked_by_path")
                                else False,
                            }
                        )

                if track and track.station_id:
                    platform_id = track.station_id
                    if platform_id not in seen_stations:
                        seen_stations.add(platform_id)
                        station = self.network.platforms.get(platform_id)
                        station_length = station.length_cm if station else 0.0
                        station_tile_count = 0
                        if not station_length:
                            done = False
                            for fr_idx in range(r_idx, len(resource_sequence)):
                                forward_resource = resource_sequence[fr_idx]
                                ft_start = t_idx if fr_idx == r_idx else 0
                                for ft_idx in range(
                                    ft_start, len(forward_resource.path_tiles)
                                ):
                                    ft_entry = forward_resource.path_tiles[ft_idx]
                                    ft_track = self.network.get_track(
                                        ft_entry.x, ft_entry.y
                                    )
                                    if (
                                        not ft_track
                                        or ft_track.station_id != platform_id
                                    ):
                                        done = True
                                        break
                                    station_length += (
                                        self.network._get_tile_length_cm(
                                            ft_entry.x, ft_entry.y
                                        )
                                    )
                                    station_tile_count += 1
                                if done:
                                    break
                        station_group_id: Optional[str] = None
                        if hasattr(self.network, "get_station_for_platform"):
                            try:
                                station_group_id = (
                                    self.network.get_station_for_platform(
                                        platform_id
                                    )
                                )
                            except Exception:
                                station_group_id = None
                        stations.append(
                            {
                                "platform_id": platform_id,
                                "station_id": station_group_id,
                                "name": station.name if station else platform_id,
                                "distance_cm": point_element_start,
                                "length_cm": station_length,
                                "tile_count": station_tile_count or 1,
                                "top_speed_kmh": station.top_speed_kmh
                                if station
                                else None,
                            }
                        )

                cumulative_distance_cm += tile_length
                is_first_step = False

        clearance_cm = max(0.0, cumulative_distance_cm)

        # Calculate effective safety margin (5cm for platform, 25cm otherwise)
        effective_safety_margin = self._get_effective_safety_margin()
        # Target stop point is clearance minus safety margin.
        # `authority_boundary_delta_cm` is the signed forward distance from
        # the head to the safety-margin-adjusted authority boundary; with an
        # active path, that is the same axis as the speed_curves samples.
        target_stop_cm = max(0.0, clearance_cm - effective_safety_margin)
        authority_boundary_delta_cm = target_stop_cm

        # Determine override reason code for forced-zero ATC cases. EB and
        # wrong-direction-cab force ATC to zero regardless of geometry; in
        # those cases every speed_curves sample carries the canonical reason.
        # The EB trigger context ("ATC overspeed", "operator EB", etc.) is
        # published as a separate NotificationCenter event when EB activates;
        # the lookahead detail only carries the steady-state string.
        override_code: Optional[AtcLimitReasonCode] = None
        override_detail: Optional[str] = None
        if self.eb_active:
            override_code = AtcLimitReasonCode.EB
            override_detail = "Emergency brake active"
        elif self._is_cab_wrong_direction():
            override_code = AtcLimitReasonCode.WRONG_DIRECTION_CAB
            override_detail = "Cab points away from authority"

        speed_curves = self.calculate_speed_curves(
            max_distance_cm=clearance_cm if clearance_cm > 0 else 500.0,
            lookahead_blocks=blocks,
            lookahead_signals=signals,
            lookahead_stations=stations,
            target_stop_cm=target_stop_cm,
            override_reason_code=override_code,
            override_reason_detail=override_detail,
        )

        # Top-level `speed_limit_reason` is a convenience read of the
        # canonical position-0 detail; derive it locally to avoid recursing
        # back through the `speed_limit_reason` property.
        top_reason: Optional[str] = None
        if speed_curves:
            top_reason = (
                speed_curves[0].get("atc_limit_reason_detail")
                or (
                    str(speed_curves[0].get("atc_limit_reason_code"))
                    if speed_curves[0].get("atc_limit_reason_code")
                    else None
                )
            )

        return {
            "train_id": self.train_id,
            "head_tile": [head_tile[0], head_tile[1]],
            "head_distance_cm": head_distance_cm,
            "travel_direction": travel_direction,
            "cab": self.cab,
            "speed_limit_reason": top_reason,
            "lookahead": {
                "blocks": blocks,
                "signals": signals,
                "switches": switches,
                "stations": stations,
                "clearance_cm": clearance_cm,
                "safety_margin_cm": effective_safety_margin,
                "target_stop_cm": target_stop_cm,
                "authority_boundary_delta_cm": authority_boundary_delta_cm,
                "speed_curves": speed_curves,
            },
        }

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
        return cls(
            train_id=data["train_id"],
            network=network,
            eb_active=data.get("eb_active", False),
            cab=data.get("cab"),
            control_mode=data.get("control_mode", "ato"),
            manual_mode_type=data.get("manual_mode_type", "simulation"),
            manual_power_percent=int(data.get("manual_power_percent", 0) or 0),
            state=state,
            handle_position=handle,
            ato_running=data.get("ato_running", False),
            # path_backed_run_active is now a derived @property; no
            # constructor arg accepted. Any persisted value is ignored
            # because the property recomputes from path/route context.
        )


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
