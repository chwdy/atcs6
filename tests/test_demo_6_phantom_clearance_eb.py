"""Regression: a demo_6 train must not spuriously EB at 3x near PLT_BR_7_A.

Root cause (fixed 2026-06-09): the published ATC limit ``c.atc.speed_limit``
was computed from ``ATCComputer.clearance_ahead()``, a block-walk that sums
``block.length_cm``. 90 of demo_6's 163 blocks have ``length_cm == 0`` (fill /
switch blocks), so when a train neared the end of its current block with a
zero-length block ahead and its route advanced the destination, the block-walk
clearance collapsed to roughly the remaining distance in the current block
(~40 cm) while the canonical lookahead *curve* (which walks real tile geometry)
correctly saw the full reserved authority (~315 cm). That phantom-low limit
made ATO brake at B8 needlessly and, at the coarse 3x timestep, grew the
overspeed margin past the 10 km/h threshold and tripped a spurious overspeed
EB — observed for ``T_YA_3`` inside ``PLT_BR_7_A_BLOCK`` at ~tick 541 (dt=0.3).

The fix sources the published ATC limit from the canonical lookahead's
distance-0 anchor (``TrainController._distance_zero_atc_anchor``), so the limit
no longer depends on ``length_cm`` metadata.

This pins the behavioral outcome: at 3x, while the ``PLT_BR_7_A`` approach is
actually exercised, no train emergency-brakes *with an active path* (the
phantom overspeed case). A legitimate no-authority EB — a train rolling with
``received_path_plan is None`` — is intentionally still allowed.
"""

import contextlib
import io

import pytest

from bootstrap import bootstrap_runtime

pytestmark = pytest.mark.scene("demo_6")


def _boot():
    with contextlib.redirect_stdout(io.StringIO()):
        network, _dispatcher, sim, _ = bootstrap_runtime(
            demo=True, setup_train=True, scene="demo_6"
        )
    return network, sim


def test_no_phantom_clearance_eb_at_3x():
    """At 3x, no demo_6 train EBs while it still holds path authority."""
    network, sim = _boot()
    train_ids = sorted(network.trains.keys())

    dt = 0.3
    reached_br7 = False
    path_present_eb: dict[str, int] = {}

    # Run past the historical EB tick (~541 at dt=0.3).
    for tick in range(1, 600):
        sim.update(dt)
        for tid in train_ids:
            c = sim.get_or_create_controller(tid)
            try:
                block_id = c._get_current_block_id()
            except Exception:
                block_id = None
            if block_id == "PLT_BR_7_A_BLOCK":
                reached_br7 = True
            if (
                c.eb.active
                and tid not in path_present_eb
                and c.path_projection.received_path_plan is not None
            ):
                path_present_eb[tid] = tick

    assert not path_present_eb, (
        "demo_6 train(s) emergency-braked at 3x while still holding path "
        f"authority: {path_present_eb}. This is the phantom-clearance overspeed "
        "EB regressing — c.atc.speed_limit must follow the canonical lookahead "
        "curve's distance-0 anchor, not the length_cm block-walk clearance."
    )
    assert reached_br7, (
        "scenario not exercised: no train occupied PLT_BR_7_A_BLOCK within "
        "600 ticks at dt=0.3 — the regression guard would be vacuous"
    )
