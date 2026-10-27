"""A detector correction that carries the head across a switch records the
train on that switch (design_new.md "Block occupancy detection", The
correction walks through the train's own blocks only).

With every block detected, the head waits at the exit seam of its block until
the block beyond the switch confirms occupied; then the correction moves the
head onto that block's entry seam, across the switch tiles. The correction
emits no movement events of its own, so the next movement report has to say
the head entered the switch — otherwise `Switch.route_owners` never names the
train standing on the points.
"""

import pytest

from block_occupancy import OccupancyInput
from tests.journey.scenes.demo_scene import DemoScene

pytestmark = pytest.mark.scene("demo_main")


class _LeadingDetector(OccupancyInput):
    """Every block detected. Current on the span's blocks plus the block the
    head is waiting at the seam of — a locomotive that arrives the moment the
    head reaches the seam."""

    def __init__(self, sim, train_id):
        self.sim, self.train_id = sim, train_id

    def detected_block_ids(self):
        return set(self.sim.network.blocks)

    def read_block_currents(self):
        controller = self.sim.controllers.get(self.train_id)
        if controller is None or controller.physics_sync.current_span is None:
            return {
                block_id: block.occupied_by == self.train_id
                for block_id, block in self.sim.network.blocks.items()
            }
        live = set(controller.physics_sync.current_span.occupied_blocks or [])
        physics = self.sim.physics_instances.get(self.train_id)
        if physics is not None and physics.pending_entry_block:
            live.add(physics.pending_entry_block)
        return {block_id: block_id in live for block_id in self.sim.network.blocks}


def test_a_correction_across_a_switch_records_the_train_on_it():
    fixture = DemoScene().build()
    sim, network, train_id = fixture.sim_controller, fixture.network, fixture.train_id
    sim.block_occupancy_detector.input_interface = _LeadingDetector(sim, train_id)
    switch = network.switches["SW_3_0"]

    covered_at = None
    for tick in range(1, 400):
        sim.update(0.1)
        controller = sim.controllers.get(train_id)
        span = controller.physics_sync.current_span if controller else None
        if span is None:
            continue
        if (switch.x, switch.y) in {tuple(t) for t in span.occupied_tiles or []}:
            covered_at = tick
            break

    assert covered_at is not None, "the train never reached SW_3_0"
    # The head reached the switch through a detector correction, not by
    # integrating across it under power.
    assert any(
        (n.message or "").startswith(f"detector read train {train_id}")
        for n in sim.notification_center.peek_recent(2000)
    )
    assert train_id in set(switch.route_owners.values())
