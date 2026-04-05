# Train State Machine

This document defines the train operational states as a finite state machine.

It separates three concerns:

- `state`: what the train is doing right now
- `path`: low-level movement authority from A to B
- `route`: higher-level service/run plan containing multiple stops and dwell logic

A train may physically occupy track in any stopped state. Occupancy is determined by physical position and block usage, not by controller state.

## State Definitions

### `OFF`

Train is powered down.

- No active operation
- No active cab
- Cannot move

### `IDLE`

Train is powered on but not direction-ready.

- Stopped
- No active cab
- Not currently executing a path or route
- Requires cab selection before movement can start

### `STANDBY`

Train is powered on, stopped, and ready to depart.

- Stopped
- Active cab selected
- Not currently executing a path or route
- Ready for manual departure or ATO/path start

### `RUNNING_MOVING`

Train is actively executing movement authority and is moving.

- Has active run context
- Normally has an active path or route-controlled movement segment
- Speed is above stopped threshold

### `RUNNING_WAITING`

Train is still in an active run/path execution context, but is temporarily stopped because it cannot yet continue.

- Stopped
- Active cab selected
- Still considered "in service" for the current movement task
- Waiting on an external condition

Examples:

- red signal ahead
- occupied block ahead
- missing path continuation while a route still expects continuation
- temporary dispatcher-controlled hold during active route execution

Non-examples:

- standalone path completed normally
- no active path or route remains

### `RUNNING_AT_STATION`

Train is in an active route and stopped for scheduled station dwell.

- Stopped
- Route remains active
- Stop is intentional and scheduled
- Distinct from obstruction waiting

### `MANUAL`

Train is under manual operating mode.

- Powered on
- Driver-controlled
- Not part of automatic ATO execution

This is a control-mode state and should only be used if the codebase continues to model manual as a top-level state.

## Core Principles

### `IDLE` vs `STANDBY`

The intended distinction is:

- `IDLE`: powered on, no cab selected
- `STANDBY`: powered on, cab selected, ready to move

This is the main semantic difference.

### `RUNNING_WAITING` vs Path Complete

`RUNNING_WAITING` means the train would continue if an external blocking condition cleared.

It must not be used when a standalone path has already been completed.

A path is considered complete when the train has reached the furthest physically reachable safe stopping point for that movement task and cannot move any farther because it must preserve the required safety margin to the physical end of track.

This remains true even if:

- the train does not physically enter the last tile center
- the train stops at safety margin from track end
- there are still one or more blocks remaining between the train and the absolute end of track
- the remaining blocks together are shorter than the safety margin

In other words, path completion is not defined by:

- reaching the last block
- reaching the last tile center
- consuming every remaining block fragment

It is defined by reaching the last safe place the train can physically occupy while respecting end-of-track safety margin.

### Path vs Route

- A `path` is a low-level movement task
- A `route` is a higher-level run containing multiple stops and dwell handling

Therefore:

- path completion does not automatically imply route completion
- route-controlled station dwell should use `RUNNING_AT_STATION`
- obstruction/path-continuation waiting should use `RUNNING_WAITING`

## Finite State Machine

The transitions below describe intended behavior.

### From `OFF`

- `power_on` -> `IDLE`
- `set_cab(A|B)` may optionally be treated as `power_on` + cab selection -> `STANDBY`

### From `IDLE`

- `set_cab(A|B)` -> `STANDBY`
- `power_off` -> `OFF`
- `enable_manual` -> `MANUAL` only if manual-with-no-cab is a supported model; otherwise require cab first

### From `STANDBY`

- `set_cab(None)` -> `IDLE`
- `power_off` -> `OFF`
- `manual depart / start path / ato_go` -> `RUNNING_MOVING`
- `enable_manual` -> `MANUAL`
- `set_cab(A|B)` -> `STANDBY` (cab switch while stopped)

### From `RUNNING_MOVING`

- obstruction / red signal / occupied track / waiting on continuation -> `RUNNING_WAITING`
- scheduled station stop under active route -> `RUNNING_AT_STATION`
- standalone path completed, no remaining active route/path continuation -> `STANDBY`
- route segment completed and next movement segment not yet available but route remains active -> `RUNNING_WAITING`
- emergency stop may remain in `RUNNING_MOVING` until actual speed reaches stopped threshold, then transition based on context

### From `RUNNING_WAITING`

- blocking condition clears and movement authority is available -> `RUNNING_MOVING`
- path/route is cancelled and no active run remains -> `STANDBY`
- operator de-selects cab -> normally disallowed while active run exists; if forced cancellation occurs first, then `IDLE`
- power off -> normally disallowed until run/path is cancelled and train is quiescent
- cab switch while stopped -> remain `RUNNING_WAITING` if the active run context still exists

### From `RUNNING_AT_STATION`

- dwell complete and next movement authority available -> `RUNNING_MOVING`
- dwell complete but route remains active and continuation is not yet available -> `RUNNING_WAITING`
- route completed at terminal -> `STANDBY`
- route cancelled -> `STANDBY`

### From `MANUAL`

- stop manual operation with cab retained -> `STANDBY`
- `set_cab(None)` -> `IDLE`
- `power_off` -> `OFF`
- start moving manually -> remain `MANUAL` if manual is treated as a persistent operating state

## Transition Rules

### Rule 1: Path completion is not waiting

If a standalone path has been fully consumed and the train has reached the furthest safe stopping point allowed by that path, the train leaves running states.

Default transition:

- `RUNNING_MOVING` -> `STANDBY`

### Rule 2: Waiting requires unresolved external dependency

Use `RUNNING_WAITING` only if the train is stopped because something outside the train still must change before movement can continue.

Examples:

- signal must clear
- occupancy ahead must clear
- dispatcher must provide continuation
- route logic is holding the train in active service context

### Rule 3: Station dwell is not obstruction waiting

Use `RUNNING_AT_STATION` for planned service stops under route control.

### Rule 4: Occupancy is orthogonal

State never determines whether a train occupies track.

The following are all valid:

- `IDLE` and occupying a block
- `STANDBY` and occupying a block
- `RUNNING_WAITING` and occupying a block

### Rule 5: Cab choice determines readiness, not occupancy

- no active cab -> `IDLE`
- active cab while stopped and not in active run -> `STANDBY`

### Rule 6: Automatic cab correction must use one high-level decision API

Any automatic cab selection or correction should ask:

- "What cab is required for this path?"

It should not ask:

- "Should I flip from the current cab?"

The state machine expects automatic direction logic to use a single high-level required-cab function consistently across:

- ATO start
- waiting-to-moving resume
- any future dispatcher-driven restart logic

## Recommended Controller Interpretation

For the current architecture, the controller should interpret the stopped states this way:

- `IDLE`: powered on, no cab, no active run
- `STANDBY`: powered on, cab selected, no active run
- `RUNNING_WAITING`: active run/path context, temporarily blocked
- `RUNNING_AT_STATION`: active route, planned dwell

## Open Questions

These choices should be confirmed during implementation:

- whether `MANUAL` remains a top-level state or becomes a control mode orthogonal to state
- whether path completion under an active route but without immediate continuation should stay `RUNNING_WAITING` or move to a more explicit route-held stopped state
- whether terminal arrival under route completion should end in `STANDBY` or a separate terminal/layover state in the future
