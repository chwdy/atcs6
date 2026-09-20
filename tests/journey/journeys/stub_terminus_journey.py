"""Journey: confirm the *current* behaviour when a train is routed toward a
dead-end terminus whose platform(s) are occupied by parked, route-less trains.

Layout is built by `StubTerminusScene`:

    approach (W->E)                ┌─ P_EAST  (5,0)   MAIN, straight  (best)
    (0,0)(1,0)(2,0)(3,0) ─ SW_THROAT ┤
                          (4,0)      └─ P_NORTH (4,1)(4,2)  DIVERGE

Two outcomes are asserted, selected by `expected`:

  * ``"divert"`` — best platform (P_EAST) occupied, P_NORTH free, B routed to
    the STATION. B re-berths at the free P_NORTH. This is occupancy-aware
    station resolution (`RouteTargetResolver._resolve_station_target_block`
    ranks a platform held by another train below a free one).

  * ``"hold"`` — either BOTH platforms occupied (StationTarget) or B routed to
    the SPECIFIC occupied platform (PlatformTarget). B has no free alternative,
    so it holds on BLK_APPROACH_E — the block before the throat switch. Its
    reservation never crosses the switch, so the switch stays free for the
    occupant(s) to leave, and B never emergency-brakes.

Both outcomes share: A remains the occupant of P_EAST_BLOCK for the whole run,
and B never emergency-brakes.
"""

from ..events import Event, Invariant, Journey
from ..invariants import no_eb, no_overspeed, state_cab_pair_valid
from ..triggers import at_tick

_APPROACH_HOLD_BLOCK = "BLK_APPROACH_E"
_APPROACH_BLOCKS = {"BLK_APPROACH_W", "BLK_APPROACH_E"}
_FREE_PLATFORM_BLOCK = "P_NORTH_BLOCK"
_BEST_PLATFORM_BLOCK = "P_EAST_BLOCK"
# Tiles B must never touch in a HOLD case (switch throat + both platforms).
_BEYOND_SWITCH_TILES = {(4, 0), (5, 0), (4, 1), (4, 2)}


# --------------------------------------------------------------------------- #
# Shared invariant: the parked occupant A never leaves P_EAST.
# --------------------------------------------------------------------------- #
def _occupant_holds_best_platform() -> Invariant:
    def check(s) -> None:
        occ = getattr(s.network.blocks.get(_BEST_PLATFORM_BLOCK), "occupied_by", None)
        assert occ == "A", (
            f"parked occupant A must keep {_BEST_PLATFORM_BLOCK}; occupied_by={occ!r}"
        )

    return Invariant(name="occupant_holds_best_platform", check=check)


# --------------------------------------------------------------------------- #
# HOLD invariants: B never reserves past the switch and never occupies any
# tile beyond it (so the switch stays clear the whole run).
# --------------------------------------------------------------------------- #
def _never_reserves_past_switch() -> Invariant:
    allowed = _APPROACH_BLOCKS | {None}

    def check(s) -> None:
        assert s.active_path_reservation_end_block_id in allowed, (
            "B reserved past the throat switch: reservation_end="
            f"{s.active_path_reservation_end_block_id!r} (allowed {allowed})"
        )

    return Invariant(name="never_reserves_past_switch", check=check)


def _never_occupies_past_switch() -> Invariant:
    def check(s) -> None:
        crossed = _BEYOND_SWITCH_TILES.intersection(s.occupied_tiles)
        assert not crossed, (
            f"B occupied tiles at/past the throat switch: {sorted(crossed)}"
        )

    return Invariant(name="never_occupies_past_switch", check=check)


# --------------------------------------------------------------------------- #
# DIVERT invariant: B never occupies the (occupied) best platform's tile.
# --------------------------------------------------------------------------- #
def _never_occupies_best_platform() -> Invariant:
    def check(s) -> None:
        assert (5, 0) not in s.occupied_tiles, (
            "B occupied the (occupied) best platform tile (5,0)"
        )

    return Invariant(name="never_occupies_best_platform", check=check)


# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #
def _stopped_at(block_id: str):
    """Fires (and latches) once B is stopped with its reservation ending on
    `block_id` after having started moving."""
    state = {"fired": False}

    def trigger(s) -> bool:
        if state["fired"]:
            return True
        if (
            s.tick > 5
            and s.speed_kmh <= 0.5
            and s.active_path_reservation_end_block_id == block_id
        ):
            state["fired"] = True
            return True
        return False

    return trigger


def _berthed_at(block_id: str):
    """Fires once B is stopped and occupying `block_id`."""
    state = {"fired": False}

    def trigger(s) -> bool:
        if state["fired"]:
            return True
        if s.tick > 5 and s.speed_kmh <= 0.5 and block_id in s.occupied_blocks:
            state["fired"] = True
            return True
        return False

    return trigger


# --------------------------------------------------------------------------- #
# Assertions
# --------------------------------------------------------------------------- #
def _assert_diverted(s) -> None:
    assert _FREE_PLATFORM_BLOCK in s.occupied_blocks, (
        f"expected B to re-berth at the free {_FREE_PLATFORM_BLOCK}; "
        f"occupied_blocks={s.occupied_blocks}"
    )


def _assert_holding(s) -> None:
    assert s.active_path_reservation_end_block_id == _APPROACH_HOLD_BLOCK, (
        f"expected B held with reservation ending at {_APPROACH_HOLD_BLOCK}; "
        f"got {s.active_path_reservation_end_block_id!r}"
    )
    assert set(s.occupied_blocks) <= _APPROACH_BLOCKS, (
        f"expected B to occupy only approach blocks; occupied={s.occupied_blocks}"
    )


def build_stub_terminus_journey(expected: str, train_id: str = "B") -> Journey:
    assert expected in ("divert", "hold")
    common = [
        no_eb(),
        no_overspeed(),
        state_cab_pair_valid(),
        _occupant_holds_best_platform(),
    ]

    if expected == "divert":
        return Journey(
            train_id=train_id,
            events=[
                Event(
                    name="reberths_at_free_platform",
                    trigger=_berthed_at(_FREE_PLATFORM_BLOCK),
                    assertions=[_assert_diverted],
                ),
            ],
            invariants=common + [_never_occupies_best_platform()],
        )

    # HOLD: settle before the switch, then confirm the hold is sustained.
    return Journey(
        train_id=train_id,
        events=[
            Event(
                name="settles_before_switch",
                trigger=_stopped_at(_APPROACH_HOLD_BLOCK),
                assertions=[_assert_holding],
            ),
            Event(
                name="hold_is_sustained",
                trigger=at_tick(180),
                assertions=[_assert_holding],
            ),
        ],
        invariants=common
        + [_never_reserves_past_switch(), _never_occupies_past_switch()],
    )
