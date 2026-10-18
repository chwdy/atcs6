"""A conflict raises the emergency cut-out (design_new.md "Block occupancy
detection", Attribution rank 5, and An overrun entry or a conflict raises the
emergency cut-out).

When current appears on a block that more than one train touches and no
touching train has it reserved, the block belongs to none of them: it joins no
block list and is not handed to an unidentified train, each train keeps the
blocks it already held, and Observe latches an emergency cut-out request on
every candidate. The block stays confirmed occupied and owned by no train, so
it resolves as a conflict again on every later read — re-latching the request
for a candidate whose brake was released — until an operator moves the trains
so that at most one still touches it.

Layout: the straight-line harness from the seam-cap suite, six 100cm blocks
BLK_0..BLK_5 on one E-W run. T_W stands on BLK_1 and T_E on BLK_3, both
stopped, so each touches BLK_2 between them (a stopped train is touched on
either side).
"""

from block import BlockReservation
from constants import OCCUPANCY_ON_THRESHOLD
from enums import Direction
from notification_center import Kind
from path_dispatcher import PathDispatcher
from simulation import SimulationController
from tests.test_block_occupancy_cap import FakeDetectorInput, build_line
from train import Train, TrainCar

ALL_BLOCKS = {f"BLK_{i}" for i in range(6)}


def build_two_trains():
    network = build_line()
    detector_input = FakeDetectorInput(detected=ALL_BLOCKS, live={"BLK_1", "BLK_3"})
    sim = SimulationController(
        network, PathDispatcher(network), input_interface=detector_input
    )
    for train_id, head_x in (("T_W", 1), ("T_E", 3)):
        train = Train(
            train_id=train_id,
            consist=[
                TrainCar(
                    car_id=f"{train_id}_c1", car_type="LOCOMOTIVE", length_cm=40.0
                )
            ],
            max_speed=80.0,
        )
        network.add_train(train)
        controller = sim._get_or_create_controller(train)
        controller.physics_sync.current_span = network.place_train(
            train, head_x, 0, Direction.EAST
        )
        sim._get_or_create_physics(train)
        controller.powered = True
    return network, sim, detector_input


def read(sim, times=1):
    """Run the Observe detector read alone, `times` times."""
    for _ in range(times):
        sim._detect_block_occupancy()


def conflict_events(sim):
    return [
        event
        for event in sim.notification_center.events_for(
            "block", "BLK_2", kind=Kind.WARNING
        )
        if event.error is not None and event.error.origin == "conflict"
    ]


def cut_out_requested(sim, train_id):
    latches = sim.controllers[train_id].route_hooks.phase_latches
    return latches.emergency_request == "cut_out"


def test_a_block_two_trains_touch_goes_to_neither_and_cuts_both_out():
    network, sim, detector_input = build_two_trains()
    detector_input.live.add("BLK_2")

    read(sim, OCCUPANCY_ON_THRESHOLD)

    # The block joins no list and no unidentified train is minted for it.
    assert network.blocks["BLK_2"].occupied_by is None
    assert not any(t.is_unidentified for t in network.trains.values())
    for train_id in ("T_W", "T_E"):
        sync = sim.controllers[train_id].physics_sync
        assert "BLK_2" not in sync.pending_detected_entered_blocks
    # Each candidate keeps what it held.
    assert network.blocks["BLK_1"].occupied_by == "T_W"
    assert network.blocks["BLK_3"].occupied_by == "T_E"

    # Every candidate holds a cut-out request; Action step 01f2 applies it.
    assert cut_out_requested(sim, "T_W")
    assert cut_out_requested(sim, "T_E")
    for train_id in ("T_W", "T_E"):
        controller = sim.controllers[train_id]
        controller.commands.apply_emergency_request()
        assert controller.eb.active is True
        assert controller.eb.cut_out is True

    events = conflict_events(sim)
    assert len(events) == 1
    entity_ids = {entity.entity_id for entity in events[0].entities}
    assert {"BLK_2", "T_W", "T_E"} <= entity_ids


def test_a_standing_conflict_re_latches_on_every_read_and_reports_once():
    network, sim, detector_input = build_two_trains()
    detector_input.live.add("BLK_2")
    read(sim, OCCUPANCY_ON_THRESHOLD)
    for train_id in ("T_W", "T_E"):
        sim.controllers[train_id].commands.apply_emergency_request()

    # The operator releases T_W's brake while the conflict still stands.
    released = sim.controllers["T_W"].eb.release()
    assert released.success, released.message

    read(sim)

    # The block still resolves as a conflict, so T_W is cut out again. T_E is
    # still cut out, and step 01f2 would apply a fresh request as a no-op.
    assert network.blocks["BLK_2"].occupied_by is None
    assert cut_out_requested(sim, "T_W")
    sim.controllers["T_W"].commands.apply_emergency_request()
    assert sim.controllers["T_W"].eb.cut_out is True
    # Reported once, not on every read it stands.
    assert len(conflict_events(sim)) == 1


def test_the_conflict_resolves_once_only_one_train_touches_the_block():
    network, sim, detector_input = build_two_trains()
    detector_input.live.add("BLK_2")
    read(sim, OCCUPANCY_ON_THRESHOLD)
    center = sim.notification_center
    assert center.latest_error_for("block", "BLK_2", origin="conflict") is not None

    # The operator lifts T_E off the track: its block clears and it is gone.
    network.remove_train("T_E")
    detector_input.live.discard("BLK_3")

    read(sim)

    # Only T_W touches BLK_2 now, so it is T_W's entry, and the conflict is no
    # longer the block's current reason.
    assert "BLK_2" in sim.controllers["T_W"].physics_sync.pending_detected_entered_blocks
    assert center.latest_error_for("block", "BLK_2", origin="conflict") is None


def test_a_reserved_touching_train_outranks_an_unreserved_one():
    network, sim, detector_input = build_two_trains()
    network.blocks["BLK_2"].reservation = BlockReservation(
        path_id="P_W", train_id="T_W", exit_edge="E"
    )
    detector_input.live.add("BLK_2")

    read(sim, OCCUPANCY_ON_THRESHOLD)

    assert "BLK_2" in sim.controllers["T_W"].physics_sync.pending_detected_entered_blocks
    assert not cut_out_requested(sim, "T_E")
    assert conflict_events(sim) == []
