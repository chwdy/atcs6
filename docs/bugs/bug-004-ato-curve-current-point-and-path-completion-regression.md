# bug-004-ato-curve-current-point-and-path-completion-regression

**Status:** Fixed  
**Date:** 2026-04-07  
**Test reference:** `tests/test_trains_ato.py::TestHandleEvaluator::test_evaluate_brakes_when_current_speed_exceeds_curve_at_train`; `tests/test_trains_ato.py::TestPathCompletionStateAndCabDirectionPlan`

## Symptom

Recent ATO changes could make the train handle display appear inconsistent with the train monitor speed curve. In particular, the evaluator sampled only future distances and did not score the current curve point at the train head (`distance_cm = 0`), so it could choose a non-braking handle while the displayed ATO curve already showed the train above the operational limit.

A related completion regression left stopped trains in `RUNNING_MOVING` or `RUNNING_WAITING` after all path/route/destination context had been cleared. This affected end-of-path behavior and made the final approach state look unsafe or stuck.

## Root Cause

`TrainController._evaluate_handles_against_curve()` built sample distances from `horizon / sample_count` through `horizon`, excluding `0 cm`. That meant current overspeed relative to the displayed ATO curve was not part of handle scoring.

`TrainController._is_standalone_path_complete()` also hard-gated completion on `path_backed_run_active`. Some flows had no surviving path context but also had `path_backed_run_active=False`, so the completion transition to `STANDBY` was blocked.

## Relevant Code Locations

- `train_controller.py::_evaluate_handles_against_curve`
- `train_controller.py::_ato_calculate_handle`
- `train_controller.py::_is_standalone_path_complete`
- `train_physics.py::tick`
- `path_dispatcher.py` path completion cleanup

## Suggested Fix

Include `distance_cm = 0` in ATO curve scoring so the selected handle accounts for the current train-head operational limit shown in the monitor.

Allow stopped trains with no active path, route, or ATO destination context to complete to `STANDBY` even if `path_backed_run_active` is false. Keep the guard that prevents standby while live path/route/destination context remains.

## Verification

Added a focused evaluator regression test for current-point curve overspeed and reran the ATO/speed-curve/dispatcher test selection.
