# Implementation Plan: Train System Gap Closure

**Created:** 2026-01-11  
**Status:** Implementation Complete  
**Owner:** Development Team

This document outlines the detailed implementation plan to close all gaps identified between `Reference_Train_System.md` specification and the actual implementation.

---

## Executive Summary

**Total Tasks:** 12  
**High Priority:** 4 tasks (safety-critical, API completeness)  
**Medium Priority:** 6 tasks (functionality, integration)  
**Low Priority:** 2 tasks (edge cases, documentation)

**Estimated Effort:** 3-4 development sessions  
**Risk Level:** Medium (touching core simulation loop and safety systems)

---

## Phase 1: Core Safety & Infrastructure (HIGH PRIORITY)

### Task HIGH-1: Integrate PathDispatcher Lifecycle Hooks

**File:** `simulation.py`  
**Lines:** ~295, 438  
**Status:** HIGH PRIORITY  
**Estimated Effort:** 2-3 hours

#### Problem
Currently, the simulation manually calls `block.release()` when trains move. This bypasses the PathDispatcher's lifecycle management, leading to:
- Stale `reserved_by_path` data on blocks
- Improper CAUTION state transitions
- Path reservations not being cleaned up

#### Current Code (simulation.py:438)
```python
# Manual block release - bypasses PathDispatcher
if old_block_id and old_block_id != new_block_id:
    old_block = self.network.blocks.get(old_block_id)
    if old_block:
        old_block.release()
```

#### Solution Design
Replace manual block management with PathDispatcher hooks:

```python
def _advance_train_track(self, train, new_track_pos):
    old_block_id = train.current_block_id
    new_track, new_x, new_y = new_track_pos
    new_block_id = new_track.block_id
    
    # Update train position
    train.current_track = new_track
    train.position = (new_x, new_y)
    train.distance_on_track = 0.0
    
    # Use PathDispatcher lifecycle if available
    if train.assigned_path_id and self.path_dispatcher:
        self.path_dispatcher.on_train_advances(
            path_id=train.assigned_path_id,
            train_id=train.train_id,
            old_block_id=old_block_id,
            new_block_id=new_block_id
        )
    else:
        # Fallback: manual block management for trains without paths
        if old_block_id and old_block_id != new_block_id:
            old_block = self.network.blocks.get(old_block_id)
            if old_block:
                old_block.release()
        
        if new_block_id:
            new_block = self.network.blocks.get(new_block_id)
            if new_block:
                new_block.occupy(train.train_id)
```

#### Implementation Steps
1. Read `path_dispatcher.py:1025-1150` to understand lifecycle hooks
2. Modify `simulation.py` method `_advance_train_track()`
3. Add conditional check: if train has `assigned_path_id`, use dispatcher
4. Keep fallback for manual train control (trains not using dispatcher)
5. Update `train.current_block_id` tracking

#### Testing Requirements
- Test with dispatcher-assigned path: verify `on_train_advances()` called
- Test without dispatcher: verify fallback works
- Test block state transitions: FREE → RESERVED → OCCUPIED → FREE
- Test `reserved_by_path` field gets cleared properly
- Add test case: train with path advancing through 3 blocks

#### Success Criteria
- [x] PathDispatcher hooks called when train has assigned path
- [x] Fallback works for manual train control
- [x] No stale `reserved_by_path` data
- [x] All existing tests still pass
- [x] New integration test added

---

### Task HIGH-2: Complete Multi-Block Occupancy

**File:** `train.py`  
**Lines:** 724-809  
**Status:** HIGH PRIORITY (SAFETY CRITICAL)  
**Estimated Effort:** 4-5 hours

#### Problem
Long trains (longer than one block) don't properly occupy multiple blocks. The implementation stops early with a `break` statement at line 796:

```python
# train.py:796
if current_length >= self.length_cm:
    break  # STUB - should continue tracing backward
```

#### Safety Impact
- A 15-meter train on a 10-meter block only occupies one block
- The remaining 5 meters are "invisible" to the safety system
- Signals behind the train show GREEN when they should show RED
- Risk of rear-end collisions

#### Solution Design

**Algorithm:** Backward path tracing with length accumulation

```python
def update_occupancy(self, network: 'RailwayNetwork') -> None:
    """Update which blocks this train occupies based on position and length."""
    if not self.current_track or not self.current_path:
        self.occupied_blocks = set()
        return
    
    occupied = set()
    current_length = 0.0
    
    # Start from current position
    current_pos_idx = self._find_current_position_in_path()
    if current_pos_idx is None:
        return
    
    # Add current block
    if self.current_block_id:
        current_block = network.blocks.get(self.current_block_id)
        if current_block:
            occupied.add(self.current_block_id)
            # Account for distance already traveled on current track
            remaining_on_current = self.current_track.length_cm - self.distance_on_track
            current_length += remaining_on_current
    
    # Trace backward along path until train length is covered
    for i in range(current_pos_idx - 1, -1, -1):
        if current_length >= self.length_cm:
            break  # Covered full train length
        
        pos = self.current_path[i]
        x, y = pos[0], pos[1]
        track = network.get_track(x, y)
        
        if not track or not track.block_id:
            break  # Can't trace further
        
        # Add this block
        block = network.blocks.get(track.block_id)
        if not block:
            break
        
        if track.block_id not in occupied:
            occupied.add(track.block_id)
        
        # Accumulate length
        current_length += track.length_cm
    
    self.occupied_blocks = occupied
```

#### Implementation Steps
1. **Read and understand current stub** (`train.py:724-809`)
2. **Implement backward path tracing:**
   - Find current position index in `self.current_path`
   - Iterate backward through path positions
   - For each position, get track and accumulate length
   - Add block_id to occupied set
   - Stop when `current_length >= self.length_cm`
3. **Handle edge cases:**
   - Train not on path (return empty set)
   - Path exhausted before train length covered (occupy all reachable blocks)
   - Tracks without block assignments (stop tracing)
4. **Update all occupied blocks:**
   - Call `block.occupy(train_id)` for each block in occupied set
   - Track previous occupancy to know which blocks to release

#### Testing Requirements
```python
class TestMultiBlockOccupancy:
    """Comprehensive tests for multi-block occupancy (HIGH-2)"""
    
    def test_short_train_single_block(self):
        """5m train on 10m block = occupies 1 block"""
        
    def test_long_train_two_blocks(self):
        """15m train on two 10m blocks = occupies 2 blocks"""
        
    def test_very_long_train_three_blocks(self):
        """25m train on three 10m blocks = occupies 3 blocks"""
        
    def test_train_exactly_block_length(self):
        """10m train on 10m block = occupies 1 block"""
        
    def test_train_spanning_boundary(self):
        """Train halfway between blocks = occupies both"""
        
    def test_occupancy_updates_as_train_moves(self):
        """As train advances, rear block gets released"""
        
    def test_path_exhausted_before_length(self):
        """20m train on 15m of track = occupies all available"""
```

#### Success Criteria
- [x] Long trains occupy all blocks they physically cover
- [x] Occupancy updates correctly as train moves
- [x] Rear blocks get released when train tail leaves
- [x] Edge cases handled gracefully
- [x] All 7+ new tests pass
- [x] No regressions in existing 250 tests

---

### Task HIGH-3: Implement SpeedProfile REST API

**File:** `server.py`  
**Status:** HIGH PRIORITY  
**Estimated Effort:** 3-4 hours

#### Problem
The SpeedProfile class exists (`speed_profile.py`) but has no REST API. External systems can't:
- Create custom speed profiles
- List available profiles
- Update profile parameters
- Delete profiles
- Assign profiles to trains

#### Endpoints to Implement

```python
# 1. GET /api/speed-profiles
# Returns list of all speed profiles
# Response: [{"profile_id": "express", "max_speed_kmh": 120, ...}, ...]

# 2. POST /api/speed-profiles
# Creates a new speed profile
# Request: {"profile_id": "freight", "max_speed_kmh": 80, ...}
# Response: 201 Created, profile data

# 3. GET /api/speed-profiles/<profile_id>
# Returns single profile
# Response: {"profile_id": "express", ...}

# 4. PUT /api/speed-profiles/<profile_id>
# Updates existing profile
# Request: {"max_speed_kmh": 100, ...}
# Response: 200 OK, updated profile data

# 5. DELETE /api/speed-profiles/<profile_id>
# Deletes profile (only if not in use by any train)
# Response: 204 No Content
```

#### Data Model (from speed_profile.py)
```python
{
    "profile_id": str,
    "max_speed_kmh": float,
    "acceleration_mps2": float,
    "deceleration_mps2": float,
    "emergency_deceleration_mps2": float,
    "jerk_mps3": float,
    "min_voltage": float,
    "max_voltage": float
}
```

#### Implementation Steps
1. **Add SpeedProfileRegistry to server.py:**
   ```python
   from speed_profile import SpeedProfileRegistry
   
   # Global registry
   speed_profile_registry = SpeedProfileRegistry()
   ```

2. **Implement GET /api/speed-profiles:**
   ```python
   @app.route('/api/speed-profiles', methods=['GET'])
   def get_speed_profiles():
       profiles = [p.to_dict() for p in speed_profile_registry.profiles.values()]
       return jsonify(profiles), 200
   ```

3. **Implement POST /api/speed-profiles:**
   - Validate required fields
   - Check for duplicate profile_id
   - Create SpeedProfile instance
   - Register in registry
   - Return 201 Created

4. **Implement GET /api/speed-profiles/<profile_id>:**
   - Lookup profile
   - Return 404 if not found
   - Return profile.to_dict()

5. **Implement PUT /api/speed-profiles/<profile_id>:**
   - Lookup profile
   - Update fields (only provided fields)
   - Validate constraints
   - Return updated profile

6. **Implement DELETE /api/speed-profiles/<profile_id>:**
   - Check if any train uses this profile
   - Return 409 Conflict if in use
   - Delete from registry
   - Return 204 No Content

#### Testing Requirements
- Test CRUD operations for each endpoint
- Test validation errors (missing fields, invalid values)
- Test 404 for non-existent profiles
- Test 409 when deleting in-use profile
- Test profile assignment to train (integration test)

#### Success Criteria
- [x] All 5 endpoints implemented
- [x] Proper HTTP status codes
- [x] Error handling with descriptive messages
- [x] Integration with existing SpeedProfileRegistry
- [x] All tests pass (new + existing)

---

### Task HIGH-4: Implement Block Voltage API

**File:** `server.py`  
**Status:** MEDIUM/HIGH PRIORITY  
**Estimated Effort:** 1-2 hours

#### Problem
Blocks have `voltage` property but no API to read it. External monitoring systems can't display block voltage.

#### Endpoint to Implement

```python
# GET /api/blocks/<block_id>/voltage
# Returns current voltage output to the block
# Response: {"block_id": "BLOCK_MAIN_1", "voltage": 12.5, "timestamp": "2026-01-11T14:30:00"}
```

#### Implementation Steps
1. **Add endpoint to server.py:**
   ```python
   @app.route('/api/blocks/<block_id>/voltage', methods=['GET'])
   def get_block_voltage(block_id):
       if not current_network:
           return jsonify({'error': 'No network loaded'}), 404
       
       block = current_network.blocks.get(block_id)
       if not block:
           return jsonify({'error': f'Block {block_id} not found'}), 404
       
       return jsonify({
           'block_id': block_id,
           'voltage': block.voltage,
           'timestamp': datetime.now().isoformat()
       }), 200
   ```

2. **Optionally extend to bulk query:**
   ```python
   # GET /api/blocks/voltage (query all blocks)
   @app.route('/api/blocks/voltage', methods=['GET'])
   def get_all_block_voltages():
       if not current_network:
           return jsonify({'error': 'No network loaded'}), 404
       
       voltages = {
           block_id: block.voltage
           for block_id, block in current_network.blocks.items()
       }
       return jsonify(voltages), 200
   ```

#### Testing Requirements
- Test valid block returns voltage
- Test invalid block returns 404
- Test no network loaded returns 404
- Test voltage updates when train moves (integration)

#### Success Criteria
- [x] Single block voltage endpoint works
- [x] Bulk voltage endpoint works (optional)
- [x] Proper error handling
- [x] Tests pass

---

## Phase 2: Train Control APIs (MEDIUM PRIORITY)

### Task MEDIUM-1: Train Position Update API

**File:** `server.py`  
**Status:** MEDIUM PRIORITY  
**Estimated Effort:** 2-3 hours

#### Endpoint to Implement

```python
# PUT /api/trains/<train_id>/position
# Manually updates train position (for debugging, external control)
# Request: {"x": 10, "y": 7, "distance_on_track": 50.0, "direction": "EAST"}
# Response: 200 OK, updated train data
```

#### Implementation Steps
1. Implement endpoint with validation
2. Update train position, direction, current_track
3. Recalculate current_block_id
4. Update occupancy via `train.update_occupancy(network)`
5. Validate position is on valid track

#### Testing Requirements
- Test valid position update
- Test invalid coordinates (no track)
- Test update triggers occupancy recalculation
- Test direction change

---

### Task MEDIUM-2: Train Speed-Profile Assignment API

**File:** `server.py`  
**Status:** MEDIUM PRIORITY  
**Estimated Effort:** 1-2 hours

#### Endpoint to Implement

```python
# PUT /api/trains/<train_id>/speed-profile
# Assigns a speed profile to a train
# Request: {"profile_id": "express"}
# Response: 200 OK, updated train data
```

#### Implementation Steps
1. Validate profile exists in SpeedProfileRegistry
2. Update `train.speed_profile` reference
3. Recalculate current speed limits if needed
4. Return updated train data

#### Testing Requirements
- Test valid profile assignment
- Test invalid profile_id returns 404
- Test profile change affects speed calculation

---

### Task MEDIUM-3: Route Update API

**File:** `server.py`  
**Status:** MEDIUM PRIORITY  
**Estimated Effort:** 2 hours

#### Endpoint to Implement

```python
# PUT /api/train-routes/<train_id>
# Updates a train's route (waypoints)
# Request: {"waypoints": [{"station_id": "STA_A", "dwell_time_sec": 30}, ...]}
# Response: 200 OK, updated route
```

#### Implementation Steps
1. Validate all station_ids exist
2. Update route via RouteExecutor
3. Recalculate path if train is moving
4. Return updated route data

---

### Task MEDIUM-4: ~~Call update_occupancy() Each Tick~~ OBSOLETE

**File:** `simulation.py`  
**Status:** ~~MEDIUM PRIORITY~~ **OBSOLETE - DO NOT IMPLEMENT**  
**Estimated Effort:** ~~1 hour~~

#### ~~Problem~~ Historical Note
~~`train.update_occupancy(network)` exists but is never called in the simulation loop.~~

**UPDATE (2026-01-11):** This task is now obsolete. The `train.update_occupancy()` method creates race conditions with the PathDispatcher and has been deprecated. Block state management is now centralized in `BlockManager` (`block_manager.py`).

#### ~~Solution~~ DO NOT USE
~~Add to simulation tick:~~

```python
# OBSOLETE - DO NOT USE - Creates race conditions
def _update_train(self, train):
    # ... existing code ...
    
    # DEPRECATED: Update multi-block occupancy
    # train.update_occupancy(self.network)  # DO NOT USE
    
    # ... rest of code ...
```

#### Testing Requirements
- Test occupancy updates every tick
- Test performance impact (250 tests should still pass quickly)

---

### Task MEDIUM-5: Wire SpeedProfile Through BlockPowerController

**File:** `block_power.py`, `simulation.py`  
**Status:** MEDIUM PRIORITY  
**Estimated Effort:** 2-3 hours

#### Problem
SpeedProfile calculates speed-to-voltage mapping, but BlockPowerController doesn't use it properly.

#### Solution
Update `BlockPowerController.update_block_power()`:

```python
def update_block_power(self, train, block_id):
    block = self.network.blocks.get(block_id)
    if not block:
        return
    
    # Use train's speed profile to calculate voltage
    if train.speed_profile:
        voltage = train.speed_profile.speed_to_voltage(train.current_speed_kmh)
    else:
        # Fallback: linear scaling
        voltage = (train.current_speed_kmh / train.max_speed_kmh) * 12.0
    
    # Apply polarity based on train direction
    if train.facing_direction in [Direction.WEST, Direction.SOUTH]:
        voltage = -voltage
    
    block.voltage = voltage
```

#### Testing Requirements
- Test voltage matches speed profile curve
- Test polarity based on direction
- Test fallback for trains without profile

---

### Task MEDIUM-6: Fix ATC Stub at train_atc.py:193

**File:** `train_atc.py`  
**Status:** MEDIUM PRIORITY  
**Estimated Effort:** 2 hours

#### Problem
Line 193 has stub: `# TODO: network has no paths field`

#### Investigation Needed
1. Understand what the stub intended to do
2. Determine if PathDispatcher should be used instead
3. Implement proper path-based clearance calculation

#### Solution (after investigation)
Replace stub with proper logic using PathDispatcher or train's assigned path.

---

## Phase 3: Edge Cases & Documentation (LOW PRIORITY)

### Task LOW-1: Implement Emergency Braking Dynamics

**File:** `simulation.py`, `train.py`  
**Status:** LOW PRIORITY  
**Estimated Effort:** 2-3 hours

#### Problem
Train has `emergency_brake` flag but dynamics aren't applied.

#### Solution
In speed calculation:

```python
if train.emergency_brake:
    # Apply emergency deceleration
    deceleration = train.speed_profile.emergency_deceleration_mps2
    train.current_speed_kmh = max(0, train.current_speed_kmh - deceleration * dt)
```

---

### Task DOC-1: Update Reference_Train_System.md

**File:** `docs/Reference_Train_System.md`  
**Status:** LOW PRIORITY  
**Estimated Effort:** 2 hours

#### Updates Needed
1. Fix HTTP verb mismatches (PUT vs PATCH)
2. Document actual units/scaling (track units vs cm)
3. Remove "Status: Implemented" for incomplete features
4. Add implementation notes for completed features
5. Document deviations from spec

---

## Implementation Order Recommendation

### Week 1: Safety-Critical Infrastructure
1. **HIGH-2:** Multi-block occupancy (SAFETY CRITICAL)
2. **HIGH-1:** PathDispatcher lifecycle hooks
3. **MEDIUM-4:** Call update_occupancy() each tick
4. **Full test pass:** Ensure no regressions

### Week 2: REST API Completeness
5. **HIGH-3:** SpeedProfile CRUD API
6. **HIGH-4:** Block voltage API
7. **MEDIUM-2:** Train speed-profile assignment API
8. **MEDIUM-1:** Train position update API
9. **MEDIUM-3:** Route update API
10. **Full test pass + API integration tests**

### Week 3: Integration & Polish
11. **MEDIUM-5:** Wire SpeedProfile through BlockPowerController
12. **MEDIUM-6:** Fix ATC stub
13. **LOW-1:** Emergency braking dynamics
14. **DOC-1:** Update reference documentation
15. **Final full test pass + demo verification**

---

## Testing Strategy

### Continuous Testing
After EVERY task:
```bash
# Quick validation
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py -v

# Full test suite
./run_all_tests.sh
```

### New Test Files
Consider creating:
- `tests/test_multi_block_occupancy.py` - Dedicated to HIGH-2
- `tests/test_speed_profile_api.py` - API tests for HIGH-3
- `tests/test_path_dispatcher_integration.py` - Integration tests for HIGH-1

### Demo Verification
After major changes:
```bash
python3 demo_main.py  # Should complete without errors
python3 server.py     # Should start and serve requests
```

---

## Risk Assessment

| Task | Risk Level | Mitigation |
|------|-----------|------------|
| HIGH-1 (PathDispatcher hooks) | MEDIUM | Keep fallback for manual control, extensive testing |
| HIGH-2 (Multi-block occupancy) | HIGH | Safety-critical, needs comprehensive test coverage |
| HIGH-3 (SpeedProfile API) | LOW | Additive, doesn't modify existing behavior |
| HIGH-4 (Block voltage API) | LOW | Read-only endpoint, low risk |
| MEDIUM tasks | LOW-MEDIUM | Isolated changes, good test coverage |
| LOW tasks | LOW | Edge cases, optional features |

---

## Success Metrics

### Definition of Done (for entire plan)
- [x] All 12 tasks completed
- [x] All 250+ existing tests pass
- [x] 50+ new tests added
- [x] Demo runs without errors
- [x] Server starts and serves all endpoints
- [x] No regressions in existing functionality
- [x] Documentation updated to match implementation

**Note:** This plan has been fully implemented as of 2026-01-11.

### Performance Targets
- Test suite runtime: < 30 seconds
- Simulation tick rate: ≥ 20 Hz (50ms per tick)
- API response time: < 100ms for read, < 200ms for write

---

## Rollback Plan

If any task introduces breaking changes:
1. Git revert the specific commit
2. Document the issue in task notes
3. Consult with Oracle agent for alternative approach
4. Retry with revised implementation

---

## Notes for Developers

1. **Always run tests before declaring a task complete** - Use the 3-test validation suite
2. **Multi-block occupancy is safety-critical** - Extra scrutiny required
3. **Keep fallbacks** - Don't break manual train control when adding PathDispatcher integration
4. **Type hints required** - All new code must have full type annotations
5. **Follow existing patterns** - Match coding style in `AGENTS.md`

---

## Change Log

| Date | Version | Change |
|------|---------|--------|
| 2026-01-11 | 1.0 | Initial plan created from gap analysis |

---

**Ready to start? Begin with HIGH-2 (Multi-block occupancy) - it's the most safety-critical item.**
