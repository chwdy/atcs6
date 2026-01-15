# API Test Coverage Analysis

## Summary

- **Total API Endpoints**: 92
- **Endpoints Tested in test_api.js**: 61
- **Endpoints NOT Tested**: 31
- **Test Coverage**: 66.3%

## API Endpoint Inventory

### 1. Static File Serving (2 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/` | GET | ✅ | Serves index.html |
| `/<path:path>` | GET | ✅ | Serves static files |

### 2. Network Endpoints (3 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/network` | GET | ✅ | Get complete network state |
| `/api/network/stats` | GET | ✅ | Get network statistics |
| `/api/network/clear` | POST | ✅ | Clear network (fresh canvas) |

### 3. Track Endpoints (6 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/track/<x>/<y>` | GET | ✅ | Get single track segment |
| `/api/track/<x>/<y>` | PATCH | ✅ | Update track segment |
| `/api/track/<x>/<y>` | DELETE | ✅ | Delete track segment |
| `/api/tracks/bulk` | POST | ✅ | Add multiple tracks (drag-to-build) |
| `/api/tracks/bulk-delete` | POST | ✅ | Delete multiple tracks (eraser drag) |

### 4. Block Endpoints (14 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/blocks` | GET | ✅ | List all blocks |
| `/api/blocks` | POST | ✅ | Create new block |
| `/api/blocks/<id>` | GET | ✅ | Get block details |
| `/api/blocks/<id>` | PATCH | ✅ | Update block properties |
| `/api/blocks/<id>` | DELETE | ✅ | Delete block |
| `/api/blocks/<id>/cycle-state` | POST | ✅ | Cycle block state (testing) |
| `/api/blocks/create-with-tracks` | POST | ✅ | Create block with tracks (validated) |
| `/api/blocks/<id>/extend` | POST | ✅ | Extend block with new tracks |
| `/api/blocks/<id>/tracks` | POST | ✅ | Add tracks to existing block |
| `/api/blocks/<id>/tracks` | DELETE | ✅ | Remove tracks from block |
| `/api/blocks/<id>/voltage` | GET | ⛔ | Get block voltage (power system) |
| `/api/blocks/<id>/end_tiles` | GET | ⛔ | Get start/end tiles of block |
| `/api/blocks/<id>/can_extend` | POST | ✅ | Validate if position can be added |

### 5. Signal Endpoints (10 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/signal` | POST | ✅ | Create signal with validation |
| `/api/signal/<id>` | GET | ✅ | Get signal by ID |
| `/api/signal/<id>` | PATCH | ✅ | Update signal properties |
| `/api/signal/<id>` | DELETE | ✅ | Delete signal by ID |
| `/api/signal/<x>/<y>` | GET | ✅ | Get signal at position |
| `/api/signal/<x>/<y>` | DELETE | ✅ | Delete signal at position |
| `/api/signal/validate/<x>/<y>` | GET | ✅ | Validate signal placement |
| `/api/signal/<id>/reachable-blocks` | GET | ✅ | Get reachable blocks (UI dropdown) |

### 6. Station Endpoints (12 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/station` | POST | ✅ | Create station |
| `/api/station/<id>` | GET | ✅ | Get station by ID |
| `/api/station/<id>` | PATCH | ✅ | Update station properties |
| `/api/station/<id>` | DELETE | ✅ | Delete station |
| `/api/station/validate/<x>/<y>` | GET | ✅ | Validate station placement |
| `/api/station/<id>/extend` | POST | ✅ | Extend station platform |
| `/api/station/<id>/remove_tile` | POST | ✅ | Remove tile from station |
| `/api/station/<id>/end_tiles` | GET | ✅ | Get start/end tiles |
| `/api/station/<id>/can_extend` | POST | ✅ | Validate extension |
| `/api/station/<id>/assign_tile` | POST | ✅ | Assign tile to block |
| `/api/station/<id>/unassign_tile` | POST | ✅ | Unassign tile from block |
| `/api/station/<id>/block_assignments` | GET | ✅ | Get block assignments |

### 7. Train Endpoints (17 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/trains` | GET | ✅ | List all trains |
| `/api/trains` | POST | ✅ | Create new train |
| `/api/trains/<id>` | GET | ✅ | Get train details |
| `/api/trains/<id>` | PATCH | ⛔ | Update train (deprecated) |
| `/api/trains/<id>` | DELETE | ✅ | Delete train |
| `/api/trains/<id>/power-on` | POST | ✅ | Power on train (OFF→IDLE) |
| `/api/trains/<id>/power-off` | POST | ✅ | Power off train |
| `/api/trains/<id>/set-direction` | POST | ✅ | Set direction (IDLE→STANDBY) |
| `/api/trains/<id>/clear-direction` | POST | ⛔ | Clear direction (STANDBY→IDLE) |
| `/api/trains/<id>/depart` | POST | ⛔ | Start moving (STANDBY→RUNNING_MOVING) |
| `/api/trains/<id>/arrive` | POST | ⛔ | Arrive at station |
| `/api/trains/<id>/turn-around` | POST | ✅ | Flip facing direction (cab switch) |
| `/api/trains/<id>/enable-manual` | POST | ✅ | Enable manual control |
| `/api/trains/<id>/disable-manual` | POST | ✅ | Disable manual control |
| `/api/trains/<id>/manual-speed` | POST | ✅ | Set manual target speed |
| `/api/trains/<id>/travel-direction` | POST | ✅ | Set travel direction (forward/reverse) |
| `/api/trains/<id>/emergency-stop` | POST | ✅ | Emergency stop |
| `/api/trains/<id>/position` | PUT | ⛔ | Update train position |
| `/api/trains/<id>/speed-profile` | PUT | ⛔ | Assign speed profile |

### 8. Train Route Endpoints (6 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/train-routes` | GET | ⛔ | List all routes |
| `/api/train-routes` | POST | ⛔ | Create new route |
| `/api/train-routes/<id>` | GET | ⛔ | Get route details |
| `/api/train-routes/<id>` | PUT | ⛔ | Update route |
| `/api/train-routes/<id>` | DELETE | ⛔ | Delete route |
| `/api/trains/<id>/assign-route` | PUT | ⛔ | Assign route to train |
| `/api/train-routes/<id>/activate` | POST | ⛔ | Activate route |
| `/api/train-routes/<id>/deactivate` | POST | ⛔ | Deactivate route |

### 9. Schedule Endpoints (4 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/schedules` | GET | ⛔ | List all schedules |
| `/api/schedules` | POST | ⛔ | Create schedule |
| `/api/schedules/<id>` | GET | ⛔ | Get schedule details |
| `/api/schedules/<id>` | DELETE | ⛔ | Delete schedule |

### 10. Simulation Endpoints (1 endpoint)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/simulation/control` | POST | ✅ | Control simulation (start/stop/speed) |

### 11. Layout Persistence Endpoints (4 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/layouts` | GET | ✅ | List saved layouts |
| `/api/layouts` | POST | ✅ | Save current layout |
| `/api/layouts/<name>/load` | POST | ✅ | Load layout |
| `/api/layouts/<name>` | DELETE | ✅ | Delete layout |

### 12. Path Dispatcher Endpoints (7 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/paths` | GET | ⛔ | List active paths |
| `/api/paths` | POST | ⛔ | Create new path |
| `/api/paths/<id>` | GET | ⛔ | Get path details |
| `/api/paths/<id>` | DELETE | ⛔ | Cancel path |
| `/api/paths/<id>/extend` | PUT | ⛔ | Extend path |
| `/api/paths/<id>/reachable-extensions` | GET | ⛔ | Get reachable extensions |
| `/api/paths/<id>/complete` | POST | ⛔ | Mark path complete |
| `/api/dispatcher/reachable/<id>` | GET | ✅ | Get reachable endpoints from block |
| `/api/dispatcher/paths` | POST | ✅ | Find all paths between blocks |

### 13. Speed Profile Endpoints (5 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/speed-profiles` | GET | ⛔ | List all speed profiles |
| `/api/speed-profiles` | POST | ⛔ | Create speed profile |
| `/api/speed-profiles/<id>` | GET | ⛔ | Get speed profile |
| `/api/speed-profiles/<id>` | PUT | ⛔ | Update speed profile |
| `/api/speed-profiles/<id>` | DELETE | ⛔ | Delete speed profile |

### 14. Scale Configuration Endpoints (2 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/scale` | GET | ⛔ | Get current model scale |
| `/api/scale` | PUT | ⛔ | Set model scale |

### 15. Switch Configuration Endpoints (7 endpoints)
| Endpoint | Method | Tested | Notes |
|----------|--------|--------|-------|
| `/api/switches` | GET | ⛔ | Get all switches |
| `/api/switches/<id>` | GET | ⛔ | Get switch details |
| `/api/tracks/<x>/<y>/switch` | POST | ⛔ | Create switch at track |
| `/api/switches/<id>` | PUT | ⛔ | Update switch configuration |
| `/api/switches/<id>` | DELETE | ⛔ | Remove switch configuration |
| `/api/switches/<id>/state` | PUT | ⛔ | Change switch state (MAIN/DIVERGE) |
| `/api/switches/<id>/lock` | POST | ⛔ | Lock switch for path |
| `/api/switches/<id>/unlock` | POST | ⛔ | Unlock switch |

## Critical Untested Endpoints (High Priority)

### 1. Train Position Update (`/api/trains/<id>/position` PUT)
**Priority: HIGH** - This is related to the bug fix documented in `plans/train_debug_panel_backend_issue.md`

**Bug Context:**
- The train debug panel was showing end_a/end_b positions stuck at initialization values
- `train.position.distance` was updating, but `train.position.end_a` and `train.position.end_b` were NOT updating
- Root cause: Backend `Train.to_dict()` and position update logic in `simulation.py` and `train_movement.py`

**Fix Implemented:**
- `network.py` line 2167-2275: `recalculate_train_position()` method added
- `simulation.py` line 348-358: Calls `recalculate_train_position()` after train movement
- `train.py` line 1278-1418: `to_dict()` properly serializes TrainPosition with end_a/end_b

**Tests Needed:**
- Test that PUT `/api/trains/<id>/position` correctly updates `end_a` and `end_b`
- Test that `distance_on_track` converts correctly to tile distance ratio
- Test that occupied_tiles is calculated correctly from TrainPosition

### 2. Block Voltage (`/api/blocks/<id>/voltage` GET)
**Priority: MEDIUM** - Part of power system (BlockPowerController)

### 3. Train Route Management (8 endpoints)
**Priority: MEDIUM** - Route/waypoint system for train dispatch

### 4. Schedule Management (4 endpoints)
**Priority: LOW** - Time-based scheduling system

### 5. Speed Profile Management (5 endpoints)
**Priority: LOW** - Speed/acceleration profiles

### 6. Switch Configuration (7 endpoints)
**Priority: HIGH** - Critical for interlocking and path routing

### 7. Path Dispatcher (5 endpoints)
**Priority: MEDIUM** - Block reservation and path management

## Recent Bug Fixes Requiring Regression Tests

### Bug Fix #1: Train Position Update Not Reflecting in Debug Panel
**Date:** Based on file timestamps and plan documentation
**Files Modified:**
- `network.py` - Added `recalculate_train_position()`
- `simulation.py` - Integrated position recalculation
- `train.py` - Fixed `to_dict()` serialization
- `static/js/app.js` - Frontend updates (consolidated loop)
- `static/js/editor-trains.js` - Added `updateControlPanelValuesFromData()`
- `static/js/editor.js` - Skip train-control during simulation

**Current Tests:**
- `tests/test_train_position_update.py` exists with 3 tests:
  1. `test_position_end_a_updates_during_movement` - Tests end_a updates
  2. `test_to_dict_serialization` - Tests serialization format
  3. `test_occupied_tiles_order_leading_end_a` - Tests occupied_tiles ordering

**Gaps:**
- No test for PUT `/api/trains/<id>/position` endpoint
- No test for `distance_on_track` to ratio conversion
- No test for multiple tile occupancy scenarios
- No test for edge cases (train at tile boundary, zero-length tiles)

### Bug Fix #2: Signal on Switch Validation
**Files:**
- `signal_validation.py` - Signal placement validation
- `network.py` - `add_signal()` checks for switches

**Current Tests:**
- `test_api.js` has `test_signal_on_switch_rejected()` - Frontend test
- Need backend Python test in `tests/test_backend.py`

### Bug Fix #3: Block Continuity and Splitting
**Files:**
- `network.py` - `remove_tile_from_block()`, `_check_block_connectivity()`
- `block_validation.py` - Validation logic

**Current Tests:**
- Some coverage in `tests/test_backend.py` but needs comprehensive edge case tests

## Recommendations

### 1. Create Comprehensive Regression Test Class
```python
class TestTrainPositionUpdateBugFix:
    """
    Comprehensive tests for train position update bug fixed in Jan 2026.
    
    BUG: train.position.end_a and train.position.end_b remained stuck at 
    initialization values during simulation, even though distance_on_track 
    was updating correctly.
    
    FIX: Added recalculate_train_position() in network.py and integrated 
    into simulation loop. Fixed Train.to_dict() to properly serialize 
    TrainPosition data.
    """
    
    def test_api_position_update_updates_end_a_and_end_b(self):
        """PUT /api/trains/<id>/position updates both ends correctly"""
        ...
    
    def test_position_update_converts_distance_ratio_to_cm(self):
        """distance_on_track conversion from ratio (0-1) to cm"""
        ...
    
    def test_position_update_multi_tile_occupancy(self):
        """Train spanning multiple tiles has correct occupied_tiles"""
        ...
    
    def test_position_update_at_tile_boundary(self):
        """Edge case: train head exactly at tile exit edge"""
        ...
    
    def test_position_update_with_zero_length_tiles(self):
        """Edge case: switches with 0cm length"""
        ...
```

### 2. Add Missing API Endpoint Tests to test_api.js

Priority order:
1. Switch configuration endpoints (critical for interlocking)
2. Train position update endpoint
3. Path dispatcher endpoints
4. Train route management endpoints
5. Speed profile endpoints
6. Schedule endpoints

### 3. Create Dedicated Test Files for Complex Systems

- `tests/test_switch_interlocking.py` - Switch locking, state changes, path conflicts
- `tests/test_path_dispatcher.py` - Path reservation, extension, completion
- `tests/test_train_routes.py` - Route creation, waypoint execution

## Test Coverage Metrics

- **Current Coverage**: 66.3% (61/92 endpoints)
- **Target Coverage**: 90%+ (83/92 endpoints)
- **Endpoints to Add**: 22 tests minimum
- **Regression Test Classes**: 1 comprehensive class (5+ tests)

## Next Steps

1. ✅ Create `API_TEST_COVERAGE_ANALYSIS.md` (this file)
2. ⏳ Create `TestTrainPositionUpdateBugFix` class in `tests/test_backend.py`
3. ⏳ Add missing endpoint tests to `tests/test_api.js`
4. ⏳ Run full test suite and verify all pass
5. ⏳ Document test results in completion message
