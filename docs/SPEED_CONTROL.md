# Speed Control — ATO, ATC, Lookahead

This document explains how the train decides what speed to run, how it
computes braking distance, and which knobs control each behaviour. It is the
"how does the simulator drive the train" reference. The architectural rules
(per-tick phase ordering, latch lifecycle, who writes what) live in
[`design_new.md`](../design_new.md); this doc covers the math and tuning that
sit *inside* Phase 2.

Keep this in sync when you change a constant or alter the navigator/ATC/curve
logic.

---

## 1. The four pieces

| Piece | Code | Purpose |
|---|---|---|
| **Lookahead** | [`lookahead_builder.py`](../lookahead_builder.py), [`train_controller.py`](../train_controller.py) `_compute_canonical_lookahead` | Walks the path forward from the head and lists upcoming blocks/switches/stations with distances. |
| **Speed curves** | [`train_controller.py`](../train_controller.py) `calculate_speed_curves` | At every sample distance, picks the most binding speed limit (`atc_limit`) and the operational-budget speed (`operational_limit`). |
| **ATC** | [`atc_calculator.py`](../atc_calculator.py), `TrainController.tick()` step `914f` | Reads `atc_limit` at the train head; warns at +5 km/h, escalates to EB at +10. |
| **ATO** | [`ato_navigator.py`](../ato_navigator.py) `compute_handle` | Picks the throttle/brake notch each tick by tracking the operational curve. |

The lookahead and the speed curves are the inputs. ATO drives, ATC catches.

---

## 2. ATC — the speed-limit ceiling

ATC is a hard ceiling derived from the most binding of:
- the current block's `speed_limit_kmh`;
- any forward block whose limit drops within braking distance (the curve folds it back as a sloping limit so the train can't be travelling faster than it can brake from);
- signal aspect (yellow/red downgrade);
- authority end (`AUTHORITY_STOP` reason → 0 at the boundary);
- `train.max_speed`.

The result is `atc_speed_limit` (the head's current ATC ceiling) plus a
`speed_curves[]` array with one entry per sample distance.

### ATC overspeed escalation

In `TrainController.tick()` step `914f`:

| `current_speed_kmh - atc_speed_limit` | Effect |
|---|---|
| ≤ +5 | normal — ATO must brake |
| > +5 (and ≤ +10) | `atc_warning_active = True` (UI flag, no EB) |
| > +10 | `eb_active = True`, `pending_path_cancel_request` armed (initiator `"eb"`) |

The +5 / +10 thresholds are inline numbers in
[`train_controller.py:712,735`](../train_controller.py:712). Any tightening
must keep the EB headroom above the largest possible single-tick overshoot.

---

## 3. Operational ceiling — what ATO actually targets

ATO never aims for the ATC limit directly. It targets the **operational
limit**, which is ATC minus a fixed margin:

```
operational_limit = min(
    operational_limit_from_curve,    # speed_curves sample at this distance
    train.max_speed,
    atc_limit - ATO_SPEED_MARGIN_KMH  # the buffer
)
```

(See [`train_controller.py:2538-2541`](../train_controller.py:2538) inside
`calculate_speed_curves`.)

`ATO_SPEED_MARGIN_KMH` is the safety buffer between ATO target and ATC ceiling.
Larger margin = more conservative cruise, more headroom to overspeed.
Smaller margin = closer to ATC, narrower safety stack.

The navigator then picks the head-tick **ceiling**:

```
ceiling_at_zero = min(
    operational_limit_at_distance_0,
    train.max_speed,
    current_block_speed_limit,
)
```

(See [`ato_navigator.py:374-379`](../ato_navigator.py:374).)

This `ceiling_at_zero` is what every "Coast: speed=X ceiling=Y" debug line
refers to.

---

## 4. ATO control loop — choosing the next handle

Per tick (step `0a4e`–`924d` of `TrainController.tick()`), the navigator runs
[`ato_navigator.py:compute_handle`](../ato_navigator.py:326):

1. **Stop check.** If `ceiling_at_zero <= STOPPED_SPEED_THRESHOLD_KMH` and the
   train is moving, return `B8` (full service brake to stop).
2. **Sample the forward curve.** Walk N samples between distance 0 and a
   planning horizon (`speed_cm_s * dt * LOOKAHEAD_TICKS`). For each sample,
   compute the deceleration required to be at-or-below the curve at that
   distance using `(u² − v²) / (2d)`. Track the largest required decel
   (`required_decel_kmh_s`) and the binding sample.
3. **Decide:**

   | Condition | Action |
   |---|---|
   | `required_decel_kmh_s > 0` | Brake. Pick the smallest brake notch whose `BRAKE_MULTIPLIERS` value ≥ `required_decel / op_decel`. |
   | `required_decel_kmh_s ≤ 0` and `speed_kmh < ceiling_at_zero - ATO_SPEED_TOLERANCE` | **Power up.** Pick a notch via `calculate_power_notch`. |
   | otherwise | **Coast.** Handle = `N`. |

   That second condition is the **ATO deadband**: the train powers up only
   when speed has fallen more than `ATO_SPEED_TOLERANCE` below the ceiling,
   and coasts once it's within that band. Above ceiling it brakes.

4. **Rate-limit the handle.** The chosen notch is run through
   `rate_limit_handle` ([`ato_navigator.py:66`](../ato_navigator.py:66)) which
   limits handle changes to ±2 notches per tick (`allow_fast=True`) so neither
   power-up nor brake snaps an entire bank at once. EB always bypasses rate
   limiting.

### Power notch picker

[`ato_navigator.py:277-290`](../ato_navigator.py:277) — given the gap
`target − current` in km/h:

| Gap (km/h) | Notch |
|---|---|
| > 8 | P5 |
| > 5 | P4 |
| > 3 | P3 |
| > 1.5 | P2 |
| else | P1 |

### Brake notch from raw speed gap

[`ato_navigator.py:293-320`](../ato_navigator.py:293) (used when ATO is not
running, e.g. operator brake to stop). Brake notches in normal ATO mode are
picked from the *required decel multiplier* against `op_decel`, not raw speed
gap — `pick_brake_notch_for_multiplier`
([`ato_navigator.py:263`](../ato_navigator.py:263)).

`BRAKE_MULTIPLIERS` (in [`enums.py`](../enums.py)) — multipliers against
`op_decel = profile.deceleration_rate * profile.operational_decel_ratio`.
`B6 = 1.00×`. `B7-B8` exceed it so the train can catch up if it falls behind
the curve. `EB` bypasses this table and uses `profile.emergency_brake_rate`
directly.

---

## 5. The safety stack

Stacking the constants gives the headroom from ATO's actual cruise speed up
to ATC EB:

```
                              ATC + 10 ─── EB escalation
                                         (pending_path_cancel_request armed)
                              ATC + 5  ─── ATC warning flag
                              ATC      ─── ATC ceiling
                              ATC − 1  ─── ATO deadband upper edge
                                         (= ceiling + ATO_SPEED_TOLERANCE)
                              ATC − 3  ─── ATO ceiling = ATC − ATO_SPEED_MARGIN_KMH
                              ATC − 5  ─── ATO power-up trigger
                                         (= ceiling − ATO_SPEED_TOLERANCE)
                              ATC − ∞  ─── below — ATO commands power
```

Numbers reflect the current values: `ATO_SPEED_MARGIN_KMH = 3.0`,
`ATO_SPEED_TOLERANCE = 2.0`. The train cruises in roughly the
`[ATC − 5, ATC − 1]` band and gets **6 km/h to ATC warning, 11 km/h to ATC
EB** — verified empirically (see §7) at 0 overshoot through 1x/2x/3x
playback.

If you tighten either constant, re-run the journey suite at all three
playbacks (`tests/test_demo_train_journey.py`) and confirm
`max(speed − atc_limit) ≤ 0`. The continuous `no_overspeed(epsilon=0.5)` and
`no_eb()` invariants in that suite are the backstop.

---

## 6. Parameter reference

### ATO — `train_controller.py`

| Constant | Default | What it controls |
|---|---|---|
| `ATO_SPEED_MARGIN_KMH` | **3.0** | The buffer ATO holds below ATC. Larger = more conservative cruise; smaller = closer to ATC, less EB headroom. ([train_controller.py:110](../train_controller.py:110)) |
| `ATO_SPEED_TOLERANCE` | **2.0** | Half-width of the ATO deadband around the ceiling. Train powers up below `ceiling − this`, coasts in between, brakes above `ceiling + this`. ([train_controller.py:102](../train_controller.py:102)) |
| `ATO_ENTRY_MARGIN` / `ATO_EXIT_MARGIN` | derived (`tol−1`, `tol+1`) | Hysteresis margins for ATO state transitions. ([train_controller.py:103-104](../train_controller.py:103)) |

### ATC — `train_controller.py`

| Constant / number | Default | What it controls |
|---|---|---|
| ATC warning threshold | inline `> 5` km/h | Speed over ATC at which `atc_warning_active = True`. ([train_controller.py:735](../train_controller.py:735)) |
| ATC EB threshold | inline `> 10` km/h | Speed over ATC at which the controller escalates to EB. ([train_controller.py:712](../train_controller.py:712)) |
| `ATC_SIGNAL_LOOKAHEAD_BLOCKS` | 3 | How many blocks downstream ATC scans for restrictive signal aspects. ([train_controller.py:92](../train_controller.py:92)) |
| `ATC_MIN_CLEARANCE_CM` | 10 | Minimum clearance the ATC speed curve treats as "stoppable in this distance". ([train_controller.py:93](../train_controller.py:93)) |
| `YELLOW_SIGNAL_SPEED_KMH` | 40 | Speed cap when next signal aspect is yellow. ([train_controller.py:95](../train_controller.py:95)) |
| `CAUTION_SPEED_LIMIT_KMH` | 40 | Speed cap when next forward block is in `CAUTION` state. ([train_controller.py:99](../train_controller.py:99)) |

### Lookahead — `constants.py` and `train_controller.py`

| Constant | Default | What it controls |
|---|---|---|
| `STOPPED_SPEED_THRESHOLD_KMH` | 0.1 | Below this the train counts as stopped (gates path completion, EB release, etc.). ([constants.py:19](../constants.py:19)) |
| `TRACK_END_SAFETY_MARGIN_CM` | 25 | Stop-short distance applied when the authority end is a buffer stop / dead end. ([constants.py:24](../constants.py:24)) |
| `PLATFORM_SAFETY_MARGIN_CM` | 5 | Stop-short distance applied when the authority end is a platform berthing point. ([constants.py:25](../constants.py:25)) |
| `LOOKAHEAD_TICKS` | 5 | Multiplier for the planning horizon: navigator looks `speed_cm_s * dt * LOOKAHEAD_TICKS` ahead. ([constants.py:45](../constants.py:45)) |
| `CURVE_SAMPLE_RESOLUTION_CM` | 3.0 | Sample step for the speed-curve walk in `calculate_speed_curves`. Smaller = smoother curves, more CPU. ([train_controller.py:107](../train_controller.py:107)) |

### Per-train (SpeedProfile)

These live on `SpeedProfile` and are documented as schema fields in
[`design_new.md`](../design_new.md) (~line 2013). Summary:

- `acceleration_rate` — km/h/s when at full throttle (P5).
- `deceleration_rate` — km/h/s the ATC curve is built against; B6 ≈ 1.00× of this rate.
- `operational_decel_ratio` — fraction of `deceleration_rate` ATO uses for its softer operational curve. `op_decel = deceleration_rate * operational_decel_ratio`.
- `emergency_brake_rate` — km/h/s applied when handle is `EB`.
- `friction_rate` — passive deceleration when coasting.
- `startup_voltage` / `lighting_voltage` — DC analog model voltages.
- `air_resistance_enabled` / `drag_coefficient` / `reference_speed_kmh` — optional aerodynamic drag model.

### Notch tables (hardcoded)

Power notch gap thresholds (km/h) in
[`ato_navigator.py:282-290`](../ato_navigator.py:282): `8 / 5 / 3 / 1.5 → P5 / P4 / P3 / P2 / P1`.

Brake notch gap thresholds (km/h) in
[`ato_navigator.py:303-320`](../ato_navigator.py:303): `25 / 18 / 12 / 8 / 5 / 3 / 1 → B8 / B7 / B6 / B5 / B4 / B3 / B2 / B1`.

`BRAKE_MULTIPLIERS` (against `op_decel`) in [`enums.py`](../enums.py):
`B1=0.20, B2=0.40, B3=0.55, B4=0.70, B5=0.85, B6=1.00, B7=1.30, B8=1.70`.

---

## 7. Tuning history

| Date | Change | Reason | Verification |
|---|---|---|---|
| 2026-05-09 | `ATO_SPEED_MARGIN_KMH` 5.0 → 3.0 | Train cruised too far below ATC; user wanted "closer to the curve, brake a bit later". | 70/70 pre-flight tests pass; demo train reaches ~55 km/h pre-Central (was ~51). |
| 2026-05-09 | ATO power-up `rate_limit_handle(allow_fast=True)` | Power-up was ±1 notch/tick while braking was ±2; re-acceleration after a slow zone felt slower than a fresh start. | Trace shows N→P5 in ~3 ticks vs ~5 before. |
| 2026-05-09 | ATO deadband `ATO_SPEED_TOLERANCE * 2` → `* 1` (4 km/h → 2 km/h) | Train topped out 4 km/h below ceiling and coasted instead of holding ceiling. | 70/70 pass. Worst-case `speed − ATC` overshoot at 1x/2x/3x = 0.000 km/h (probe in `_diag_overspeed.py` style). |

When you tune one of these, append a row here with the date, the change, the
reason, and the verification you did. Easier to undo a regression with the
context attached than to dig through commits.
