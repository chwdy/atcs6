# Future-Aware ATO Control Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the fixed-lookahead ATO control with a predictive system that selects handle positions based on where the train will actually be after applying that handle, solving the fundamental tradeoff between speed limit anticipation (needs far lookahead) and stopping precision (needs close lookahead).

**Architecture:** Keep the existing speed curve generation and 4-zone control structure intact. Replace only the target acquisition step (`LOOKAHEAD_SAMPLE_CM` sampling) with a predictive model that:
1. Simulates forward in time for each candidate handle position
2. Compares predicted speed/position against the speed curve
3. Selects the handle that best tracks the curve without overshoot

The speed curve itself is already correct - it encodes all the physics of braking distances, safety margins, block speed limits, and the operational deceleration ratio. The problem is that the current system samples the curve at a fixed distance ahead, when it should instead ask: "which handle input will make me best follow this curve?"

**Tech Stack:** Python, pytest, existing physics functions (`calculate_max_safe_speed`, `calculate_speed_for_target`, `get_speed_conversion_factor`)

---

## Background & Root Cause Analysis

### Current Architecture

```
Speed curve (pre-computed)     ATO handle calculator
─────────────────────────      ─────────────────────
                               1. Sample curve at LOOKAHEAD_SAMPLE_CM (30cm)
 [operational_limit]              → gives "effective_target" speed
 at each distance_cm           2. Compare current_speed to effective_target
                               3. Use 4-zone control to pick handle
```

### The Problem

`LOOKAHEAD_SAMPLE_CM` is a compromise constant:
- **At 30cm:** Speed limit transitions are anticipated well, but the train sees `operational_limit=0` when it's still 30cm from the stop point → stops 30cm early.
- **At 10cm:** Train stops closer to target (only 10cm early), but speed limit changes at 30cm away aren't anticipated → overshoots during deceleration.

### Why Future-Awareness Solves This

Instead of asking "what speed does the curve say at X cm ahead?", ask:
**"If I apply handle H, will I be on/below the curve at every point until I stop?"**

This eliminates the fixed lookahead entirely. The ATO evaluates candidate handles by simulating the train forward in small time steps and checking whether the predicted trajectory stays on or below the operational speed curve. The best handle is the one that stays closest to the curve without exceeding it.

### Key Physical Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| Tick interval | ~100ms | `time.sleep(0.1)` in simulation loop |
| N scale factor | 1:160 | `get_speed_conversion_factor()` = 100000/3600/160 ≈ 0.1736 cm/s per km/h |
| Accel rate | 10.0 km/h/s | `profile.acceleration_rate` |
| Decel rate | 15.0 km/h/s | `profile.deceleration_rate` |
| Operational ratio | 0.6 | `profile.operational_decel_ratio` |
| P1-P5 multipliers | 0.2, 0.4, 0.6, 0.8, 1.0 | Fraction of `acceleration_rate` |
| B1-B8 multipliers | 0.125, 0.25, ..., 1.0 | Fraction of `deceleration_rate` |
| Neutral friction | 0.5 km/h/s | `profile.friction_rate` |
| Air drag | `0.001 * (v/100)^2` | Quadratic at reference 100 km/h |

At 20 km/h in N scale:
- Speed in cm/s: 20 × 0.1736 ≈ 3.47 cm/s
- Distance per tick (100ms): ~0.35 cm
- B4 decel: -7.5 km/h/s → stops from 20 km/h in ~2.67s → ~4.6 cm

---

## Task 1: Add Forward Simulation Helper

**Files:**
- Modify: `train_controller.py` (add after `_brake_level_to_handle`, around line 2040)
- Test: `tests/test_trains_ato.py` - add `TestForwardSimulation` class

This is the core primitive: given current speed + handle position, simulate the train forward in small time steps and return the predicted speed at a given distance. This properly handles unit conversion (km/h → cm/s) and the handle multiplier system.

**Step 1: Write the failing test**

Add to `tests/test_trains_ato.py`:

```python
class TestForwardSimulation:
    """
    Tests for forward simulation used by future-aware ATO.

    The forward simulation predicts where a train will be and how fast
    it will be going after applying a specific handle for a given distance.
    """

    @pytest.fixture
    def controller_with_profile(self, network):
        """Create a controller with a known speed profile for predictable physics."""
        from train_controller import TrainController
        from speed_profile import SpeedProfile

        train = Train(train_id="sim_test", name="Sim Test")
        train.max_speed = 60.0
        network.trains["sim_test"] = train

        # Register a simple linear profile
        profile = SpeedProfile(
            profile_id="test_sim",
            name="Test Simulation",
            speed_voltage_curve=[(0, 0), (60, 8), (120, 12)],
            acceleration_rate=10.0,  # 10 km/h per second
            deceleration_rate=15.0,  # 15 km/h per second
            friction_rate=0.5,
            operational_decel_ratio=0.6,
            air_resistance_enabled=False,  # Disable for predictable tests
        )
        if not hasattr(network, "speed_profiles"):
            from speed_profile import SpeedProfileRegistry
            network.speed_profiles = SpeedProfileRegistry()
        network.speed_profiles.register(profile)
        train.speed_profile_id = "test_sim"

        controller = TrainController(train_id="sim_test", network=network)
        controller.power_on = True
        controller.atc_speed_limit = 60.0
        controller.current_block_speed_limit = 60.0
        return controller

    def test_simulate_forward_braking_reduces_speed(self, controller_with_profile):
        """B4 braking from 20 km/h should result in lower speed after 5cm."""
        predicted_speed = controller_with_profile._simulate_forward(
            current_speed_kmh=20.0,
            handle=HandlePosition.B4,
            distance_cm=5.0,
        )
        assert predicted_speed < 20.0, "B4 braking should reduce speed"
        assert predicted_speed >= 0.0, "Speed should not go negative"

    def test_simulate_forward_power_increases_speed(self, controller_with_profile):
        """P5 power from 10 km/h should result in higher speed after 5cm."""
        predicted_speed = controller_with_profile._simulate_forward(
            current_speed_kmh=10.0,
            handle=HandlePosition.P5,
            distance_cm=5.0,
        )
        assert predicted_speed > 10.0, "P5 should increase speed"

    def test_simulate_forward_neutral_slight_decrease(self, controller_with_profile):
        """Neutral handle should cause slight speed decrease from friction."""
        predicted_speed = controller_with_profile._simulate_forward(
            current_speed_kmh=20.0,
            handle=HandlePosition.N,
            distance_cm=5.0,
        )
        # Should decrease slightly due to friction (0.5 km/h/s)
        assert predicted_speed < 20.0, "Neutral should cause slight decrease"
        assert predicted_speed > 19.0, "Friction decrease should be small"

    def test_simulate_forward_zero_distance(self, controller_with_profile):
        """Zero distance should return current speed unchanged."""
        predicted_speed = controller_with_profile._simulate_forward(
            current_speed_kmh=30.0,
            handle=HandlePosition.B4,
            distance_cm=0.0,
        )
        assert predicted_speed == 30.0

    def test_simulate_forward_braking_to_zero(self, controller_with_profile):
        """Heavy braking from low speed over long distance should reach 0."""
        predicted_speed = controller_with_profile._simulate_forward(
            current_speed_kmh=5.0,
            handle=HandlePosition.B8,
            distance_cm=50.0,
        )
        assert predicted_speed == 0.0, "Should stop within 50cm from 5 km/h with B8"

    def test_simulate_forward_higher_brake_stops_faster(self, controller_with_profile):
        """Higher brake notch should result in lower speed at same distance."""
        speed_b2 = controller_with_profile._simulate_forward(
            current_speed_kmh=20.0,
            handle=HandlePosition.B2,
            distance_cm=3.0,
        )
        speed_b6 = controller_with_profile._simulate_forward(
            current_speed_kmh=20.0,
            handle=HandlePosition.B6,
            distance_cm=3.0,
        )
        assert speed_b6 < speed_b2, "B6 should reduce speed more than B2"
```

**Step 2: Run test to verify it fails**

Run: `conda run -n general pytest tests/test_trains_ato.py::TestForwardSimulation -v`
Expected: FAIL with "AttributeError: 'TrainController' object has no attribute '_simulate_forward'"

**Step 3: Write minimal implementation**

Add to `train_controller.py` after `_brake_level_to_handle` (around line 2040):

```python
def _simulate_forward(
    self,
    current_speed_kmh: float,
    handle: HandlePosition,
    distance_cm: float,
    dt: float = 0.02,
) -> float:
    """
    Simulate train forward in time and return predicted speed at given distance.

    Uses the same physics model as TrainPhysics: converts handle position
    to acceleration rate via POWER_MULTIPLIERS/BRAKE_MULTIPLIERS, then
    integrates forward in small time steps until the target distance is
    reached.

    Args:
        current_speed_kmh: Train's current speed in km/h
        handle: Handle position to simulate
        distance_cm: Distance ahead to predict speed at
        dt: Simulation time step in seconds (default 20ms for accuracy)

    Returns:
        Predicted speed in km/h at the given distance
    """
    if distance_cm <= 0:
        return current_speed_kmh

    from enums import POWER_MULTIPLIERS, BRAKE_MULTIPLIERS

    profile = self._get_speed_profile()

    # Calculate acceleration for this handle (same as TrainPhysics)
    if handle in POWER_MULTIPLIERS:
        accel_kmh_s = profile.acceleration_rate * POWER_MULTIPLIERS[handle]
    elif handle in BRAKE_MULTIPLIERS:
        accel_kmh_s = -profile.deceleration_rate * BRAKE_MULTIPLIERS[handle]
    elif handle == HandlePosition.N:
        accel_kmh_s = 0.0  # Friction handled separately below
    elif handle == HandlePosition.EB:
        accel_kmh_s = -profile.emergency_brake_rate
    else:
        accel_kmh_s = 0.0

    km_h_to_cm_s = get_speed_conversion_factor()
    speed_cm_s = current_speed_kmh * km_h_to_cm_s
    distance_remaining = distance_cm

    while distance_remaining > 0 and speed_cm_s >= 0:
        # Apply acceleration
        speed_cm_s += accel_kmh_s * km_h_to_cm_s * dt

        # Apply friction when coasting (neutral)
        if handle == HandlePosition.N and speed_cm_s > 0:
            speed_cm_s -= profile.friction_rate * km_h_to_cm_s * dt

        # Clamp to non-negative
        if speed_cm_s <= 0:
            speed_cm_s = 0.0
            break

        # Advance distance
        step_dist = speed_cm_s * dt
        distance_remaining -= step_dist

    return speed_cm_s / km_h_to_cm_s
```

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_trains_ato.py::TestForwardSimulation -v`
Expected: PASS

**Step 5: Commit**

```bash
git add train_controller.py tests/test_trains_ato.py
git commit -m "feat: add _simulate_forward for physics-based speed prediction"
```

---

## Task 2: Add Curve-Tracking Handle Evaluator

**Files:**
- Modify: `train_controller.py` (add new method after `_simulate_forward`)
- Test: `tests/test_trains_ato.py` - add `TestHandleEvaluator` class

This is the key insight: instead of sampling the curve at one point, we evaluate each candidate handle by checking whether its predicted trajectory stays on/below the speed curve. The best handle is the one that stays closest to the curve without exceeding it.

**Step 1: Write the failing test**

Add to `tests/test_trains_ato.py`:

```python
class TestHandleEvaluator:
    """
    Tests for handle evaluation against speed curve.

    The evaluator scores each handle position by simulating forward and
    comparing the predicted trajectory against the pre-computed speed curve.
    """

    @pytest.fixture
    def controller_with_profile(self, network):
        """Create a controller with a known speed profile."""
        from train_controller import TrainController
        from speed_profile import SpeedProfile

        train = Train(train_id="eval_test", name="Eval Test")
        train.max_speed = 60.0
        network.trains["eval_test"] = train

        profile = SpeedProfile(
            profile_id="test_eval",
            name="Test Eval",
            speed_voltage_curve=[(0, 0), (60, 8), (120, 12)],
            acceleration_rate=10.0,
            deceleration_rate=15.0,
            friction_rate=0.5,
            operational_decel_ratio=0.6,
            air_resistance_enabled=False,
        )
        if not hasattr(network, "speed_profiles"):
            from speed_profile import SpeedProfileRegistry
            network.speed_profiles = SpeedProfileRegistry()
        network.speed_profiles.register(profile)
        train.speed_profile_id = "test_eval"

        controller = TrainController(train_id="eval_test", network=network)
        controller.power_on = True
        controller.atc_speed_limit = 60.0
        controller.current_block_speed_limit = 60.0
        return controller

    def test_evaluate_selects_brake_when_above_curve(self, controller_with_profile):
        """When speed is above curve, should select a brake handle."""
        # Curve that drops to 0 at 10cm (approaching stop)
        curve = [
            {"distance_cm": 0.0, "operational_limit": 10.0, "atc_limit": 15.0},
            {"distance_cm": 5.0, "operational_limit": 5.0, "atc_limit": 10.0},
            {"distance_cm": 10.0, "operational_limit": 0.0, "atc_limit": 0.0},
        ]
        result = controller_with_profile._evaluate_handles_against_curve(
            current_speed_kmh=15.0,
            curve=curve,
        )
        assert controller_with_profile._is_brake_handle(result), (
            f"Should brake when above curve, got {result.value}"
        )

    def test_evaluate_selects_power_when_below_curve(self, controller_with_profile):
        """When speed is well below curve, should select a power handle."""
        # Flat curve at 40 km/h
        curve = [
            {"distance_cm": 0.0, "operational_limit": 40.0, "atc_limit": 45.0},
            {"distance_cm": 50.0, "operational_limit": 40.0, "atc_limit": 45.0},
            {"distance_cm": 100.0, "operational_limit": 40.0, "atc_limit": 45.0},
        ]
        result = controller_with_profile._evaluate_handles_against_curve(
            current_speed_kmh=10.0,
            curve=curve,
        )
        assert controller_with_profile._is_power_handle(result), (
            f"Should accelerate when below curve, got {result.value}"
        )

    def test_evaluate_selects_neutral_when_matching_curve(self, controller_with_profile):
        """When speed matches curve, should maintain (N or low power)."""
        # Flat curve at 20 km/h
        curve = [
            {"distance_cm": 0.0, "operational_limit": 20.0, "atc_limit": 25.0},
            {"distance_cm": 50.0, "operational_limit": 20.0, "atc_limit": 25.0},
            {"distance_cm": 100.0, "operational_limit": 20.0, "atc_limit": 25.0},
        ]
        result = controller_with_profile._evaluate_handles_against_curve(
            current_speed_kmh=20.0,
            curve=curve,
        )
        # Should be N or low power (P1/P2) to maintain speed against friction
        assert result in (
            HandlePosition.N, HandlePosition.P1, HandlePosition.P2
        ), f"Should maintain when matching curve, got {result.value}"
```

**Step 2: Run test to verify it fails**

Run: `conda run -n general pytest tests/test_trains_ato.py::TestHandleEvaluator -v`
Expected: FAIL with "AttributeError"

**Step 3: Write minimal implementation**

Add to `train_controller.py` after `_simulate_forward`:

```python
def _evaluate_handles_against_curve(
    self,
    current_speed_kmh: float,
    curve: List[Dict],
    horizon_cm: float = 30.0,
    sample_count: int = 5,
) -> HandlePosition:
    """
    Evaluate all handle positions against speed curve and return best one.

    For each candidate handle, simulates the train forward and samples
    the predicted speed at multiple distances. Compares each sample
    against the operational_limit at that distance on the curve.

    Scoring:
    - Penalty for being ABOVE the curve (overshoot) - weighted heavily
    - Penalty for being too far BELOW the curve (inefficient)
    - Best handle minimizes total penalty

    Args:
        current_speed_kmh: Train's current speed in km/h
        curve: Speed curve points [{distance_cm, operational_limit, ...}, ...]
        horizon_cm: How far ahead to evaluate (default 30cm)
        sample_count: Number of sample points within horizon

    Returns:
        Best handle position
    """
    if not curve:
        return HandlePosition.N

    # Clamp horizon to curve extent
    max_curve_dist = curve[-1].get("distance_cm", 0)
    horizon_cm = min(horizon_cm, max_curve_dist)
    if horizon_cm <= 0:
        # At end of curve - just brake
        return self._calculate_braking_notch(current_speed_kmh)

    candidates = [
        HandlePosition.P5, HandlePosition.P4, HandlePosition.P3,
        HandlePosition.P2, HandlePosition.P1, HandlePosition.N,
        HandlePosition.B1, HandlePosition.B2, HandlePosition.B3,
        HandlePosition.B4, HandlePosition.B5, HandlePosition.B6,
        HandlePosition.B7, HandlePosition.B8,
    ]

    sample_distances = [
        horizon_cm * (i + 1) / sample_count for i in range(sample_count)
    ]

    best_handle = HandlePosition.N
    best_score = float("inf")
    OVERSHOOT_WEIGHT = 10.0  # Penalize exceeding curve heavily

    for handle in candidates:
        score = 0.0

        for dist in sample_distances:
            predicted_speed = self._simulate_forward(
                current_speed_kmh, handle, dist
            )
            curve_limit = self._sample_curve_at(curve, dist, "operational_limit")

            if predicted_speed > curve_limit:
                # Above curve - heavy penalty
                overshoot = predicted_speed - curve_limit
                score += overshoot * OVERSHOOT_WEIGHT
            else:
                # Below curve - mild penalty (want to be close to curve)
                gap = curve_limit - predicted_speed
                score += gap

        if score < best_score:
            best_score = score
            best_handle = handle

    return best_handle
```

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_trains_ato.py::TestHandleEvaluator -v`
Expected: PASS

**Step 5: Commit**

```bash
git add train_controller.py tests/test_trains_ato.py
git commit -m "feat: add curve-tracking handle evaluator for future-aware ATO"
```

---

## Task 3: Integrate Future-Aware Control into `_ato_calculate_handle`

**Files:**
- Modify: `train_controller.py:2076-2232` (`_ato_calculate_handle` method)
- Test: Run existing ATO test suite for regression check

This is the integration step. The key design decision: **keep the 4-zone structure for the stopping zone** (Zone 1), but replace the target acquisition and Zones 2-4 with the curve-tracking evaluator. Zone 1 stays because when `operational_limit <= 0.5`, the train must unconditionally brake to stop - no prediction needed.

**Step 1: Read the current method**

Confirm current structure at `train_controller.py:2076-2232`:
- Lines 2106-2119: TARGET ACQUISITION (sample curve at fixed 30cm) ← **REPLACE**
- Lines 2120-2129: Cap at ATC constraints ← **KEEP**
- Lines 2134-2146: ZONE 1: STOPPING ← **KEEP** (unconditional brake)
- Lines 2152-2228: ZONES 2-4 ← **REPLACE with evaluator**

**Step 2: Modify `_ato_calculate_handle`**

Replace the method body (lines 2098-2232) with:

```python
def _ato_calculate_handle(self) -> HandlePosition:
    """
    ATO handle calculation using future-aware curve-tracking approach.

    Instead of sampling the speed curve at a fixed lookahead distance,
    evaluates all candidate handle positions by simulating the train
    forward and checking which handle best tracks the operational
    speed curve without overshooting.

    Special cases:
    1. Stopping zone: When curve says stop (operational_limit <= 0.5),
       use direct braking notch calculation (no prediction needed).
    2. Very low speed (< 2 km/h): Use direct braking to avoid
       simulation inaccuracy at near-zero speeds.

    Also sets self.handle_reason with diagnostic info.
    """
    train = self._get_train()
    if not train:
        self.handle_reason = "No train"
        return HandlePosition.N

    current_speed = train.current_speed
    current_handle = self.handle_position

    # =================================================================
    # Get speed curve
    # =================================================================
    lookahead = self.get_monitor_lookahead()
    curves = lookahead.get("lookahead", {}).get("speed_curves", [])

    if not curves:
        self.handle_reason = "No curves"
        self._previous_speed = current_speed
        return HandlePosition.N

    # =================================================================
    # ZONE 1: STOPPING (curve at position 0 says stop)
    # =================================================================
    curve_at_zero = self._sample_curve_at(curves, 0.0, "operational_limit")

    # Also cap at real-time ATC constraints
    effective_limit_at_zero = min(
        curve_at_zero,
        train.max_speed,
        self.atc_speed_limit,
        self.current_block_speed_limit,
    )

    if effective_limit_at_zero <= STOPPED_SPEED_THRESHOLD_KMH:
        if current_speed > 0:
            result_handle = self._calculate_braking_notch(current_speed)
            reason = f"Stopping: curve@0={curve_at_zero:.1f}, braking at {current_speed:.1f}"
        else:
            result_handle = HandlePosition.N
            reason = "Stopped"
        self._previous_speed = current_speed
        self.handle_reason = reason
        return result_handle

    # =================================================================
    # FUTURE-AWARE CONTROL: Evaluate handles against curve
    # =================================================================
    # Determine evaluation horizon based on current speed
    # At higher speeds, look further ahead; at low speeds, shorter horizon
    km_h_to_cm_s = get_speed_conversion_factor()
    speed_cm_s = current_speed * km_h_to_cm_s

    # Horizon = distance covered in ~1 second, clamped to [5, 50] cm
    horizon_cm = max(5.0, min(50.0, speed_cm_s * 1.0))

    result_handle = self._evaluate_handles_against_curve(
        current_speed_kmh=current_speed,
        curve=curves,
        horizon_cm=horizon_cm,
    )

    # Safety cap: if result would exceed ATC limits, override
    predicted = self._simulate_forward(current_speed, result_handle, 5.0)
    if predicted > self.atc_speed_limit + ATO_SPEED_TOLERANCE:
        result_handle = self._calculate_braking_notch(current_speed)
        reason = f"ATC cap: predicted {predicted:.1f} > limit {self.atc_speed_limit:.1f}"
    else:
        curve_target = self._sample_curve_at(curves, horizon_cm / 2, "operational_limit")
        reason = (
            f"FutureAware: handle={result_handle.value} "
            f"speed={current_speed:.1f} curve@{horizon_cm/2:.0f}cm={curve_target:.1f} "
            f"horizon={horizon_cm:.0f}cm"
        )

    self._previous_speed = current_speed
    self.handle_reason = reason
    return result_handle
```

**Step 3: Run existing tests to verify no regression**

Run: `conda run -n general pytest tests/test_trains_ato.py -v -x`
Expected: PASS

Run: `conda run -n general pytest tests/test_backend.py -v`
Expected: PASS

**Step 4: Commit**

```bash
git add train_controller.py
git commit -m "feat: integrate future-aware curve tracking into ATO handle calculation"
```

---

## Task 4: Add Integration Tests

**Files:**
- Test: `tests/test_trains_ato.py` - add `TestFutureAwareIntegration` class

These tests verify the end-to-end behavior: the ATO makes correct handle decisions in realistic scenarios.

**Step 1: Write integration tests**

Add to `tests/test_trains_ato.py`:

```python
class TestFutureAwareIntegration:
    """
    Integration tests for future-aware ATO control.

    These tests verify that the complete ATO pipeline (curve generation →
    handle evaluation → handle selection) produces correct behavior in
    realistic scenarios.
    """

    @pytest.fixture
    def controller_with_profile(self, network):
        """Create a controller with known profile and no air resistance."""
        from train_controller import TrainController
        from speed_profile import SpeedProfile

        train = Train(train_id="integ_test", name="Integration Test")
        train.max_speed = 60.0
        network.trains["integ_test"] = train

        profile = SpeedProfile(
            profile_id="test_integ",
            name="Test Integration",
            speed_voltage_curve=[(0, 0), (60, 8), (120, 12)],
            acceleration_rate=10.0,
            deceleration_rate=15.0,
            friction_rate=0.5,
            operational_decel_ratio=0.6,
            air_resistance_enabled=False,
        )
        if not hasattr(network, "speed_profiles"):
            from speed_profile import SpeedProfileRegistry
            network.speed_profiles = SpeedProfileRegistry()
        network.speed_profiles.register(profile)
        train.speed_profile_id = "test_integ"

        controller = TrainController(train_id="integ_test", network=network)
        controller.power_on = True
        controller.atc_speed_limit = 60.0
        controller.current_block_speed_limit = 60.0
        return controller

    def test_braking_curve_produces_brake_handle(self, controller_with_profile):
        """When approaching a stop, the evaluator should select brake handles."""
        controller = controller_with_profile
        train = controller._get_train()
        train.current_speed = 20.0

        # Simulate a braking curve: speed drops from 20 to 0 over 20cm
        curve = [
            {"distance_cm": 0.0, "operational_limit": 15.0, "atc_limit": 20.0},
            {"distance_cm": 5.0, "operational_limit": 10.0, "atc_limit": 15.0},
            {"distance_cm": 10.0, "operational_limit": 5.0, "atc_limit": 10.0},
            {"distance_cm": 15.0, "operational_limit": 2.0, "atc_limit": 5.0},
            {"distance_cm": 20.0, "operational_limit": 0.0, "atc_limit": 0.0},
        ]

        result = controller._evaluate_handles_against_curve(
            current_speed_kmh=20.0,
            curve=curve,
            horizon_cm=20.0,
        )
        assert controller._is_brake_handle(result), (
            f"Should brake approaching stop, got {result.value}"
        )

    def test_flat_curve_maintains_speed(self, controller_with_profile):
        """When curve is flat, should maintain current speed."""
        controller = controller_with_profile
        train = controller._get_train()
        train.current_speed = 30.0

        curve = [
            {"distance_cm": 0.0, "operational_limit": 30.0, "atc_limit": 35.0},
            {"distance_cm": 25.0, "operational_limit": 30.0, "atc_limit": 35.0},
            {"distance_cm": 50.0, "operational_limit": 30.0, "atc_limit": 35.0},
        ]

        result = controller._evaluate_handles_against_curve(
            current_speed_kmh=30.0,
            curve=curve,
            horizon_cm=30.0,
        )
        # Should be neutral or low power to maintain against friction
        assert not controller._is_brake_handle(result) or result == HandlePosition.B1, (
            f"Should maintain speed on flat curve, got {result.value}"
        )

    def test_acceleration_zone_selects_power(self, controller_with_profile):
        """When speed is well below curve, should select power handle."""
        controller = controller_with_profile

        curve = [
            {"distance_cm": 0.0, "operational_limit": 50.0, "atc_limit": 55.0},
            {"distance_cm": 25.0, "operational_limit": 50.0, "atc_limit": 55.0},
            {"distance_cm": 50.0, "operational_limit": 50.0, "atc_limit": 55.0},
        ]

        result = controller._evaluate_handles_against_curve(
            current_speed_kmh=5.0,
            curve=curve,
            horizon_cm=30.0,
        )
        assert controller._is_power_handle(result), (
            f"Should accelerate when well below curve, got {result.value}"
        )

    def test_speed_reduction_zone_selects_appropriate_brake(self, controller_with_profile):
        """When approaching speed limit reduction, should brake proportionally."""
        controller = controller_with_profile

        # Speed drops from 40 to 20 at 15cm
        curve = [
            {"distance_cm": 0.0, "operational_limit": 35.0, "atc_limit": 40.0},
            {"distance_cm": 10.0, "operational_limit": 25.0, "atc_limit": 30.0},
            {"distance_cm": 15.0, "operational_limit": 20.0, "atc_limit": 25.0},
            {"distance_cm": 30.0, "operational_limit": 20.0, "atc_limit": 25.0},
        ]

        result = controller._evaluate_handles_against_curve(
            current_speed_kmh=35.0,
            curve=curve,
            horizon_cm=30.0,
        )
        assert controller._is_brake_handle(result), (
            f"Should brake for speed reduction, got {result.value}"
        )
```

**Step 2: Run tests**

Run: `conda run -n general pytest tests/test_trains_ato.py::TestFutureAwareIntegration -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_trains_ato.py
git commit -m "test: add integration tests for future-aware ATO control"
```

---

## Task 5: Handle Smoothing and Jitter Prevention

**Files:**
- Modify: `train_controller.py` (add rate-limiting to `_ato_calculate_handle`)
- Test: `tests/test_trains_ato.py` - add jitter test

The evaluator might select different handles on consecutive ticks because the curve changes slightly each tick (the train has moved). Add rate-limiting: the handle can only change by ±1 notch per tick (using the existing `_adjust_handle_toward_brake` / `_adjust_handle_toward_power` helpers).

**Step 1: Write the failing test**

Add to `tests/test_trains_ato.py`:

```python
class TestHandleSmoothing:
    """Tests for handle change rate limiting to prevent jitter."""

    def test_rate_limit_prevents_jump(self):
        """Handle should not jump more than 1 notch per evaluation."""
        from train_controller import TrainController, _rate_limit_handle

        # Currently at P3, evaluator wants B4 → should only go to P2
        result = _rate_limit_handle(
            current=HandlePosition.P3,
            target=HandlePosition.B4,
        )
        # Should move one step toward brake: P3 → P2
        assert result == HandlePosition.P2

    def test_rate_limit_allows_same(self):
        """Same handle should pass through unchanged."""
        from train_controller import _rate_limit_handle

        result = _rate_limit_handle(
            current=HandlePosition.B3,
            target=HandlePosition.B3,
        )
        assert result == HandlePosition.B3

    def test_rate_limit_allows_adjacent(self):
        """Adjacent handle should pass through."""
        from train_controller import _rate_limit_handle

        result = _rate_limit_handle(
            current=HandlePosition.N,
            target=HandlePosition.B1,
        )
        assert result == HandlePosition.B1

    def test_rate_limit_exception_for_stopping(self):
        """Stopping zone should allow faster handle changes."""
        from train_controller import _rate_limit_handle

        # When target is EB or high brake in stopping, allow 2-step jump
        result = _rate_limit_handle(
            current=HandlePosition.P3,
            target=HandlePosition.B6,
            allow_fast=True,
        )
        # Should move 2 steps: P3 → P1
        assert result == HandlePosition.P1
```

**Step 2: Run test to verify it fails**

Run: `conda run -n general pytest tests/test_trains_ato.py::TestHandleSmoothing -v`
Expected: FAIL

**Step 3: Write implementation**

Add module-level function in `train_controller.py` (near the other helper functions):

```python
def _rate_limit_handle(
    current: HandlePosition,
    target: HandlePosition,
    allow_fast: bool = False,
) -> HandlePosition:
    """
    Rate-limit handle changes to prevent jitter.

    Limits handle movement to ±1 notch per call (±2 if allow_fast=True).

    Args:
        current: Current handle position
        target: Desired handle position
        allow_fast: Allow 2-step changes (for stopping zone)

    Returns:
        Rate-limited handle position
    """
    from enums import HANDLE_POSITION_INDEX, INDEX_TO_HANDLE_POSITION

    current_idx = HANDLE_POSITION_INDEX.get(current, 5)  # N = index 5
    target_idx = HANDLE_POSITION_INDEX.get(target, 5)

    max_step = 2 if allow_fast else 1

    if target_idx > current_idx:
        # Moving toward brake
        new_idx = min(current_idx + max_step, target_idx)
    elif target_idx < current_idx:
        # Moving toward power
        new_idx = max(current_idx - max_step, target_idx)
    else:
        new_idx = current_idx

    return INDEX_TO_HANDLE_POSITION.get(new_idx, HandlePosition.N)
```

Then update `_ato_calculate_handle` to apply rate limiting before returning:

In the future-aware control section (Task 3's code), before the final `return result_handle`, add:

```python
    # Rate-limit handle changes to prevent jitter
    # Allow fast changes only when in stopping zone (curve dropping to 0 nearby)
    is_near_stop = False
    for point in curves:
        if (point.get("operational_limit", 999) <= STOPPED_SPEED_THRESHOLD_KMH
                and point.get("distance_cm", 999) < 50.0):
            is_near_stop = True
            break

    result_handle = _rate_limit_handle(
        current=current_handle,
        target=result_handle,
        allow_fast=is_near_stop,
    )
```

**Step 4: Run test to verify it passes**

Run: `conda run -n general pytest tests/test_trains_ato.py::TestHandleSmoothing -v`
Expected: PASS

**Step 5: Run full regression**

Run: `conda run -n general pytest tests/test_trains_ato.py -v -x`
Expected: PASS

**Step 6: Commit**

```bash
git add train_controller.py tests/test_trains_ato.py
git commit -m "feat: add handle rate limiting to prevent ATO jitter"
```

---

## Task 6: Performance Optimization

**Files:**
- Modify: `train_controller.py` (optimize `_evaluate_handles_against_curve`)

The evaluator calls `_simulate_forward` for each of 14 handles × 5 sample points = 70 forward simulations per tick. This might be too slow. Optimize:

1. **Reduce candidates:** Only evaluate handles adjacent to current (±3 notches) plus extremes (P5, B8, N)
2. **Cache curve lookups:** Pre-sample the curve at sample distances once
3. **Early exit:** If a handle produces overshoot at the first sample point, skip it

**Step 1: Profile current performance**

Before optimizing, measure. Add a timing test:

```python
def test_evaluator_performance(self, controller_with_profile):
    """Evaluator should complete within 5ms for real-time use."""
    import time
    controller = controller_with_profile

    curve = [
        {"distance_cm": i * 5.0, "operational_limit": max(0, 30.0 - i * 1.5), "atc_limit": max(0, 35.0 - i * 1.5)}
        for i in range(21)
    ]

    start = time.perf_counter()
    for _ in range(100):
        controller._evaluate_handles_against_curve(
            current_speed_kmh=25.0,
            curve=curve,
            horizon_cm=30.0,
        )
    elapsed = (time.perf_counter() - start) / 100

    assert elapsed < 0.005, f"Evaluator took {elapsed*1000:.1f}ms, should be < 5ms"
```

**Step 2: Apply optimizations if needed**

Update `_evaluate_handles_against_curve` to reduce candidate set:

```python
# Smart candidate selection: only evaluate handles near current ± 3 notches
from enums import HANDLE_POSITION_INDEX, INDEX_TO_HANDLE_POSITION

current_idx = HANDLE_POSITION_INDEX.get(self.handle_position, 5)
candidate_indices = set()

# Always include extremes and neutral
candidate_indices.add(HANDLE_POSITION_INDEX[HandlePosition.P5])  # Full power
candidate_indices.add(HANDLE_POSITION_INDEX[HandlePosition.N])   # Neutral
candidate_indices.add(HANDLE_POSITION_INDEX[HandlePosition.B8])  # Full brake

# Include ±3 notches from current
for offset in range(-3, 4):
    idx = current_idx + offset
    if 0 <= idx <= 13:
        candidate_indices.add(idx)

candidates = [INDEX_TO_HANDLE_POSITION[i] for i in sorted(candidate_indices)
              if i in INDEX_TO_HANDLE_POSITION]
```

Also pre-sample the curve:

```python
# Pre-sample curve at all distances once
curve_limits = [
    self._sample_curve_at(curve, dist, "operational_limit")
    for dist in sample_distances
]

# Then in the inner loop, use curve_limits[j] instead of re-sampling
```

**Step 3: Run performance test**

Run: `conda run -n general pytest tests/test_trains_ato.py::TestFutureAwareIntegration::test_evaluator_performance -v`
Expected: PASS (< 5ms)

**Step 4: Run full regression**

Run: `conda run -n general pytest tests/test_backend.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add train_controller.py tests/test_trains_ato.py
git commit -m "perf: optimize future-aware handle evaluator candidate selection"
```

---

## Task 7: Full Test Suite Verification

**Files:** No changes - verification only

**Step 1: Run mandatory test suites**

```bash
# 1. JavaScript syntax validation
conda run -n general pytest tests/test_javascript_syntax.py -v

# 2. App smoke tests
conda run -n general pytest tests/test_app_smoke.py -v

# 3. Backend unit tests (includes all modular test files)
conda run -n general pytest tests/test_backend.py -v
```

All must pass.

**Step 2: Run ATO-specific tests**

```bash
conda run -n general pytest tests/test_trains_ato.py -v
conda run -n general pytest tests/test_speed_curve_debug.py -v
```

**Step 3: Manual simulation test**

```bash
python3 server.py
```

Start the server, create a train with a path to a station. Verify:
1. Train accelerates to cruising speed
2. Train decelerates smoothly before speed limit reductions
3. Train stops within ±5cm of station stop point (not 30cm early)
4. No handle jittering visible in train monitor

---

## Summary

| Component | Before | After |
|-----------|--------|-------|
| Target acquisition | Sample curve at fixed 30cm | Simulate forward for each handle candidate |
| Handle selection | 4-zone rules based on target vs current speed | Score handles by curve-tracking quality |
| Stopping | Sees `limit=0` at 30cm → stops 30cm early | Zone 1 direct braking unchanged; evaluator naturally follows declining curve |
| Speed limits | Must see limit in advance at fixed distance | Evaluator checks trajectory against full curve ahead |
| Jitter prevention | Trend detection + hysteresis | Rate-limited handle changes (±1 notch/tick) |
| Lookahead constant | `LOOKAHEAD_SAMPLE_CM = 30` (hardcoded) | Dynamic horizon based on speed (5-50cm) |

**Key files modified:**
- `train_controller.py`: `_simulate_forward`, `_evaluate_handles_against_curve`, `_rate_limit_handle`, modified `_ato_calculate_handle`
- `tests/test_trains_ato.py`: `TestForwardSimulation`, `TestHandleEvaluator`, `TestFutureAwareIntegration`, `TestHandleSmoothing`
