"""Pure-function unit tests for ATC predicates.

These tests demonstrate that the value-input refactor in
``train_controller/atc.py`` allows the ATC math to be exercised
**without** constructing a controller, network, dispatcher, or train.
Each test builds hand-rolled inputs (small dataclasses for Block /
Signal stand-ins) and asserts the result of the pure function.

This is the architectural payoff of the sub-module split: pure
functions take values, not references, so unit tests don't need the
whole world.

Where these tests sit in the test pyramid:

- ``tests/test_atc_pure.py``: this file — fast, no IO, isolated math.
- ``tests/test_trains_controller.py``: integration tests using a real
  ``TrainController`` and ``RailwayNetwork`` to verify the thin
  wrappers wire the pure functions correctly.
- Journey tests (``tests/test_demo_train_journey.py`` et al.): full
  per-tick simulation that catches emergent behavior.
"""

from dataclasses import dataclass
from typing import Optional

from enums import BlockState, SignalState
from train_controller.atc import (
    ATC_NO_BLOCK_FALLBACK_CM,
    CAUTION_SPEED_LIMIT_KMH,
    PLATFORM_SAFETY_MARGIN_CM,
    YELLOW_SIGNAL_SPEED_KMH,
    clearance_ahead_pure,
    effective_safety_margin_pure,
    is_approaching_yellow_signal_pure,
    is_caution_ahead_pure,
    is_signal_red_for_block_pure,
    speed_limit_pure,
)


# ---------------------------------------------------------------------------
# Test helpers — hand-rolled stand-ins for Block / Signal
# ---------------------------------------------------------------------------


@dataclass
class FakeBlock:
    """Minimal stand-in for the real Block dataclass.

    Real ``Block`` has a derived ``.state`` ``@property``; this fake
    stores the state directly so tests can stub it easily.
    """

    block_id: str
    length_cm: float
    state: BlockState = BlockState.FREE


@dataclass
class FakeSignal:
    monitored_block_id: str
    state: SignalState
    detection_direction: Optional[str] = None


# ---------------------------------------------------------------------------
# clearance_ahead_pure
# ---------------------------------------------------------------------------


def test_clearance_ahead_no_current_block_returns_fallback():
    """When ``current_block_id`` is None, return the no-block fallback."""
    result = clearance_ahead_pure(
        current_block_id=None,
        remaining_in_current_cm=50.0,
        blocks_ahead=[],
        is_obstructed=lambda b: False,
        is_signal_red=lambda bid: False,
        ato_destination_block_id=None,
        fallback_clearance_to_destination=lambda base: base,
    )
    assert result == ATC_NO_BLOCK_FALLBACK_CM


def test_clearance_ahead_single_block_walk():
    """Walks blocks summing length until the list is exhausted."""
    blocks = [
        FakeBlock("B1", 200.0),
        FakeBlock("B2", 300.0),
        FakeBlock("B3", 400.0),
    ]
    result = clearance_ahead_pure(
        current_block_id="B0",
        remaining_in_current_cm=50.0,
        blocks_ahead=blocks,
        is_obstructed=lambda b: False,
        is_signal_red=lambda bid: False,
        ato_destination_block_id=None,
        fallback_clearance_to_destination=lambda base: base,
    )
    # 50 + 200 + 300 + 400 = 950
    assert result == 950.0


def test_clearance_ahead_stops_at_obstructed_block():
    """The walk breaks at the first obstructed block."""
    blocks = [
        FakeBlock("B1", 200.0),
        FakeBlock("B2", 300.0),  # obstructed
        FakeBlock("B3", 400.0),
    ]
    result = clearance_ahead_pure(
        current_block_id="B0",
        remaining_in_current_cm=0.0,
        blocks_ahead=blocks,
        is_obstructed=lambda b: b.block_id == "B2",
        is_signal_red=lambda bid: False,
        ato_destination_block_id=None,
        fallback_clearance_to_destination=lambda base: base,
    )
    # 0 + 200, then B2 obstructed → break
    assert result == 200.0


def test_clearance_ahead_stops_at_red_signal():
    """The walk breaks at the first block with a red signal."""
    blocks = [
        FakeBlock("B1", 200.0),
        FakeBlock("B2", 300.0),
    ]
    result = clearance_ahead_pure(
        current_block_id="B0",
        remaining_in_current_cm=0.0,
        blocks_ahead=blocks,
        is_obstructed=lambda b: False,
        is_signal_red=lambda bid: bid == "B2",
        ato_destination_block_id=None,
        fallback_clearance_to_destination=lambda base: base,
    )
    # 0 + 200, then signal for B2 is red → break
    assert result == 200.0


def test_clearance_ahead_uses_fallback_when_no_blocks_ahead():
    """Empty ``blocks_ahead`` + an active destination → use the fallback."""
    result = clearance_ahead_pure(
        current_block_id="B0",
        remaining_in_current_cm=100.0,
        blocks_ahead=[],
        is_obstructed=lambda b: False,
        is_signal_red=lambda bid: False,
        ato_destination_block_id="STA_BLK",
        fallback_clearance_to_destination=lambda base: base + 250.0,
    )
    # base 100, fallback adds 250
    assert result == 350.0


# ---------------------------------------------------------------------------
# speed_limit_pure
# ---------------------------------------------------------------------------


def test_speed_limit_no_authority_short_circuits():
    """Without a received path plan, return (0, ``"no_authority"``)."""
    speed, reason = speed_limit_pure(
        received_path_plan_exists=False,
        clearance_cm=10000.0,
        decel_rate_kmh_s=15.0,
        safety_margin_cm=25.0,
        caution_ahead=False,
        yellow_signal_ahead=False,
        block_limit_kmh=float("inf"),
        train_max_speed_kmh=100.0,
        calculate_max_safe_speed=lambda c, d, s: 100.0,
    )
    assert speed == 0.0
    assert reason == "no_authority"


def test_speed_limit_plain_kinematic_far_clearance():
    """With path authority + far clearance, returns kinematic safe speed."""
    speed, reason = speed_limit_pure(
        received_path_plan_exists=True,
        clearance_cm=2000.0,  # 20m → >1000 → "Clearance" reason
        decel_rate_kmh_s=15.0,
        safety_margin_cm=25.0,
        caution_ahead=False,
        yellow_signal_ahead=False,
        block_limit_kmh=float("inf"),
        train_max_speed_kmh=100.0,
        calculate_max_safe_speed=lambda c, d, s: 60.0,
    )
    assert speed == 60.0
    assert reason == "Clearance: 20m"


def test_speed_limit_braking_zone_reason():
    """Clearance < 1000cm produces the ``Braking`` reason."""
    speed, reason = speed_limit_pure(
        received_path_plan_exists=True,
        clearance_cm=500.0,  # 5m → <1000 → "Braking" reason
        decel_rate_kmh_s=15.0,
        safety_margin_cm=25.0,
        caution_ahead=False,
        yellow_signal_ahead=False,
        block_limit_kmh=float("inf"),
        train_max_speed_kmh=100.0,
        calculate_max_safe_speed=lambda c, d, s: 20.0,
    )
    assert speed == 20.0
    assert reason == "Braking: 5m ahead"


def test_speed_limit_caution_cap_layered():
    """CAUTION cap overrides kinematic when kinematic > 40 km/h."""
    speed, reason = speed_limit_pure(
        received_path_plan_exists=True,
        clearance_cm=10000.0,
        decel_rate_kmh_s=15.0,
        safety_margin_cm=25.0,
        caution_ahead=True,
        yellow_signal_ahead=False,
        block_limit_kmh=float("inf"),
        train_max_speed_kmh=100.0,
        calculate_max_safe_speed=lambda c, d, s: 100.0,
    )
    assert speed == CAUTION_SPEED_LIMIT_KMH
    assert reason == "CAUTION block ahead"


def test_speed_limit_yellow_cap_layered():
    """Yellow-signal cap overrides kinematic when kinematic > 40 km/h."""
    speed, reason = speed_limit_pure(
        received_path_plan_exists=True,
        clearance_cm=10000.0,
        decel_rate_kmh_s=15.0,
        safety_margin_cm=25.0,
        caution_ahead=False,
        yellow_signal_ahead=True,
        block_limit_kmh=float("inf"),
        train_max_speed_kmh=100.0,
        calculate_max_safe_speed=lambda c, d, s: 100.0,
    )
    assert speed == YELLOW_SIGNAL_SPEED_KMH
    assert reason == "Yellow signal ahead"


def test_speed_limit_block_cap_dominant():
    """A binding block cap below all other caps wins."""
    speed, reason = speed_limit_pure(
        received_path_plan_exists=True,
        clearance_cm=10000.0,
        decel_rate_kmh_s=15.0,
        safety_margin_cm=25.0,
        caution_ahead=False,
        yellow_signal_ahead=False,
        block_limit_kmh=20.0,  # Below kinematic 100
        train_max_speed_kmh=100.0,
        calculate_max_safe_speed=lambda c, d, s: 100.0,
    )
    assert speed == 20.0
    assert reason == "Block limit: 20 km/h"


# ---------------------------------------------------------------------------
# is_caution_ahead_pure
# ---------------------------------------------------------------------------


def test_caution_ahead_first_block_caution():
    blocks = [FakeBlock("B1", 200.0, BlockState.CAUTION)]
    assert is_caution_ahead_pure(blocks) is True


def test_caution_ahead_first_block_free():
    """First block FREE means no caution, even if later blocks are CAUTION."""
    blocks = [
        FakeBlock("B1", 200.0, BlockState.FREE),
        FakeBlock("B2", 200.0, BlockState.CAUTION),
    ]
    assert is_caution_ahead_pure(blocks) is False


def test_caution_ahead_empty_list():
    assert is_caution_ahead_pure([]) is False


def test_caution_ahead_skips_none():
    """None entries in the list are skipped; first concrete block matters."""
    blocks = [None, FakeBlock("B1", 200.0, BlockState.CAUTION)]
    assert is_caution_ahead_pure(blocks) is True


# ---------------------------------------------------------------------------
# effective_safety_margin_pure
# ---------------------------------------------------------------------------


def test_safety_margin_station_destination_uses_platform_margin():
    result = effective_safety_margin_pure(
        ato_destination_block_id="STA_BLK",
        station_id_for_block=lambda bid: "STATION_A" if bid == "STA_BLK" else None,
        default_margin_cm=25.0,
    )
    assert result == PLATFORM_SAFETY_MARGIN_CM


def test_safety_margin_non_station_destination_uses_default():
    result = effective_safety_margin_pure(
        ato_destination_block_id="BLK_X",
        station_id_for_block=lambda bid: None,
        default_margin_cm=25.0,
    )
    assert result == 25.0


def test_safety_margin_no_destination_uses_default():
    result = effective_safety_margin_pure(
        ato_destination_block_id=None,
        station_id_for_block=lambda bid: None,
        default_margin_cm=25.0,
    )
    assert result == 25.0


# ---------------------------------------------------------------------------
# is_approaching_yellow_signal_pure
# ---------------------------------------------------------------------------


def test_yellow_signal_within_lookahead():
    signals = [FakeSignal(monitored_block_id="B2", state=SignalState.YELLOW)]
    result = is_approaching_yellow_signal_pure(
        blocks_ahead_ids=["B1", "B2", "B3"],
        signals=signals,
        travel_edge=None,
        signal_lookahead_blocks=3,
    )
    assert result is True


def test_yellow_signal_outside_lookahead_depth():
    signals = [FakeSignal(monitored_block_id="B4", state=SignalState.YELLOW)]
    result = is_approaching_yellow_signal_pure(
        blocks_ahead_ids=["B1", "B2", "B3", "B4"],
        signals=signals,
        travel_edge=None,
        signal_lookahead_blocks=3,  # Only checks first 3
    )
    assert result is False


def test_yellow_signal_direction_filter():
    """Eastbound signal does not flag a westbound train."""
    signals = [
        FakeSignal(
            monitored_block_id="B2",
            state=SignalState.YELLOW,
            detection_direction="E",
        ),
    ]
    result = is_approaching_yellow_signal_pure(
        blocks_ahead_ids=["B1", "B2"],
        signals=signals,
        travel_edge="W",
        signal_lookahead_blocks=3,
    )
    assert result is False


def test_yellow_signal_double_yellow_also_fires():
    """DOUBLE_YELLOW counts as a yellow-signal hit."""
    signals = [
        FakeSignal(monitored_block_id="B1", state=SignalState.DOUBLE_YELLOW),
    ]
    result = is_approaching_yellow_signal_pure(
        blocks_ahead_ids=["B1"],
        signals=signals,
        travel_edge=None,
        signal_lookahead_blocks=3,
    )
    assert result is True


# ---------------------------------------------------------------------------
# is_signal_red_for_block_pure
# ---------------------------------------------------------------------------


def test_signal_red_matching_block():
    signals = [FakeSignal(monitored_block_id="B1", state=SignalState.RED)]
    assert (
        is_signal_red_for_block_pure(
            block_id="B1", signals=signals, travel_edge=None
        )
        is True
    )


def test_signal_red_no_match():
    signals = [FakeSignal(monitored_block_id="B2", state=SignalState.RED)]
    assert (
        is_signal_red_for_block_pure(
            block_id="B1", signals=signals, travel_edge=None
        )
        is False
    )


def test_signal_red_direction_filter():
    """Eastbound red signal does not block a westbound train."""
    signals = [
        FakeSignal(
            monitored_block_id="B1",
            state=SignalState.RED,
            detection_direction="E",
        ),
    ]
    assert (
        is_signal_red_for_block_pure(
            block_id="B1", signals=signals, travel_edge="W"
        )
        is False
    )


def test_signal_yellow_not_treated_as_red():
    """Only RED state triggers the predicate; yellow is ignored."""
    signals = [FakeSignal(monitored_block_id="B1", state=SignalState.YELLOW)]
    assert (
        is_signal_red_for_block_pure(
            block_id="B1", signals=signals, travel_edge=None
        )
        is False
    )
