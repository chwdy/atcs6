"""Tick-by-tick journey test for the cab-switch-after-demo scenario.

After the demo train arrives at East Terminal and the route deactivates,
the operator issues `set_cab("B")`. Per design_new.md step `22a9` the
canonical lookahead becomes the no-authority empty payload:

  - `atc_limit = 0` at distance 0
  - `operational_limit = 0` at distance 0
  - `atc_limit_reason_code = no_authority`
  - single `speed_curves` anchor at `distance_cm = 0.0`
  - `clearance_cm = 0` and `authority_boundary_delta_cm = 0`
  - empty `blocks` / `signals` / `switches` / `stations` lists

The test runs at the same three playback speeds as the parent demo
journey so regressions show up at every dt the demo supports.
"""

import pytest

from enums import AtcLimitReasonCode, TrainState

from tests.journey.actions import attempt_set_switch_position  # noqa: F401 (sanity)
from tests.journey.assertions import (
    assert_ato,
    assert_cab,
    assert_eb,
    assert_no_active_path,
    assert_speed_eq,
    assert_state,
)
from tests.journey.events import Event, Journey
from tests.journey.harness import JourneyHarness
from tests.journey.invariants import (
    no_eb,
    no_overspeed,
    no_pending_path_cancel_request,
    no_pending_power_off,
    no_pending_route_abort,
    state_cab_pair_valid,
)
from tests.journey.scenes.demo_scene import DemoScene
from tests.journey.triggers import predicate, train_stopped_for

pytestmark = pytest.mark.scene("demo_main")


def _enqueue_set_cab(cab: str):
    """Action: enqueue a `set_cab` operator command on the controller's
    inbox. Cab selection is manual-only (design_new.md set_cab contract), so
    the operator switches to manual first; the canonical drain runs set_mode
    before set_cab in the same tick. The drain happens on the next tick's
    Phase 2 entry."""

    def action(s) -> None:
        controller = s.sim_controller.get_or_create_controller(s.fixture.train_id)
        controller.commands.enqueue_command("set_mode", {"mode": "manual"})
        controller.commands.enqueue_command("set_cab", {"cab": cab})

    return action


def _assert_no_authority_curve(s) -> None:
    """Assert the canonical no-authority lookahead shape at distance 0.

    Per design_new.md step `22a9`: a single `speed_curves` anchor at
    `distance_cm = 0.0` with `atc_limit = 0`, `operational_limit = 0`,
    and `atc_limit_reason_code = no_authority`. Blocks/signals/switches/
    stations all empty; clearance and authority boundary delta both `0`.
    """
    controller = s.sim_controller.get_or_create_controller(s.fixture.train_id)
    la = controller.get_lookahead()
    inner = la.get("lookahead", {})

    curves = inner.get("speed_curves") or []
    assert len(curves) == 1, (
        f"speed_curves expected single anchor, got {len(curves)}: {curves}"
    )
    anchor = curves[0]
    assert anchor.get("distance_cm") == 0.0, (
        f"anchor distance_cm expected 0.0, got {anchor.get('distance_cm')}"
    )
    assert anchor.get("atc_limit") == 0.0, (
        f"anchor atc_limit expected 0.0, got {anchor.get('atc_limit')}"
    )
    assert anchor.get("operational_limit") == 0.0, (
        f"anchor operational_limit expected 0.0, got {anchor.get('operational_limit')}"
    )
    assert anchor.get("atc_limit_reason_code") == AtcLimitReasonCode.NO_AUTHORITY.value, (
        f"anchor reason_code expected no_authority, got "
        f"{anchor.get('atc_limit_reason_code')!r}"
    )

    assert inner.get("blocks") == [], f"blocks expected [], got {inner.get('blocks')}"
    assert inner.get("signals") == [], (
        f"signals expected [], got {inner.get('signals')}"
    )
    assert inner.get("switches") == [], (
        f"switches expected [], got {inner.get('switches')}"
    )
    assert inner.get("stations") == [], (
        f"stations expected [], got {inner.get('stations')}"
    )
    assert inner.get("clearance_cm") == 0.0, (
        f"clearance_cm expected 0.0, got {inner.get('clearance_cm')}"
    )
    assert inner.get("authority_boundary_delta_cm") == 0.0, (
        f"authority_boundary_delta_cm expected 0.0, got "
        f"{inner.get('authority_boundary_delta_cm')}"
    )

    # Mirror onto controller scalar fields.
    assert controller.atc.speed_limit == 0.0, (
        f"controller.atc.speed_limit expected 0.0, got {controller.atc.speed_limit}"
    )
    assert controller.target_speed == 0.0, (
        f"controller.target_speed expected 0.0, got {controller.target_speed}"
    )
    assert controller.atc._last_limit_reason_code == AtcLimitReasonCode.NO_AUTHORITY, (
        f"_last_atc_limit_reason_code expected NO_AUTHORITY, "
        f"got {controller.atc._last_limit_reason_code!r}"
    )


def _assert_travel_direction(expected: str):
    def check(s) -> None:
        controller = s.sim_controller.get_or_create_controller(s.fixture.train_id)
        la = controller.get_lookahead()
        got = la.get("travel_direction")
        assert got == expected, f"travel_direction expected {expected!r}, got {got!r}"

    return check


def _build_cab_switch_journey(train_id: str = "DEMO_TRAIN") -> Journey:
    events = [
        # Event 1: wait until the demo has settled at East Terminal. The
        # parent demo journey shows the train reaches READY after path
        # completion with no active path and no assigned route.
        Event(
            name="settled_at_terminal",
            trigger=train_stopped_for(settle_ticks=30, after_tick=50),
            assertions=[
                assert_state(TrainState.READY),
                assert_cab("A"),
                assert_ato(False),
                assert_eb(False),
                assert_speed_eq(0.0, epsilon=0.5),
                assert_no_active_path(),
                # Pre-switch baseline: same NO_AUTHORITY shape (cab A is
                # facing into a dead-end so no forward authority exists).
                _assert_no_authority_curve,
                _assert_travel_direction("NORTH"),
            ],
            actions=[
                # Issue the cab switch. The command lands in the inbox; the
                # next tick's Phase 2 drain executes it before the rest of
                # the tick. We assert the post-drain state on the next
                # event.
                _enqueue_set_cab("B"),
            ],
        ),
        # Event 2: one tick later, the cab switch has been drained. The
        # train is still at East Terminal but now in cab B facing SOUTH.
        # Critically, the canonical lookahead is the no-authority empty
        # payload: ATC = 0, ATO operational_limit = 0, reason
        # NO_AUTHORITY. No phantom motion permission.
        Event(
            name="after_cab_switch_drained",
            trigger=predicate(lambda s: s.cab == "B"),
            assertions=[
                assert_state(TrainState.READY),
                assert_cab("B"),
                assert_ato(False),
                assert_eb(False),
                assert_speed_eq(0.0, epsilon=0.05),
                assert_no_active_path(),
                _assert_no_authority_curve,
                _assert_travel_direction("SOUTH"),
            ],
        ),
        # Event 3: many ticks later, the steady state is unchanged. The
        # train sits inert until a new route is assigned.
        Event(
            name="steady_state_no_phantom_motion",
            trigger=predicate(lambda s: s.tick >= 100 and s.cab == "B"),
            assertions=[
                assert_state(TrainState.READY),
                assert_cab("B"),
                assert_speed_eq(0.0, epsilon=0.05),
                assert_no_active_path(),
                _assert_no_authority_curve,
            ],
        ),
    ]

    # Continuous invariants — these run every tick from tick 0 onward, so
    # the entire demo journey leading up to the cab switch must also
    # satisfy them. Notably `no_eb` proves Option B's path-completion
    # flow does not accidentally fire EB when the path transitions out
    # of ACTIVE.
    invariants = [
        no_overspeed(epsilon=0.5),
        no_eb(),
        no_pending_path_cancel_request(),
        no_pending_route_abort(),
        no_pending_power_off(),
        state_cab_pair_valid(),
    ]

    return Journey(train_id=train_id, events=events, invariants=invariants)


@pytest.mark.parametrize(
    "dt,label",
    [(0.1, "1x"), (0.2, "2x"), (0.3, "3x")],
)
def test_cab_switch_after_demo_completes(dt, label):
    # Cover ~200 s of sim time plus a settle margin at every playback,
    # mirroring the parent demo journey's tick budget.
    base_max_ticks_at_0_1 = 2200
    max_ticks = max(50, int(base_max_ticks_at_0_1 * 0.1 / dt))

    harness = JourneyHarness(
        scene=DemoScene(),
        journey=_build_cab_switch_journey(),
        dt=dt,
        max_ticks=max_ticks,
    )
    result = harness.run()
    assert result.success, "\n" + result.report()
