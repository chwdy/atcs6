# Plan: Investigate Path Completion vs Waiting and Fix Cab Direction

## Problem Statement

The observed bug is:

- a train runs to the end of a standalone path
- it stops at the furthest safe point permitted by that path
- a reverse-direction path is assigned
- the train does not turn around correctly before moving again

Current evidence suggests the visible cab-direction bug may be downstream of a state-model bug:

- the train may be transitioning to `RUNNING_WAITING` when the standalone path has actually completed
- later logic then treats the train as "waiting to resume the same run" instead of "finished path, ready for a new one"

There is also a code-structure problem:

- automatic cab logic during `RUNNING_WAITING -> RUNNING_MOVING` currently uses a narrower helper instead of the general required-cab decision flow

## Goals

1. Determine the real reason the train fails to reverse correctly.
2. Define the correct distinction between path completion and waiting.
3. Make state transitions consistent with the intended finite state machine.
4. Make all automatic cab correction use one high-level required-cab function.
5. Add regression coverage for the real bug.

## Investigation Questions

### State Transition Questions

- Where exactly does the controller decide that a moving train should enter `RUNNING_WAITING`?
- Under what condition does the code consider a path "finished"?
- Is path completion inferred from block/path cancellation, lack of clearance, last tile traversal, or some other condition?
- When the dispatcher cancels a completed path, does the controller explicitly leave running states?

### Path Completion Questions

- How does the controller distinguish:
  - blocked before end of authority
  - normal stop at furthest reachable point of authority
- Does the current clearance logic treat "no more authority ahead" the same as "authority exists but is blocked"?
- What happens when the last reachable movement is shorter than the safety margin?

### Cab Direction Questions

- In the failing scenario, is `_required_cab_for_path()` called at all?
- If it is called, what answer does it return?
- If it is not called, which narrower helper is used instead?
- When a reverse path is newly assigned after path completion, is the train still in a state whose transition path bypasses the general required-cab logic?

### Data Source Questions

- Is the controller reading path data from dispatcher live state or stale `train.current_path` cache?
- When a new reverse path is assigned, is the train head tile guaranteed to be present in the effective path?
- Are there cases where cab-direction logic must succeed even if the head tile is not in the path?

## Likely Root Causes to Confirm

### Hypothesis A: Wrong State After Standalone Path Completion

The train reaches the effective end of a standalone path, but the controller transitions to `RUNNING_WAITING` instead of exiting running states.

Consequence:

- later logic treats the train as still in active run continuation
- resume logic fires instead of fresh-start logic
- cab correction happens in the wrong place or with the wrong assumptions

### Hypothesis B: Resume Logic Uses the Wrong API

The `RUNNING_WAITING -> RUNNING_MOVING` transition uses direct flip detection instead of the general required-cab decision.

Consequence:

- the controller asks a narrow question
- fallback cases are skipped
- reverse-path reassignment may fail even when the general decision path could determine the correct cab

### Hypothesis C: Path Completion and Blocked Stop Are Not Distinguished

Current clearance / stopping logic may collapse both of these into the same transition:

- "cannot continue because blocked"
- "cannot continue because authority has been fully consumed"

Consequence:

- both outcomes become `RUNNING_WAITING`
- the controller loses the semantic distinction required for correct next-step behavior

## Proposed Fix Strategy

### Phase 1: Confirm Actual Runtime Flow

Trace the failing scenario end-to-end:

1. train starts on standalone path
2. train reaches furthest safe stopping point
3. dispatcher/path logic cancels or completes the path
4. new reverse path is assigned
5. train attempts to move again

Capture at each step:

- controller state
- train state
- active cab
- assigned path id
- effective path tiles
- path completion/cancellation event
- which cab-decision function is called

### Phase 2: Correct State Semantics

If the path is truly complete and no active route continuation exists:

- transition out of running states
- default target should be `STANDBY`

Do not use `RUNNING_WAITING` for standalone path completion.

Keep `RUNNING_WAITING` only for unresolved external waiting conditions such as:

- red signal
- occupancy conflict
- missing continuation while still in active route/run context

### Phase 3: Unify Automatic Cab Decision Logic

Make `_required_cab_for_path()` the single high-level cab decision API.

All automatic cab selection/correction should use it:

- ATO start
- waiting-to-moving transition
- dispatcher-driven restart logic

Any lower-level helper should exist only inside `_required_cab_for_path()`.

Direct transition logic should not call a raw "needs flip" helper.

### Phase 4: Rework Path Completion Detection

Implement or clarify a decision that distinguishes:

- path blocked before completion
- path fully consumed

The path should count as complete when:

- the train has advanced to the furthest physically reachable safe stopping point for that task
- the train cannot move farther because it must preserve the end-of-track safety margin
- the stop is not caused by a signal, occupancy conflict, or missing continuation that could later clear

This must work even when:

- the stopping point is before the geometric end of the last tile
- there are still blocks remaining between the train and the absolute track end
- the total remaining distance is shorter than the safety margin
- the train does not enter the last block center

### Phase 5: Align Dispatcher and Controller Semantics

Ensure dispatcher-side path completion/cancellation and controller-side state transitions agree.

If dispatcher marks the path complete or cancels it because the train has consumed the authority, controller should not remain in a running-path waiting state unless a route explicitly keeps the train in active execution context.

## Concrete Code Plan

1. Audit every assignment to:
   - `RUNNING_WAITING`
   - `RUNNING_MOVING`
   - `RUNNING_AT_STATION`
   - `STANDBY`

2. Identify the exact branch that handles:
   - obstruction stop
   - end-of-path stop
   - dispatcher path cancellation/completion

3. Refactor state transition logic so:
   - obstruction stop -> `RUNNING_WAITING`
   - route dwell stop -> `RUNNING_AT_STATION`
   - standalone path completion -> `STANDBY`

4. Refactor automatic direction logic so:
   - call sites use `_required_cab_for_path()`
   - low-level flip helper is removed or hidden inside the required-cab implementation

5. Strengthen `_required_cab_for_path()` if fallback cases are still weak:
   - live dispatcher path first
   - connectivity-based determination when possible
   - fallback when head is not in path
   - explicit `None` only when direction genuinely cannot be determined

6. Re-test the original failing scenario with logging or targeted unit coverage.

## Test Plan

### State Semantics Tests

- standalone path completes at furthest safe stopping point -> `STANDBY`
- blocked by signal before path end -> `RUNNING_WAITING`
- route-controlled station dwell -> `RUNNING_AT_STATION`
- active cab retained after standalone path completion -> still not `IDLE`

### Cab Direction Tests

- after standalone path completion, assigning reverse path chooses opposite cab before restart
- same scenario on zigzag/diagonal staircase track
- same scenario when head tile is not directly present in effective path

### Authority Edge Case Tests

- final block shorter than safety margin but otherwise clear -> path still counts as complete
- stopping at safety distance from dead-end track counts as completion, not waiting
- multiple short blocks remain before the physical end of track, but total remaining distance is below safety margin -> path still counts as complete

### Integration Tests

- path completes and is cancelled by dispatcher, controller exits running states correctly
- route with multiple stops still uses running states correctly between path segments and station dwell

## Expected Outcome

After the fix:

- standalone path completion no longer leaves the train in `RUNNING_WAITING`
- reverse-path reassignment is handled from the correct stopped-ready state
- automatic cab correction uses one consistent decision function
- blocked stops and completed-path stops are no longer conflated

## Non-Goals

This investigation should not initially attempt to redesign the entire dispatcher/route architecture.

The focus is:

- correct state semantics
- correct cab-selection flow
- correct path-completion detection
- regression coverage for the observed behavior
