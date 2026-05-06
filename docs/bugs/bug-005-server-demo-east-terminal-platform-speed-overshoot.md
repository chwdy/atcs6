# bug-005-server-demo-east-terminal-platform-speed-overshoot

**Status:** Fixed  
**Date:** 2026-04-07  
**Test reference:** `tests/test_demo_main.py::test_bootstrap_demo_train_respects_east_terminal_speed_limit_at_3x`

## Symptom

In the actual server-start demo scene, clicking play at 3x speed let `DEMO_TRAIN` approach East Terminal too fast. East Terminal's platform block had the intended `20 km/h` speed limit, but the train entered the station block at about `46.8 km/h` and then ATC tripped emergency braking.

## Root Cause

The platform speed limit was present on the station block, so this was not a station-block construction issue.

ATO handle selection reacted too late to the upcoming restriction. The evaluator sampled a very short horizon near the train head and could return neutral or insufficient braking while the near-future operational curve already required slowing for East Terminal. The previous current-point fix helped with display/handle mismatch but did not look far enough ahead to avoid entering the 20 km/h platform block too fast.

After fixing the lookahead horizon, a second issue appeared: physics would continue past the assigned path's final tile by falling back to the track's default exit. That could clear or bypass the intended terminal stop while the train was still moving.

## Relevant Code Locations

- `train_controller.py::_ato_calculate_handle`
- `train_controller.py::_evaluate_handles_against_curve`
- `train_controller.py::calculate_speed_curves`
- `bootstrap.py::bootstrap_runtime`
- `demo_main.py::setup_demo_train`

## Suggested Fix

Expand the evaluator horizon when a lower nonzero operational restriction is within a bounded lookahead window. This lets ATO track the curve and prepare for fast drops without clamping speed to the lowest future value.

Treat the active path end as a movement boundary in physics and do not complete/clear a path merely because the train head reaches the final tile while still moving.

## Verification

Added a literal bootstrap demo regression test that starts the demo runtime, runs `DEMO_TRAIN` at `dt=0.3` to match 3x play speed, and asserts:

- East Terminal's platform block inherits `20 km/h`.
- The train reaches East Terminal.
- The train does not exceed the East Terminal platform speed limit.
- ATC emergency braking does not trigger.
- The train stops in the East Terminal block rather than running past the assigned path.
