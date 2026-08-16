"""Recovering from a fatal physical-entry conflict (design_new.md "Fatal
physical-entry conflict procedure", Clearing the halt).

A fatal conflict says two trains claim one piece of track. It halts every train
tick, which is right — the runtime has lost its grip on where the trains are.
What is not right is requiring a process restart to get moving again: the
operator goes and sorts the conflict out on the layout, and the whole layout
state should still be there when they come back.

`SimulationController.clear_failed_runtime()` reopens the gate. It does not
resume mid-motion: every train is written straight to OFF at zero speed, so
nothing moves until the operator powers it back up deliberately.
"""

import pytest

from enums import HandlePosition, TrainState
from runtime_payloads import FatalConflictKind, FatalPhysicalEntryConflict
from services import ValidationError, train_service
from tests.test_unidentified_adoption import _State, an_unidentified_on, adopt
from tests.test_unidentified_trains import build_sim, tick


def a_conflict(train_id="T1", block_id="BLK_1", prior="T0"):
    return FatalPhysicalEntryConflict(
        resource_type="block",
        resource_id=block_id,
        incoming_train_id=train_id,
        prior_owner_train_id=prior,
        conflict_kind=FatalConflictKind.OCCUPIED_BY_OTHER_TRAIN,
    )


def a_halted_sim():
    """A runtime with one real train on it, halted by a fatal conflict."""
    network, sim, detector = build_sim()
    unidentified_id = an_unidentified_on(sim, detector)
    adopt(sim, unidentified_id, "T1")
    controller = sim.controllers["T1"]
    controller.commands.power_on_cmd()
    controller.physics_sync.current_speed_kmh = 42.0
    sim.enter_failed_runtime(a_conflict())
    return network, sim, detector


def test_the_halt_is_what_it_always_was():
    network, sim, detector = a_halted_sim()

    assert sim.failed_runtime is True
    assert sim.failed_runtime_payload is not None
    assert sim.failed_runtime_at is not None


def test_clearing_releases_the_gate_and_returns_the_conflict():
    network, sim, detector = a_halted_sim()

    cleared = sim.clear_failed_runtime()

    assert cleared.resource_id == "BLK_1"
    assert cleared.conflict_kind is FatalConflictKind.OCCUPIED_BY_OTHER_TRAIN
    assert sim.failed_runtime is False
    assert sim.failed_runtime_payload is None
    assert sim.failed_runtime_at is None


def test_clearing_leaves_every_train_off_and_stopped():
    """Not a resume: the operator powers back up deliberately, so a train that
    was rolling when the conflict hit does not carry on rolling."""
    network, sim, detector = a_halted_sim()
    controller = sim.controllers["T1"]
    assert controller.ops.state is not TrainState.OFF
    assert controller.physics_sync.current_speed_kmh == 42.0

    sim.clear_failed_runtime()

    assert controller.ops.state is TrainState.OFF
    assert controller.ops.handle_position is HandlePosition.N
    assert controller.physics_sync.current_speed_kmh == 0.0
    assert controller.eb.active is False
    assert controller.ato.running is False


def test_the_runtime_ticks_again_after_clearing():
    network, sim, detector = a_halted_sim()
    frozen_time = sim.simulation_time

    sim.clear_failed_runtime()
    tick(sim, times=5)

    assert sim.simulation_time > frozen_time
    assert sim.failed_runtime is False
    # The train is still where it was; occupancy survived the halt untouched.
    assert network.blocks["BLK_1"].occupied_by == "T1"


def test_the_layout_survives_the_whole_round_trip():
    network, sim, detector = a_halted_sim()

    sim.clear_failed_runtime()

    assert sorted(network.trains) == ["T1"]
    # Clearing the halt reaches `OFF` through `complete_power_off()`, so the cab
    # is cleared with it: `(OFF, None)` is the only legal pair in the Allowed
    # `(state, cab)` pairs table (design_new.md "Controller state machine").
    assert sim.controllers["T1"].ops.state == TrainState.OFF
    assert sim.controllers["T1"].commands.cab is None
    assert network.trains["T1"].speed_profile_id == "default_T1"


def test_clearing_an_unhalted_runtime_is_refused():
    network, sim, detector = build_sim()

    with pytest.raises(ValueError, match="not halted"):
        sim.clear_failed_runtime()


def test_the_train_can_be_powered_back_up_after_clearing():
    network, sim, detector = a_halted_sim()
    sim.clear_failed_runtime()

    result = sim.controllers["T1"].commands.power_on_cmd()

    assert result.success is True
    assert sim.controllers["T1"].ops.state is not TrainState.OFF


# --------------------------------------------------- service-layer behaviour


def test_the_service_layer_reports_what_it_cleared():
    network, sim, detector = a_halted_sim()

    cleared = train_service.clear_failed_runtime(_State(network, sim))

    assert cleared == {
        "resource_type": "block",
        "resource_id": "BLK_1",
        "incoming_train_id": "T1",
        "prior_owner_train_id": "T0",
    }
    assert sim.failed_runtime is False


def test_the_service_layer_turns_an_unhalted_runtime_into_a_validation_error():
    network, sim, detector = build_sim()

    with pytest.raises(ValidationError, match="not halted"):
        train_service.clear_failed_runtime(_State(network, sim))
