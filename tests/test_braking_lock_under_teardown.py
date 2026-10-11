"""The braking-distance lock keeps up while a teardown latch is pending.

design_new.md "Teardown outranks normal progress" and "Braking-distance lock":
step `66aa`'s **Lock the braking distance** sub-step runs on every Plan pass for
a train with a live path, gated by neither `is_clean_for_create` nor a pending
teardown latch. An orderly cancel latched on a rolling train is not committed
until the train stops ("Action latched-cancel consumption"), so the train keeps
running on its live path to its reservation end. Every reserved block within
its braking distance must stay locked the whole way, or a higher-priority run
could preempt track the train needs to stop.

Scene: `demo_test` with the SHORT branch's curves too tight, so the one-off
route takes the LONG branch of seven 40 cm blocks and the braking distance
spans several blocks at speed.
"""

from runtime_payloads import ErrorRecord
from tests.journey.scenes.demo_test_scene import DemoTestScene

DT = 0.1
LATCH_AT_SPEED_KMH = 15.0


def _forward_blocks(path, head_block):
    """Block ids strictly ahead of the head along the path's own sequence."""
    out = []
    for entry in path.resource_sequence:
        if entry.resource_type != "block":
            continue
        if entry.sequence_index <= path.current_resource_index:
            continue
        if not out and entry.resource_id == head_block:
            continue
        out.append(entry.resource_id)
    return out


def _unlocked_blocks_inside_braking_distance(network, controller, path):
    """Reserved blocks within the braking distance ahead of the head that the
    path has not locked."""
    head_block = controller._get_current_block_id()
    braking_cm = controller.braking_distance_cm(DT)
    far_seam_cm = float(controller._get_remaining_distance_in_current_block())
    missing = []
    for block_id in _forward_blocks(path, head_block):
        block = network.blocks[block_id]
        if block.reserved_by_path != path.path_id:
            break
        far_seam_cm += float(block.length_cm or 0.0)
        if block.locked_by_path != path.path_id:
            missing.append(block_id)
        if far_seam_cm >= braking_cm:
            break
    return missing


def test_lock_keeps_up_with_a_rolling_train_under_a_pending_cancel():
    fixture = DemoTestScene(
        short_block_radius_cm=10.0, equalize_branch_lengths=True
    ).build()
    sim, network, train_id = fixture.sim_controller, fixture.network, fixture.train_id
    dispatcher = sim.path_dispatcher
    controller = sim.get_or_create_controller(train_id)

    # Run until the train is rolling at speed on a live path.
    for _ in range(600):
        sim.update(DT)
        if (
            controller.physics_sync.current_speed_kmh > LATCH_AT_SPEED_KMH
            and dispatcher.get_active_path(train_id) is not None
        ):
            break
    else:
        raise AssertionError("train never reached speed on a live path")

    controller.route_hooks.phase_latches.set_path_cancel_request(
        ErrorRecord("operator cancel while rolling", "operator"), "info"
    )

    rolling_ticks_checked = 0
    for tick in range(1, 600):
        sim.update(DT)
        path = dispatcher.get_active_path(train_id)
        if path is None or controller.is_stopped():
            break
        rolling_ticks_checked += 1
        missing = _unlocked_blocks_inside_braking_distance(network, controller, path)
        assert not missing, (
            f"tick {tick} after the cancel latched: reserved blocks {missing} lie "
            f"within the braking distance of a rolling train but are not locked "
            f"(head={controller._get_current_block_id()}, "
            f"speed={controller.physics_sync.current_speed_kmh:.1f} km/h)"
        )

    assert rolling_ticks_checked > 0, "train stopped before any rolling tick was checked"
