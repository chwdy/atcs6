"""
The runtime: `SimulationController` owns the session's hardware interfaces,
the safe state, the hardware-failure latch and the runtime loop's passes
(design_v2.md "Terms", The runtime). Each pass runs the runtime step through
`run_train_step`, which hands the detectors' reading to `TrainRuntime`.
"""

import logging
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import operation_environment
from block_occupancy import BlockOccupancyDetector, InactiveOccupancyInput
from block_power import BlockPowerController
from detector_reading_log import DetectorReadingLog
from enums import BlockState, OperationMode
from hardware.safety import SafeStateError
from hardware.watchdog import WatchdogTripRecord
from network import RailwayNetwork
from notification_center import (
    EntityRef,
    Kind,
    NotificationCenter,
    block_ref,
    make_notification_emitter,
    switch_ref,
    train_ref,
)
from runtime_payloads import ErrorRecord, HardwareFailure
from switch_power import (
    SimulatedSwitchDriver,
    SwitchOutputOwner,
    SwitchPowerController,
)
from train_runtime import TrainRuntime

if TYPE_CHECKING:
    from session_state import SessionState

logger = logging.getLogger(__name__)


def _hardware_entity_ref(payload: HardwareFailure) -> Optional[EntityRef]:
    """The block or switch a hardware failure names, when it names one. A lost
    controller or a failed detector transaction names no single entity."""
    if not payload.resource_id:
        return None
    if payload.component == "switch_output":
        return switch_ref(payload.resource_id)
    return block_ref(payload.resource_id)


class SimulationController(SwitchOutputOwner):
    """The runtime (design_v2.md "Terms", The runtime).

    It holds the operating environment's interfaces, drives the outputs only
    through `BlockPowerController` and `SwitchPowerController`, latches a
    hardware failure, and runs the runtime step on each pass of the runtime
    loop.
    """

    def __init__(
        self,
        network: RailwayNetwork,
        notification_center: Optional[NotificationCenter] = None,
        operation_mode: Any = OperationMode.VIRTUAL_HARDWARE,
        input_interface: Optional[Any] = None,
        block_output_interface: Optional[Any] = None,
        switch_output_interface: Optional[Any] = None,
        environment_active: bool = False,
        activation_issues: Optional[List[Dict[str, Any]]] = None,
    ):
        self.network = network

        # The operating environment (design_v2.md "Operating environment and
        # activation"). No environment is active until an activation builds its
        # interfaces; until then the placeholder interfaces bound below are
        # never driven.
        self.operation_mode: OperationMode = operation_environment.parse(operation_mode)
        self.environment_active = bool(environment_active)
        self.activation_issues = list(activation_issues or [])

        # The one event stream (design_v2.md "Notifications and errors").
        self.notification_center: NotificationCenter = (
            notification_center if notification_center is not None else NotificationCenter()
        )

        # The hardware-failure latch (design_v2.md "Safe state, watchdog and
        # hardware failure"). It keeps only the flag: the diagnostic lives in
        # the event stream.
        self.hardware_failure: bool = False

        # Wall time the current pass started: the pass's `wall_time`, which the
        # detector reading log writes and the hold time is measured on.
        self._pass_started_wall_time: Optional[float] = None

        # The per-pass record of what every block detector read (design_v2.md
        # "Detector reading log"). Off until `enable(...)`: the real server
        # enables it at boot; tests leave it off.
        self.detector_reading_log = DetectorReadingLog(
            notification_center=self.notification_center
        )

        # Bumped by every safe-state entry, so a Setup tool can tell that the
        # outputs were dropped under it.
        self.safe_state_generation: int = 0
        # The Real Hardware loss-of-control watchdog's trip record, wired from
        # the active output interface in `_bind_interfaces`.
        self._watchdog_trip_record: Optional[WatchdogTripRecord] = None

        # The block and switch managers, and the narrow callbacks they need.
        network._ensure_managers()
        self.block_manager = network.block_manager
        self.switch_manager = network.switch_manager
        self.block_manager.notification_emitter = make_notification_emitter(
            self.notification_center, "BlockManager"
        )
        self.switch_manager.notification_emitter = make_notification_emitter(
            self.notification_center, "SwitchManager"
        )
        self.switch_manager.environment_active_getter = lambda: self.environment_active

        # The trains and the runtime step (design_v2.md "Runtime step"). It
        # writes block voltages through whichever `power_controller` is bound
        # at the time, because activation builds a fresh one.
        self.train_runtime = TrainRuntime(
            network,
            set_block_voltage=lambda block_id, volts: self.power_controller.set_block_voltage(
                block_id, volts
            ),
            emit=self._emit_train_event,
            throw_switch=lambda switch_id, position: network.switch_manager.request_position_change(
                switch_id, position
            )[0],
            outputs_active=lambda: not self.runtime_halted,
        )
        self._train_step_was_live = False

        self._bind_interfaces(
            input_interface, block_output_interface, switch_output_interface
        )

    # ------------------------------------------------------------------
    # The runtime step (design_v2.md "Runtime step")
    # ------------------------------------------------------------------

    def _emit_train_event(
        self,
        operation: str,
        kind: str,
        train_id: str,
        message: str,
        payload: Dict[str, Any],
    ) -> None:
        """Put one train event on the event stream (design_v2.md "Event
        log", Train events)."""
        self.notification_center.emit(
            source="SimulationController",
            operation=operation,
            kind=Kind.WARNING if kind == "warning" else Kind.INFO,
            message=message,
            entities=[train_ref(train_id)],
            payload=payload,
        )

    def note_session_live(self, live: bool) -> None:
        """Tell the train runtime whether the session state is Live. The
        runtime loop calls this on every iteration, so the step on which the
        session leaves Live stops every train and ends every claim
        (design_v2.md "Trains", Leaving Live stops every train)."""
        if self._train_step_was_live and not live:
            self.train_runtime.leave_live(time.time())
        self._train_step_was_live = live

    def run_train_step(self, live: bool, wall_time: Optional[float] = None) -> None:
        """One runtime step: read the detectors, write the reading to the
        detector reading log, then hand the blocks that read occupied to the
        train runtime (design_v2.md "Runtime step").

        While `live` is False the step updates each train's blocks only (the
        stopped pass), so trains still appear and disappear on the page, and
        nothing is claimed or powered.

        `wall_time` is the pass's start time. The runtime loop leaves it out,
        so the step samples the clock; a replay passes the recorded value, so
        the hold time is measured the same way it was on the rig
        (design_v2.md "Operating environment and activation", The hold time is
        measured on pass wall time).
        """
        self._pass_started_wall_time = time.time() if wall_time is None else wall_time
        if self.runtime_halted or not self.occupancy_is_real:
            return
        if live and not self.environment_active:
            return

        detector = self.block_occupancy_detector
        try:
            detector.step()
        except Exception as exc:  # noqa: BLE001 - a failed detector read is fatal
            self.enter_hardware_failure(
                HardwareFailure(
                    component="block_occupancy",
                    operation="read",
                    message=(
                        f"block detector read failed: {type(exc).__name__}: {exc}"
                    ),
                )
            )
            return
        self.detector_reading_log.record_pass(
            pass_kind="tick" if live else "stopped",
            wall_time=self._pass_started_wall_time,
            operating_delta_seconds=0.0,
            detector=detector,
            environment=self.operation_mode.value if self.environment_active else None,
        )
        self.train_runtime.step(
            detector.confirmed_occupied, self._pass_started_wall_time, live
        )

    # ------------------------------------------------------------------
    # Safe state and the watchdog (design_v2.md "Safe state, watchdog and
    # hardware failure")
    # ------------------------------------------------------------------

    def enter_safe_state(self) -> None:
        """Put every output into its safe state: every block at 0 V and every
        point motor de-energized.

        This is the sequence itself. Points are dropped before block voltages,
        and each half is its controller's non-recording device write, so this
        call touches no recorded field and is safe from any thread. Its direct
        callers are the Real Hardware `OutputWatchdog` daemon and process
        shutdown; every runtime-thread caller goes through
        `enter_output_safe_state()`.
        """
        if self.switch_power_controller is not None:
            self.switch_power_controller.de_energize_device_outputs()
        if self.power_controller is not None:
            self.power_controller.zero_device_outputs()

    def enter_output_safe_state(self) -> None:
        """The runtime-thread entry to the safe state: advance
        `safe_state_generation`, run `enter_safe_state()`, then set every
        block's recorded voltage to 0. Every caller holds
        `AppState.simulation_lock`."""
        self.safe_state_generation += 1
        try:
            self.enter_safe_state()
        finally:
            if self.power_controller is not None:
                self.power_controller.reconcile_recorded_voltages()

    def consume_watchdog_trip(self) -> None:
        """Consume a loss-of-control watchdog trip once, on the runtime thread
        (design_v2.md "Runtime loop", Every time round consumes a watchdog
        trip).

        The watchdog daemon dropped the outputs below the model and recorded
        only whether the drop worked. When it failed, this is a hardware
        failure. When it worked, the runtime advances `safe_state_generation`,
        sets every block's recorded voltage to 0, and emits one warning; the
        session goes on.
        """
        trip_record = self._watchdog_trip_record
        if trip_record is None:
            return
        failed = trip_record.consume()
        if failed is None:
            return
        if failed:
            self.enter_hardware_failure(
                HardwareFailure(
                    component="output_watchdog",
                    operation="enter_safe_state",
                    message=(
                        "loss-of-control watchdog could not complete the "
                        "safe-state drop; the rail may still be energized"
                    ),
                )
            )
            return
        self.safe_state_generation += 1
        if self.power_controller is not None:
            self.power_controller.reconcile_recorded_voltages()
        self.notification_center.emit(
            source="SimulationController",
            operation="consume_watchdog_trip",
            kind=Kind.WARNING,
            message=(
                "loss-of-control watchdog drove every output to the safe "
                "state after the control loop stopped refreshing them"
            ),
            entities=[],
            event_time=time.time(),
        )

    def try_enter_safe_state(self) -> Optional[List[str]]:
        """`enter_safe_state()` for callers that must keep going when the rig
        cannot be commanded at all.

        Returns `None` when every output was safed, or the per-chip failures.
        `OutputSafety` attempts every chip before it raises, so the list is
        what did not answer.
        """
        try:
            self.enter_safe_state()
        except SafeStateError as exc:
            logger.exception("safe-state command failed")
            return list(exc.failures)
        except Exception as exc:  # noqa: BLE001 - the caller decides what it means
            logger.exception("safe-state command failed")
            return [str(exc)]
        return None

    @property
    def runtime_halted(self) -> bool:
        """True while a hardware failure is latched: the runtime stays off the
        failed device until an activation succeeds."""
        return self.hardware_failure

    def enter_hardware_failure(self, payload: HardwareFailure) -> None:
        """Enter the safe state, latch `hardware_failure`, and leave no
        environment active (design_v2.md "Session state", A hardware I/O
        failure).

        Idempotent, because one bus failure can be observed by several output
        calls in the same step; the first failure is the one reported. The
        flag is latched even if the safe-state write itself fails.
        """
        if self.runtime_halted:
            return
        safe_state_error: Optional[str] = None
        try:
            self.enter_output_safe_state()
        except Exception as exc:  # noqa: BLE001 - the failure still must latch
            safe_state_error = str(exc)
            logger.exception("hardware safe-state command failed")
        finally:
            self.hardware_failure = True
            self.environment_active = False

        entity = _hardware_entity_ref(payload)
        self.notification_center.emit(
            source="SimulationController",
            operation="enter_hardware_failure",
            kind=Kind.ERROR,
            message=(
                f"hardware failure in {payload.component}: every train is "
                "halted and the outputs are in their safe state"
            ),
            entities=[entity] if entity is not None else [],
            error=ErrorRecord(payload.message, payload.component),
            event_time=time.time(),
            payload={
                "component": payload.component,
                "operation": payload.operation,
                "resource_id": payload.resource_id,
                "message": payload.message,
                "safe_state_error": safe_state_error,
                "requires_new_activation": True,
            },
        )

    # ------------------------------------------------------------------
    # Activation (design_v2.md "Operating environment and activation")
    # ------------------------------------------------------------------

    def activate_interfaces(
        self,
        environment: OperationMode,
        input_interface: Optional[Any],
        block_output_interface: Optional[Any],
        switch_output_interface: Optional[Any],
    ) -> None:
        """Replace the whole environment boundary while not Live."""
        self.enter_output_safe_state()
        self.environment_active = False
        self.operation_mode = operation_environment.parse(environment)

        self._bind_interfaces(
            input_interface, block_output_interface, switch_output_interface
        )
        # A fresh session has thrown no coil, so the points may lie either way
        # until activation throws them (design_v2.md "Operating environment
        # and activation", Activation sets what v2 knows about each switch).
        for switch in self.network.switches.values():
            switch.position_known = False
        self.activation_issues = []
        self.environment_active = True

    def finish_activation(self) -> None:
        """The rest of a successful activation (design_v2.md "Operating
        environment and activation"): remove every train, throw every switch
        once to the position the layout shows, and clear `hardware_failure`,
        because the activation rebuilt every interface and the device answers
        again."""
        # Trains are not carried from one activation to the next; the runtime
        # step finds them again from the detectors (design_v2.md "Trains").
        self.train_runtime.clear()
        if self.switch_manager is not None:
            self.switch_manager.throw_all_to_layout()
        if self.hardware_failure:
            self.hardware_failure = False
            self.notification_center.emit(
                source="SimulationController",
                operation="finish_activation",
                kind=Kind.INFO,
                message="hardware failure cleared by a successful activation",
                event_time=time.time(),
            )

    def release_interfaces(self) -> None:
        """Drop the hardware boundary once its device handle is released.

        Every chip of a session is built on one open device, so closing that
        device leaves each driver holding a handle it can no longer write
        through. Rebinding to the placeholder interfaces keeps a released
        device out of the next safe state. Does no I/O of its own.
        """
        self.environment_active = False
        self._bind_interfaces(None, None, None)

    def _wire_power_controllers(self) -> None:
        """Hand the block and switch power controllers their dependencies: the
        event sink, the hardware-failure latch, and (switch side) the block
        power controller whose blocks feed the switches."""
        self.power_controller.wire_dependencies(
            notification_center=self.notification_center,
            enter_hardware_failure=self.enter_hardware_failure,
        )
        self.switch_power_controller.wire_dependencies(
            notification_center=self.notification_center,
            enter_hardware_failure=self.enter_hardware_failure,
            power_controller=self.power_controller,
        )

    def _bind_interfaces(
        self,
        input_interface: Optional[Any],
        block_output_interface: Optional[Any],
        switch_output_interface: Optional[Any],
    ) -> None:
        """Wire the three interfaces and the objects that read them, using the
        placeholder for each one passed `None`. Pure rebinding: no device I/O
        and no session flags."""
        self._watchdog_trip_record = None
        for iface in (block_output_interface, switch_output_interface):
            watchdog = getattr(iface, "_watchdog", None) if iface is not None else None
            trip_record = (
                getattr(watchdog, "trip_record", None) if watchdog is not None else None
            )
            if trip_record is not None:
                self._watchdog_trip_record = trip_record
                break

        self.power_controller = (
            BlockPowerController(self.network, output_interface=block_output_interface)
            if block_output_interface is not None
            else BlockPowerController(self.network)
        )
        self.network.power_controller = self.power_controller
        self.switch_output_interface = (
            switch_output_interface
            if switch_output_interface is not None
            else SimulatedSwitchDriver()
        )
        self.network.switch_output_interface = self.switch_output_interface
        self.switch_power_controller = SwitchPowerController(
            self.network,
            output_interface=self.switch_output_interface,
        )
        self._wire_power_controllers()
        self.network.switch_power_controller = self.switch_power_controller
        self.input_interface = (
            input_interface
            if input_interface is not None
            else InactiveOccupancyInput(self.network)
        )
        self.network.input_interface = self.input_interface
        self.block_occupancy_detector = BlockOccupancyDetector(
            network=self.network,
            input_interface=self.input_interface,
            notify=lambda operation, message: self.notification_center.emit(
                source="BlockOccupancyDetector",
                operation=operation,
                kind=Kind.WARNING,
                message=message,
            ),
        )

    def deactivate_environment(
        self, issues: List[Dict[str, Any]], environment: Optional[OperationMode] = None
    ) -> Optional[List[str]]:
        """Leave the outputs safe and record why no environment is active.

        Returns whatever `try_enter_safe_state` reported: `None` when the
        outputs are safe, else the chips that did not answer. The bookkeeping
        runs either way, so a session that refuses to operate still says why.
        """
        failures = self.try_enter_safe_state()
        if environment is not None:
            self.operation_mode = operation_environment.parse(environment)
        self.environment_active = False
        self.activation_issues = list(issues)
        return failures

    @property
    def occupancy_is_real(self) -> bool:
        """True while a detector interface is open: an activation (or Setup
        on the same environment) bound one. False while the placeholder input
        is bound, when the runtime makes no pass."""
        return not isinstance(self.input_interface, InactiveOccupancyInput)


def add_train_runtime_fields(
    payload: Dict[str, Any], sim_controller: SimulationController
) -> None:
    """Add the live fields to a snapshot (design_v2.md "Live fields").

    - each block gets `state`, `occupied_by`, `reads_occupied`, `train_id`,
      `claimed_by_train_id` and `voltage`;
    - each switch gets `held` and `alternate_next`;
    - `running_trains` is the map from `train_id` to a train entry.

    Every field is worked out here on each read from the train runtime;
    nothing stores a second copy.
    """
    runtime = sim_controller.train_runtime
    held = runtime.held_switch_ids()
    for block_id, entry in (payload.get("blocks") or {}).items():
        owner = runtime.train_of_block(block_id)
        claimer = runtime.claimer_of_block(block_id)
        if owner is not None or block_id in runtime.occupied:
            entry["state"] = BlockState.OCCUPIED.value
            entry["occupied_by"] = owner.train_id if owner is not None else None
        elif claimer is not None:
            entry["state"] = BlockState.RESERVED.value
        else:
            entry["state"] = BlockState.FREE.value
        entry["reads_occupied"] = block_id in runtime.occupied
        entry["train_id"] = owner.train_id if owner is not None else None
        entry["claimed_by_train_id"] = claimer.train_id if claimer is not None else None
        entry["voltage"] = float(
            sim_controller.power_controller.get_recorded_voltage(block_id)
        )
    switches = payload.get("switches") or {}
    for switch_id, entry in (switches.items() if isinstance(switches, dict) else ()):
        entry["held"] = switch_id in held
        entry["alternate_next"] = runtime.alternate_next.get(switch_id)
    payload["running_trains"] = runtime.trains_payload()


def build_network_payload(
    network: RailwayNetwork,
    sim_controller: Optional[SimulationController],
    *,
    session_state: Optional["SessionState"] = None,
) -> Dict[str, Any]:
    """The snapshot: the layout plus its live fields (design_v2.md "Data flow
    to the page").

    `session_state` is `AppState.session_state()`; a caller with no
    `AppState` gets the session read as Live.
    """
    from session_state import LIVE

    session_state = session_state or LIVE
    payload = network.to_dict()
    payload["session_state"] = session_state.to_dict()

    if not sim_controller:
        payload["hardware_failure"] = False
        payload["runtime_halted"] = False
        payload["operation_mode"] = operation_environment.DEFAULT_ENVIRONMENT.value
        payload["operation_environment"] = operation_environment.DEFAULT_ENVIRONMENT.value
        payload["environment_active"] = False
        payload["activation_issues"] = []
        payload["occupancy_is_real"] = False
        return payload

    add_train_runtime_fields(payload, sim_controller)

    payload["operation_mode"] = sim_controller.operation_mode.value
    payload["operation_environment"] = sim_controller.operation_mode.value
    payload["environment_active"] = bool(sim_controller.environment_active)
    payload["activation_issues"] = list(sim_controller.activation_issues)
    payload["occupancy_is_real"] = bool(sim_controller.occupancy_is_real)

    # The hardware-failure latch keeps only its flag; the banner reads the
    # failure's detail from the last `enter_hardware_failure` event in the one
    # stream (design_v2.md "Notifications and errors").
    hw_halted = bool(sim_controller.hardware_failure)
    payload["hardware_failure"] = hw_halted
    payload["runtime_halted"] = hw_halted
    hw_event = (
        sim_controller.notification_center.latest_by_operation("enter_hardware_failure")
        if hw_halted
        else None
    )
    if not hw_halted:
        payload["hardware_failure_detail"] = None
    elif hw_event is not None:
        payload["hardware_failure_detail"] = {
            "component": hw_event.payload.get("component"),
            "operation": hw_event.payload.get("operation"),
            "resource_id": hw_event.payload.get("resource_id"),
            "message": hw_event.payload.get("message"),
            "requires_new_activation": True,
        }
    else:
        # Latched, but the event aged out of the stream: the halt is still
        # real, so the banner keeps its recovery instruction.
        payload["hardware_failure_detail"] = {"requires_new_activation": True}

    return payload
