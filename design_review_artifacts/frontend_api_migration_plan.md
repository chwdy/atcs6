# Frontend / API Migration Plan

## Context

The backend was migrated on branch `attempt-0504` to align with `design_new.md`. Several public-ish surfaces that the Flask routes and the JS frontend currently depend on either disappeared, became read-only properties, or changed their commit timing (synchronous to latched). This plan identifies every affected surface, prescribes the migration step, and flags items that need a product decision before implementation.

The most consequential category is the latched semantics for `power_off_cmd()` / `activate_emergency_cmd()` / path cancel: the API call returns success when the request is **accepted**, not when the train has actually stopped, the path has been cancelled, and resources have been released. The frontend currently treats those responses as the operation having completed and re-renders accordingly; that read needs to change.

A second high-impact category is removed fields. Both `target_speed` and `speed_limit_reason` no longer exist on `Train` — the JS train monitor still reads them off the train payload. They must be sourced from the controller payload (`onboard.target_speed`, `onboard.speed_limit_reason`) which is already attached to each train by `simulation.build_network_payload`.

A third high-impact category is removed enum values: `TrainState.RUNNING_WAITING` and `TrainState.MANUAL` no longer exist. The JS state machine still branches on `'running_waiting'` (state-dot color, wait-reason banner) and `'manual'` (manual control panel enable/disable). Both need to be re-keyed: `running_waiting` is now subsumed by `running_moving` with `clearance_ahead_cm == 0`; `manual` is now `onboard.control_mode === 'manual'`, not a state value.

## Inventory of affected surfaces

### Backend route handlers (server-side)

| File:line | What it does | What changes |
|---|---|---|
| `routes/paths.py:311-330` | `POST /api/paths/<id>/complete` calls removed `state.path_dispatcher.complete_path(...)`. | Method no longer exists on `PathDispatcher`. Either delete the route (path completion is now driven internally by `simulation` calling `try_complete_active_path` after each tick), or change it to call `try_complete_active_path(train_id, event_time)` — but that requires resolving the train_id from the path. |
| `routes/paths.py:156-199` | `DELETE /api/paths/<id>` directly mutates train fields (`train.assigned_path_id = None`, `train.current_path = None`, etc.) and calls `state.path_dispatcher.cancel_path(...)` synchronously. | Per the migration, API-driven cancellation should write `controller.pending_path_cancel_request` and let Phase 3 commit. Direct synchronous mutation of `train.assigned_path_id` is also stale: `assigned_path_id` is now a read-only @property derived from dispatcher state. The handler should issue the latch and return 202 Accepted with the path's current status. |
| `routes/trains.py:257-270` | `POST /api/trains/<id>/power-off` returns 200 with full train dict in the response body. | Backend semantics changed: `power_off_cmd()` now writes `pending_power_off` + (if there is a live path) `pending_path_cancel_request`. The Phase-3 cleanup in a later tick is what flips the train to OFF. The handler should return 202 Accepted with the latched-request descriptor; the eventual OFF state must be observed via polling `/onboard` or via `/notifications`. |
| `routes/trains.py:489-502` | `POST /api/trains/<id>/emergency-stop` returns 200 with full train dict. | Same pattern as power-off. `activate_emergency_cmd()` writes EB latch + path-cancel latch; the cancel resolves later. The route should return 202. The synchronous "state == emergency_stop" return is incorrect — `emergency_stop` is not a `TrainState` value (the test on `tests/test_api.js:1070` asserting `state === 'emergency_stop'` was already wrong vs. the new enum and is now further wrong). |
| `routes/trains.py:360-385` | `POST /api/trains/<id>/depart` branches on `train.state == TrainState.RUNNING_WAITING` and calls `controller.clear_to_move()`. | `RUNNING_WAITING` was deleted from the enum. `Train.clear_to_move()` and `Train.wait_for_path()` are stubbed `pass # removed in v1`. `controller.clear_to_move()` does not exist on the controller. The RUNNING_WAITING branch is dead and must be removed. The 'depart' endpoint becomes a thin wrapper over `controller.depart()` / `controller.depart_from_station(...)`. |
| `routes/trains.py:407-421` | `POST /api/trains/<id>/enable-manual` calls `controller.enable_manual()` — that method survives but is now a no-op state-wise: it sets `control_mode = "manual"` without changing operational state. | The route still works mechanically. But its returned `train.state` will never be `"manual"` anymore (state stays IDLE/STANDBY/RUNNING_AT_STATION). The frontend's expectation that the response `state` flips to `"manual"` (`tests/test_api.js:964`) is broken. |
| `routes/trains.py:423-439` | `POST /api/trains/<id>/disable-manual` — same. | Same: state value no longer changes; only `onboard.control_mode` toggles. |
| `routes/trains.py:441-459` | `POST /api/trains/<id>/manual-speed` — guards on the train being "in MANUAL state", but MANUAL state no longer exists. The train method `set_manual_speed` always returns True. | The route's `if train.set_manual_speed(...)` branch always succeeds; the 400 path is unreachable. The route can be simplified, but the more interesting question is whether `manual-speed` should be rejected when `onboard.control_mode != "manual"`. |
| `routes/trains.py:461-483` | `POST /api/trains/<id>/travel-direction` — same pattern, `set_travel_direction` always returns True. | The 400 reject branch on the route is unreachable. Same product question as above. |
| `routes/trains.py:704-737` | Speed profile assignment handler returns `_train_to_dict(train)`. | Indirectly affected — the response body shape changes (no `target_speed`, no `speed_limit_reason`). |
| `simulation.py:430-448` (`build_network_payload`) | Per-train `train_data["onboard"] = controller.to_dict()` is already attached. | Already correct — the canonical home for `target_speed` / `speed_limit_reason` / `state` (controller's view) is `train_data.onboard`. Frontend just needs to read from there. |
| `train.py:988-1086` (`Train.to_dict`) | Still serializes `travel_direction` (mirrored on Train), `assigned_path_id`, `ato_destination_block_id`, `manual_target_speed`. Does **not** serialize `target_speed` or `speed_limit_reason`. | The shape is correct for v1. Frontend that expects `train.target_speed` will get `undefined`. |
| `train_controller.py:4059-4123` (`TrainController.to_dict`) | Serializes `state`, `target_speed`, `speed_limit_reason`, `control_mode`, `active_cab`, `travel_direction`, `assigned_path_id`, `ato_path_id`, `ato_destination_block_id`, `clearance_ahead_cm`. | Already exposes everything the frontend now needs as `train.onboard.*`. No change needed here. |
| `routes/trains.py:1-53` (`_train_to_dict`) | Wraps `train.to_dict(...)` and injects live path tiles. | Add the `onboard` payload here too (or keep relying on the network endpoint to add it). Several individual train endpoints (`POST /trains`, `PATCH /trains/<id>`, `POST /power-on`, etc.) return `_train_to_dict(train)` without onboard merged in, so the frontend cannot read `target_speed` or `state` from those responses today. |

### Frontend JavaScript

| File:line | What it does | What changes |
|---|---|---|
| `static/js/train-monitor.js:219` | `target_speed_kmh: train.target_speed \|\| 0` (fallback path). | Read from `train.onboard?.target_speed` or pull from `/api/trains/<id>/lookahead` payload's `target_speed_kmh`. |
| `static/js/train-monitor.js:222` | `speed_limit_reason: train.speed_limit_reason`. | Read from `train.onboard?.speed_limit_reason`. |
| `static/js/train-monitor.js:247, 257` | Branches on `state === 'running_waiting'` for the wait-reason banner and the speed-limit-reason hide. | `running_waiting` no longer exists. New rule: when the train is `running_moving` and `onboard.clearance_ahead_cm <= 0` (or `wait_reason` is set), behave like the old `running_waiting`. The simplest fix is to keep the existing branch keyed on the new condition, e.g. `state === 'running_moving' && (train.wait_reason \|\| onboard.clearance_ahead_cm === 0)`. |
| `static/js/train-monitor.js:381, 384, 410, 420, 447, 456, 459, 475, 476, 505, 514, 797` | Same `target_speed` / `speed_limit_reason` / `running_waiting` reads at three other code paths. | Same fix in each. The cleanest refactor is one helper that resolves these three values from `(train, payload)` once. |
| `static/js/editor-trains.js:179` | State-dot CSS class map has `running_waiting: 'train-state-waiting'`. | Remove the entry (state never appears) or keep the CSS class and trigger it from a derived condition. The CSS rule (`.train-state-waiting`) is still useful as a "stalled with live authority" badge. |
| `static/js/editor-trains.js:181, 1142, 1158, 483, 1384-1392` | All `state === 'manual'` checks for enabling/disabling manual panel buttons and the manual-speed slider. | Replace with `onboard?.control_mode === 'manual'`. Note: `onboard.state` is still a value, but it's the controller's view (which equals `train.state` in v1). For mode use `control_mode`. |
| `static/js/editor-trains.js:632` | Slider disabled by `train.state !== 'manual'`. | Replace with `train.onboard?.control_mode !== 'manual'`. |
| `static/js/editor-trains.js:1395-1399` | `train.travel_direction` button highlight. | `train.travel_direction` still exists on the Train payload, and `onboard.travel_direction` is also exposed. Either source works in v1; prefer the onboard one for consistency. |
| `static/js/editor.js:1979-1981` | State-color map has `running_waiting`, `manual`. | Same as editor-trains.js:179/181. |
| `static/js/app.js:691, 692, 697, 698` | `lookahead.target_speed_kmh` / `lookahead.speed_limit_reason` reads. | These are reading from the lookahead-API payload, not from the train. The lookahead payload still carries `target_speed_kmh` and `speed_limit_reason` (built in `train_controller.get_monitor_lookahead`); confirm by reading the function or by adding fallback to `onboard.target_speed`/`onboard.speed_limit_reason`. |
| `static/js/editor-paths.js:221, 440, 599, 907` | Calls `api.cancelPath(pathId)` and immediately treats success as the path being gone. | Backend semantics change: cancel is now latched. The promise resolves with `result.success === true` once the latch is written, but the path is not yet removed and the train has not yet stopped. Frontend should refresh from `/api/paths` (or wait for a notification) before assuming the cancel is committed. |
| `static/js/api.js:804-809` (`cancelPath`) | Issues `DELETE /api/paths/<id>`. | Wrapper itself does not need to change; the consumer interpretation does. Optionally annotate the JSDoc to say "request queued; path is committed in a later tick". |
| `static/js/api.js:536-540` (`trainPowerOff`) | Issues `POST /power-off`. | Same: the response indicates queued, not OFF. JSDoc and callers (editor-trains.js:756) need to know to poll for OFF. |

### JS test suite (tests/test_api.js)

| Line | Test | What changes |
|---|---|---|
| `tests/test_api.js:964` | Asserts `result.data.state === 'manual'` after `enable-manual`. | `state` no longer changes; assert on `result.data.onboard?.control_mode === 'manual'` once handler is updated to merge `onboard` into the response. |
| `tests/test_api.js:989` | Asserts `state === 'idle'` after `disable-manual`. | Now the state never was `manual` to begin with, so the assertion as written may still pass spuriously. Prefer `onboard.control_mode === 'ato'`. |
| `tests/test_api.js:1015` | `manual_target_speed === 50.0` — still valid (Train field). | Likely keep as-is; verify the route returns `_train_to_dict` carrying `manual_target_speed`. |
| `tests/test_api.js:1070` | Asserts `state === 'emergency_stop'` after emergency-stop. | `emergency_stop` is not a TrainState value anywhere in the codebase. Replace with `result.data.onboard?.eb_active === true` once the handler returns onboard. Also revisit whether the response is 200 sync or 202 latched (see open question Q1). |

### Backend test runner / scenario runner

| File:line | What it does | What changes |
|---|---|---|
| `scenario_runner.py:222-241` | `_evaluate_checkpoint` resolves `assertion.path` against `build_network_payload(network, sim_controller)`. | The payload shape itself is OK because `build_network_payload` already attaches `train_data["onboard"]`. Existing scenario JSON files that assert `trains.<id>.target_speed` or `trains.<id>.state == "running_waiting"` need to be rewritten to use `trains.<id>.onboard.target_speed` or condition on `onboard.clearance_ahead_cm`. Affected scenarios live under `tests/scenarios/`; auditing them is out of this plan's scope — flag for whoever owns scenario authoring. |
| `persistence.py:55, 163-178` | Save/load round-trips `network.to_dict()`. | No direct frontend impact, but layouts saved before the migration may carry `train.target_speed` / `train.speed_limit_reason` / `train.state == "running_waiting"` in the `trains` array. Loading older layouts must tolerate these by ignoring unknown fields and remapping `running_waiting` -> `running_moving` (one-line shim in `network.from_dict` / `Train.from_dict`). The `_migrate_route_station_schema` pattern in persistence.py:289 is the right place; add a sibling migrator for legacy train state. (See open question Q3.) |

## Migration steps (priority order)

### Step 1 — Fix the three runtime-broken backend routes

These will 500 or silently misbehave on the first request after the backend migration; nothing else can be tested until they are corrected.

1. `routes/paths.py:310-330` — `POST /api/paths/<id>/complete`. Either delete the endpoint (recommended; v1 completion is internal) or change it to:

   ```
   path = state.path_dispatcher.get_path(path_id)
   if not path or not path.train_id:
       return 404 / 400
   result = state.path_dispatcher.try_complete_active_path(
       path.train_id,
       state.sim_controller.simulation_time,
   )
   return jsonify(result.to_dict()), 200 if result.success else 409
   ```
   See open question Q4.

2. `routes/trains.py:367-385` — strip the `train.state == TrainState.RUNNING_WAITING` branch and the `controller.clear_to_move()` call. Reduce to: if STANDBY -> `controller.depart()`; if RUNNING_AT_STATION -> `controller.depart_from_station(...)`; else 400.

3. `routes/paths.py:156-199` — `DELETE /api/paths/<id>` should stop synchronously mutating `train.assigned_path_id`, `train.current_path`, `train.path_index`, `train.reserved_blocks`. Replace the body with: locate the active path's train, write `controller.pending_path_cancel_request = PendingPathCancelRequest(cancellation_kind=API, ...)`, return 202 Accepted with `{ "status": "queued", "path_id": path_id }`. The Phase-3 consumer in `simulation.py` will commit it. (See open question Q1 about the 202 vs 200 contract.)

### Step 2 — Make every train-mutating endpoint return `onboard` in its response

Currently `_train_to_dict(train, network)` (`routes/trains.py:21-54`) does **not** attach `onboard`. The `/api/network` payload does. So any endpoint that returns `_train_to_dict` (power-on, set-cab, set-direction, depart, etc.) leaves the frontend with no way to read `target_speed`, `state` (controller-side), `eb_active`, or `control_mode` from the response.

Either:
- Add `train_data["onboard"] = controller.to_dict()` inside `_train_to_dict`, or
- Refactor every train endpoint to call `build_network_payload(...)` and pull out the single train's entry.

The first is smaller; do that.

### Step 3 — Update the JS to read controller-derived fields

In one editing pass:

- `static/js/train-monitor.js`: replace every `train.target_speed` with `train.onboard?.target_speed ?? 0`; every `train.speed_limit_reason` with `train.onboard?.speed_limit_reason ?? null`. Audit lines 219, 222, 256, 381, 384, 410, 419, 447, 456, 459, 475, 476, 505, 514.
- `static/js/editor-trains.js`: replace every `train.state === 'manual'` and similar with `train.onboard?.control_mode === 'manual'`. Audit lines 181, 483, 632, 1142, 1158, 1196, 1293, 1342, 1356, 1384-1392.
- Remove `running_waiting` from the state class map at editor-trains.js:179 and editor.js:1979. Replace the wait-reason banner branch in train-monitor.js (lines 247, 410, 505) with: show banner when `train.state === 'running_moving' && (train.wait_reason || (onboard?.clearance_ahead_cm ?? Infinity) <= 0)`.

### Step 4 — Adapt JS to latched cancel/power-off semantics

After Step 1.3, the `cancelPath` and `trainPowerOff` API responses no longer mean "done." Two options (open question Q2):

- **Optimistic UI**: keep the immediate re-render after `cancelPath`/`powerOff`, but also schedule a refresh of the network state ~200-500 ms later to reconcile.
- **Poll/notify**: after the latch is accepted, poll `/api/trains/<id>/onboard` (or `/notifications`) until `eb_active`, `power_on`, or path absence is observed.

Recommended: optimistic UI for the affordance + a single forced refresh after one tick. The `/api/trains/<id>/notifications` endpoint already exists and was designed for this kind of toast/feedback — wire it in.

### Step 5 — Update tests/test_api.js

- Delete the assertion `state === 'manual'` at line 964; replace with `onboard.control_mode === 'manual'` once Step 2 lands.
- Delete the assertion `state === 'emergency_stop'` at line 1070; replace with `onboard.eb_active === true`.
- If the emergency-stop endpoint becomes 202 (Q1), update the success assertion accordingly.

### Step 6 — Audit scenario JSONs and persistence migration

Out of scope for this plan to enumerate, but flagged: scan `tests/scenarios/*.json` for paths that name `trains.*.target_speed`, `trains.*.speed_limit_reason`, `trains.*.state == "running_waiting"`, `trains.*.state == "manual"`. Rewrite to `onboard.*` paths or to the equivalent derived condition. Add a one-shot migrator in `Train.from_dict` for layouts that carry the dead state strings (see Q3).

## API response semantics changes

### `POST /api/trains/<id>/power-off`

**Before** (200 OK):
```json
{ "status": "success", "train": { "id": "...", "state": "off", ... } }
```

**After** (202 Accepted, recommended):
```json
{
  "status": "queued",
  "train_id": "...",
  "request": { "kind": "power_off" },
  "note": "OFF transition deferred to phase 3"
}
```

Frontend re-render of "OFF" only after observing `train.onboard.power_on === false` on a subsequent fetch (typically within one tick = 100-200 ms via the polling loop in app.js).

### `POST /api/trains/<id>/emergency-stop`

**Before** (200 OK):
```json
{ "status": "success", "train": { "state": "emergency_stop", ... } }
```

**After** (202 Accepted):
```json
{
  "status": "queued",
  "train_id": "...",
  "request": { "kind": "eb" },
  "onboard": { "eb_active": true, ... }
}
```

The `eb_active` flag flips synchronously on the controller (it is the latch trigger, not the latched action). The path-cancel and route-abort consequences are deferred. Including a slim `onboard` snapshot in the 202 lets the UI show "EB" instantly.

### `DELETE /api/paths/<id>`

**Before** (200 OK):
```json
{ "success": true, "path_id": "...", "released_blocks": [...], ... }
```

**After** (202 Accepted):
```json
{
  "status": "queued",
  "path_id": "...",
  "train_id": "...",
  "note": "cancel committed when train is stopped"
}
```

The released-resources list is no longer available at request time (the train may still be moving over them). The frontend should refresh the path list after one tick instead of mutating local state.

### `POST /api/paths/<id>/complete`

Recommend deleting the endpoint. v1 completion is owned by the simulation tick (`simulation.py` calls `try_complete_active_path` per phase 2). Manual completion via API is not in the design.

If kept (Q4): change the body to call `try_complete_active_path(train_id, simulation_time)` and return `PathCompletionResult.to_dict()`.

## Frontend JS changes

Concrete read-site list, by file:

**`static/js/train-monitor.js`** — read controller fields off `train.onboard`:
- Line 219: `train.target_speed` -> `train.onboard?.target_speed`
- Line 222: `train.speed_limit_reason` -> `train.onboard?.speed_limit_reason`
- Line 247: `state === 'running_waiting'` -> condition on `(state === 'running_moving' && wait_reason)`
- Line 256-257: same as 247
- Lines 381, 384, 410, 419, 447, 456, 459, 475, 476, 505, 514: same three substitutions repeated across `update()`, `updateFromController()`, and `_render()` paths.

**`static/js/editor-trains.js`** — manual-mode and running_waiting:
- Line 179: drop `running_waiting` row from `getTrainStateVisualClass`
- Line 181: drop `manual` row from same map
- Line 483: `(onboard?.train_state || train.state) === 'running_waiting'` -> condition on wait_reason
- Line 632: slider `disabled` should read `train.onboard?.control_mode !== 'manual'`
- Line 1142: same — `controlMode = onboard.control_mode`
- Line 1158: state branch on `running_waiting` -> condition on wait_reason
- Lines 1384, 1390-1392: state === 'manual' -> control_mode === 'manual'

**`static/js/editor.js`**:
- Lines 1979-1981: drop `running_waiting` and `manual` branches from the color map.

**`static/js/app.js`**:
- Lines 691-698: these read the lookahead-API response, not the train payload; if `get_monitor_lookahead` still returns `target_speed_kmh`/`speed_limit_reason` (it does — see `train_controller.py:1515-1524`), no change. Verify with one manual call to `/api/trains/<id>/lookahead` after the migration.

## Risks

1. **202 Accepted vs 200 OK contract churn.** Switching from sync to latched semantics changes promise resolution timing. UI elements that auto-disable after a successful response (e.g. "Power Off" button) may need a separate disabled-while-pending state. If we keep the 200 status code but change the body to a "queued" descriptor, we avoid breaking the existing `result.ok` checks in the JS test suite — but lose the HTTP-level signal.

2. **Loaded layouts with dead state values.** Saved layouts under `layouts/*.json` written before the migration may contain `"state": "running_waiting"` or `"state": "manual"` in train rows. `Train.from_dict` will throw on unknown enum values unless a shim exists. This is a backend issue but it surfaces as "loading an autosave hangs the app" from the user's perspective.

3. **`assigned_path_id` divergence between Train and dispatcher.** The migration made `controller.assigned_path_id` a read-only property reading dispatcher state, but `Train.assigned_path_id` is still a stored field that several routes write directly (e.g. `routes/paths.py:96, 188`). These writes are now redundant at best and stale at worst. The cancel-path handler in `routes/paths.py:188` writes `train.assigned_path_id = None` synchronously while the dispatcher may still hold the path (because cancellation is now deferred). The frontend reading `train.assigned_path_id` may report "no path" before the path is actually gone. The clean fix is to drop the field from `Train` entirely, but that is a bigger backend change; for now, frontend should prefer `train.onboard?.assigned_path_id` or `train.onboard?.ato_info?.path_id`.

4. **Loss of sync feedback for "did the cancel actually work".** If the user double-clicks a cancel button while the latch is queued, the second request finds no active path and either no-ops (good) or 404s (bad). The handler should be idempotent.

5. **Test flakes from latched timing.** Any JS test that does `await cancel(...)` then immediately reads "the path is gone" will start failing intermittently. test_api.js does this in several places via `cancelPath` -> `getPaths`. Fix by waiting one tick or by polling.

## Testing approach

For each migration step, the verification path:

- **Step 1 (broken routes)**: hit each of the three endpoints with `curl` after backend boot. Expected: 200/202 instead of 500. Specifically:
  - `curl -X POST /api/trains/T1/depart` when T1 is in STANDBY: 200 OK.
  - `curl -X DELETE /api/paths/PATH-XYZ`: 202 Accepted with `status: queued`.
  - `curl -X POST /api/paths/PATH-XYZ/complete`: either 404 (deleted) or PathCompletionResult.

- **Step 2 (onboard merged in responses)**: `curl /api/trains/T1` and `curl -X POST /api/trains/T1/power-on` should both return JSON with an `onboard` key holding `target_speed`, `state`, `control_mode`, etc.

- **Step 3 (JS field migration)**: load the app, drive a train, observe the train monitor: target speed, speed-limit-reason, and the wait-reason banner should populate correctly. Switch a stopped train to manual mode: the manual panel buttons enable; switch back: they disable. There should be no `undefined` rendered.

- **Step 4 (latched semantics)**: cancel an active path while the train is moving. Observe: the path stays in the path list initially. Within ~1 second, the train brakes, the path disappears, and a notification appears. Power off a moving train: 400 (still rejected). Power off a stopped train: 202, then within a tick the OFF state appears.

- **Step 5 (test_api.js)**: re-run the JS test suite. Targeted assertions that previously checked `state === 'manual'` / `state === 'emergency_stop'` should now check `onboard.control_mode` / `onboard.eb_active`.

- **Step 6 (scenarios)**: re-run `python -m pytest tests/test_scenarios.py`. Failed assertions whose path resolves to None will indicate which scenario JSONs need rewriting.

## Open questions

**Q1 — HTTP status code for latched commands.** Should `power-off`, `emergency-stop`, and path cancel return 202 Accepted, or keep 200 OK with a body field `status: "queued"`? 202 is more REST-correct but breaks existing client code that checks `response.ok` (200-299) and treats anything in that range as "done". 200 with `status` keeps clients working but loses the HTTP-level signal. **Recommendation: 200 OK with `status: "queued"`** — minimal client churn, the body field is enough for any caller that needs to distinguish.

**Q2 — Optimistic vs notification-driven UI for latched ops.** When the user clicks "Cancel Path", do we (a) immediately gray out the path in the UI and silently reconcile after the next poll, or (b) show "Cancellation queued..." and only remove it when the notification confirms? (a) is faster-feeling, (b) is more honest. **Recommendation: (a) optimistic gray-out + (b) notification toast on commit** — both, not either.

**Q3 — Legacy state migrator on layout load.** Layouts saved before this branch may carry `state: "running_waiting"` or `state: "manual"` on train records. Should `Train.from_dict` accept and silently remap these (`running_waiting` -> `running_moving`; `manual` -> `idle` + `control_mode = "manual"` after wiring controller from snapshot)? Or refuse to load with a clear error? **Recommendation: silently remap and log a warning** — keeps autosaves loadable.

**Q4 — Delete `POST /api/paths/<id>/complete` outright?** v1 completion is internal (simulation calls `try_complete_active_path` per tick). The endpoint exists today only for testing/debugging. If kept, it's a hazard (manual completion does not respect the stop-check). If removed, scenario tests that complete paths via API need to be rewritten to wait for the simulation to do it. **Recommendation: delete.** No production caller exists.

**Q5 — Should `routes/trains.py` `_train_to_dict` be the single source of truth, or should we make every endpoint return `build_network_payload(...)["trains"][...]` instead?** The first is small and keeps the response shape predictable per endpoint; the second is uniform but heavier. **Recommendation: amend `_train_to_dict` to merge `onboard`** — Step 2 above.
