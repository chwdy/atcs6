"""Trigger factories for journey events.

Each factory returns a `Snapshot -> bool` predicate. Triggers are evaluated
once per tick in the order events are declared. The first time a pending
event's trigger returns True its assertions/actions run and the event is
consumed. Triggers should therefore be monotonic — once they fire they stay
True (so a single tick can drain multiple pending events).
"""

from typing import Callable, Set, Tuple

from .harness import Snapshot

Predicate = Callable[[Snapshot], bool]


# --------------------------------------------------------------------------- #
# Tick-based
# --------------------------------------------------------------------------- #


def at_tick(n: int) -> Predicate:
    return lambda s: s.tick >= n


# --------------------------------------------------------------------------- #
# Head / span based (stateful — once True, stays True)
# --------------------------------------------------------------------------- #


def head_visited(tile: Tuple[int, int]) -> Predicate:
    """Fires once the train head has been observed at `tile`, or once the
    tile has appeared in `current_span.occupied_tiles`.
    """
    state = {"fired": False}
    target = tuple(tile)

    def trigger(s: Snapshot) -> bool:
        if state["fired"]:
            return True
        if s.head_tile == target or target in s.occupied_tiles:
            state["fired"] = True
            return True
        return False

    return trigger


def head_at_or_past(tile: Tuple[int, int], axis: str) -> Predicate:
    """Fires once head_tile coordinate has reached or passed `tile` along
    `axis` ("x+" / "x-" / "y+" / "y-"). Coarser than `head_visited` but works
    when the tick advances skip the exact tile.
    """
    state = {"fired": False}
    tx, ty = tile

    def trigger(s: Snapshot) -> bool:
        if state["fired"]:
            return True
        if s.head_tile is None:
            return False
        hx, hy = s.head_tile
        ok = (
            (axis == "x+" and hx >= tx)
            or (axis == "x-" and hx <= tx)
            or (axis == "y+" and hy >= ty)
            or (axis == "y-" and hy <= ty)
        )
        if ok:
            state["fired"] = True
            return True
        return False

    return trigger


# --------------------------------------------------------------------------- #
# Resource-event based (uses ProgressTracker; works at high dt)
# --------------------------------------------------------------------------- #


def block_entered(block_id: str) -> Predicate:
    def trigger(s: Snapshot) -> bool:
        return s.tracker is not None and block_id in s.tracker.blocks_entered

    return trigger


def block_vacated(block_id: str) -> Predicate:
    def trigger(s: Snapshot) -> bool:
        return s.tracker is not None and block_id in s.tracker.blocks_vacated

    return trigger


def switch_entered(switch_id: str) -> Predicate:
    def trigger(s: Snapshot) -> bool:
        return s.tracker is not None and switch_id in s.tracker.switches_entered

    return trigger


def switch_vacated(switch_id: str) -> Predicate:
    def trigger(s: Snapshot) -> bool:
        return s.tracker is not None and switch_id in s.tracker.switches_vacated

    return trigger


def block_at_tile_entered(tile: Tuple[int, int]) -> Predicate:
    """Fires once the (dynamically resolved) block owning `tile` is entered.

    Works even when block IDs are auto-split by station insertion: we resolve
    `tile -> block_id` at trigger time, not at journey-definition time.
    """
    state = {"resolved_block_id": None}

    def trigger(s: Snapshot) -> bool:
        if s.tracker is None:
            return False
        if state["resolved_block_id"] is None:
            track = s.network.get_track(*tile)
            if track is None:
                return False
            block_id = getattr(track, "block_id", None)
            if not block_id:
                return False
            state["resolved_block_id"] = block_id
        return state["resolved_block_id"] in s.tracker.blocks_entered

    return trigger


def block_at_tile_vacated(tile: Tuple[int, int]) -> Predicate:
    state = {"resolved_block_id": None}

    def trigger(s: Snapshot) -> bool:
        if s.tracker is None:
            return False
        if state["resolved_block_id"] is None:
            track = s.network.get_track(*tile)
            if track is None:
                return False
            block_id = getattr(track, "block_id", None)
            if not block_id:
                return False
            state["resolved_block_id"] = block_id
        return state["resolved_block_id"] in s.tracker.blocks_vacated

    return trigger


# --------------------------------------------------------------------------- #
# Train motion
# --------------------------------------------------------------------------- #


def train_stopped(after_tick: int = 5, epsilon_kmh: float = 0.5) -> Predicate:
    """Fires when the train is stopped after the given warmup tick threshold.
    The warmup avoids matching the initial tick-0 stop.
    """

    def trigger(s: Snapshot) -> bool:
        return s.tick > after_tick and s.speed_kmh <= epsilon_kmh

    return trigger


def train_stopped_for(
    settle_ticks: int = 30,
    after_tick: int = 5,
    epsilon_kmh: float = 0.05,
) -> Predicate:
    """Fires after the train has been stopped (within `epsilon_kmh`) for
    `settle_ticks` consecutive observations. Use this when a downstream check
    depends on the post-stop state machine settling (e.g. dwell entry / path
    completion / route teardown).
    """
    state = {"first_zero_tick": None}

    def trigger(s: Snapshot) -> bool:
        if s.tick <= after_tick:
            return False
        if s.speed_kmh > epsilon_kmh:
            state["first_zero_tick"] = None
            return False
        if state["first_zero_tick"] is None:
            state["first_zero_tick"] = s.tick
            return False
        return (s.tick - state["first_zero_tick"]) >= settle_ticks

    return trigger


# --------------------------------------------------------------------------- #
# Path / route lifecycle (stateful)
# --------------------------------------------------------------------------- #


def path_completed_after_active() -> Predicate:
    """Fires when an active path was previously observed and is now gone."""
    state = {"saw_active": False, "fired": False}

    def trigger(s: Snapshot) -> bool:
        if state["fired"]:
            return True
        if s.active_path_id is not None:
            state["saw_active"] = True
            return False
        if state["saw_active"] and s.active_path_id is None:
            state["fired"] = True
            return True
        return False

    return trigger


def route_run_removed_after_assigned() -> Predicate:
    state = {"saw_assigned": False, "fired": False}

    def trigger(s: Snapshot) -> bool:
        if state["fired"]:
            return True
        if s.assigned_route_id is not None:
            state["saw_assigned"] = True
            return False
        if state["saw_assigned"] and s.assigned_route_id is None:
            state["fired"] = True
            return True
        return False

    return trigger


# --------------------------------------------------------------------------- #
# Free-form
# --------------------------------------------------------------------------- #


def predicate(fn: Predicate) -> Predicate:
    return fn
