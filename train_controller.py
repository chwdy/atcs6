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

from constants import STOPPED_SPEED_THRESHOLD_KMH
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


TRACK_END_SAFETY_MARGIN_CM = 25.0
PLATFORM_SAFETY_MARGIN_CM = 5.0
ATC_SIGNAL_LOOKAHEAD_BLOCKS = 3
ATC_MIN_CLEARANCE_CM = 10.0
ATC_NO_BLOCK_FALLBACK_CM = 100000.0
YELLOW_SIGNAL_SPEED_KMH = 40.0
# Speed cap applied when the next forward block is in CAUTION state, per
# design_new.md ("`CAUTION` vs reservation independence"). Signal-state limits
# remain a separate concern (`YELLOW_SIGNAL_SPEED_KMH`).
CAUTION_SPEED_LIMIT_KMH = 40.0

SPEED_TREND_EPSILON = 0.1
ATO_SPEED_TOLERANCE = 2.0
ATO_ENTRY_MARGIN = ATO_SPEED_TOLERANCE - 1.0
ATO_EXIT_MARGIN = ATO_SPEED_TOLERANCE + 1.0

# Speed curve constants
CURVE_SAMPLE_RESOLUTION_CM = (
    3.0  # 3cm resolution for speed curves (balance between precision and performance)
)
ATO_SPEED_MARGIN_KMH = 5.0  # ATO stays 5 km/h below ATC limit
NEAR_STOP_DETECTION_CM = 50.0  # Distance to detect approaching stop for rate limiting
ATO_RESTRICTION_LOOKAHEAD_CM = 150.0
ATO_OVERSHOOT_WEIGHT = 10.0  # Penalty weight for exceeding speed curve in evaluator


# =============================================================================
# Data Structures for Lookahead and ATO
# =============================================================================


# `LookaheadPoint` and `ATOInfo` live in `train_controller_types` so the
# helper modules (`lookahead_builder`, `atc_calculator`, `ato_navigator`)
# can import them without a circular dep on this file. Re-exported here for
# backwards compatibility with existing callers.
from train_controller_types import ATOInfo, LookaheadPoint  # noqa: E402,F401


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
    eb_active: bool = (
        False  # Emergency brake active (renamed from emergency_stop_active)
    )
    atc_warning_active: bool = False  # True when overspeed by 5-10 km/h

    # -------------------------------------------------------------------------
    # Notifications (for frontend toast messages via API)
    # -------------------------------------------------------------------------
    pending_notifications: List[str] = field(default_factory=list)

    # -------------------------------------------------------------------------
    # Cab & Mode State
    # -------------------------------------------------------------------------
    active_cab: Optional[str] = None  # 'A', 'B', or None
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
    path_backed_run_active: bool = False

    # -------------------------------------------------------------------------
    # ATC State (calculated each tick)
    # -------------------------------------------------------------------------
    # Note: target_speed and speed_limit_reason are read live from
    # `self.lookahead[0]` per design_new.md; they are not stored.
    clearance_ahead_cm: float = 0.0
    atc_speed_limit: float = 0.0
    current_block_speed_limit: float = float("inf")

    # Configuration
    safety_margin_cm: float = TRACK_END_SAFETY_MARGIN_CM
    signal_lookahead_blocks: int = ATC_SIGNAL_LOOKAHEAD_BLOCKS

    # -------------------------------------------------------------------------
    # ATO Info (calculated each tick; lookahead is a derived property below)
    # -------------------------------------------------------------------------
    ato_info: Optional[ATOInfo] = None

    _previous_speed: float = 0.0
    handle_reason: str = ""  # Diagnostic: reason for current handle position
    # Internal cache for the controller's most recent speed_limit_reason as
    # produced by `_calculate_atc_speed_limit()`. Read through the
    # `speed_limit_reason` property below; not part of the public schema.
    _last_speed_limit_reason: Optional[str] = None
    # Canonical structured reason code for the most recent ATC limit selection.
    # Mirrors the `atc_limit_reason_code` field in the canonical lookahead
    # payload at distance 0. Updated by `tick()` and the lookahead builder.
    _last_atc_limit_reason_code: Optional[AtcLimitReasonCode] = None

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
    # Canonical drain order from design_new.md.
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
        "ato_go",
        "ato_stop",
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
    def ato_path_id(self) -> Optional[str]:
        """Computed read derived from received_path_plan.path_id."""
        return self.assigned_path_id

    @property
    def ato_destination_block_id(self) -> Optional[str]:
        """Computed read derived from received_path_plan.authority_end_block_id.

        Falls back to legacy `end_block_id` when the dispatcher's Path schema
        has not yet been migrated.
        """
        plan = self.received_path_plan
        if plan is None:
            return None
        if hasattr(plan, "authority_end_block_id"):
            value = plan.authority_end_block_id
            if value is not None:
                return value
        return getattr(plan, "end_block_id", None)

    @property
    def travel_direction(self) -> Optional[str]:
        """Read-only derived value computed each read from (self.cab, self.current_span).

        Per design_new.md: the cab's physical end is the head, and travel_direction
        is the network-frame direction pointing from the opposite physical end
        through the cab end and onward. When no next span resource exists ahead,
        it is the direction outward through the cab edge of the cab end's tile.
        Returns None when no cab is selected.

        Implementation note: in the current codebase, controller-owned
        `current_span` storage is not yet wired (the canonical TrainSpan is
        being introduced incrementally). Until it is, derive direction from the
        existing TrainPosition fields (facing_direction / leading_end / entry
        edges) so the property still returns a meaningful value. The contract
        ("derived, not stored, no writers") is preserved either way.
        """
        if self.active_cab is None:
            return None
        train = self._get_train()
        if not train or not getattr(train, "position", None):
            return None
        position = train.position
        # Prefer the current facing direction when known — this is the same
        # network-frame direction implied by (cab, end positions, entry edges)
        # in the existing geometry.
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
        is also exported via `get_monitor_lookahead()` / `get_lookahead()`.
        """
        return self._calculate_lookahead()

    @property
    def target_speed(self) -> float:
        """Live read: speed_limit_kmh from the first lookahead point.

        Per design_new.md, "target speed" is the operational_limit at distance
        0 in the canonical lookahead. The current shared LookaheadPoint schema
        carries `speed_limit_kmh`, which we use as the v1 target-speed read.
        """
        points = self._calculate_lookahead()
        if not points:
            return 0.0
        first = points[0]
        speed = getattr(first, "speed_limit_kmh", 0.0)
        if speed == float("inf"):
            return 0.0
        return float(speed)

    @property
    def speed_limit_reason(self) -> Optional[str]:
        """Live read of the most recent ATC speed-limit reason.

        Per design_new.md, the canonical source is the distance-0 lookahead
        speed-curve metadata. The current LookaheadPoint schema does not carry
        a reason string, so the controller exposes the reason cached by
        `_calculate_atc_speed_limit()` during the same tick. This read does
        not write anything and is recomputed by the next tick's ATC step.
        """
        return self._last_speed_limit_reason

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
        except Exception:
            # Defensive: do not let a single bad command crash the tick.
            logger.exception(
                "command-drain raised for train %s; continuing tick",
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
            self._last_speed_limit_reason = "Emergency brake active"
            self._last_atc_limit_reason_code = AtcLimitReasonCode.EB

            logger.debug(
                "[TrainController %s] EB active -> handle=EB speed=%.2f",
                self.train_id,
                self.current_speed_kmh,
            )

            # While EB is latched, controller does not run post-EB restart
            # logic. Explicit release_emergency_cmd() is required to clear
            # eb_active. This early-exit holds until the operator releases.
            return

        # ===== 1.5 REACTIVE SPEED-BASED STATE EVALUATION (per design_new.md) =====
        # Observe speed written by the previous tick's on_physics_updated and
        # adjust state-machine transitions accordingly.
        if self.state == TrainState.READY and self.current_speed_kmh > STOPPED_SPEED_THRESHOLD_KMH:
            # Manual driver pre-rolled before depart was issued, or another
            # rare edge case. Move into RUNNING_MOVING. Per design_new.md
            # "ato_running meaning and lifecycle", arm ato_running when this
            # transition happens under ATO with an active authority.
            self.state = TrainState.RUNNING_MOVING
            if self.control_mode == "ato" and self.received_path_plan is not None:
                self.ato_running = True
                self.path_backed_run_active = True
        elif (
            self.state == TrainState.RUNNING_MOVING
            and self.current_speed_kmh <= STOPPED_SPEED_THRESHOLD_KMH
            and self.received_path_plan is None
        ):
            # No-authority braking completion: zero speed reached without live
            # authority. Route through transition_to_ready() so path-run and
            # ato_running side-effects are cleared in lockstep.
            self.transition_to_ready()

        # ===== 2. LOOKAHEAD (computed live; no caching) =====
        # Per design_new.md, lookahead is a pure derived value. We trigger one
        # local computation here so the ATC info collection below uses the same
        # live values; downstream consumers re-read it via the property.
        live_lookahead = self._calculate_lookahead()

        # ===== 3. ATC INFO COLLECTION =====
        if self.active_cab is None:
            self.atc_speed_limit = 0.0
            self._last_speed_limit_reason = "No cab selected"
            self._last_atc_limit_reason_code = AtcLimitReasonCode.NO_CAB
            logger.debug(
                "[TrainController %s] ATC blocked: no cab selected",
                self.train_id,
            )
        elif self._is_cab_wrong_direction():
            self.atc_speed_limit = 0.0
            self._last_speed_limit_reason = "Cab points away from authority"
            self._last_atc_limit_reason_code = AtcLimitReasonCode.WRONG_DIRECTION_CAB
            logger.debug(
                "[TrainController %s] ATC blocked: cab opposite active authority",
                self.train_id,
            )
        else:
            # Get block speed limit for current block
            self.current_block_speed_limit = self._get_current_block_speed_limit()

            # Calculate clearance and ATC speed from existing methods
            # (These use lookahead data internally)
            self.clearance_ahead_cm = self._calculate_clearance_ahead()
            (
                self.atc_speed_limit,
                self._last_speed_limit_reason,
                self._last_atc_limit_reason_code,
            ) = self._calculate_atc_speed_limit_with_code()
            logger.debug(
                "[TrainController %s] ATC: clearance=%.2f limit=%.2f reason=%s code=%s",
                self.train_id,
                self.clearance_ahead_cm,
                self.atc_speed_limit,
                self._last_speed_limit_reason,
                self._last_atc_limit_reason_code,
            )

        # ===== 4. ATC ACTION =====
        overspeed = self.current_speed_kmh - self.atc_speed_limit

        if overspeed > 10:
            # Emergency brake - overspeed > 10 km/h. Per design_new.md the
            # EB controller-local effects are: set eb_active = True,
            # force handle to EB, clear ato_running, and (when an active path
            # exists) write pending_path_cancel_request(EB).
            self.eb_active = True
            self._last_speed_limit_reason = "Emergency brake: overspeed"
            self._last_atc_limit_reason_code = AtcLimitReasonCode.EB
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
            # Warning only
            self.atc_warning_active = True
            logger.debug(
                "[TrainController %s] ATC warning: overspeed=%.2f",
                self.train_id,
                overspeed,
            )
        else:
            self.atc_warning_active = False

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
                self.handle_position = self._ato_calculate_handle()
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
            "[TrainController %s] effective_limit=%.2f reason=%s",
            self.train_id,
            effective_limit,
            self._last_speed_limit_reason,
        )

        if self._has_path_backed_run_context(train):
            self.path_backed_run_active = True

        # ===== 8. STATE TRANSITIONS =====
        if self.state == TrainState.RUNNING_MOVING:
            # Check for station arrival
            if self._is_at_station():
                self.state = TrainState.RUNNING_AT_STATION
                self.ato_running = False
                # RouteExecutor will handle dwell timing
                logger.debug(
                    "[TrainController %s] state -> RUNNING_AT_STATION",
                    self.train_id,
                )

            elif self._is_standalone_path_complete(train):
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

        path_tiles = self.get_path_tiles()
        dest_block_id = self.ato_destination_block_id
        current_block_id = self._get_current_block_id()

        # Check if we're already in the destination block
        if current_block_id == dest_block_id:
            return self._calculate_clearance_to_end_of_current_block()

        # Not in destination block yet - need current_path to trace forward
        if not path_tiles:
            return base_clearance

        # Check if destination block has a station (for precise stopping)
        station_id = self._get_station_in_block(dest_block_id)
        station_positions: set = set()
        if station_id:
            station = self.network.platforms.get(station_id)
            if station:
                station_positions = set(station.positions)

        # Get head tile and path index
        head_tile = self._get_head_tile()
        if not head_tile:
            return base_clearance

        head_index = self._get_head_path_index(head_tile)
        if head_index < 0:
            return base_clearance

        # Sum distances from current position to end of destination block
        clearance = base_clearance
        in_dest_block = False

        # Determine scan direction so we iterate toward the train's
        # actual travel direction, not always forward in the array.
        scan_step = self._get_path_scan_step(train, head_index)
        if scan_step == -1:
            ahead_entries = list(reversed(path_tiles[:head_index]))
        else:
            ahead_entries = list(path_tiles[head_index + 1 :])

        # Start from next tile after head (in travel direction)
        for entry_idx, path_entry in enumerate(ahead_entries):
            tile_x, tile_y = path_entry[0], path_entry[1]
            tile_pos = (tile_x, tile_y)

            # Get track at this tile
            track = self.network.get_track(tile_x, tile_y)
            if not track:
                break

            # Check if this tile's block is obstructed (occupied by another train)
            if track.block_section_id and track.block_section_id != current_block_id:
                block = self.network.blocks.get(track.block_section_id)
                if block and self._is_block_obstructed(block, track.block_section_id):
                    break
                current_block_id = track.block_section_id

            # Track if we've entered the destination block
            if track.block_section_id == dest_block_id:
                in_dest_block = True

            # Add tile length
            tile_length = self.network._get_tile_length_cm(tile_x, tile_y)
            clearance += tile_length

            # If destination has a station, stop at end of station tiles
            if station_positions and tile_pos in station_positions:
                # Check if this is the last station tile in the remaining
                # ahead entries (direction-aware).
                remaining_ahead = ahead_entries[entry_idx + 1 :]
                has_more_station_tiles = any(
                    (p[0], p[1]) in station_positions for p in remaining_ahead
                )
                if not has_more_station_tiles:
                    # This is the last station tile - stopping point reached
                    break
            # If no station, stop at end of destination block
            elif in_dest_block and track.block_section_id != dest_block_id:
                # We've exited the destination block
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
        if self.active_cab not in ("A", "B"):
            return False
        if self.received_path_plan is None:
            return False
        required = self._required_cab_for_path()
        if required is None:
            return False
        return required != self.active_cab

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
        if not path or not path.path_tiles:
            return None
        from path_utils import convert_tiles_to_train_path
        return convert_tiles_to_train_path(path.path_tiles)

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

    def _get_path_scan_step(self, train: "Train", head_path_index: int) -> int:
        """
        Determine whether lookahead should scan path forward (+1) or backward (-1).

        Lookahead direction follows the active cab's facing direction, not the stored
        path order. This allows monitor lookahead to flip correctly when switching cab.
        """
        path_tiles = self.get_path_tiles()
        if not path_tiles:
            return 1

        path_length = len(path_tiles)
        if path_length <= 1:
            return 1

        facing_direction = (
            train.position.facing_direction
            if train.position and train.position.facing_direction
            else None
        )
        if facing_direction is None:
            if head_path_index >= path_length - 1 and head_path_index > 0:
                return -1
            return 1

        head_entry = path_tiles[head_path_index]
        head_tile = (head_entry[0], head_entry[1])

        next_index = head_path_index + 1
        if next_index < path_length:
            next_entry = path_tiles[next_index]
            next_direction = self._direction_between_tiles(
                head_tile, (next_entry[0], next_entry[1])
            )
            if next_direction == facing_direction:
                return 1

        prev_index = head_path_index - 1
        if prev_index >= 0:
            prev_entry = path_tiles[prev_index]
            prev_direction = self._direction_between_tiles(
                head_tile, (prev_entry[0], prev_entry[1])
            )
            if prev_direction == facing_direction:
                return -1

        # facing_direction didn't match either neighbor (common on diagonal /
        # curved tracks after a cab switch).  Fall back to destination-aware
        # logic: find which end of the path contains the destination block.
        dest_block_id = self.ato_destination_block_id
        if dest_block_id and self.network:
            # Find the first path tile belonging to the destination block,
            # scanning from both ends toward the head.
            dest_forward_idx: Optional[int] = None
            dest_backward_idx: Optional[int] = None
            for i in range(head_path_index + 1, path_length):
                pt = path_tiles[i]
                tk = self.network.get_track(pt[0], pt[1])
                if tk and tk.block_section_id == dest_block_id:
                    dest_forward_idx = i
                    break
            for i in range(head_path_index - 1, -1, -1):
                pt = path_tiles[i]
                tk = self.network.get_track(pt[0], pt[1])
                if tk and tk.block_section_id == dest_block_id:
                    dest_backward_idx = i
                    break
            if dest_forward_idx is not None and dest_backward_idx is None:
                return 1
            if dest_backward_idx is not None and dest_forward_idx is None:
                return -1
            # Both found — prefer the one further away (more of the path left)
            if dest_forward_idx is not None and dest_backward_idx is not None:
                if (
                    dest_forward_idx - head_path_index
                    >= head_path_index - dest_backward_idx
                ):
                    return 1
                return -1

        # No destination available — use path_index progression heuristic:
        # if the head is in the first half of the path, it's likely moving
        # forward (+1); in the second half, backward (-1).
        if head_path_index <= path_length // 2:
            return 1
        return -1

    def _build_empty_monitor_lookahead(
        self,
        *,
        head_distance_cm: float = 0.0,
        travel_direction: Optional[str] = None,
        speed_limit_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build a consistent empty lookahead payload for Train Monitor.

        Per design_new.md "canonical lookahead payload", the empty/no-cab/
        no-train form preserves the full top-level and nested key shape with
        empty lists and zero distances. Compatibility consumers see the same
        keys regardless of whether a path is live.
        """
        return {
            "train_id": self.train_id,
            "head_tile": None,
            "head_distance_cm": head_distance_cm,
            "travel_direction": travel_direction,
            "cab": self.active_cab,
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

        path_tiles = self.get_path_tiles()
        if train.position and path_tiles:
            head_index = self._get_head_path_index(head_tile)
            if head_index < 0:
                return block.length_cm

            head_tile_length = self.network._get_tile_length_cm(*head_tile)
            remaining = max(0.0, head_tile_length - train.position.head_distance_cm)

            # Determine scan direction: when the train travels in the
            # reverse direction relative to the path array (scan_step=-1),
            # tiles "ahead" are at *lower* indices.  Scanning forward
            # (head_index+1:) in that case would count tiles *behind* the
            # train, producing a wrong remaining distance.
            scan_step = self._get_path_scan_step(train, head_index)
            if scan_step == -1:
                # Scan backward in path array (tiles ahead of the train)
                ahead_tiles = reversed(path_tiles[:head_index])
            else:
                ahead_tiles = path_tiles[head_index + 1 :]

            for x, y, *_ in ahead_tiles:
                track = self.network.get_track(x, y)
                if not track or track.block_section_id != current_block_id:
                    break
                remaining += self.network._get_tile_length_cm(x, y)

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
        directly (or call `power_on_cmd()` / `power_off_cmd()`); this setter
        exists so test fixtures and external snapshots can still write the
        legacy boolean shape without re-introducing a stored field.
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

        Per design_new.md, this is the post-physics usable-authority stop
        check used by Phase 3 path completion. The full predicate requires
        depart-acceptance (state == RUNNING_MOVING), active authority, and a
        distance-0 ATC limit of 0 whose reason code is
        `AtcLimitReasonCode.AUTHORITY_STOP`. v1 keys this predicate on the
        enum value (not on free-form reason strings).
        """
        if not self.is_stopped():
            return False
        if self.state != TrainState.RUNNING_MOVING:
            return False
        if self.eb_active:
            return False
        if self.received_path_plan is None:
            return False
        if self.atc_speed_limit > STOPPED_SPEED_THRESHOLD_KMH:
            return False
        return self._last_atc_limit_reason_code == AtcLimitReasonCode.AUTHORITY_STOP

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
        return self.active_cab in ("A", "B")

    def _align_cab_to_required_path_direction(self) -> bool:
        """Ensure the stopped train's active cab matches the required path direction."""
        train = self._get_train()
        if not train:
            return False

        required_cab = self._required_cab_for_path()
        if required_cab is None or self.active_cab == required_cab:
            return True

        logger.info(
            "[TrainController %s] auto-correcting cab %s -> %s for path direction",
            self.train_id,
            self.active_cab,
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

        current_cab = self.active_cab or "A"

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
        if self.route_executor is not None:
            route_id = self.route_executor.assigned_route_id_for(self.train_id)
            if route_id is not None:
                route = self.route_executor.route_registry.get(route_id)
                if route:
                    # Check if platform is in the route's waypoints
                    for waypoint in route.waypoints:
                        if waypoint.platform_id == station_id:
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

    def _evaluate_handles_against_curve(
        self,
        current_speed_kmh: float,
        curve: List[Dict],
        horizon_cm: float = 30.0,
        sample_count: int = 5,
    ) -> HandlePosition:
        """Forward to `ATONavigator.evaluate_handles_against_curve(...)`."""
        return self._get_ato_navigator().evaluate_handles_against_curve(
            current_speed_kmh, curve, horizon_cm, sample_count
        )

    def _ato_calculate_handle(self) -> HandlePosition:
        """Forward to `ATONavigator.compute_handle()`."""
        return self._get_ato_navigator().compute_handle()

    def _calculate_power_notch(
        self, current_speed: float, target_speed: float
    ) -> HandlePosition:
        """Forward to `ATONavigator.calculate_power_notch(...)`."""
        from ato_navigator import ATONavigator

        return ATONavigator.calculate_power_notch(current_speed, target_speed)

    def _calculate_braking_notch(self, current_speed: float) -> HandlePosition:
        """Forward to `ATONavigator.calculate_braking_notch(current_speed)`."""
        from ato_navigator import ATONavigator

        return ATONavigator.calculate_braking_notch(current_speed)

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

        Per design_new.md (lines 107-125), commands are drained in the order:
        power_on -> set_cab -> set_mode -> set_manual_mode_type ->
        set_handle / set_manual_power_percent -> activate_emergency_cmd ->
        release_emergency_cmd -> power_off. ato_go / ato_stop are processed
        at the end.

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
            elif kind == "ato_go":
                result = self._execute_ato_go(payload, event_time)
            elif kind == "ato_stop":
                result = self._execute_ato_stop(payload, event_time)
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

    def _execute_ato_go(self, payload, event_time) -> CommandResult:
        return self.ato_go()

    def _execute_ato_stop(self, payload, event_time) -> CommandResult:
        return self.ato_stop()

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

    # Legacy alias retained as a thin shim so existing callers keep working.
    def emergency_stop_cmd(self) -> CommandResult:
        return self.activate_emergency_cmd()

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
            return self.emergency_stop_cmd()

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

        old_cab = self.active_cab

        # Per design_new.md: same-cab no-op success — does not change state,
        # does not disengage ato_running, no notification.
        if cab == old_cab:
            return CommandResult(True, f"Cab already {cab}")

        self.active_cab = cab

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
                # property falls back to the legacy field, so we must explicitly flip.
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
        if self.active_cab not in ("A", "B"):
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
        Start ATO movement toward destination.

        Rules:
        - Must be in ATO mode
        - Must be powered on
        - Train state must be IDLE or STANDBY

        ATO Automation:
        - Automatically selects cab from path direction when no cab is set
        - Automatically corrects cab direction to match path when already set
        - Uses set_cab() for proper state updates (facing_direction, entry edges)
        """
        train = self._get_train()
        if train is None:
            return CommandResult(False, "Train not found")

        if self.control_mode != "ato":
            return CommandResult(False, "Not in ATO mode")

        if not self.power_on:
            return CommandResult(False, "Power is off")

        # Allow READY only - auto-selection picks/corrects the cab as needed.
        if self.state != TrainState.READY:
            return CommandResult(
                False, f"Train not in READY (is {self.state.value})"
            )

        # Recalculate ATO info fresh to ensure cab direction matches
        # the current path (path may have been assigned since last tick)
        self.ato_info = self._calculate_ato_info()

        # ATO Auto-selection/correction: determine the required cab from path
        # direction and select or switch if needed. This handles both:
        #   1. cab=None: auto-select cab from path
        #   2. cab=A/B but wrong: auto-correct cab to match path direction
        if self.ato_info and self.ato_info.required_cab:
            required_cab = self.ato_info.required_cab
            if self.active_cab != required_cab:
                old_cab = self.active_cab
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
        elif self.active_cab is None:
            # No ATO info available and no cab - cannot determine direction
            return CommandResult(
                False,
                "No cab selected and no path available to auto-select direction",
            )

        # After auto-selection, verify we're still in READY (set_cab keeps
        # state == READY for non-dwelling controllers).
        if self.state != TrainState.READY:
            return CommandResult(False, f"Train not in READY (is {self.state.value})")
        if self.active_cab not in ("A", "B"):
            return CommandResult(False, "ATO requires a cab to be selected")

        self.state = TrainState.RUNNING_MOVING
        self.ato_running = True
        self.path_backed_run_active = True
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
        self.path_backed_run_active = False

    # -------------------------------------------------------------------------
    # Route Executor Commands (for RouteExecutor integration)
    # -------------------------------------------------------------------------

    def depart(self) -> bool:
        """
        Start moving (STANDBY/IDLE -> RUNNING_MOVING).

        Used by RouteExecutor when path is acquired and train should start moving.

        ATO mode: accepts STANDBY or IDLE; delegates to ato_go() which
        auto-selects cab from path direction (enabling automatic reversal when
        a new route goes the opposite direction).

        Manual mode: accepts STANDBY only; requires facing_direction to be set.

        Returns:
            True if successful, False if preconditions not met
        """
        train = self._get_train()
        if train is None:
            return False

        # ATO mode: delegate to ato_go() which accepts READY, auto-selects cab
        # from path direction, and transitions to RUNNING_MOVING.
        if self.control_mode == "ato":
            if self.state != TrainState.READY:
                return False
            result = self.ato_go()
            return result.success

        # Manual mode: requires READY with a cab and facing direction
        if self.state != TrainState.READY:
            return False
        if self.active_cab not in ("A", "B"):
            return False
        if train.facing_direction is None:
            return False
        self.path_backed_run_active = self._has_path_backed_run_context(train)
        self.state = TrainState.RUNNING_MOVING
        return True

    def arrive_at_station(self) -> bool:
        """
        Arrive at station (RUNNING_MOVING -> RUNNING_AT_STATION).

        Used by RouteExecutor when train reaches a waypoint station.

        Returns:
            True if successful, False if not in correct state
        """
        train = self._get_train()
        if train is None:
            return False

        if self.state != TrainState.RUNNING_MOVING:
            return False

        self.state = TrainState.RUNNING_AT_STATION
        self.ato_running = False  # Stop ATO at station

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
        self.path_backed_run_active = False
        self.state = TrainState.READY

    def depart_from_station(self, has_more_waypoints: bool = True) -> bool:
        """
        Depart from station after dwell time.

        Used by RouteExecutor after dwell time completes. Transitions to READY.

        Args:
            has_more_waypoints: Reserved for callers that signal route end;
                v1 ignores the value because route-run teardown is owned by
                RouteExecutor (not the controller).

        Returns:
            True if successful, False if not in correct state
        """
        train = self._get_train()
        if train is None:
            return False

        if self.state != TrainState.RUNNING_AT_STATION:
            return False

        self.state = TrainState.READY
        if not has_more_waypoints:
            self.ato_running = False

        return True

    def turn_around(self) -> bool:
        """
        Flip facing direction by switching to the opposite cab.

        Delegates to set_cab() which handles all the necessary state changes:
        - Swaps active cab (A <-> B) and leading_end
        - Flips entry edges for both ends
        - Flips distances for both ends
        - Updates path_index for new head position
        - Syncs facing_direction from computed property

        Returns:
            True if successful, False if preconditions not met

        Preconditions:
        - Train must be stopped
        - ATO must not be actively running (ato_running == False)
        - Train must have an active cab (A or B)
        """
        train = self._get_train()
        if train is None:
            return False

        if not self.is_stopped():
            return False

        if self.ato_running:
            return False

        if self.active_cab not in ("A", "B"):
            return False

        # Delegate to set_cab with the opposite cab
        new_cab = "B" if self.active_cab == "A" else "A"
        result = self.set_cab(new_cab)
        return result.success

    def set_direction_and_standby(self, direction: Direction) -> bool:
        """
        Select cab A and stay in READY.

        Used by RouteExecutor when activating a route for a train. Per
        design_new.md the v1 state machine has a single READY state that
        covers powered-stopped regardless of cab; cab selection is its own
        orthogonal field.

        Returns:
            True if successful, False if not in READY without a cab.
        """
        train = self._get_train()
        if train is None:
            return False

        if self.state != TrainState.READY or self.active_cab is not None:
            return False

        if train.position:
            train.position.leading_end = LeadingEnd.A
        self.active_cab = "A"
        return True

    def clear_direction(self) -> bool:
        """
        Clear cab selection and stay in READY.

        Allowed only when stopped and in READY with a cab selected.
        """
        train = self._get_train()
        if train is None:
            return False

        if not self.is_stopped():
            return False

        if self.state != TrainState.READY or self.active_cab is None:
            return False

        if train.position:
            train.position.leading_end = None
        self.active_cab = None
        return True

    def enable_manual(self) -> bool:
        """
        Enable manual control mode. Must be stopped.

        Per design_new.md, manual is a control mode, not a separate
        operational state. The state stays at READY (or RUNNING_AT_STATION
        if dwelling).
        """
        train = self._get_train()
        if train is None:
            return False

        if not self.is_stopped():
            return False

        if self.state == TrainState.OFF:
            return False

        self.control_mode = "manual"
        self.ato_running = False
        # State stays where it is (READY / RUNNING_AT_STATION).

        return True

    def disable_manual(self) -> bool:
        """
        Disable manual control mode. Must be stopped.

        Per design_new.md, manual is a control mode. Switching back to ATO
        leaves the state at STANDBY/IDLE based on cab.

        Returns:
            True if successful, False if moving
        """
        train = self._get_train()
        if train is None:
            return False

        if not self.is_stopped():
            return False

        self.control_mode = "ato"
        self.ato_running = False
        # State stays where it is (STANDBY / IDLE / RUNNING_AT_STATION).

        return True

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
            "emergency_stop_active": self.eb_active,
            "atc_warning_active": self.atc_warning_active,
            "pending_notifications": self.pending_notifications.copy(),
            "active_cab": self.active_cab,
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
            "occupied_tiles": [list(t.tile) for t in span.occupied_tiles],
            "occupied_blocks": list(span.occupied_blocks),
            "occupied_resources": list(span.occupied_resources),
            "leading_end": span.leading_end.value if span.leading_end else None,
        }

    def _build_fallback_lookahead(
        self,
        train: "Train",
        head_tile: Tuple[int, int],
        head_distance_cm: float,
        travel_direction: Optional[str],
    ) -> Dict[str, Any]:
        """
        Build lookahead data when no path is available.

        This is used when:
        - Train is first placed (no path yet)
        - Path is canceled (train must stop safely)
        - Train enters last tile and path is cleared

        The lookahead is built from the train's occupied blocks and facing direction,
        calculating clearance to the end of the current block.
        """
        blocks: List[Dict[str, Any]] = []
        signals: List[Dict[str, Any]] = []
        switches: List[Dict[str, Any]] = []
        stations: List[Dict[str, Any]] = []

        # Get current block from head tile
        head_track = (
            self.network.get_track(head_tile[0], head_tile[1]) if self.network else None
        )
        current_block_id = head_track.block_section_id if head_track else None

        clearance_cm = 0.0
        block_length_cm = 0.0
        tile_count = 1

        # If controller has a known clearance (from simulation), use it as baseline
        # This ensures we don't underestimate clearance when path is temporarily unavailable
        if self.clearance_ahead_cm > 0:
            clearance_cm = self.clearance_ahead_cm

        if current_block_id and self.network and train.position:
            block = self.network.blocks.get(current_block_id)
            facing_dir = train.position.facing_direction

            # Calculate remaining distance in current tile
            current_tile_length = self.network._get_tile_length_cm(
                head_tile[0], head_tile[1]
            )
            remaining_in_tile = max(0, current_tile_length - head_distance_cm)

            # Calculate remaining distance in the ENTIRE block (not just current tile)
            # This is critical for first placement in multi-tile blocks
            if clearance_cm == 0 and block and block.length_cm > 0:
                # Get all tiles in the block to calculate proper remaining distance
                block_positions = self.network.get_block_positions(current_block_id)
                if block_positions:
                    tile_count = len(block_positions)
                    per_tile_length = block.length_cm / tile_count

                    # Calculate how far into the block the train is
                    # We need to find where head_tile is in the block
                    # and sum the lengths of tiles behind + distance in current tile
                    distance_into_block = head_distance_cm

                    # For tiles the train has already passed through in this block
                    # (tiles in occupied_tiles that are in this block, before head tile)
                    if train.position.occupied_tiles:
                        for tile in train.position.occupied_tiles:
                            if tile == head_tile:
                                break
                            tile_track = self.network.get_track(tile[0], tile[1])
                            if (
                                tile_track
                                and tile_track.block_section_id == current_block_id
                            ):
                                distance_into_block += self.network._get_tile_length_cm(
                                    tile[0], tile[1]
                                )

                    # Remaining distance = total block length - distance already traveled
                    clearance_cm = max(0.0, block.length_cm - distance_into_block)
                    block_length_cm = block.length_cm
                else:
                    # Fallback: use remaining in tile if we can't get block positions
                    clearance_cm = remaining_in_tile
                    block_length_cm = current_tile_length
            elif clearance_cm == 0:
                # No block info available, use remaining in tile as minimum
                clearance_cm = remaining_in_tile
                block_length_cm = current_tile_length
            else:
                # We have clearance from simulation, just get block length for info
                if block and block.length_cm > 0:
                    block_length_cm = block.length_cm
                    block_positions = self.network.get_block_positions(current_block_id)
                    if block_positions:
                        tile_count = len(block_positions)
                else:
                    block_length_cm = current_tile_length

            # The clearance is the remaining distance in the current block
            # For safety, we use the remaining tile distance as minimum
            # This ensures the train stops before the end of the known track

            # Build block info
            block_state = None
            block_speed_limit: Optional[float] = None
            if block:
                if block.state:
                    block_state = block.state.value
                if block.speed_limit_kmh and block.speed_limit_kmh < float("inf"):
                    block_speed_limit = block.speed_limit_kmh

            blocks.append(
                {
                    "id": current_block_id,
                    "name": current_block_id,
                    "state": block_state or BlockState.OCCUPIED.value,
                    "start_cm": -head_distance_cm,  # Block starts behind train
                    "end_cm": clearance_cm,
                    "length_cm": block_length_cm
                    if block_length_cm > 0
                    else head_distance_cm + clearance_cm,
                    "tile_count": tile_count,
                    "speed_limit_kmh": block_speed_limit,
                }
            )

            # Check if there's a station in this block
            if head_track and head_track.station_id:
                platform_id = head_track.station_id
                station = self.network.platforms.get(platform_id)
                if station:
                    # Get station length from station positions
                    station_tile_count = (
                        len(station.positions) if station.positions else 1
                    )
                    station_length_cm = (
                        station.length_cm if station.length_cm > 0 else block_length_cm
                    )
                    station_group_id: Optional[str] = None
                    if hasattr(self.network, "get_station_for_platform"):
                        try:
                            station_group_id = self.network.get_station_for_platform(
                                platform_id
                            )
                        except Exception:
                            station_group_id = None
                    stations.append(
                        {
                            "platform_id": platform_id,
                            "station_id": station_group_id,
                            "name": station.name if station else platform_id,
                            "distance_cm": 0,  # We're already in the station
                            "length_cm": station_length_cm,
                            "tile_count": station_tile_count,
                            "top_speed_kmh": station.top_speed_kmh if station else None,
                        }
                    )

        # Calculate safety margin
        effective_safety_margin = self._get_effective_safety_margin()
        target_stop_cm = max(0.0, clearance_cm - effective_safety_margin)
        # Without an active path, the authority boundary delta is the
        # distance from the head to the safety-margin-adjusted end of the
        # current occupied block. The forward axis matches `speed_curves`.
        authority_boundary_delta_cm = target_stop_cm

        # EB forces ATC to zero regardless of geometry; surface that as the
        # canonical override on every curve sample.
        override_code: Optional[AtcLimitReasonCode] = None
        override_detail: Optional[str] = None
        if self.eb_active:
            override_code = AtcLimitReasonCode.EB
            override_detail = "Emergency brake active"

        # Calculate speed curves for the available clearance. Without a live
        # path, the canonical reason code at the stop boundary is
        # `no_authority` rather than `authority_stop`.
        speed_curves = self.calculate_speed_curves(
            max_distance_cm=clearance_cm if clearance_cm > 0 else 50.0,
            lookahead_blocks=blocks,
            lookahead_signals=signals,
            lookahead_stations=stations,
            target_stop_cm=target_stop_cm,
            override_reason_code=override_code,
            override_reason_detail=override_detail,
            path_end_reason_code=AtcLimitReasonCode.NO_AUTHORITY,
        )

        return {
            "train_id": self.train_id,
            "head_tile": [head_tile[0], head_tile[1]],
            "head_distance_cm": head_distance_cm,
            "travel_direction": travel_direction,
            "cab": self.active_cab,
            "speed_limit_reason": self.speed_limit_reason,
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

    def get_monitor_lookahead(self) -> Dict[str, Any]:
        """Backward-compatible alias for `get_lookahead()`.

        Older callers (UI, monitor exports) refer to the lookahead payload
        through this name. The canonical name in v1 is `get_lookahead()`.
        """
        return self._compute_canonical_lookahead()

    def _compute_canonical_lookahead(self) -> Dict[str, Any]:
        train = self._get_train()
        if not train or not self.network:
            return self._build_empty_monitor_lookahead(
                speed_limit_reason=self.speed_limit_reason
            )

        if self.active_cab not in ("A", "B"):
            self._last_speed_limit_reason = "No cab selected"
            self._last_atc_limit_reason_code = AtcLimitReasonCode.NO_CAB
            payload = self._build_empty_monitor_lookahead(
                speed_limit_reason=self._last_speed_limit_reason
            )
            # Stamp the no_cab reason code on a single anchor curve point so
            # consumers sampling the payload see the structured reason even
            # with no path/cab.
            payload["lookahead"]["speed_curves"] = [
                {
                    "distance_cm": 0.0,
                    "atc_limit": 0.0,
                    "operational_limit": 0.0,
                    "atc_limit_reason_code": AtcLimitReasonCode.NO_CAB.value,
                }
            ]
            return payload

        head_tile = self._get_head_tile()
        head_distance_cm = self._get_head_distance_cm()
        travel_direction = (
            train.position.facing_direction.name
            if train.position and train.position.facing_direction
            else None
        )

        if not head_tile:
            return self._build_empty_monitor_lookahead(
                head_distance_cm=head_distance_cm,
                travel_direction=travel_direction,
                speed_limit_reason=self.speed_limit_reason,
            )

        path_tiles = self.get_path_tiles()
        head_path_index = self._get_head_path_index(head_tile)
        if head_path_index < 0 and path_tiles:
            # Head tile is not in the path.  This commonly happens right after
            # a return-path assignment + cab switch: the cab switch moves the
            # head to the opposite end of the train which may be one tile past
            # the path start.  If the head is in the same block as path[0] or
            # path[-1], anchor the scan at that path endpoint so the full
            # path-based lookahead is used instead of the fallback.
            head_track_obj = self.network.get_track(head_tile[0], head_tile[1])
            head_block = head_track_obj.block_section_id if head_track_obj else None
            if head_block:
                # Check first path tile
                first_entry = path_tiles[0]
                first_track = self.network.get_track(first_entry[0], first_entry[1])
                first_block = first_track.block_section_id if first_track else None
                # Check last path tile
                last_entry = path_tiles[-1]
                last_track = self.network.get_track(last_entry[0], last_entry[1])
                last_block = last_track.block_section_id if last_track else None

                if head_block == first_block:
                    head_path_index = 0
                    # Account for extra distance: head is behind path[0]
                    extra_tiles_cm = 0.0
                    # Scan from head to path[0] through the block
                    # (simplified: just add the head tile's length)
                    head_tile_length = self.network._get_tile_length_cm(
                        head_tile[0], head_tile[1]
                    )
                    extra_tiles_cm = head_tile_length - head_distance_cm
                    head_distance_cm = -extra_tiles_cm  # Negative = before path start
                elif head_block == last_block:
                    head_path_index = len(path_tiles) - 1

        if head_path_index < 0 or not path_tiles:
            # No path available - build lookahead from occupied blocks
            # This handles: initial placement, path canceled, entering last tile
            return self._build_fallback_lookahead(
                train, head_tile, head_distance_cm, travel_direction
            )

        blocks: List[Dict[str, Any]] = []
        signals: List[Dict[str, Any]] = []
        switches: List[Dict[str, Any]] = []
        stations: List[Dict[str, Any]] = []

        cumulative_distance_cm = -head_distance_cm
        path_scan_step = self._get_path_scan_step(train, head_path_index)

        current_block_id = None
        block_back_length_cm = 0.0
        block_back_tile_count = 0
        head_track = self.network.get_track(head_tile[0], head_tile[1])
        if head_track:
            current_block_id = head_track.block_section_id

        if current_block_id:
            behind_start = (
                head_path_index - 1 if path_scan_step == 1 else head_path_index + 1
            )
            behind_end = -1 if path_scan_step == 1 else len(path_tiles)
            for j in range(behind_start, behind_end, -path_scan_step):
                prev_entry = path_tiles[j]
                prev_x, prev_y = prev_entry[0], prev_entry[1]
                prev_track = self.network.get_track(prev_x, prev_y)
                prev_block_id = prev_track.block_section_id if prev_track else None
                if prev_block_id != current_block_id:
                    break
                block_back_length_cm += self.network._get_tile_length_cm(prev_x, prev_y)
                block_back_tile_count += 1

        current_block_start_cm = (
            cumulative_distance_cm - block_back_length_cm
            if current_block_id
            else cumulative_distance_cm
        )

        seen_blocks: set[str] = set()
        seen_signals: set[str] = set()
        seen_switches: set[Tuple[int, int]] = set()
        seen_stations: set[str] = set()
        current_entry_edge: Optional[str] = None
        neighbor_index = head_path_index - path_scan_step
        if 0 <= neighbor_index < len(path_tiles):
            neighbor_entry = path_tiles[neighbor_index]
            neighbor_tile = (neighbor_entry[0], neighbor_entry[1])
            direction_into_head = self._direction_between_tiles(
                neighbor_tile, head_tile
            )
            exit_edge = (
                self._direction_to_edge(direction_into_head)
                if direction_into_head
                else None
            )
            if exit_edge:
                current_entry_edge = OPPOSITE_EDGES.get(exit_edge)
        if current_entry_edge is None and train.position:
            current_entry_edge = train.position.head_entry_edge

        loop_end = len(path_tiles) if path_scan_step == 1 else -1
        for i in range(head_path_index, loop_end, path_scan_step):
            path_entry = path_tiles[i]
            x, y = path_entry[0], path_entry[1]
            track = self.network.get_track(x, y)
            tile_length = self.network._get_tile_length_cm(x, y) if track else 50.0
            tile_block_id = track.block_section_id if track else None
            tile_travel_direction = (
                path_entry[2].name
                if len(path_entry) > 2 and hasattr(path_entry[2], "name")
                else None
            )
            if (
                tile_travel_direction
                and path_scan_step == -1
                and hasattr(path_entry[2], "opposite")
            ):
                tile_travel_direction = path_entry[2].opposite().name
            point_element_start = max(0.0, cumulative_distance_cm)

            if tile_block_id and tile_block_id != current_block_id:
                current_block_id = tile_block_id
                current_block_start_cm = cumulative_distance_cm

            if tile_block_id and tile_block_id not in seen_blocks:
                seen_blocks.add(tile_block_id)
                block_length = 0.0
                tile_count = 0
                for j in range(i, loop_end, path_scan_step):
                    next_entry = path_tiles[j]
                    next_x, next_y = next_entry[0], next_entry[1]
                    next_track = self.network.get_track(next_x, next_y)
                    next_block_id = next_track.block_section_id if next_track else None
                    if next_block_id != tile_block_id:
                        break
                    block_length += self.network._get_tile_length_cm(next_x, next_y)
                    tile_count += 1

                if (
                    tile_block_id == current_block_id
                    and i == head_path_index
                    and block_back_length_cm > 0.0
                ):
                    block_length += block_back_length_cm
                    tile_count += block_back_tile_count

                block_state = None
                block_speed_limit: Optional[float] = None  # None means no limit
                block = self.network.blocks.get(tile_block_id)
                if block:
                    if block.state:
                        block_state = block.state.value
                    if block.speed_limit_kmh and block.speed_limit_kmh < float("inf"):
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
                        "speed_limit_kmh": block_speed_limit,  # None if no limit
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
                    # Calculate precise signal distance accounting for train position
                    # When signal is on the same tile as train head (i == head_path_index),
                    # we need to subtract head_distance_cm to get actual relative distance
                    signal_distance = point_element_start
                    if i == head_path_index:
                        signal_distance = cumulative_distance_cm
                    signals.append(
                        {
                            "id": signal_id,
                            "name": signal.name or signal_id,
                            "state": signal.state.value if signal.state else "unknown",
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
                        for j in range(i, len(path_tiles)):
                            next_entry = path_tiles[j]
                            next_x, next_y = next_entry[0], next_entry[1]
                            next_track = self.network.get_track(next_x, next_y)
                            if not next_track or next_track.station_id != platform_id:
                                break
                            station_length += self.network._get_tile_length_cm(
                                next_x, next_y
                            )
                            station_tile_count += 1
                    station_group_id: Optional[str] = None
                    if hasattr(self.network, "get_station_for_platform"):
                        try:
                            station_group_id = self.network.get_station_for_platform(
                                platform_id
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
                            "top_speed_kmh": station.top_speed_kmh if station else None,
                        }
                    )

            next_index = i + path_scan_step
            if 0 <= next_index < len(path_tiles):
                next_entry = path_tiles[next_index]
                direction = self._direction_between_tiles(
                    (x, y), (next_entry[0], next_entry[1])
                )
                exit_edge = self._direction_to_edge(direction) if direction else None
                if exit_edge and track:
                    if current_entry_edge is None or exit_edge not in track.get_exits(
                        current_entry_edge, use_switch_state=True
                    ):
                        inferred_entry = track.get_entry_for_exit(
                            exit_edge, use_switch_state=True
                        )
                        if inferred_entry:
                            current_entry_edge = inferred_entry

                can_traverse, next_entry_edge = self._can_traverse_path_step(
                    train,
                    (x, y),
                    (next_entry[0], next_entry[1]),
                    current_entry_edge,
                )
                cumulative_distance_cm += tile_length
                if not can_traverse:
                    break
                current_entry_edge = next_entry_edge
            else:
                cumulative_distance_cm += tile_length

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

        return {
            "train_id": self.train_id,
            "head_tile": [head_tile[0], head_tile[1]],
            "head_distance_cm": head_distance_cm,
            "travel_direction": travel_direction,
            "cab": self.active_cab,
            "speed_limit_reason": self.speed_limit_reason,
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
            eb_active=data.get("eb_active", data.get("emergency_stop_active", False)),
            atc_warning_active=data.get("atc_warning_active", False),
            active_cab=data.get("active_cab"),
            control_mode=data.get("control_mode", "ato"),
            manual_mode_type=data.get("manual_mode_type", "simulation"),
            manual_power_percent=int(data.get("manual_power_percent", 0) or 0),
            state=state,
            handle_position=handle,
            ato_running=data.get("ato_running", False),
            path_backed_run_active=data.get("path_backed_run_active", False),
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
