# Defaults & Fallbacks Audit

A review inventory of every default / fallback value the app substitutes. Generated 2026-06-09 in response to the `DEFAULT_TILE_LENGTH_CM` phantom-EB finding.

The dangerous class is **silent substitution**: a value that replaces missing / zero / `None` data without erroring, so bad data flows through looking valid. `DEFAULT_TILE_LENGTH_CM` was exactly this. Tier 1 below collects them; tiers 2–7 are intentional tunables grouped by domain (still worth a sanity pass).

---

## ⚠️ Hazards & inconsistencies (review these first)

1. **Geometry defaults to `0.0`, then a *different* default masks it.** `Block.length_cm`, `Switch.main/diverge_length_cm`, `Platform.length_cm` all default to `0.0` (dataclass *and* deserialization). For a **normal tile**, `_get_tile_length_cm` then silently substitutes `DEFAULT_TILE_LENGTH_CM = 50` cm for motion ([network.py:1301](network.py:1301)); for a **switch** with no entity it substitutes `0.0` ([network.py:1291-1293](network.py:1291)). Meanwhile `clearance_ahead()` summed the **raw `length_cm` (0)** — the phantom-EB root. The mismatch (raw 0 vs 50 vs switch-0) is the core problem: there is no single answer to "how long is this block."
2. ~~**`ATO_SPEED_MARGIN_KMH` defined twice with different values.**~~ **RESOLVED (Phase 0):** removed the dead `1.0` in `ato.py`; the effective `3.0` in `train_controller/__init__.py` is now the single definition.
3. ~~**Lighting voltage mismatch (2.0 vs 2.5).**~~ **RESOLVED (Phase 1, Tier 6):** train lighting already came from `SpeedProfile.lighting_voltage` ([train_physics.py:564](train_physics.py:564)); the vestigial `DEFAULT_LIGHTING_VOLTAGE = 2.5` / `BlockPowerController.default_lighting_voltage` (never drove a voltage) was removed.
4. **`ATC_NO_BLOCK_FALLBACK_CM = 100000.0`** ([atc.py:75](train_controller/atc.py:75), [__init__.py:101](train_controller/__init__.py:101)) — on a block-lookup miss, clearance becomes ~infinite (1 km). A lookup failure grants *unlimited* authority rather than zero. Fail-open, not fail-safe.
5. **Two braking-rate representations:** `SpeedProfile.deceleration_rate = 15` km/h/s + `emergency_brake_rate = 30` km/h/s (physics uses these) vs `Train.max_braking_rate = 3.0` **m/s²** ([train.py:71](train.py:71), config field). Different units, different homes — easy to confuse / drift.
6. **`_MODEL_SCALE = 160.0` is a mutable module global** ([model_scale.py:12](model_scale.py:12)); `set_scale()` rescales every cm/km-h conversion process-wide.

---

## Tier 1 — Silent substitution / fallback defaults (highest risk)

**Done (Phase 3):** `Block`/`Switch`/`Platform` `from_dict` now **reject a missing length** (explicit value required; `0` is allowed) — no more silent `0.0` on load. Scenes carry explicit lengths so loading is unaffected; the guard catches hand-authored / malformed input. Create-time enforcement (editor forms + create-API) is a follow-up: blocks/switches/platforms are placed-then-configured, so requiring length at placement needs the editor to collect it first. `DEFAULT_TILE_LENGTH_CM`/`ATC_NO_BLOCK_FALLBACK_CM` are kept (geometry fallbacks remain, per the "allow 0" policy).


| value | location | substitutes for | note |
|---|---|---|---|
| `Block.length_cm = 0.0` | [block.py:119](block.py:119), deser [block.py:307](block.py:307), [routes/blocks.py:222](routes/blocks.py:222) | unauthored block length | → 90/163 demo_6 blocks are 0 |
| `Switch.main/diverge_length_cm = 0.0` | [switch.py:129-130](switch.py:129), [switch.py:737-738](switch.py:737), [services/switch_service.py:89-90](services/switch_service.py:89), [step_player.py:193-194](step_player.py:193) | unauthored switch length | switches stay 0 (no 50cm fallback) |
| `Platform.length_cm = 0.0` | [railway_platform.py:63](railway_platform.py:63), [railway_platform.py:214](railway_platform.py:214), [routes/platforms.py:38](routes/platforms.py:38) | unauthored platform length | |
| `DEFAULT_TILE_LENGTH_CM = 50.0` | [network.py:42](network.py:42), used [network.py:1301](network.py:1301) | per-tile length when block length is 0 | **the one that bit us** |
| `DEFAULT_TRAIN_LENGTH_CM = 50.0` | [network.py:43](network.py:43) | train length fallback | |
| `ATC_NO_BLOCK_FALLBACK_CM = 100000.0` | [atc.py:75](train_controller/atc.py:75), [__init__.py:101](train_controller/__init__.py:101) | clearance when no block resolved | fail-open (huge authority) |
| `sample_curve_at(..., 0.0)` | [ato.py:374-388](train_controller/ato.py:374) | missing curve sample → `0.0` | could feed phantom-0 limit |
| `Train.max_speed = 100.0`, `min_radius_cm = 30.0` | [train.py:147](train.py:147), [train.py:153](train.py:153) | missing train config | |
| `train length = 1.0 m` | [routes/trains.py:96](routes/trains.py:96) | missing `length` on create | |
| `HANDLE_POSITION_INDEX.get(.., 5)` | [ato.py:157-158](train_controller/ato.py:157) | unknown handle → index 5 | |

---

## Tier 2 — Physics / SpeedProfile tunables
**Done (Phase 1):** the notch tables (`power_multipliers` / `brake_multipliers`) are now per-train fields on `SpeedProfile` (default to the standard table), serialized in `to_dict`/`from_dict`, and read by `TrainPhysics` + `ATOComputer`. They flow through the existing speed-profile CRUD API automatically, and are editable in the speed-profile editor's **Notch response** section (`editor-speed-profiles.ts`). Verified end-to-end in the browser: editing a notch persists per-train via the existing API.

[speed_profile.py:58-71](speed_profile.py:58) (and the `from_dict` mirror at :283-292):
`lighting_voltage 2.0` · `acceleration_rate 10.0 km/h/s` · `deceleration_rate 15.0` · `emergency_brake_rate 30.0` · `friction_rate 0.5` · `operational_decel_ratio 0.8` · `drag_coefficient 0.001` · `reference_speed_kmh 100.0` · `max_voltage 12.0` · `air_resistance_enabled True`

Brake/power notch multipliers [enums.py:295-323](enums.py:295): `P1 0.2 / P2 0.4 / P3 0.6 / P4 0.8 / P5 1.0`; `B1 0.20 / B2 0.40 / B3 0.55 / B4 0.70 / B5 0.85 / B6 1.00 / B7 1.30 / B8 1.70` (× operational decel).

Train config (separate, **m/s²**) [train.py:27-80](train.py:61): `TrainCar.max_speed 120` · `Train.max_speed 100` · `acceleration 2.0` · `max_braking_rate 3.0` · `num_cars 1` · `priority 5`.

---

> **Done (Phase 2):** Tiers 3/4/5/7 below are now live-tunable from the **Advanced** section of the Settings panel, backed by `runtime_config.RuntimeConfig` + `GET/PUT /api/settings`. The EB/warning overspeed thresholds (previously inline literals) are named (`OVERSPEED_EB_THRESHOLD_KMH` / `OVERSPEED_WARN_THRESHOLD_KMH`) and tunable too. Structural constants (`COPY_COUNT_BASE`, priority bounds) are intentionally not exposed.

## Tier 3 — ATC / ATO control tunables
[train_controller/__init__.py:99-109](train_controller/__init__.py:99) & [atc.py:75-76](train_controller/atc.py:75): `ATC_SIGNAL_LOOKAHEAD_BLOCKS 3` · `ATC_MIN_CLEARANCE_CM 10.0` · `SPEED_TREND_EPSILON 0.1` · `ATO_SPEED_MARGIN_KMH 3.0`.
[train_controller/ato.py:83-117](train_controller/ato.py:83): `ATO_SPEED_TOLERANCE_MAX 2.0` · `MIN 0.05` · `RATIO 0.1` · `LOOKAHEAD_HEADROOM 0.5` · `MIN_LOOKAHEAD_CM 5.0` · `LOOKAHEAD_SAMPLE_COUNT 4` · `BRAKE_DEADBAND_KMH 0.3` · `NOMINAL_TICK_DT 0.1` · `ATO_SPEED_MARGIN_KMH 1.0` (see hazard #2).
EB/warning thresholds are **inline literals, not named**: EB at `overspeed > 10`, warn at `> 5` ([__init__.py:750](train_controller/__init__.py:750),[:774](train_controller/__init__.py:774)) — no design number (doc gap).

---

## Tier 4 — Safety / geometry globals
[constants.py](constants.py): `STOPPED_SPEED_THRESHOLD_KMH 0.1` · `TRACK_END_SAFETY_MARGIN_CM 25.0` · `PLATFORM_SAFETY_MARGIN_CM 5.0` · `FLANK_PROTECTION_DISTANCE_CM 5.0` · `CAUTION_SPEED_LIMIT_KMH 40.0`.

---

## Tier 5 — Timing / simulation
`MAX_SIM_DELTA_SECONDS 1.0` ([runtime.py:35](runtime.py:35)) · `AUTOSAVE_INTERVAL 300s` ([runtime.py:21](runtime.py:21)) · `IDLE_HOLD_RELEASE_TICKS 30` · `IDLE_HOLD_COOLDOWN_TICKS 20` · `LOOKAHEAD_TICKS 5` (constants.py) · `NOMINAL_TICK_DT 0.1` ([ato.py:112](train_controller/ato.py:112)) · `simulation_speed = 1.0` ([routes/simulation.py:36](routes/simulation.py:36)) · `_MODEL_SCALE 160.0` ([model_scale.py:12](model_scale.py:12), mutable global).

---

## Tier 6 — Power / voltage
[block_power.py:19-21](block_power.py:19): `MAX_TRACK_VOLTAGE 12.0` · `MIN_TRACK_VOLTAGE 0.0` · `DEFAULT_LIGHTING_VOLTAGE 2.5` (see hazard #3).
[hardware/catalog.py:60](hardware/catalog.py:60): `SWITCH_THROW_PULSE_S 0.01`.

---

## Tier 7 — Routing / dispatch
`DEFAULT_PATH_PRIORITY 5` ([runtime_payloads.py:49](runtime_payloads.py:49)) · `MIN_PRIORITY 1` · `MAX_PRIORITY 10` · `COPY_COUNT_BASE 1_000_000` (constants.py).
`priority` defaults to `5` on deserialization in several places: [train.py:68](train.py:68), [train_route.py:371](train_route.py:371), [train_route.py:550](train_route.py:550), [route_executor.py:235](route_executor.py:235), [route_executor.py:672](route_executor.py:672).
