# Journey-testing framework reference

This is the framework `journey-debug` uses to make behavior questions falsifiable. The framework itself is general — it just drives ticks and captures structured state. It doesn't know anything about Option B, the cab-switch scenario, or any other specific debug. Use it as building blocks.

## What's in `tests/journey/`

```
tests/journey/
├── harness.py           — JourneyHarness, Snapshot, ProgressTracker, _capture_snapshot, Failure/JourneyResult
├── events.py            — Event, Journey, Invariant dataclasses
├── triggers.py          — Predicate factories (at_tick, head_visited, train_stopped, …)
├── assertions.py        — Snapshot→None assertion factories (assert_state, assert_cab, assert_atc_reason_in, …)
├── actions.py           — Side-effect probes that run on event fire (attempt_set_switch_position, …)
├── invariants.py        — Continuous checks (no_overspeed, no_eb, state_cab_pair_valid, …)
├── scenes/              — Scene factories (DemoScene, EmptyScene, base.SceneFixture)
└── journeys/            — Pre-built journeys (demo_journey, demo_journey_strict, …)
```

Pytest entry points live at `tests/test_demo_train_journey.py`, `tests/test_demo_cab_switch.py`, etc.

## How a journey runs

`JourneyHarness(scene, journey, dt, max_ticks).run()` does roughly:

1. `scene.build()` returns a `SceneFixture(network, sim_controller, train_id)`.
2. Attach a `ProgressTracker` to the dispatcher so block/switch entry+vacate events get captured even when high `dt` would skip them in a post-tick snapshot.
3. Tick 0 — capture the pre-update snapshot, run invariants, fire any events whose triggers are already true.
4. For tick in 1..max_ticks:
   - `sim_controller.update(dt)` advances the simulation by `dt` seconds.
   - Capture a `Snapshot` (controller fields + path + route + tracker view).
   - Run every invariant against the snapshot — fail fast.
   - Drain pending events whose trigger fires this tick: assertions first, then actions. Each event is consumed in declared order.
5. If all events are consumed, success. If the loop runs out with events pending, fail with `trigger_timeout`.

Failures produce a structured `Failure` object with a recent-tick history table, the offending snapshot, the pending event name, and the last completed event. Render via `result.report()`.

## The Snapshot — what's available per tick

`harness.Snapshot` exposes per-tick state without needing to read controller internals directly. The fields you'll use most often:

| Field | Source | Notes |
|---|---|---|
| `tick`, `sim_time` | harness | tick is the iteration counter; sim_time is `sim_controller.simulation_time` |
| `state` | `controller.state` | `TrainState` enum |
| `cab` | `controller.cab` | `"A"` / `"B"` / `None` |
| `speed_kmh` | `controller.current_speed_kmh` | float |
| `eb_active` | `controller.eb_active` | bool |
| `ato_running` | `controller.ato_running` | bool |
| `handle_position` | `controller.handle_position` | `HandlePosition` enum |
| `handle_reason` | `controller.handle_reason` | str |
| `atc_limit_kmh` | `controller.atc_speed_limit` | float |
| `atc_reason_code` | `controller._last_atc_limit_reason_code` | `AtcLimitReasonCode` enum (canonical) |
| `atc_reason_str` | `controller.speed_limit_reason` | free-form detail string |
| `head_tile`, `tail_tile` | `current_span.head/tail.tile` | `(x, y)` or `None` |
| `occupied_tiles` | `current_span.occupied_tiles` | list of `(x, y)` |
| `occupied_blocks` | `current_span.occupied_blocks` | list of block ids |
| `command_inbox_kinds` | `controller.command_inbox[*].kind` | drained at top of next tick |
| `latch_path_cancel_request` | `controller.phase_latches.path_cancel_request is not None` | bool |
| `latch_route_abort` / `power_off` / `route_deactivate` | same | bool |
| `active_path_id`, `active_path_status`, `active_path_authority_end_block_id` | dispatcher | path lifecycle |
| `assigned_route_id` | `sim.route_executor.assigned_route_id_for(train_id)` | str or `None` |
| `tracker` | `ProgressTracker` | cumulative + per-tick `blocks_entered` / `blocks_vacated` / `switches_entered` / `switches_vacated` |

Need a field that isn't in `Snapshot`? Read it directly off `s.controller` or `s.network` or `s.sim_controller`. The Snapshot is convenience; the live objects underneath are the source of truth.

## Running existing journeys

```bash
# The reference demo journey at three playback speeds
conda run -n general --no-capture-output python -m pytest \
  tests/test_demo_train_journey.py -v

# Cab-switch-after-demo (the scenario this skill grew out of)
conda run -n general --no-capture-output python -m pytest \
  tests/test_demo_cab_switch.py -v

# A single parametrize variant
conda run -n general --no-capture-output python -m pytest \
  'tests/test_demo_train_journey.py::test_demo_train_full_journey[0.3-3x]' -v
```

Conda matters: plain `python` / `pytest` falls through to `/usr/bin/python3` (3.9, no flask, no PEP 604 union syntax) and breaks at import time. Same rule applies to any ad-hoc probe.

## Writing a new journey

A journey is a `Journey(train_id, events=[…], invariants=[…])`. Build it inside a factory so each pytest variant gets a fresh closure (triggers keep `fired` state and would leak across parametrize otherwise).

### Minimal example

```python
from tests.journey.harness import JourneyHarness
from tests.journey.scenes.demo_scene import DemoScene
from tests.journey.events import Event, Journey
from tests.journey.triggers import at_tick, train_stopped_for, predicate
from tests.journey.assertions import (
    assert_state, assert_cab, assert_ato, assert_speed_eq, assert_no_active_path,
)
from tests.journey.invariants import no_overspeed, no_eb, state_cab_pair_valid
from enums import TrainState


def _enqueue_set_cab(cab: str):
    def action(s):
        controller = s.sim_controller.get_or_create_controller(s.fixture.train_id)
        controller.enqueue_command("set_cab", {"cab": cab})
    return action


def build_journey(train_id="DEMO_TRAIN"):
    return Journey(
        train_id=train_id,
        events=[
            Event(
                name="train_settled",
                trigger=train_stopped_for(settle_ticks=30, after_tick=50),
                assertions=[assert_state(TrainState.READY), assert_cab("A")],
                actions=[_enqueue_set_cab("B")],
            ),
            Event(
                name="after_cab_switch",
                trigger=predicate(lambda s: s.cab == "B"),
                assertions=[
                    assert_state(TrainState.READY),
                    assert_cab("B"),
                    assert_ato(False),
                    assert_speed_eq(0.0),
                    assert_no_active_path(),
                ],
            ),
        ],
        invariants=[no_overspeed(), no_eb(), state_cab_pair_valid()],
    )


import pytest

@pytest.mark.parametrize("dt,label", [(0.1, "1x"), (0.2, "2x"), (0.3, "3x")])
def test_scenario(dt, label):
    harness = JourneyHarness(
        scene=DemoScene(),
        journey=build_journey(),
        dt=dt,
        max_ticks=max(50, int(2200 * 0.1 / dt)),
    )
    result = harness.run()
    assert result.success, "\n" + result.report()
```

### When to write an Event vs an invariant

- **Event** — fires once, on a specific trigger, then is consumed. Use for "at this moment, assert X."
- **Invariant** — runs on every tick, never consumed. Use for "across the whole run, X must always hold." Good examples: `no_eb()`, `no_overspeed()`, `state_cab_pair_valid()`.

If you want a per-tick check from tick N onward, an invariant with an internal "have we started yet" flag is overkill; usually it's cleaner to assert on each subsequent event's snapshot.

### Triggers worth knowing

- `at_tick(N)` — fires once tick ≥ N.
- `head_visited((x, y))` — once head_tile has been observed at the given tile (stateful — stays True after firing).
- `block_at_tile_entered((x, y))` / `block_at_tile_vacated((x, y))` — resolve the tile→block_id at trigger time so they survive station-induced block splits.
- `switch_entered("SW_X_Y")` / `switch_vacated("SW_X_Y")` — uses the ProgressTracker.
- `train_stopped(after_tick=N)` — fires once speed drops below `epsilon_kmh` after warmup.
- `train_stopped_for(settle_ticks=K, after_tick=N)` — fires after K consecutive stopped ticks; useful when the post-stop state machine needs to settle (dwell, path completion, route teardown).
- `path_completed_after_active()` / `route_run_removed_after_assigned()` — fire when the corresponding field transitions to None after having been set.
- `predicate(lambda s: …)` — escape hatch for ad-hoc conditions. Predicate functions don't need to be monotonic, but if they aren't, only the first firing counts.

### Assertions worth knowing

- `assert_state(TrainState.READY)`, `assert_cab("B")`, `assert_eb(False)`, `assert_ato(False)`
- `assert_speed_eq(0.0, epsilon=0.5)`, `assert_speed_le(40.0)`, `assert_speed_gt(0.0)`
- `assert_head_in([(9, 3), (9, 4)])`, `assert_no_active_path()`, `assert_has_active_path()`
- `assert_route_assigned("DEMO_ROUTE")`, `assert_command_inbox_contains("power_on")`
- `assert_atc_reason_in([AtcLimitReasonCode.NO_AUTHORITY])`, `assert_atc_reason_not_in([…])`
- `assert_block_at_tile_occupied_by((4, 0), "DEMO_TRAIN")`, `assert_block_at_tile_unoccupied((4, 0))`
- `assert_switch_position("SW_3_0", 0)`, `assert_switch_locked("SW_9_0", locked_state=1)`, `assert_switch_unoccupied("SW_3_0")`

Custom one-shot assertions are fine — just write a `def check(s): assert …` and pass it directly. The helpers exist for the patterns that repeat; they aren't required.

### Actions worth knowing

- `attempt_set_switch_position("SW_3_0", new_position=1, should_succeed=False)` — probes that a flip from outside is refused while a train straddles the switch.
- Custom actions: any `(Snapshot) -> None` that touches the world. Use them to enqueue operator commands, mutate switch state, or inject conditions and assert side effects.

## Probe scripts (not pytest)

For raw inspection without writing a Journey, drive the harness primitives directly:

```python
from tests.journey.scenes.demo_scene import DemoScene
from tests.journey.harness import ProgressTracker, _capture_snapshot

fx = DemoScene().build()
sim = fx.sim_controller
controller = sim.get_or_create_controller(fx.train_id)

tracker = ProgressTracker(fx.train_id)
tracker.attach(sim.path_dispatcher)

for tick in range(1, 1000):
    tracker.reset_per_tick()
    sim.update(0.1)
    # … sample whatever you need …

snap = _capture_snapshot(tick, fx, tracker)
print(snap.short())
```

This is the right shape for "I want to see what the canonical lookahead looks like 50 ticks into the demo" or "what does the controller report between path completion and the next route assignment?" Probe scripts go in `/tmp/` or the project root and get deleted at the end of the investigation — they're not the deliverable.

## Common gotchas

- **`ProgressTracker.reset_per_tick()`** before each `sim.update()` if you care about the per-tick `this_tick_entered_*` fields. The cumulative sets keep growing regardless.
- **Trigger closure state leaks across parametrize variants.** If you cache a journey at module level, the second parametrize run gets a journey whose triggers have already fired. Always rebuild the journey per test (the factory pattern at the top of `demo_journey.py`).
- **High `dt` can skip past block-entry events.** That's why `ProgressTracker` wraps `dispatcher.on_controller_progress` — it captures every entry/vacate event reported by the simulation, even if the post-tick snapshot wouldn't have shown the transient `occupied_by` write. If a journey fails only at `dt=0.3`, this is the first thing to check.
- **`enqueue_command(kind, payload)`** takes a payload dict for parameterized commands: `controller.enqueue_command("set_cab", {"cab": "B"})`. For commands without payload (`power_on`, `power_off`, `activate_emergency_cmd`), pass `kind` alone.
- **Path-active queries** read the dispatcher (`sim.path_dispatcher.get_active_path(train_id)`), not the controller. The controller's `received_path_plan` is a derived property over the dispatcher.

## Pointers

- The journey harness was built for v1 (routes, paths, ATO, latches). `design_v2.md` has none of those, so a v1 journey has no v2 prediction to compare against. Predict from these `design_v2.md` sections instead: **When a train stops**, **Claiming the next block**, **Switches**, **When a train overruns**, and **Track power**. Run `doc_map.py` from the doc-review skill for their current line ranges.
