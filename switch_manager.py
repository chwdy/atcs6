"""
Switch manager - the one entry point for changing a switch's position.

Position-change requests route through `SwitchManager.request_position_change(...)`,
which calls `Switch.apply_position_change(...)` for the data update, drives
the throw, and emits the event. Callers must not invoke
`Switch.apply_position_change(...)` directly.
"""

from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from notification_center import Kind, switch_ref

if TYPE_CHECKING:
    from network import RailwayNetwork
    from switch import Switch


class SwitchManager:
    """The one entry point for switch position changes.

    The `Switch` entity stays the owner of `position` writes via its data-only
    helper `Switch.apply_position_change(...)`; the manager is the single
    caller that pairs that write with the throw and the event.
    """

    def __init__(
        self,
        switches: Dict[str, "Switch"],
        network: Optional["RailwayNetwork"] = None,
        *,
        notification_emitter: Optional[Callable] = None,
        environment_active_getter: Optional[Callable[[], bool]] = None,
    ):
        """The manager holds the network plus narrow injected callbacks only;
        it never receives or looks up `SimulationController`.

        Args:
            switches: fallback switch registry, used when ``network`` is None.
            network: optional `RailwayNetwork`; when wired the manager reads
                ``network.switches`` and
                ``network.switch_power_controller`` from it.
            notification_emitter: narrow
                `(operation, kind, message, entities, error=None, payload=None)`
                callback routing a manager-owned event into `NotificationCenter`
                (runtime-wired with source "SwitchManager"); `None` skips emit.
            environment_active_getter: callback returning whether an operating
                environment is active; gates the operating throw in
                `request_position_change(...)`. `None` means "not gated"
                (the pre-runtime / editor default lets the throw proceed).

        A throw that fails to reach the device is reported by
        `SwitchPowerController`, not here (design_new.md "Power", An unissued
        throw is fatal to a hardware-backed session).
        """
        self.switches = switches
        self.network = network
        self.notification_emitter = notification_emitter
        self.environment_active_getter = environment_active_getter

    # ------------------------------------------------------------------
    # Registry accessor (prefer network over fallback dict)
    # ------------------------------------------------------------------

    @property
    def _switch_registry(self) -> Dict[str, "Switch"]:
        if self.network is not None:
            return self.network.switches
        return self.switches

    # ------------------------------------------------------------------
    # Switch position change request (admin / API / UI)
    # ------------------------------------------------------------------

    def request_position_change(
        self, switch_id: str, new_position: int, manual_override: bool = False
    ) -> tuple[bool, str]:
        """Request a position change on `switch_id`.

        Validates and applies the change via `Switch.apply_position_change(...)`,
        then drives the throw. Returns the
        `(success, message)` tuple from the data-layer helper.

        `manual_override` (default False) is the operator-override flag per
        the `disabled` and `manual` availability flags canonical home in
        design_new.md (under TrackResource). Without the override, a
        switch with `Switch.manual == True` rejects the request; with the
        override, the manual filter is bypassed (the disabled filter always
        applies regardless).

        This is the OPERATING throw, so it needs an operating environment: a
        command that reaches a coil belongs to whoever owns the outputs, and
        while no environment is active nobody does. A commissioning bench throw
        drives its own coil and records the result through
        `record_bench_alignment` instead.
        """
        switch = self._switch_registry.get(switch_id)
        if switch is None:
            return (False, f"Unknown switch {switch_id}")
        if (
            self.environment_active_getter is not None
            and not self.environment_active_getter()
        ):
            return (False, "selected operating environment is not active")
        return self._apply_alignment(
            switch,
            int(new_position),
            manual_override=manual_override,
            drive=True,
            operation="request_position_change",
        )

    def record_bench_alignment(self, switch_id: str, position: int) -> tuple[bool, str]:
        """Record the alignment a commissioning bench throw has ALREADY driven
        onto the points (design_new.md "Hardware setup", Self-test and
        commissioning ownership).

        Commissioning owns the device exactly when no operating environment
        does, so `request_position_change` cannot serve a bench throw twice
        over: it refuses while the environment is inactive, which is every
        bench throw, and it would drive the coil a second time through an
        output the commissioning session has taken.

        So this is that same model write without either half — the position
        applies — for a coil the
        caller has already pulsed. It commands nothing: no path through here
        reaches a motor, which is why it may run while the outputs belong to
        commissioning. A bench throw is an operator alignment, so it carries
        the same `manual` override an operator's own throw does.
        """
        switch = self._switch_registry.get(switch_id)
        if switch is None:
            return (False, f"Unknown switch {switch_id}")
        return self._apply_alignment(
            switch,
            int(position),
            manual_override=True,
            drive=False,
            operation="record_bench_alignment",
        )


    def _apply_alignment(
        self,
        switch: "Switch",
        new_position: int,
        *,
        manual_override: bool,
        drive: bool,
        operation: str,
    ) -> tuple[bool, str]:
        """Apply one commanded alignment: the position, the coil when this
        caller owns it, and the event.

        Shared by the operating throw and the commissioning bench throw, which
        differ only in who drives the coil. Everything else an alignment means
        to the rest of the app is the same either way, so it is the same code
        and the two cannot drift into disagreeing about what a thrown switch
        did to the layout.
        """
        if drive and switch.motor_unavailable:
            return False, "switch motor is unavailable"
        switch_id = switch.switch_id
        old_position = int(switch.position)
        success, message = switch.apply_position_change(
            new_position, manual_override=manual_override
        )
        if not success:
            return (False, message)

        if drive:
            # Drive the physical throw (design_new.md "Power", Switch throw
            # output) on every accepted command, including one that names the
            # alignment `Switch.position` already held: the operator asking for
            # MAIN on a switch the app already calls MAIN is how a divergence
            # between the model and the actual points gets corrected, so it has
            # to reach a coil. `_drive_throw` skips only what the output itself
            # last drove there. A throw the hardware could not be commanded to
            # make is reported as a failed command; `Switch.position` is not
            # rolled back, and the service layer surfaces this `(False, reason)`
            # as the operator's `command_result` warning.
            fail_reason = self._drive_throw(switch, int(new_position))
            if fail_reason is not None:
                switch.position_known = False
                return (False, fail_reason)

        # design_new.md "Event log": a confirmed operator alignment CHANGE is a
        # SWITCH_THROWN event. A re-assert of the position already held is not
        # one — the log records alignment changes, and nothing changed.
        if (
            int(new_position) != old_position
            and self.notification_emitter is not None
        ):
            self.notification_emitter(
                operation=operation,
                kind=Kind.INFO,
                message=(
                    f"switch {switch_id} thrown to "
                    f"{'DIVERGE' if int(new_position) else 'MAIN'}"
                ),
                entities=[switch_ref(switch_id)],
                payload={
                    "switch_id": switch_id,
                    "from_position": old_position,
                    "to_position": int(new_position),
                },
            )
        return (True, message)


    def _resolve_switch_power_controller(self):
        """Locate the `SwitchPowerController` live off the owning
        `RailwayNetwork`. Reading ``network.switch_power_controller`` live (not a captured
        handle) is required because the runtime swaps it on every interface
        bind/release. Returns None in editor/unit setups that never wired one
        (the throw is then a no-op).

        `SwitchManager` never reaches `SwitchOutput` itself: the controller is
        that interface's only caller (design_new.md "Power", Switch
        power-controller role)."""
        if self.network is not None:
            return getattr(self.network, "switch_power_controller", None)
        return None

    def _drive_throw(
        self, switch: "Switch", target_state: int, *, force: bool = False
    ) -> Optional[str]:
        """Drive the physical throw (design_new.md "Power", Switch throw output).

        Behaves the same for every caller, and callers issue it on every
        commanded alignment — not only when `Switch.position` changed. What
        decides whether a coil actually fires is the OUTPUT's own record of the
        last throw it made, never the model's belief about where the points are.
        The two are different things: `Switch.position` is what the app intends,
        `get_last_throw` is what it has actually commanded the motor to do, and
        they part company routinely — a scene that assigns positions at build
        time, a bench test that pulses coils directly, a hand-thrown point, a
        power cycle. Gating on the model meant a re-command of the position the
        app already believed in was reported as a success while no coil moved,
        which is precisely the state in which the operator most needs to be able
        to say "go there" and have it happen.

        With no last throw on record — a fresh session, every switch — the
        throw is issued. That is what aligns the physical layout to the model on
        the first command that names each switch.

        `force` drops even that skip, and activation uses it. Having
        driven a coil once is not evidence the points seated: a solenoid can
        stall, bounce back, or be moved by hand afterwards, and the motor
        reports nothing either way. A train is about to run over these points,
        and points sitting on the wrong side feed that train's `track voltage`
        down the other branch, lighting the occupancy detector on a block the
        train is nowhere near. Re-driving the coil costs one pulse and removes
        the whole failure, so acquisition always throws rather than reasoning
        about whether it needs to.

        The throw is blind: a solenoid point motor reports nothing back, so
        there is no confirmation step and `None` is not a claim that the points
        moved. The one failure this can report is a command that never left the
        software — no hardware binding for this switch, or its chip is missing —
        which `SwitchPowerController.set_throw` signals by returning False. When
        no controller is wired at all the throw is a no-op and this returns
        `None`.
        """
        if switch.motor_unavailable:
            return "switch motor is unavailable"
        controller = self._resolve_switch_power_controller()
        if controller is None:
            return None
        if not force:
            last = controller.get_last_throw(switch.switch_id)
            if last is not None and int(last) == int(target_state):
                return None                # the motor was last driven there
        # `SwitchPowerController.set_throw` reports an unissued throw itself
        # (the warning and the hardware-failure latch); this manager only reads
        # the False return and passes it to its caller.
        if not controller.set_throw(switch.switch_id, int(target_state)):
            return "throw not issued (no hardware binding or output failure)"
        return None

    def throw_all_to_layout(self) -> List[str]:
        """Throw every switch once to the position the layout shows, at the
        activation of Virtual Hardware or Real Hardware (design_new.md "Switch
        entity", Activation throws every switch to the layout).

        A fresh hardware session does not know where any points lie; one pulse
        to `Switch.position` puts them there, so each switch it reaches becomes
        known. Disabled and manual switches are left alone: automation does not
        drive them. The write is the position the model already holds, so
        nothing changes in the model and no flip is counted. Returns the ids
        thrown.
        """
        thrown: List[str] = []
        skipped: List[str] = []
        failed: List[str] = []
        for switch_id in sorted(self._switch_registry):
            switch = self._switch_registry[switch_id]
            if switch.disabled or switch.manual or switch.motor_unavailable:
                skipped.append(switch_id)
                continue
            if self._drive_throw(switch, int(switch.position), force=True) is None:
                switch.position_known = True
                thrown.append(switch_id)
            else:
                failed.append(switch_id)
        if self.notification_emitter is not None and (thrown or failed):
            self.notification_emitter(
                operation="throw_all_to_layout",
                kind=Kind.INFO if not failed else Kind.WARNING,
                message=(
                    f"activation threw {len(thrown)} switch(es) to the layout's "
                    f"positions"
                    + (f"; {len(failed)} could not be thrown" if failed else "")
                ),
                entities=[switch_ref(switch_id) for switch_id in thrown + failed],
                payload={"thrown": thrown, "skipped": skipped, "failed": failed},
            )
        return thrown


