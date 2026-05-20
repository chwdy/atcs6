"""
Switch State Manager - single source of truth for switch lock and physical
occupancy events.

`SwitchManager` is the database-style lock manager for switch resources and
the single serialization point for atomic multi-switch lock acquisition,
release, and physical-entry handling. It mirrors `BlockManager`'s contract:

- Path-driven multi-switch lock acquire is atomic / all-or-nothing.
- Path-driven release is keyed by `path_id` and walks the registry once.
- Physical-entry handling owns the flank-lock-to-train-protection conversion
  and the fatal-conflict reporting path that earlier lived inline on
  `PathDispatcher`.
- Position-change requests route through `SwitchManager.request_position_change(...)`,
  which calls `Switch.apply_position_change(...)` for the data update and
  then notifies `BlockManager.on_switch_position_changed(...)`. Callers must
  not invoke `Switch.apply_position_change(...)` directly; the manager is
  the single entry point so the cross-resource notification is never
  skipped.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, TYPE_CHECKING

from runtime_payloads import (
    FatalPhysicalEntryConflict,
    FatalConflictKind,
    SwitchLockClaim,
)

if TYPE_CHECKING:
    from network import RailwayNetwork
    from switch import Switch


# ============================================================================
# Canonical value types
# ============================================================================


@dataclass
class SwitchLockRequest:
    """Canonical value type for one requested switch lock.

    Per design_new.md "SwitchManager" — `source_switch_id` is None for
    primary (main) claims and the originating switch_id for flank claims.
    The role is derived from this field, not stored separately.
    """

    switch_id: str
    locked_state: int
    source_switch_id: Optional[str] = None


@dataclass
class LockAcquireResult:
    """Result of an atomic `acquire_locks_for_segment(...)` call.

    `success == True` means every requested lock was recorded. `success ==
    False` means the call rejected before any persistent state change; locks
    that succeeded earlier in the call are rolled back inside the method.
    """

    success: bool
    failed_switch_id: Optional[str] = None
    reason: Optional[str] = None
    locked_switch_ids: List[str] = field(default_factory=list)


# ============================================================================
# Manager
# ============================================================================


class SwitchManager:
    """Centralized switch lock manager.

    Owns the dispatcher-facing API for path-level switch lock acquire/release,
    tail-vacate, clean physical entry, and switch-position-change requests.
    Mirrors `BlockManager`. The underlying `Switch` entity stays the canonical
    owner of `position` writes via its data-only helper
    `Switch.apply_position_change(...)`; the manager is the single caller that
    pairs that write with the cross-resource notification to `BlockManager`.
    """

    def __init__(
        self,
        switches: Dict[str, "Switch"],
        simulation_controller: Optional[object] = None,
        network: Optional["RailwayNetwork"] = None,
    ):
        self.switches = switches
        self.simulation_controller = simulation_controller
        self.network = network

    # ------------------------------------------------------------------
    # Registry accessor (prefer network over fallback dict)
    # ------------------------------------------------------------------

    @property
    def _switch_registry(self) -> Dict[str, "Switch"]:
        if self.network is not None:
            return self.network.switches
        return self.switches

    # ------------------------------------------------------------------
    # Path-level multi-switch acquire (all-or-nothing)
    # ------------------------------------------------------------------

    def acquire_locks_for_segment(
        self,
        requests: List[SwitchLockRequest],
        path_id: str,
        train_id: str,
    ) -> LockAcquireResult:
        """Atomically acquire every requested switch lock and align the
        switch's physical position to the requested state.

        Acquiring a lock with `locked_state` only RECORDS the path's
        requirement; it does not by itself move the switch. If the switch is
        currently in a different position and not physically occupied, the
        manager throws it via `apply_position_change(...)` here so the train
        can actually traverse the locked segment when it arrives. If a switch
        is occupied by another train, `Switch.acquire_lock` will already
        have rejected the request before we reach the throw.

        On any failure, release the locks recorded in this call and return a
        failure result.
        """
        recorded: List[SwitchLockRequest] = []
        for req in requests:
            switch = self._switch_registry.get(req.switch_id)
            if switch is None:
                self._rollback(recorded, path_id)
                return LockAcquireResult(
                    success=False,
                    failed_switch_id=req.switch_id,
                    reason="unknown_switch",
                )
            claim = SwitchLockClaim(
                path_id=path_id,
                train_id=train_id,
                locked_state=req.locked_state,
                source_switch_id=req.source_switch_id,
            )
            ok = switch.acquire_lock(claim)
            if not ok:
                self._rollback(recorded, path_id)
                return LockAcquireResult(
                    success=False,
                    failed_switch_id=req.switch_id,
                    reason="acquire_lock_rejected",
                )
            # Align the physical position with the locked state. The lock
            # accept already ruled out the unsafe "switch occupied by another
            # train + position change required" case (Switch.acquire_lock
            # rejects it). Misalignment with no occupant means the path
            # acquired the right to throw — do it now so clearance walks and
            # train physics see a usable route.
            if int(switch.position) != int(req.locked_state):
                ok_throw, msg = switch.apply_position_change(int(req.locked_state))
                if not ok_throw:
                    # Roll back this lock and any earlier ones acquired in
                    # this segment so the request is fully atomic.
                    switch.release_lock(
                        path_id=path_id,
                        source_switch_id=req.source_switch_id,
                    )
                    self._rollback(recorded, path_id)
                    return LockAcquireResult(
                        success=False,
                        failed_switch_id=req.switch_id,
                        reason=f"apply_position_change_rejected: {msg}",
                    )
                block_manager = self._resolve_block_manager()
                if block_manager is not None:
                    block_manager.on_switch_position_changed(req.switch_id)
            # Per design_new.md, `Switch.locks` is the canonical store. The
            # UI fields `track.locked_by_path` / `track.locked_state` /
            # `track.switch_state` are derived properties on `TrackSegment`
            # that resolve through `network.switches[switch_id]`; no manual
            # Track-side mirror is needed.
            recorded.append(req)
        return LockAcquireResult(
            success=True,
            locked_switch_ids=[r.switch_id for r in recorded],
        )

    def _rollback(
        self, recorded: List[SwitchLockRequest], path_id: str
    ) -> None:
        for req in recorded:
            switch = self._switch_registry.get(req.switch_id)
            if switch is None:
                continue
            switch.release_lock(path_id, req.source_switch_id)

    # ------------------------------------------------------------------
    # Path-level release
    # ------------------------------------------------------------------

    def release_path_locks(self, path_id: str) -> List[str]:
        """Release every lock owned by `path_id` across every switch."""
        released: List[str] = []
        for switch in self._switch_registry.values():
            keys = [k for k in list(switch.locks.keys()) if k[0] == path_id]
            if not keys:
                continue
            for (_pid, source_id) in keys:
                switch.release_lock(path_id, source_id)
            released.append(switch.switch_id)
        return released

    def release_path_locks_for_switches(
        self, switch_ids: List[str], path_id: str
    ) -> List[str]:
        """Release `path_id`'s locks scoped to the given switch_ids only."""
        released: List[str] = []
        for sid in switch_ids:
            switch = self._switch_registry.get(sid)
            if switch is None:
                continue
            keys = [k for k in list(switch.locks.keys()) if k[0] == path_id]
            if not keys:
                continue
            for (_pid, source_id) in keys:
                switch.release_lock(path_id, source_id)
            released.append(sid)
        return released

    # ------------------------------------------------------------------
    # Tail-vacate
    # ------------------------------------------------------------------

    def on_switch_tail_vacated(self, switch_id: str, train_id: str) -> None:
        """Switch tail-vacate entry — called by PathDispatcher per event."""
        switch = self._switch_registry.get(switch_id)
        if switch is None:
            return
        switch.on_train_tail_vacated(train_id, network=self.network)

    # ------------------------------------------------------------------
    # Switch position change request (admin / API / UI)
    # ------------------------------------------------------------------

    def request_position_change(
        self, switch_id: str, new_position: int
    ) -> tuple[bool, str]:
        """Request a position change on `switch_id`.

        Validates and applies the change via `Switch.apply_position_change(...)`,
        then notifies `BlockManager.on_switch_position_changed(switch_id)` so
        the block reachability graph stays in sync. Returns the
        `(success, message)` tuple from the data-layer helper.
        """
        switch = self._switch_registry.get(switch_id)
        if switch is None:
            return (False, f"Unknown switch {switch_id}")

        success, message = switch.apply_position_change(new_position)
        if not success:
            return (False, message)

        block_manager = self._resolve_block_manager()
        if block_manager is not None:
            block_manager.on_switch_position_changed(switch_id)
        return (True, message)

    def _resolve_block_manager(self):
        """Locate the BlockManager — preferred via SimulationController, with
        a network fallback for editor/test setups that wired the manager
        only on the network object."""
        if self.simulation_controller is not None:
            bm = getattr(self.simulation_controller, "block_manager", None)
            if bm is not None:
                return bm
        if self.network is not None:
            return getattr(self.network, "block_manager", None)
        return None

    # ------------------------------------------------------------------
    # Clean physical entry + flank conversion + fatal-conflict reporting
    # ------------------------------------------------------------------

    def on_physical_entry(
        self,
        switch_id: str,
        train_id: str,
        path_id: Optional[str],
    ) -> bool:
        """Record a clean physical entry of `train_id` into `switch_id`.

        Returns True on clean entry. Reports a fatal physical-entry conflict
        and returns False on conflict (disabled switch entry, or occupancy by
        a different train). When `path_id` is provided, also consumes the
        path-owned primary lock for this switch and converts each required
        flank-protection claim on neighbours into a train-owned
        `train_protection_claim`.
        """
        switch = self._switch_registry.get(switch_id)
        if switch is None:
            return False

        if getattr(switch, "disabled", False):
            self._report_fatal_conflict(
                switch_id=switch_id,
                train_id=train_id,
                conflict_kind=FatalConflictKind.DISABLED_RESOURCE_ENTRY,
                prior_owner_train_id=None,
            )
            return False

        current = getattr(switch, "occupied_by", None)
        if current is not None and current != train_id:
            self._report_fatal_conflict(
                switch_id=switch_id,
                train_id=train_id,
                conflict_kind=FatalConflictKind.SWITCH_OCCUPIED_BY_OTHER_TRAIN,
                prior_owner_train_id=current,
            )
            return False

        switch.occupied_by = train_id

        if path_id:
            self._convert_locks_on_entry(switch, train_id, path_id)
        return True

    def _convert_locks_on_entry(
        self, switch_entity: "Switch", train_id: str, path_id: str
    ) -> None:
        """Consume the path-owned primary lock for this switch and convert
        each required flank-protection claim on neighbours into a train-owned
        `train_protection_claim`. Per design_new.md, the lock key is
        `(path_id, source_switch_id)` where `source_switch_id is None` for
        primary claims and the originating switch_id for flank claims.
        After the train physically occupies this switch, the neighbour's
        flank lock is replaced by a `(train_id, this_switch_id, locked_state)`
        entry in `train_protection_claims`, later cleared on tail-vacate via
        `Switch.release_train_protection_from(...)`.
        """
        primary_key = (path_id, None)
        if primary_key in switch_entity.locks:
            switch_entity.release_lock(path_id, None)

        for neighbour_switch_id, _blocking_state in list(
            getattr(switch_entity, "flank_protection", []) or []
        ):
            neighbour = self._switch_registry.get(neighbour_switch_id)
            if neighbour is None:
                continue
            flank_key = (path_id, switch_entity.switch_id)
            if flank_key not in neighbour.locks:
                continue
            _claim_train_id, locked_state = neighbour.locks[flank_key]
            neighbour.release_lock(path_id, switch_entity.switch_id)
            claim = (train_id, switch_entity.switch_id, int(locked_state))
            if claim not in neighbour.train_protection_claims:
                neighbour.train_protection_claims.append(claim)

    def _report_fatal_conflict(
        self,
        *,
        switch_id: str,
        train_id: str,
        conflict_kind: FatalConflictKind,
        prior_owner_train_id: Optional[str],
    ) -> None:
        """Forward a fatal switch-entry conflict to the runtime.

        Mirrors `BlockManager._report_fatal_conflict`. Receiver issues must
        not mask the underlying conflict, so this helper does not swallow
        exceptions raised by the wired controller.
        """
        if self.simulation_controller is None:
            return
        active_path = None
        dispatcher = getattr(self.simulation_controller, "path_dispatcher", None)
        if dispatcher is not None:
            getter = getattr(dispatcher, "get_active_path", None)
            if callable(getter):
                active_path = getter(train_id)
        payload = FatalPhysicalEntryConflict(
            resource_type="switch",
            resource_id=switch_id,
            incoming_train_id=train_id,
            prior_owner_train_id=prior_owner_train_id,
            conflict_kind=conflict_kind,
            incoming_path_id=active_path.path_id if active_path else None,
        )
        self.simulation_controller.enter_failed_runtime(payload)
