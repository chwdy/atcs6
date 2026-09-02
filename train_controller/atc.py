"""
ATC Computer sub-module of ``TrainController``.

Per design_new.md "TrainController" sub-module decomposition: the ATC
Computer owns the live ATC speed-limit / clearance computation plus
the cached fields the rest of the controller and the API surface read.

This module absorbs the previous standalone ``atc_calculator.py``
``ATCCalculator`` helper. Under the v1 architecture, the ATC fields
(``speed_limit``, ``clearance_ahead_cm``, ``current_block_speed_limit``,
``safety_margin_cm``, ``signal_lookahead_blocks``,
``_last_limit_reason_code``) live on this class rather than on the
façade; cross-sub-module reads go through the parent controller
back-reference ``self._c``.

# Pure-function pattern

Every predicate (clearance, speed limit, CAUTION, yellow-signal, signal
red, safety margin) has TWO surfaces:

1. A **module-level pure function** (``..._pure``) that takes its inputs
   as explicit values / callables. No controller, no network, no
   dispatcher. Independently unit-testable — see ``tests/test_atc_pure.py``.

2. The existing **instance method** on ``ATCComputer`` is a thin wrapper
   that gathers values from ``self._c`` and delegates to the pure
   function. All existing call sites (``controller.atc.clearance_ahead()``,
   etc.) continue to work unchanged.

# Constants

The v1 tunables ``CAUTION_SPEED_LIMIT_KMH``,
``PLATFORM_SAFETY_MARGIN_CM``, ``ATC_NO_BLOCK_FALLBACK_CM``, and
``STOPPED_SPEED_THRESHOLD_KMH`` are imported at module level and
referenced by the pure functions directly. They are not per-call
inputs because v1 treats them as global policy.

# External API

  - ``controller.atc.speed_limit`` (cached cap; refreshed each tick)
  - ``controller.atc.clearance_ahead_cm`` (cached clearance)
  - ``controller.atc.current_block_speed_limit`` (cached block cap)
  - ``controller.atc.safety_margin_cm`` (tunable; default
    ``TRACK_END_SAFETY_MARGIN_CM``)
  - ``controller.atc.signal_lookahead_blocks`` (tunable scan depth)
  - ``controller.atc.clearance_ahead()`` (recompute clearance now)
  - ``controller.atc.speed_limit_now()`` (recompute speed cap now,
    returns ``(speed, reason_string)``)
  - ``controller.atc.speed_limit_with_code()`` (recompute, returns
    ``(speed, reason, reason_code)``)
  - ``controller.atc.is_caution_ahead()`` /
    ``controller.atc.is_approaching_yellow_signal()`` /
    ``controller.atc.is_signal_red_for_block(block_id)`` /
    ``controller.atc.effective_safety_margin()``
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple

from constants import (
    CAUTION_SPEED_LIMIT_KMH,
    PLATFORM_SAFETY_MARGIN_CM,
    STOPPED_SPEED_THRESHOLD_KMH,
    TRACK_END_SAFETY_MARGIN_CM,
)
from enums import AtcLimitReasonCode, BlockState, SignalState

if TYPE_CHECKING:
    from train_controller import TrainController


# ATC tuning constants local to this module (not authored on
# `constants.py` because they have no consumer outside ATCComputer).
ATC_NO_BLOCK_FALLBACK_CM = 100000.0
ATC_SIGNAL_LOOKAHEAD_BLOCKS = 3


# =============================================================================
# Pure functions — independently unit-testable; no controller required
# =============================================================================


def clearance_ahead_pure(
    *,
    current_block_id: Optional[str],
    remaining_in_current_cm: float,
    blocks_ahead: List[Any],
    is_obstructed: Callable[[Any], bool],
    ato_destination_block_id: Optional[str],
    fallback_clearance_to_destination: Callable[[float], float],
) -> float:
    """Available clearance in cm ahead of the train head.

    Pure: depends only on the explicit value/callable inputs. The walk
    sums block lengths until an obstruction (other train, foreign
    reservation, end of path). When ``blocks_ahead`` is empty but
    ``ato_destination_block_id`` is set, derives clearance from
    ``fallback_clearance_to_destination(base_clearance)`` instead.

    Signal state is intentionally not an ATC input (design_new.md:1849):
    signals are display state derived from blocks, not an independent
    clearance source. ``blocks_ahead`` walks only the train's own live
    authority (``received_path_plan``), whose switches are already locked
    in position and whose blocks are reserved in this train's direction,
    so the switch-blocks-route and opposite-direction-reservation cases a
    red signal would flag cannot occur for a block on this walk; foreign
    occupancy / reservation is already caught by ``is_obstructed``.

    The caller resolves block IDs to ``Block`` objects (with
    ``block_id`` and ``length_cm`` attributes) before calling. Passing
    a falsy ``current_block_id`` returns the no-block fallback.
    """
    if not current_block_id:
        return ATC_NO_BLOCK_FALLBACK_CM

    clearance = remaining_in_current_cm

    for block in blocks_ahead:
        if is_obstructed(block):
            break
        clearance += block.length_cm

    if not blocks_ahead and ato_destination_block_id:
        clearance = fallback_clearance_to_destination(clearance)

    return clearance


def speed_limit_pure(
    *,
    received_path_plan_exists: bool,
    clearance_cm: float,
    decel_rate_kmh_s: float,
    safety_margin_cm: float,
    caution_ahead: bool,
    block_limit_kmh: float,
    train_max_speed_kmh: float,
    calculate_max_safe_speed: Callable[[float, float, float], float],
) -> Tuple[float, Optional[str]]:
    """Compute (speed_limit_kmh, reason_string) from the binding caps.

    Pure: takes the three candidate caps (kinematic safe-speed from
    clearance, CAUTION cap, block cap) and the
    inputs needed to compute the kinematic cap. Returns the smallest
    binding speed plus a human-readable reason layered by which caps
    fired.

    Per design_new.md step ``22a9`` (canonical lookahead), without a
    received path plan the controller publishes no forward authority:
    ATC limit is ``0`` and the reason is ``no_authority``. There is
    no block-bounded fallback — movement authority is binary on
    ``received_path_plan``.
    """
    if not received_path_plan_exists:
        return 0.0, "no_authority"

    safe_speed = calculate_max_safe_speed(
        clearance_cm, decel_rate_kmh_s, safety_margin_cm
    )

    limiting_speed = safe_speed
    limiting_reason: Optional[str] = None

    if clearance_cm < float("inf"):
        if clearance_cm < 1000:
            limiting_reason = f"Braking: {clearance_cm / 100:.0f}m ahead"
        else:
            limiting_reason = f"Clearance: {clearance_cm / 100:.0f}m"

    if caution_ahead:
        if CAUTION_SPEED_LIMIT_KMH < limiting_speed:
            limiting_speed = CAUTION_SPEED_LIMIT_KMH
            limiting_reason = "CAUTION block ahead"
        elif limiting_reason:
            limiting_reason = f"{limiting_reason}; CAUTION block"

    if block_limit_kmh < float("inf"):
        if block_limit_kmh < limiting_speed:
            limiting_speed = block_limit_kmh
            limiting_reason = f"Block limit: {block_limit_kmh:.0f} km/h"
        elif limiting_reason and block_limit_kmh < train_max_speed_kmh:
            limiting_reason = f"{limiting_reason}; Block: {block_limit_kmh:.0f}"

    return limiting_speed, limiting_reason


def is_caution_ahead_pure(blocks_ahead: List[Any]) -> bool:
    """True when the first non-None block ahead is in CAUTION state.

    Pure: depends only on the explicit list. Each entry is a ``Block``
    object (with ``.state`` attribute) or ``None``. ``None`` entries
    are skipped; the first concrete block determines the result.
    """
    for block in blocks_ahead:
        if block is None:
            continue
        return block.state == BlockState.CAUTION
    return False


def effective_safety_margin_pure(
    *,
    ato_destination_block_id: Optional[str],
    station_id_for_block: Callable[[str], Optional[str]],
    default_margin_cm: float,
    platform_margin_cm: float = PLATFORM_SAFETY_MARGIN_CM,
) -> float:
    """Pick the safety margin: platform margin when the destination is
    a station, otherwise the default (typically track-end) margin.

    Pure: takes the destination id and a lookup callable; no
    controller required.
    """
    if not ato_destination_block_id:
        return default_margin_cm
    if station_id_for_block(ato_destination_block_id):
        return platform_margin_cm
    return default_margin_cm


def is_approaching_yellow_signal_pure(
    *,
    blocks_ahead_ids: List[str],
    signals: List[Any],
    travel_edge: Optional[str],
    signal_lookahead_blocks: int,
) -> bool:
    """True when a yellow / double-yellow signal lies within
    ``signal_lookahead_blocks`` blocks ahead in the train's travel
    direction.

    Pure: takes the block-id walk, the full signal list, the train's
    travel edge, and the scan depth. Each signal must have
    ``monitored_block_id``, ``state``, and optionally
    ``detection_direction`` attributes.
    """
    blocks_checked = 0
    for block_id in blocks_ahead_ids:
        if blocks_checked >= signal_lookahead_blocks:
            break
        for signal in signals:
            if not (
                hasattr(signal, "monitored_block_id")
                and signal.monitored_block_id == block_id
            ):
                continue
            if travel_edge and hasattr(signal, "detection_direction"):
                if signal.detection_direction != travel_edge:
                    continue
            if signal.state in (
                SignalState.YELLOW,
                SignalState.DOUBLE_YELLOW,
            ):
                return True
        blocks_checked += 1
    return False


def is_signal_red_for_block_pure(
    *,
    block_id: str,
    signals: List[Any],
    travel_edge: Optional[str],
) -> bool:
    """True when a red signal protects ``block_id`` against the
    train's travel direction.

    Pure: only considers signals whose ``detection_direction`` matches
    the train's facing direction. A signal with ``detection_direction
    = "E"`` monitors eastbound traffic and should not block a
    westbound train.
    """
    for signal in signals:
        if not (
            hasattr(signal, "monitored_block_id")
            and signal.monitored_block_id == block_id
        ):
            continue
        if signal.state != SignalState.RED:
            continue
        if travel_edge and hasattr(signal, "detection_direction"):
            if signal.detection_direction != travel_edge:
                continue
        return True
    return False


# =============================================================================
# ATCComputer — thin wrappers that gather IO and delegate to the pure functions
# =============================================================================


class ATCComputer:
    """Owns the ATC clearance + speed-limit computation and cached caps."""

    def __init__(self, controller: "TrainController") -> None:
        self._c = controller

        # Cached caps — refreshed by `produce_physics_command()` (and on
        # demand by the recompute helpers below).
        self.clearance_ahead_cm: float = 0.0
        self.speed_limit: float = 0.0
        self.current_block_speed_limit: float = float("inf")
        self._last_limit_reason_code: Optional[AtcLimitReasonCode] = None

        # Tunables ``safety_margin_cm`` and ``signal_lookahead_blocks`` are
        # exposed as live reads of this module's globals (see the properties
        # below), not instance copies captured here.

    @property
    def safety_margin_cm(self) -> float:
        """The track-end / authority-end buffer, read live from the module
        global so a runtime ``PUT`` (which rebinds
        ``train_controller.atc.TRACK_END_SAFETY_MARGIN_CM`` through
        ``runtime_config._propagate``) takes effect for controllers that
        already exist (design_new.md:3857/3888). The smaller
        ``PLATFORM_SAFETY_MARGIN_CM`` is layered in by
        ``effective_safety_margin()`` when the destination is a station.
        """
        return TRACK_END_SAFETY_MARGIN_CM

    @property
    def signal_lookahead_blocks(self) -> int:
        """Signal-scan depth, read live from the module global for the same
        reason as ``safety_margin_cm`` (design_new.md:3857/3908)."""
        return ATC_SIGNAL_LOOKAHEAD_BLOCKS

    # ------------------------------------------------------------------
    # Clearance ahead
    # ------------------------------------------------------------------

    def clearance_ahead(self) -> float:
        """Available clearance in cm ahead of the train head.

        Thin wrapper: gathers inputs from ``self._c`` and delegates to
        :func:`clearance_ahead_pure`.
        """
        c = self._c
        train = c._get_train()
        if not train or not c.network:
            return ATC_NO_BLOCK_FALLBACK_CM

        current_block_id = c._get_current_block_id()
        if not current_block_id:
            return ATC_NO_BLOCK_FALLBACK_CM

        # Resolve block IDs to Block objects, stopping at the first None
        # (preserving original early-termination behavior).
        blocks_ahead: List[Any] = []
        for block_id in c._get_blocks_ahead():
            block = c.network.blocks.get(block_id)
            if block is None:
                break
            blocks_ahead.append(block)

        return clearance_ahead_pure(
            current_block_id=current_block_id,
            remaining_in_current_cm=c._get_remaining_distance_in_current_block(),
            blocks_ahead=blocks_ahead,
            is_obstructed=lambda block: c._is_block_obstructed(block, block.block_id),
            ato_destination_block_id=c.path_projection.ato_destination_block_id,
            fallback_clearance_to_destination=(
                lambda base: c._calculate_clearance_to_destination(base)
            ),
        )

    # ------------------------------------------------------------------
    # ATC speed limit
    # ------------------------------------------------------------------

    def speed_limit_now(self) -> Tuple[float, Optional[str]]:
        """Return (speed_limit_kmh, reason_string).

        Thin wrapper: gathers inputs from ``self._c`` and delegates to
        :func:`speed_limit_pure`.
        """
        from train_controller import calculate_max_safe_speed

        c = self._c
        train = c._get_train()

        return speed_limit_pure(
            received_path_plan_exists=c.path_projection.received_path_plan is not None,
            clearance_cm=self.clearance_ahead_cm,
            decel_rate_kmh_s=c._get_speed_profile().deceleration_rate,
            safety_margin_cm=self.effective_safety_margin(),
            caution_ahead=self.is_caution_ahead(),
            block_limit_kmh=c._get_current_block_speed_limit(),
            train_max_speed_kmh=train.max_speed if train else 0.0,
            calculate_max_safe_speed=calculate_max_safe_speed,
        )

    def speed_limit_with_code(
        self,
    ) -> Tuple[float, Optional[str], Optional[AtcLimitReasonCode]]:
        """``speed_limit_now()`` plus the structured ``AtcLimitReasonCode``."""
        c = self._c
        limiting_speed, limiting_reason = self.speed_limit_now()
        clearance = self.clearance_ahead_cm
        limiting_code: Optional[AtcLimitReasonCode] = None

        if clearance < float("inf"):
            if limiting_speed <= STOPPED_SPEED_THRESHOLD_KMH:
                limiting_code = AtcLimitReasonCode.AUTHORITY_STOP
            else:
                limiting_code = AtcLimitReasonCode.OBSTRUCTION

        if self.is_caution_ahead():
            limiting_code = AtcLimitReasonCode.CAUTION

        if c.path_projection.received_path_plan is None and limiting_code in (
            None,
            AtcLimitReasonCode.AUTHORITY_STOP,
            AtcLimitReasonCode.OBSTRUCTION,
        ):
            limiting_code = AtcLimitReasonCode.NO_AUTHORITY

        return limiting_speed, limiting_reason, limiting_code

    # ------------------------------------------------------------------
    # Predicates and helpers
    # ------------------------------------------------------------------

    def is_caution_ahead(self) -> bool:
        """True when the next forward block is in CAUTION state.

        Thin wrapper around :func:`is_caution_ahead_pure`.
        """
        c = self._c
        if not c.network:
            return False
        blocks_ahead = [
            c.network.blocks.get(bid) for bid in c._get_blocks_ahead()
        ]
        return is_caution_ahead_pure(blocks_ahead)

    def effective_safety_margin(self) -> float:
        """5cm when destination is a station, otherwise the controller's
        configured ``safety_margin_cm`` (typically 25cm).

        Thin wrapper around :func:`effective_safety_margin_pure`.
        """
        c = self._c
        train = c._get_train()
        if not train or not c.network:
            return self.safety_margin_cm
        return effective_safety_margin_pure(
            ato_destination_block_id=c.path_projection.ato_destination_block_id,
            station_id_for_block=lambda bid: c._get_station_in_block(bid),
            default_margin_cm=self.safety_margin_cm,
            platform_margin_cm=PLATFORM_SAFETY_MARGIN_CM,
        )

    def is_approaching_yellow_signal(self) -> bool:
        """True when a yellow / double-yellow signal lies within
        ``signal_lookahead_blocks`` blocks ahead in the train's travel
        direction.

        Thin wrapper around :func:`is_approaching_yellow_signal_pure`.
        """
        c = self._c
        if not c.network:
            return False

        facing = c._facing_direction()
        travel_edge = c._direction_to_edge(facing) if facing else None

        return is_approaching_yellow_signal_pure(
            blocks_ahead_ids=c._get_blocks_ahead(),
            signals=list(c.network.signals.values()),
            travel_edge=travel_edge,
            signal_lookahead_blocks=self.signal_lookahead_blocks,
        )

    def is_signal_red_for_block(self, block_id: str) -> bool:
        """True when a red signal protects ``block_id`` against the
        train's facing direction.

        Thin wrapper around :func:`is_signal_red_for_block_pure`.
        """
        c = self._c
        if not c.network:
            return False

        facing = c._facing_direction()
        travel_edge = c._direction_to_edge(facing) if facing else None

        return is_signal_red_for_block_pure(
            block_id=block_id,
            signals=list(c.network.signals.values()),
            travel_edge=travel_edge,
        )
