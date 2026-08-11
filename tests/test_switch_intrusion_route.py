"""Switch intrusion check is route-aware (design_new.md "Canonical runtime procedures").

A train physically entering a switch must hold a lock *granting the route it
occupies* — not merely any main lock on the switch. A lock recorded for a
different route does not protect the traversal and is a fatal intrusion. Before
the fix, the check only verified that the `(path_id, None)` main-lock key was
present, so a train that locked route A but physically entered route B passed
as a clean entry.
"""

from types import SimpleNamespace

from switch import Switch
from switch_manager import SwitchManager
from runtime_payloads import SwitchLockClaim, FatalConflictKind


def _manager_with_locked_switch(locked_route):
    """A SwitchManager holding one switch with a main lock recorded for
    `locked_route`, plus a sim stub that captures fatal-conflict payloads."""
    switch = Switch(switch_id="SW_1", x=0, y=0)
    assert switch.acquire_lock(
        SwitchLockClaim(
            path_id="P1",
            train_id="T1",
            locked_state=0,
            source_switch_id=None,
            route=locked_route,
        )
    )
    # The lock recorded its route for the route-grant comparison.
    assert switch.lock_routes.get(("P1", None)) == locked_route

    captured = []
    sim = SimpleNamespace(
        block_manager=SimpleNamespace(on_switch_position_changed=lambda s: None),
        network=None,
        path_dispatcher=None,
        enter_failed_runtime=lambda payload: captured.append(payload),
    )
    manager = SwitchManager(
        switches={"SW_1": switch}, simulation_controller=sim, network=None
    )
    return manager, switch, captured


def test_entry_on_locked_route_is_clean():
    """Entering on the same route the lock was acquired for is a clean entry."""
    route = frozenset({"N", "E"})
    manager, switch, captured = _manager_with_locked_switch(route)

    ok = manager.on_physical_entry("SW_1", "T1", "P1", route)

    assert ok is True
    assert captured == []  # no fatal conflict
    assert switch.route_owners.get(route) == "T1"


def test_entry_on_different_route_than_lock_is_intrusion():
    """Holding a lock for route A but physically entering route B is a fatal
    SWITCH_INTRUSION — the lock does not grant the occupied route."""
    manager, switch, captured = _manager_with_locked_switch(frozenset({"N", "E"}))

    ok = manager.on_physical_entry("SW_1", "T1", "P1", frozenset({"E", "S"}))

    assert ok is False
    assert len(captured) == 1
    assert captured[0].conflict_kind == FatalConflictKind.SWITCH_INTRUSION
    # No occupancy state was written for the intruding entry.
    assert switch.route_owners == {}


def test_entry_with_routeless_main_lock_is_granted():
    """A legacy / routeless main lock (no recorded route) is treated as a
    generic grant, so the entry is clean (no new false intrusion)."""
    switch = Switch(switch_id="SW_1", x=0, y=0)
    # Acquire a main lock without a route (lock_routes stays empty for the key).
    assert switch.acquire_lock(
        SwitchLockClaim(path_id="P1", train_id="T1", locked_state=0)
    )
    assert switch.lock_routes.get(("P1", None)) is None

    captured = []
    sim = SimpleNamespace(
        block_manager=SimpleNamespace(on_switch_position_changed=lambda s: None),
        network=None,
        path_dispatcher=None,
        enter_failed_runtime=lambda payload: captured.append(payload),
    )
    manager = SwitchManager(
        switches={"SW_1": switch}, simulation_controller=sim, network=None
    )

    ok = manager.on_physical_entry("SW_1", "T1", "P1", frozenset({"N", "E"}))

    assert ok is True
    assert captured == []
