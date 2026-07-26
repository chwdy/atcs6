# Test coverage and suite-splitting plan

## Current reference point

The last green measurement before the in-progress tile-to-segment refactor was:

- Python: 1,495 tests, 73.5% line coverage, 59.5% branch coverage.
- Frontend: 366 tests in 38 files; coverage was not configured.
- Python wall time: 94.5 seconds with xdist's default scheduler and 80.3
  seconds with `--dist=worksteal`.

This is a reference point, not a permanent baseline. Re-measure immediately
after the current segment refactor is green because file names, executable
branches, and test counts are changing.

## Coverage remediation

### Phase 0 — restore a trustworthy baseline

1. Finish the tile -> segment -> block migration and get `python -m pytest
   tests/` green.
2. Run coverage with branch measurement and omit test code, demo scene builders,
   generated files, hardware demos, and entry-point-only scripts.
3. Generate machine-readable JSON and HTML artifacts and record per-module line
   and branch coverage in CI.
4. Run Vitest with its V8 coverage provider and record a separate frontend
   baseline. Do not combine Python and TypeScript percentages.

Exit criterion: two repeatable green coverage runs with the same collected test
count and no unexplained skip or xfail outcomes.

### Phase 1 — test live HTTP contracts

Prioritize route modules that were below 40% in the reference run:

1. `routes/route_runs.py`
2. `routes/inspector.py`
3. `routes/tunnels.py`
4. `routes/switches.py`
5. `routes/speed_profiles.py`
6. `routes/trains.py`
7. `routes/paths.py`

For every route, cover the success response, malformed body, missing resource,
domain conflict, and service-unavailable path where applicable. Assert response
shape as well as status. The migrated switch, speed-profile, scale, train-route,
train-position, layout, and retired-v0 tests in `tests/test_api_routes.py` are the
starting pattern.

Exit criterion: every registered route has at least one contract test and every
documented error status is exercised.

### Phase 2 — runtime and persistence failure behavior

Add focused tests for:

- runtime start, stop, restart, task cancellation, loop exception, delta clamp,
  and safe-state cleanup;
- corrupted snapshot fallback, missing layout, atomic-save failure, interrupted
  replacement, and replay failure;
- step recorder/player empty state, unsupported operations, and partial replay.

Use small in-memory networks and temporary directories. Do not bootstrap a full
demo merely to test lifecycle or serialization behavior.

Exit criterion: `runtime.py`, `persistence.py`, `step_recorder.py`, and
`step_player.py` each reach at least 80% line and 70% branch coverage.

### Phase 3 — segment/block safety invariants

After the segment migration settles, cover these boundaries directly:

- unknown, empty, duplicate, reversed, disconnected, and closed-loop segment
  groupings;
- missing endpoints and tiles, block-id collisions, rehoming, deleting the last
  member, and persistence round-trips;
- occupancy/reservation exclusion across multiple segments in one block;
- geometry writes going only through the segment geometry manager;
- reverse-index invalidation after every topology mutation.

Favor table-driven and property-style tests over additional long demo journeys.

Exit criterion: the grouping and geometry managers each exceed 90% line and 80%
branch coverage, with every documented invariant represented by a named test.

### Phase 4 — frontend behavior

Add V8 coverage and focus on behavior-heavy modules rather than renderer drawing
line counts:

- API error translation and cancellation;
- editor state transitions and stale-selection cleanup;
- hardware binding validation and failed commissioning commands;
- train command enablement, rejected commands, and notification handling;
- persistence restore with malformed or older schemas.

Canvas rendering code should be tested through stable handler decisions and
recorded draw calls, not pixel snapshots unless the visual output itself is the
contract.

Exit criterion: establish a ratcheted baseline, then require changed frontend
files not to lower line or branch coverage.

### Phase 5 — thresholds and ratcheting

Start CI thresholds at the freshly measured floor, rounded down by one point.
Raise them only after tests land:

- first target: 80% Python lines / 70% Python branches;
- second target: 85% Python lines / 75% Python branches;
- critical safety modules: 90% lines / 85% branches.

Use a ratchet: coverage may stay level or increase, but a pull request may not
reduce the repository total or any designated critical module. Exclude dead code
instead of writing tests solely to preserve obsolete implementations.

## Suite split

### Classification

Every Python test should have exactly one primary layer marker:

- `unit`: one module or pure service, no Flask client, no simulation loop;
- `api`: Flask test client exercising an HTTP contract;
- `integration`: several backend components or a short bounded simulation;
- `journey`: a scene plus a multi-tick behavioral outcome.

`slow` is an additional marker, not a layer. Initially mark a test slow when its
normal call time exceeds two seconds. Keep the existing `scene(...)` marker as
journey metadata.

Enforce marker registration in `pytest.ini` and fail CI on unknown markers.

### Pull-request jobs

Run these jobs in parallel:

1. **Frontend** — typecheck, Vitest, and production build. Target under 60s.
2. **Backend fast** — `unit or api`, excluding `slow`. Target under 30s.
3. **Backend integration** — `integration and not slow`. Target under 45s.
4. **Journey demo 4** — journey tests marked `scene("demo_4")`.
5. **Journey demo 5** — journey tests marked `scene("demo_5")`.
6. **Journey demo 6** — journey tests marked `scene("demo_6")`.
7. **Journey other** — remaining journey and slow integration tests.

All seven remain required checks. Splitting changes scheduling, not which tests
gate a pull request.

Use `--dist=worksteal` inside jobs with enough tests. Do not use eight workers
for a shard containing only one or two long journeys.

### Balancing the journey shards

Scene names alone will not remain balanced. Capture JUnit durations from every
CI run and review the 25 slowest tests weekly. If one scene exceeds the target:

1. first replace full replay with a seeded/minimal scenario;
2. then split that scene into two explicit CI node-id manifests if necessary;
3. keep one owner for each test so no journey silently disappears or runs twice.

The current first optimization candidates are the demo 4/5 no-path-revisit
replays, demo 6 phantom-clearance replay, giant-delta integration, idle-hold
release, reserved-corridor voltage, and crossover-state journeys.

### Coverage jobs

For pull requests:

- collect Python coverage from backend-fast and backend-integration;
- collect frontend coverage from Vitest;
- report journey coverage separately so a slow scene cannot block quick feedback
  merely to produce a combined report.

Nightly or on main:

- run all shards with branch coverage;
- combine Python coverage data from the Python shards;
- publish HTML, JSON, Cobertura, JUnit, and slow-test artifacts;
- fail on unexpected skips, xfails, collection-count drops, or threshold drops.

### Local commands

- `python run_all_tests.py`: every local gate, matching CI intent.
- `python -m pytest -m "unit or api"`: fast backend feedback.
- `python -m pytest -m integration`: backend integration feedback.
- `python -m pytest -m journey`: every journey.
- `python -m pytest -m slow --durations=0`: performance work.

Do not change the default `pytest` command to omit slow tests. A plain full run
must continue to collect everything.

## Guardrails

- Zero unexpected skips or xfails. An unsupported environment should be a
  separate, explicitly configured job, not a runtime skip in a behavior test.
- No test writes to the repository's real `layouts/` directory.
- No live server is required for API tests; use Flask's test client.
- No test depends on execution order or shared mutable globals without restoring
  them in `finally`/fixture teardown.
- A deleted legacy runner is complete only after its live contracts are mapped to
  pytest and intentionally removed contracts are asserted as retired.
