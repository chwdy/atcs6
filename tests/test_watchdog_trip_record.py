"""Loss-of-control watchdog trip record (design_v2.md "Safe state, watchdog and
hardware failure", Loss-of-control watchdog).

The watchdog daemon drops the physical outputs to the safe state on its own
thread and may not touch model state, so it records only the transport result
in a thread-safe trip record. The runtime loop consumes that record once on
its next iteration through `SimulationController.consume_watchdog_trip()`
(design_v2.md "Runtime loop", Every time round consumes a watchdog trip): it
advances
`safe_state_generation`, reconciles every block's recorded track voltage to
zero, and escalates a failed drop to `hardware_failure` — a successful drop
emits one warning and does not end the session.
"""

from hardware.watchdog import WatchdogTripRecord
from network import RailwayNetwork
from simulation import SimulationController


# --------------------------------------------------------------- unit: record


def test_no_trip_consumes_to_none():
    record = WatchdogTripRecord()
    assert record.consume() is None


def test_a_successful_drop_consumes_once_as_not_failed():
    record = WatchdogTripRecord()
    record.record(failed=False)
    assert record.consume() is False
    # Consumed exactly once.
    assert record.consume() is None


def test_a_failed_drop_consumes_as_failed():
    record = WatchdogTripRecord()
    record.record(failed=True)
    assert record.consume() is True
    assert record.consume() is None


def test_repeated_trips_collapse_and_a_failure_sticks():
    record = WatchdogTripRecord()
    record.record(failed=False)
    record.record(failed=True)
    record.record(failed=False)
    # One pending trip; once any of them failed, the pending trip is a failure.
    assert record.consume() is True
    assert record.consume() is None


# ----------------------------------------------------- integration: consumer


def _sim():
    network = RailwayNetwork()
    sim = SimulationController(network)
    sim.environment_active = True
    return sim


def test_a_failed_drop_escalates_to_hardware_failure_on_the_next_iteration():
    sim = _sim()
    sim._watchdog_trip_record = WatchdogTripRecord()
    generation_before = sim.safe_state_generation

    sim._watchdog_trip_record.record(failed=True)
    sim.consume_watchdog_trip()

    assert sim.hardware_failure is True
    assert sim.safe_state_generation == generation_before + 1
    # Consumed exactly once: a second iteration neither re-escalates nor re-advances.
    generation_after = sim.safe_state_generation
    sim.consume_watchdog_trip()
    assert sim.safe_state_generation == generation_after


def test_a_successful_drop_warns_without_ending_the_session():
    sim = _sim()
    sim._watchdog_trip_record = WatchdogTripRecord()
    generation_before = sim.safe_state_generation

    sim._watchdog_trip_record.record(failed=False)
    sim.consume_watchdog_trip()

    assert sim.hardware_failure is False
    assert sim.safe_state_generation == generation_before + 1
    ops = [n.operation for n in sim.notification_center.peek_recent(50)]
    assert "consume_watchdog_trip" in ops


def test_no_trip_is_a_no_op():
    sim = _sim()
    sim._watchdog_trip_record = WatchdogTripRecord()
    generation_before = sim.safe_state_generation

    sim.consume_watchdog_trip()

    assert sim.hardware_failure is False
    assert sim.safe_state_generation == generation_before
