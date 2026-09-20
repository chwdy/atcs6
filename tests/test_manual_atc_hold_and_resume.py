"""Manual `simulation` ATC enforcement holds the train at the limit and lets it
auto-resume — it never rewrites the driver's handle.

design_new.md (Driver/operator inputs, tick step `924d`) specifies that a manual
`simulation` driver notch is the *raw* command: `TrainPhysics` caps the
integrated speed at the live distance-0 `atc_limit` without `handle_position`
itself being rewritten. So when ATC restricts a manually-driven train it must:

  * decelerate toward the limit at the service brake rate (reactive, no snap),
  * HOLD at the limit rather than brake past it toward a stop,
  * keep `handle_position` at the driver's raw notch, and
  * AUTO-RESUME acceleration the moment the limit lifts, with no re-command.

Regression guard: the earlier controller-side rewrite
(`TrainController.tick(...)` overwriting `handle_position` with a braking notch)
destroyed the raw command, so once ATC braked a manual train the notch stuck and
the train braked past the limit toward a stand and never auto-resumed. Every
assertion below fails under that behavior.
"""

import pytest

from enums import HandlePosition
from tests.journey.scenes.demo_scene import DemoScene
from tests.journey.harness import ProgressTracker

pytestmark = pytest.mark.scene("demo_main")


def _drive_manual_into_atc_limit(dt: float):
    fx = DemoScene().build()
    sim = fx.sim_controller
    network = fx.network
    c = sim.get_or_create_controller(fx.train_id)
    tid = fx.train_id

    tracker = ProgressTracker(tid)
    tracker.attach(sim.path_dispatcher)

    def step_seconds(seconds: float):
        for _ in range(max(1, int(round(seconds / dt)))):
            tracker.reset_per_tick()
            sim.update(dt)

    # Enter manual `simulation` from rest (set_mode requires the train stopped),
    # replacing the demo's ATO start-up commands.
    c.commands.command_inbox.clear()
    c.commands.enqueue_command("power_on")
    c.commands.enqueue_command("set_mode", {"mode": "manual"})
    c.commands.enqueue_command("set_manual_mode_type", {"mode_type": "simulation"})
    step_seconds(1.0)  # drain commands + let Plan acquire route authority
    assert c.commands.control_mode == "manual"
    assert c.manual.manual_mode_type == "simulation"

    # Drive on power to a steady cruise under authority.
    c.commands.enqueue_command("set_handle", {"position": HandlePosition.P5})
    step_seconds(4.5)
    cruise = c.physics_sync.current_speed_kmh
    assert cruise > 10.0, f"train did not reach a manual cruise (got {cruise:.1f})"

    # Impose a gentle ATC restriction ~6 km/h below the current speed. That is
    # under the 10 km/h ATC-EB threshold, so it exercises the service-brake ATC
    # cap rather than an emergency brake.
    limit = round(cruise - 6.0, 1)
    saved = {bid: blk.speed_limit_kmh for bid, blk in network.blocks.items()}
    for blk in network.blocks.values():
        blk.speed_limit_kmh = limit
    step_seconds(6.0)  # brake down to the limit and settle

    assert not c.eb.active, "a 6 km/h overspeed must not trip the ATC emergency brake"
    # The raw driver command must be untouched.
    assert c.ops.handle_position == HandlePosition.P5, (
        f"ATC rewrote the driver handle to {c.ops.handle_position}; the raw "
        "manual notch must be preserved"
    )
    # The train holds AT the limit — it did not brake past it toward a stand.
    held = c.physics_sync.current_speed_kmh
    assert held >= limit - 0.75, (
        f"train braked past the limit (held {held:.2f} < limit {limit:.2f}); "
        "the ATC cap must hold at the limit, not brake toward a stop"
    )
    assert held <= limit + 1.0, (
        f"train did not slow to the limit (held {held:.2f} > limit {limit:.2f})"
    )

    # Lift the restriction; the train must auto-resume on the raw handle with no
    # fresh driver command.
    for bid, blk in network.blocks.items():
        blk.speed_limit_kmh = saved[bid]
    step_seconds(2.5)
    assert c.ops.handle_position == HandlePosition.P5
    resumed = c.physics_sync.current_speed_kmh
    assert resumed > held + 1.0, (
        f"train did not auto-resume after the limit lifted (held {held:.2f}, "
        f"resumed {resumed:.2f}); the raw handle must drive again"
    )


@pytest.mark.parametrize("dt", [0.1, 0.2, 0.3])
def test_manual_simulation_holds_at_atc_limit_and_auto_resumes(dt):
    _drive_manual_into_atc_limit(dt)
