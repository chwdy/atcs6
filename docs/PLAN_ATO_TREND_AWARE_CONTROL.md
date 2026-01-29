# Implementation Plan: Trend-Aware ATO Control with Speed Curve Visualization

**Date:** 2026-01-28  
**Status:** Draft  
**Author:** AI Assistant + User Review

---

## Problem Statement

The current ATO (Automatic Train Operation) handle calculation is **stateless** and causes two critical issues:

### Issue 1: Handle Jittering Near Stops

When approaching a stop with a steep speed curve, the handle jumps wildly (e.g., P8 → P1 → B3 → P2) because each tick recalculates the "ideal" handle from scratch based purely on speed difference.

**Root cause:** No rate limiting, no memory of previous state.

### Issue 2: Speed Oscillation During Cruising

With target = 60 km/h and tolerance = 2 km/h:
1. Train accelerates to 58 km/h → enters tolerance → coast
2. Speed drops to 57.5 km/h → below target → accelerate
3. Speed rises to 58.1 km/h → enters tolerance → coast
4. Repeat forever

**Root cause:** Symmetric tolerance band causes oscillation.

### Issue 3: Braking Curve Mismatch

The ATC speed limit is calculated using 100% deceleration rate, but ATO uses partial braking (B1-B8 with 12.5%-100% multipliers). This mismatch can cause the train to overshoot.

**Root cause:** `operational_decel_ratio = 0.5` is too conservative, and braking curve doesn't match actual ATO behavior.

---

## Solution: Trend-Aware Control

Instead of phases or state machines, track the **speed trend** (derivative) and only adjust when the current action isn't working.

### Core Concepts

1. **Hysteresis via asymmetric margins**: Entry threshold ≠ exit threshold
2. **Trend detection**: Only increase power/brake if current action isn't producing results
3. **Rate limiting**: Handle changes limited to ±1 notch per tick (jerk limiting)
4. **Correct deceleration ratio**: Use 0.7 (70%) for operational braking curves

---

## Speed Zones

For `tolerance = 2.0 km/h`:
- **Entry margin** = `tolerance - 1 = 1.0 km/h` (strict)
- **Exit margin** = `tolerance + 1 = 3.0 km/h` (loose)

| Zone | Condition | Description |
|------|-----------|-------------|
| **Slow Down Zone** | `speed >= target - 1` | Need to reduce speed |
| **Dead Band** | `target - 3 <= speed < target - 1` | Do nothing |
| **Speed Up Zone** | `speed < target - 3` | Need to increase speed |

Example for target = 60 km/h:
- ≥ 59 km/h: Slow down zone
- 57-59 km/h: Dead band (cruise here)
- < 57 km/h: Speed up zone

---

## Trend Detection

```python
SPEED_TREND_EPSILON = 0.1  # km/h - ignore tiny fluctuations

speed_rising = current_speed > previous_speed + SPEED_TREND_EPSILON
speed_falling = current_speed < previous_speed - SPEED_TREND_EPSILON
speed_stable = not speed_rising and not speed_falling
```

---

## Control Logic Table

### Zone 1: Slow Down (speed >= target - 1)

| Current Handle | Speed Trend | Action | Rationale |
|----------------|-------------|--------|-----------|
| Power (P1-P5) | any | → Neutral | Stop accelerating immediately |
| Neutral | any | → B1 | Start gentle braking |
| Brake (B1-B7) | rising | → brake +1 notch | Current brake not enough |
| Brake (B1-B8) | falling/stable | → keep | Braking is working |

### Zone 2: Dead Band (target - 3 <= speed < target - 1)

| Current Handle | Speed Trend | Action | Rationale |
|----------------|-------------|--------|-----------|
| any | any | → keep | Do nothing, coast through |

### Zone 3: Speed Up (speed < target - 3)

| Current Handle | Speed Trend | Action | Rationale |
|----------------|-------------|--------|-----------|
| Brake (B2-B8) | any | → brake -1 notch | Release brake gradually |
| Brake (B1) | any | → Neutral | Brake fully released |
| Neutral | any | → P1 | Start gentle acceleration |
| Power (P1-P4) | falling | → power +1 notch | Current power not enough |
| Power (P1-P5) | rising/stable | → keep | Acceleration is working |

---

## Priority Override

The trend-aware logic applies **only for speed maintenance**. These conditions take priority:

1. **Emergency Brake Active** → return `HandlePosition.EB`
2. **Target = 0 (must stop)** → use braking notch based on current speed
3. **Approaching clearance limit** → use braking curve logic
4. **Otherwise** → use trend-aware speed maintenance

---

## Implementation Details

### File: `speed_profile.py`

**Change:**
```python
# Line 52-54
operational_decel_ratio: float = 0.7  # Changed from 0.5
```

**Rationale:** 70% of max deceleration is more realistic for operational braking and provides better margin.

---

### File: `enums.py`

**Add after `HANDLE_ORDER` (around line 205):**
```python
# Handle position index for notch arithmetic
# Lower index = more power, higher index = more brake
HANDLE_POSITION_INDEX = {
    HandlePosition.P5: 0,
    HandlePosition.P4: 1,
    HandlePosition.P3: 2,
    HandlePosition.P2: 3,
    HandlePosition.P1: 4,
    HandlePosition.N: 5,
    HandlePosition.B1: 6,
    HandlePosition.B2: 7,
    HandlePosition.B3: 8,
    HandlePosition.B4: 9,
    HandlePosition.B5: 10,
    HandlePosition.B6: 11,
    HandlePosition.B7: 12,
    HandlePosition.B8: 13,
    HandlePosition.EB: 14,
}

# Reverse lookup: index to handle position
INDEX_TO_HANDLE_POSITION = {v: k for k, v in HANDLE_POSITION_INDEX.items()}
```

---

### File: `train_controller.py`

#### New Field

Add to `TrainController` dataclass (around line 217):
```python
# Previous speed for trend detection
_previous_speed: float = 0.0
```

#### New Constants

Add near top of file:
```python
# Trend-aware control constants
SPEED_TREND_EPSILON = 0.1  # km/h - minimum change to detect trend
ATO_SPEED_TOLERANCE = 2.0  # km/h - base tolerance for hysteresis
ATO_ENTRY_MARGIN = ATO_SPEED_TOLERANCE - 1.0  # 1.0 km/h
ATO_EXIT_MARGIN = ATO_SPEED_TOLERANCE + 1.0   # 3.0 km/h
```

#### New Helper Methods

```python
def _detect_speed_trend(self, current_speed: float) -> str:
    """
    Detect speed trend based on previous speed.
    
    Returns:
        'rising', 'falling', or 'stable'
    """
    if current_speed > self._previous_speed + SPEED_TREND_EPSILON:
        return 'rising'
    elif current_speed < self._previous_speed - SPEED_TREND_EPSILON:
        return 'falling'
    return 'stable'


def _adjust_handle_toward_brake(self, current: HandlePosition) -> HandlePosition:
    """
    Move handle one notch toward braking (P5→P4→...→N→B1→...→B8).
    Returns B8 if already at B8.
    """
    from enums import HANDLE_POSITION_INDEX, INDEX_TO_HANDLE_POSITION
    
    current_idx = HANDLE_POSITION_INDEX.get(current, 5)  # Default to N
    # Don't go past B8 (index 13), don't touch EB
    new_idx = min(current_idx + 1, 13)
    return INDEX_TO_HANDLE_POSITION.get(new_idx, HandlePosition.B8)


def _adjust_handle_toward_power(self, current: HandlePosition) -> HandlePosition:
    """
    Move handle one notch toward power (B8→B7→...→N→P1→...→P5).
    Returns P5 if already at P5.
    """
    from enums import HANDLE_POSITION_INDEX, INDEX_TO_HANDLE_POSITION
    
    current_idx = HANDLE_POSITION_INDEX.get(current, 5)  # Default to N
    # Don't go past P5 (index 0)
    new_idx = max(current_idx - 1, 0)
    return INDEX_TO_HANDLE_POSITION.get(new_idx, HandlePosition.P5)


def _is_power_handle(self, handle: HandlePosition) -> bool:
    """Check if handle is a power notch (P1-P5)."""
    return handle in (
        HandlePosition.P1, HandlePosition.P2, HandlePosition.P3,
        HandlePosition.P4, HandlePosition.P5
    )


def _is_brake_handle(self, handle: HandlePosition) -> bool:
    """Check if handle is a brake notch (B1-B8)."""
    return handle in (
        HandlePosition.B1, HandlePosition.B2, HandlePosition.B3,
        HandlePosition.B4, HandlePosition.B5, HandlePosition.B6,
        HandlePosition.B7, HandlePosition.B8
    )
```

#### Updated `_ato_calculate_handle()` Method

Replace the existing method (lines 1222-1270) with:

```python
def _ato_calculate_handle(self) -> HandlePosition:
    """
    ATO automatically selects appropriate handle position using trend-aware control.

    The ATO system:
    1. Respects ATC limits (never exceeds)
    2. Uses trend-aware control to avoid oscillation
    3. Rate-limits handle changes to ±1 notch per tick
    4. Uses asymmetric hysteresis for stable cruising
    """
    train = self._get_train()
    if not train:
        return HandlePosition.N

    current_speed = train.current_speed
    current_handle = self.handle_position

    # Get effective target (ATC-limited)
    effective_target = min(
        train.max_speed,
        self.atc_speed_limit,
        self.current_block_speed_limit,
    )

    # Detect speed trend
    trend = self._detect_speed_trend(current_speed)
    
    # Update previous speed for next tick
    self._previous_speed = current_speed

    # =======================================================================
    # PRIORITY 1: Need to stop (ATC limit = 0 or no path)
    # =======================================================================
    if effective_target <= STOPPED_SPEED_THRESHOLD_KMH:
        if current_speed > STOPPED_SPEED_THRESHOLD_KMH:
            return self._calculate_braking_notch(current_speed)
        return HandlePosition.N

    # =======================================================================
    # PRIORITY 2: Approaching clearance limit (use braking curve)
    # =======================================================================
    profile = self._get_speed_profile()
    # Use operational deceleration for braking distance calculation
    operational_decel = profile.deceleration_rate * profile.operational_decel_ratio
    braking_distance = profile.get_braking_distance_cm(
        current_speed, 0.0, operational_decel
    )

    # Start braking if within 1.2x braking distance
    if self.clearance_ahead_cm < braking_distance * 1.2:
        return self._calculate_braking_notch(current_speed)

    # =======================================================================
    # PRIORITY 3: Trend-aware speed maintenance
    # =======================================================================
    
    # Zone 1: Slow down zone (speed >= target - entry_margin)
    if current_speed >= effective_target - ATO_ENTRY_MARGIN:
        if self._is_power_handle(current_handle):
            # Stop accelerating immediately
            return HandlePosition.N
        elif current_handle == HandlePosition.N:
            # Start gentle braking
            return HandlePosition.B1
        elif self._is_brake_handle(current_handle):
            if trend == 'rising':
                # Braking but speed still rising - brake harder
                return self._adjust_handle_toward_brake(current_handle)
            else:
                # Braking is working - keep current
                return current_handle
    
    # Zone 2: Dead band (target - exit_margin <= speed < target - entry_margin)
    elif current_speed >= effective_target - ATO_EXIT_MARGIN:
        # Do nothing - maintain current handle
        return current_handle
    
    # Zone 3: Speed up zone (speed < target - exit_margin)
    else:
        if self._is_brake_handle(current_handle):
            # Release brake gradually
            return self._adjust_handle_toward_power(current_handle)
        elif current_handle == HandlePosition.N:
            # Start gentle acceleration
            return HandlePosition.P1
        elif self._is_power_handle(current_handle):
            if trend == 'falling':
                # Accelerating but speed still falling - power up
                return self._adjust_handle_toward_power(current_handle)
            else:
                # Acceleration is working - keep current
                return current_handle

    # Fallback (should not reach here)
    return current_handle
```

#### Speed Curves Calculation (for visualization)

Add new method:

```python
def calculate_speed_curves(self, max_distance_cm: float = 500.0) -> List[Dict]:
    """
    Calculate ATC and operational speed limits at various distances ahead.
    
    Used for train monitor visualization.
    
    Args:
        max_distance_cm: Maximum distance to calculate curves for
        
    Returns:
        List of dicts with distance_cm, atc_limit, operational_limit
    """
    train = self._get_train()
    if not train:
        return []
    
    profile = self._get_speed_profile()
    max_speed = train.max_speed
    
    curves = []
    
    # Sample every 25cm
    step = 25.0
    distance = 0.0
    while distance <= max_distance_cm:
        # ATC limit: 100% deceleration (emergency ceiling)
        atc_limit = calculate_max_safe_speed(
            distance,
            profile.deceleration_rate,
            self.safety_margin_cm
        )
        
        # Operational limit: 70% deceleration (what ATO follows)
        operational_limit = calculate_max_safe_speed(
            distance,
            profile.deceleration_rate * profile.operational_decel_ratio,
            self.safety_margin_cm
        )
        
        curves.append({
            'distance_cm': distance,
            'atc_limit': min(atc_limit, max_speed),
            'operational_limit': min(operational_limit, max_speed)
        })
        
        distance += step
    
    return curves
```

---

### File: `routes/trains.py` or Lookahead Endpoint

Include speed curves in the lookahead response:

```python
# In the lookahead payload construction:
speed_curves = controller.calculate_speed_curves(
    max_distance_cm=lookahead_distance
)

payload = {
    "lookahead": {
        "blocks": [...],
        "signals": [...],
        "switches": [...],
        "stations": [...],
        "clearance_cm": clearance,
        "speed_curves": speed_curves  # NEW
    }
}
```

---

### File: `static/js/train-monitor.js`

#### Update `render()` method

Add call to draw speed curves after clearing canvas (around line 712):

```javascript
render() {
    const data = this.lookaheadData;
    if (!data) return;
    
    const rect = this.canvas.getBoundingClientRect();
    const width = rect.width;
    const height = rect.height;
    
    this.ctx.clearRect(0, 0, width, height);
    
    const padding = { left: 40, right: 10, top: 8, bottom: 50 };  // Increased left for Y-axis labels
    const speedCurveAreaHeight = 60;  // NEW: Reserve space for speed curves
    const blockTrackY = height * 0.5;  // Shift down to make room
    const stationTrackY = height * 0.75;
    const trackWidth = width - padding.left - padding.right;
    
    // ... existing code ...
    
    // NEW: Draw speed curves at top of canvas
    if (data.speed_curves && data.speed_curves.length > 0) {
        this.drawSpeedCurves(
            data.speed_curves,
            cmToX,
            data.current_speed_kmh,
            data.target_speed_kmh,
            padding,
            speedCurveAreaHeight
        );
    }
    
    // ... rest of existing render code ...
}
```

#### Add new `drawSpeedCurves()` method

```javascript
drawSpeedCurves(speedCurves, cmToX, currentSpeed, targetSpeed, padding, areaHeight) {
    /**
     * Draw ATC and operational speed limit curves at top of canvas.
     * 
     * - Red line: ATC limit (emergency ceiling, 100% deceleration)
     * - Green line: Operational limit (ATO target, 70% deceleration)
     * - Yellow dashed line: Current speed
     */
    if (!speedCurves || speedCurves.length === 0) return;
    
    const curveTop = padding.top;
    const curveBottom = curveTop + areaHeight;
    
    // Find max speed for Y-axis scaling
    let maxSpeed = 80;  // Default
    speedCurves.forEach(p => {
        maxSpeed = Math.max(maxSpeed, p.atc_limit, p.operational_limit);
    });
    maxSpeed = Math.ceil(maxSpeed / 10) * 10;  // Round up to nearest 10
    
    const speedToY = (speed) => {
        const ratio = speed / maxSpeed;
        return curveBottom - ratio * areaHeight;
    };
    
    // Draw Y-axis labels
    this.ctx.font = '8px JetBrains Mono, monospace';
    this.ctx.fillStyle = '#666';
    this.ctx.textAlign = 'right';
    this.ctx.textBaseline = 'middle';
    this.ctx.fillText(`${maxSpeed}`, padding.left - 5, curveTop);
    this.ctx.fillText(`${Math.round(maxSpeed/2)}`, padding.left - 5, curveTop + areaHeight/2);
    this.ctx.fillText('0', padding.left - 5, curveBottom);
    
    // Draw Y-axis line
    this.ctx.strokeStyle = '#333';
    this.ctx.lineWidth = 1;
    this.ctx.beginPath();
    this.ctx.moveTo(padding.left, curveTop);
    this.ctx.lineTo(padding.left, curveBottom);
    this.ctx.stroke();
    
    // Draw ATC limit curve (red/orange - emergency ceiling)
    this.ctx.strokeStyle = '#e83f5b';
    this.ctx.lineWidth = 1.5;
    this.ctx.beginPath();
    speedCurves.forEach((point, i) => {
        const x = cmToX(point.distance_cm);
        const y = speedToY(point.atc_limit);
        if (x < padding.left || x > this.canvas.width - padding.right) return;
        if (i === 0) this.ctx.moveTo(x, y);
        else this.ctx.lineTo(x, y);
    });
    this.ctx.stroke();
    
    // Draw operational limit curve (green - what ATO follows)
    this.ctx.strokeStyle = '#04d361';
    this.ctx.lineWidth = 2;
    this.ctx.beginPath();
    speedCurves.forEach((point, i) => {
        const x = cmToX(point.distance_cm);
        const y = speedToY(point.operational_limit);
        if (x < padding.left || x > this.canvas.width - padding.right) return;
        if (i === 0) this.ctx.moveTo(x, y);
        else this.ctx.lineTo(x, y);
    });
    this.ctx.stroke();
    
    // Draw current speed line (yellow dashed)
    if (currentSpeed > 0) {
        const currentY = speedToY(currentSpeed);
        this.ctx.strokeStyle = '#ffcd1e';
        this.ctx.lineWidth = 1.5;
        this.ctx.setLineDash([5, 3]);
        this.ctx.beginPath();
        this.ctx.moveTo(padding.left, currentY);
        this.ctx.lineTo(this.canvas.width - padding.right, currentY);
        this.ctx.stroke();
        this.ctx.setLineDash([]);
        
        // Label current speed
        this.ctx.fillStyle = '#ffcd1e';
        this.ctx.textAlign = 'left';
        this.ctx.fillText(`${Math.round(currentSpeed)}`, this.canvas.width - padding.right + 3, currentY);
    }
    
    // Draw target speed line (cyan dotted)
    if (targetSpeed > 0 && targetSpeed < maxSpeed) {
        const targetY = speedToY(targetSpeed);
        this.ctx.strokeStyle = '#00bcd4';
        this.ctx.lineWidth = 1;
        this.ctx.setLineDash([2, 4]);
        this.ctx.beginPath();
        this.ctx.moveTo(padding.left, targetY);
        this.ctx.lineTo(this.canvas.width - padding.right, targetY);
        this.ctx.stroke();
        this.ctx.setLineDash([]);
    }
    
    // Legend
    this.ctx.font = '8px Inter, sans-serif';
    const legendX = padding.left + 10;
    const legendY = curveTop + 8;
    
    // ATC limit legend
    this.ctx.fillStyle = '#e83f5b';
    this.ctx.fillRect(legendX, legendY - 4, 12, 2);
    this.ctx.fillStyle = '#999';
    this.ctx.textAlign = 'left';
    this.ctx.fillText('ATC', legendX + 15, legendY);
    
    // Operational limit legend
    this.ctx.fillStyle = '#04d361';
    this.ctx.fillRect(legendX + 45, legendY - 4, 12, 2);
    this.ctx.fillStyle = '#999';
    this.ctx.fillText('ATO', legendX + 60, legendY);
}
```

#### Update `updateFromController()` method

Ensure speed curves are passed through (around line 446):

```javascript
updateFromController(train, payload) {
    // ... existing code ...
    
    this.lookaheadData = {
        current_speed_kmh: train.speed || 0,
        target_speed_kmh: train.target_speed || 0,
        state: train.state,
        wait_reason: train.wait_reason,
        speed_limit_reason: speedLimitReason,
        blocks: lookahead?.blocks || [],
        signals: lookahead?.signals || [],
        switches: lookahead?.switches || [],
        stations: lookahead?.stations || [],
        distance_into_path_cm: 0,
        clearance_cm: lookahead?.clearance_cm || 0,
        speed_curves: lookahead?.speed_curves || []  // NEW
    };
    
    // ... rest of existing code ...
}
```

---

### File: `tests/test_backend.py`

Add comprehensive tests:

```python
class TestTrendAwareATOControl:
    """
    Tests for trend-aware ATO control logic.
    
    These tests verify:
    1. Hysteresis prevents oscillation in dead band
    2. Trend detection correctly identifies rising/falling speed
    3. Handle adjustments are rate-limited to ±1 notch
    4. Priority overrides (stopping, braking curve) work correctly
    """
    
    def test_dead_band_no_action(self, network):
        """In dead band (target-3 to target-1), handle should not change."""
        # Setup: train at 58 km/h, target 60 km/h (within dead band 57-59)
        # Expect: handle unchanged regardless of current handle
        pass
    
    def test_slow_down_zone_power_to_neutral(self):
        """When speed >= target-1 and accelerating, should go to neutral."""
        pass
    
    def test_slow_down_zone_brake_harder_if_rising(self):
        """When braking but speed still rising, should increase brake."""
        pass
    
    def test_slow_down_zone_keep_brake_if_working(self):
        """When braking and speed falling, should keep current brake."""
        pass
    
    def test_speed_up_zone_release_brake(self):
        """When speed < target-3 and braking, should release brake gradually."""
        pass
    
    def test_speed_up_zone_power_harder_if_falling(self):
        """When accelerating but speed still falling, should increase power."""
        pass
    
    def test_speed_up_zone_keep_power_if_working(self):
        """When accelerating and speed rising, should keep current power."""
        pass
    
    def test_handle_adjustment_one_notch_only(self):
        """Handle changes should be limited to ±1 notch per tick."""
        pass
    
    def test_stopping_priority_overrides_trend(self):
        """When target=0, should use braking notch regardless of trend."""
        pass
    
    def test_braking_curve_priority_overrides_trend(self):
        """When within braking distance, should brake regardless of trend."""
        pass


class TestSpeedCurveCalculation:
    """Tests for speed curve calculation for visualization."""
    
    def test_curves_decrease_with_distance(self):
        """Speed limits should decrease as distance decreases."""
        pass
    
    def test_operational_lower_than_atc(self):
        """Operational limit (70% decel) should be lower than ATC (100%)."""
        pass
    
    def test_curves_capped_at_max_speed(self):
        """Speed limits should not exceed train's max speed."""
        pass
```

---

## Visual Result

```
┌─────────────────────────────────────────────────────────────────────────┐
│ 80 ┤                                                                     │
│    │  ────── ATC (red)    ────── ATO (green)                            │
│ 60 ├──────────────────────────────────────────────────── current (58)   │
│    │        ╲ ATC                                                        │
│ 40 │          ╲      ╲ ATO                                               │
│    │            ╲      ╲                                                 │
│ 20 │              ╲      ╲                                               │
│  0 ┴────┬────┬────┬╲───┬──╲─┬────┬────┬────┬────┬────┬                  │
│         0   50  100  150  200  250  300  350  400  450  cm              │
├─────────────────────────────────────────────────────────────────────────┤
│  ▶ [===BLOCK A===][===BLOCK B===][===BLOCK C===]...                     │
│     🚦              🚦              ⇋                                    │
└─────────────────────────────────────────────────────────────────────────┘
```

**Legend:**
- **Red curve (ATC):** Emergency speed ceiling - train MUST stay below this
- **Green curve (ATO):** Operational target - what ATO actually follows
- **Yellow dashed line:** Current train speed
- **Cyan dotted line:** Target speed (when cruising)

---

## Summary of Changes

| File | Change | Lines |
|------|--------|-------|
| `speed_profile.py` | `operational_decel_ratio = 0.7` | ~1 |
| `enums.py` | Add `HANDLE_POSITION_INDEX` and `INDEX_TO_HANDLE_POSITION` | ~20 |
| `train_controller.py` | Add `_previous_speed` field | ~1 |
| `train_controller.py` | Add constants `SPEED_TREND_EPSILON`, `ATO_*_MARGIN` | ~5 |
| `train_controller.py` | Add helper methods for trend detection and handle adjustment | ~50 |
| `train_controller.py` | Rewrite `_ato_calculate_handle()` | ~80 |
| `train_controller.py` | Add `calculate_speed_curves()` | ~40 |
| `routes/trains.py` | Include speed_curves in lookahead payload | ~5 |
| `train-monitor.js` | Update `render()` to call `drawSpeedCurves()` | ~10 |
| `train-monitor.js` | Add `drawSpeedCurves()` method | ~100 |
| `train-monitor.js` | Update `updateFromController()` for speed_curves | ~2 |
| `tests/test_backend.py` | Add `TestTrendAwareATOControl` and `TestSpeedCurveCalculation` | ~100 |

**Total estimated changes:** ~400 lines

---

## Testing Plan

1. **Unit tests:** Run new test classes for trend-aware control
2. **Integration test:** Start simulation, observe handle behavior in console
3. **Visual test:** Open train monitor, verify speed curves display correctly
4. **Regression test:** Run full test suite to ensure no breakage

```bash
# Run all tests including new ones
conda run -n general pytest tests/test_backend.py -v

# Run specific new tests
conda run -n general pytest tests/test_backend.py::TestTrendAwareATOControl -v
conda run -n general pytest tests/test_backend.py::TestSpeedCurveCalculation -v
```

---

## Rollback Plan

If issues arise:
1. Revert `operational_decel_ratio` to 0.5
2. Revert `_ato_calculate_handle()` to original implementation
3. Speed curves visualization is additive (no rollback needed)
