# Implementation Status Matrix

This document tracks the implementation status of features described in reference documentation.

## Legend
- ✅ Fully Implemented - Feature complete and tested
- ⚠️ Partially Implemented - Core functionality exists but incomplete
- ❌ Not Implemented - Specified but not yet built
- 📝 Documented Only - Reference exists but no implementation planned

---

## Feature Status

### 6-Bit Track System ([`reference/Reference_6Bit_Track_System.md`](../reference/Reference_6Bit_Track_System.md))
- ✅ Track bit constants and edge masks
- ✅ Forbidden track bit validation
- ✅ Switch point detection
- ✅ Curve detection logic
- ✅ Edge-to-edge path calculation

### Train System v2 ([`reference/Reference_Train_System_v2.md`](../reference/Reference_Train_System_v2.md))
- ✅ [`TrainController`](../train_controller.py) - ATC + ATO + command handling
- ✅ [`TrainPhysics`](../train_physics.py) - Movement calculations and track navigation
- ✅ Handle model with discrete notches (P1-P5, N, B1-B8, EB)
- ✅ State machine ([`TrainState`](../enums.py) enum)
- ✅ DC analog control (block voltage mapping via [`SpeedProfile`](../speed_profile.py))
- ✅ Power control (on/off, emergency stop)
- ✅ Cab selection and direction
- ✅ Route-based autonomous travel (ATO) via [`train_route.py`](../train_route.py)
- ✅ Manual driving with ATC protection
- ⚠️ Automatic WAITING ↔ MOVING transitions (implemented but needs validation)
- ⚠️ Multi-block occupancy (basic implementation, needs refinement)

### Signal System ([`reference/Reference_Signals_and_Blocks.md`](../reference/Reference_Signals_and_Blocks.md))
- ⚠️ Basic BLOCK signals (RED/GREEN only) - See [`railway_signal.py`](../railway_signal.py)
- ❌ SWITCH signals (interlocking with switch points)
- ❌ PRE_SIGNAL type (lookahead signaling)
- ❌ ROUTE signals (path-based aspect control)
- ❌ Route-based reservations (`approaching_train_id`)
- ❌ Complex signal state calculation (Block B lookahead)
- ⚠️ Signal validation - Basic checks exist in [`signal_validation.py`](../signal_validation.py)

### Pathfinding ([`Reference_Vehicle_Pathfinding.md`](../Reference_Vehicle_Pathfinding.md))
- ⚠️ A* pathfinding for trains - Basic implementation exists in [`pathfinder.py`](../pathfinder.py)
- ❌ Dynamic path recalculation during movement
- ❌ Multi-train path coordination
- ⚠️ Path dispatcher - Basic implementation in [`path_dispatcher.py`](../path_dispatcher.py)

### Construction ([`reference/Reference_Construction_Pathfinding.md`](../reference/Reference_Construction_Pathfinding.md))
- ❌ Track building pathfinding algorithms
- ❌ Construction preview system
- ❌ Cost calculation for construction paths

### UI Interaction ([`Reference_UI_Interaction.md`](../Reference_UI_Interaction.md))
- ❌ Simutrans-style tool interactions
- ⚠️ Basic editor interaction exists in [`static/js/editor.js`](../static/js/editor.js)
- ⚠️ Track placement UI (functional but not Simutrans-style)

### Track Display ([`reference/Reference_Track_Display.md`](../reference/Reference_Track_Display.md))
- ✅ Isometric rendering - See [`static/js/renderer.js`](../static/js/renderer.js)
- ✅ Track tile rendering with 6-bit system
- ✅ Track overlay rendering (blocks, signals)
- ✅ Train rendering on tracks - See [`static/js/renderer-trains.js`](../static/js/renderer-trains.js)

---

## Module Status

### Core Infrastructure
| Module | Implementation Status | Notes |
|--------|----------------------|-------|
| [`track.py`](../track.py) | ✅ Complete | 6-bit track system fully implemented |
| [`block.py`](../block.py) | ✅ Complete | Block entity with state management |
| [`block_manager.py`](../block_manager.py) | ✅ Complete | Centralized block state authority |
| [`railway_signal.py`](../railway_signal.py) | ⚠️ Partial | Basic signals only, missing advanced types |
| [`switch.py`](../switch.py) | ✅ Complete | Switch point management |
| [`station.py`](../station.py) | ✅ Complete | Station entities |

### Train System
| Module | Implementation Status | Notes |
|--------|----------------------|-------|
| [`train.py`](../train.py) | ✅ Complete | Train data entity |
| [`train_controller.py`](../train_controller.py) | ✅ Complete | ATC + ATO + command handling |
| [`train_physics.py`](../train_physics.py) | ✅ Complete | Movement and block management |
| [`speed_profile.py`](../speed_profile.py) | ✅ Complete | Speed-voltage mapping |
| [`train_route.py`](../train_route.py) | ✅ Complete | Route and waypoint system |
| [`train_state_manager.py`](../train_state_manager.py) | ⚠️ Partial | May be superseded by train_controller |

### Simulation & Control
| Module | Implementation Status | Notes |
|--------|----------------------|-------|
| [`simulation.py`](../simulation.py) | ✅ Complete | Simulation orchestrator |
| [`path_dispatcher.py`](../path_dispatcher.py) | ⚠️ Partial | Basic path assignment |
| [`pathfinder.py`](../pathfinder.py) | ⚠️ Partial | Basic A* implementation |
| [`route_executor.py`](../route_executor.py) | ✅ Complete | Waypoint navigation, dwell timing, flexible platform resolution |

### Application Infrastructure
| Module | Implementation Status | Notes |
|--------|----------------------|-------|
| [`app_state.py`](../app_state.py) | ✅ Complete | Dependency injection container for runtime objects |
| [`app_factory.py`](../app_factory.py) | ✅ Complete | Flask app factory |
| [`bootstrap.py`](../bootstrap.py) | ✅ Complete | Network initialization (demo or empty) |
| [`runtime.py`](../runtime.py) | ✅ Complete | Background thread management (sim loop, autosave) |
| [`model_scale.py`](../model_scale.py) | ✅ Complete | Model scale factor and speed conversion utilities |

### Debugging & Testing
| Module | Implementation Status | Notes |
|--------|----------------------|-------|
| [`debug_inspector.py`](../debug_inspector.py) | ✅ Complete | Generic object inspector API + `/debug` UI |
| [`scenario_runner.py`](../scenario_runner.py) | ✅ Complete | Scenario replay engine with Flask test client and assertion checking |
| [`step_recorder.py`](../step_recorder.py) | ✅ Complete | Records layout construction as ordered steps |
| [`step_player.py`](../step_player.py) | ✅ Complete | Replays construction steps to rebuild a layout |

### Network Management
| Module | Implementation Status | Notes |
|--------|----------------------|-------|
| [`network.py`](../network.py) | ✅ Complete | Railway network core |
| [`persistence.py`](../persistence.py) | ✅ Complete | Save/load functionality |
| [`server.py`](../server.py) | ✅ Complete | Flask REST API |

### Frontend
| Module | Implementation Status | Notes |
|--------|----------------------|-------|
| [`static/js/api.js`](../static/js/api.js) | ✅ Complete | Backend API client |
| [`static/js/renderer.js`](../static/js/renderer.js) | ✅ Complete | Isometric rendering |
| [`static/js/editor.js`](../static/js/editor.js) | ⚠️ Partial | Basic editing, needs enhancement |
| [`static/js/train-monitor.js`](../static/js/train-monitor.js) | ✅ Complete | Train control panel |
| [`static/js/debug-inspector.js`](../static/js/debug-inspector.js) | ✅ Complete | Debug inspector tree-view UI at `/debug` |
| [`static/js/scenario-recorder.js`](../static/js/scenario-recorder.js) | ✅ Complete | Activity recorder intercepting API calls |

---

## Archive

The following references are archived as outdated:

- **[`archive/Reference_TrainMover_ATC.md`](../archive/Reference_TrainMover_ATC.md)** - Superseded by current [`train_controller.py`](../train_controller.py) implementation. The `TrainMover` class described in this document was never implemented; functionality merged into `TrainController` + `TrainPhysics`.

- **[`archive/Reference_Train_System.md`](../archive/Reference_Train_System.md)** - Superseded by [`reference/Reference_Train_System_v2.md`](../reference/Reference_Train_System_v2.md). Version 1 had overlapping responsibilities across too many classes. Version 2 simplified to a clean two-class model.

---

## Notes

### Implementation Priority
Features marked ❌ are documented but not yet scheduled for implementation. Prioritize based on:
1. Critical path features (blocking other work)
2. Features needed for MVP functionality
3. Nice-to-have enhancements

### Testing Coverage
All ✅ features should have corresponding tests in [`tests/`](../tests/). Partial implementations (⚠️) may have incomplete test coverage.

### Documentation vs Implementation
Some references describe "ideal" designs that may not match current implementation due to:
- Incremental development (not all features built yet)
- Design evolution (simpler approaches discovered)
- Scope changes (features deferred or cancelled)

Always check the actual code for definitive behavior.

---

## Recent Completions (2026)

- **2026-03-31** — `Train.to_dict()` now accepts live path data from PathDispatcher; stale `train.current_path` cache removed from serialization. `ato_destination_block_id` synced on path extension.
- **2026-03-30** — Debug inspector (`debug_inspector.py` + `/debug` UI) implemented.
- **2026-03-30** — Scenario runner (`scenario_runner.py`) and step recorder/player implemented for regression testing.
- **2026-02-24** — Future-aware ATO control: trend detection, asymmetric hysteresis, and full predictive simulation (`_simulate_forward`) implemented and integrated into `_ato_calculate_handle`.
- **2026-02-11** — PathDispatcher made single source of truth for path data; `TrainController.get_path_tiles()` reads from dispatcher.
- **2026-02-06** — Route/station redesign complete: `RouteExecutor`, flexible platform resolution, PRESERVE travel direction policy.

- **2026-04-04** — `scenario_runner.py` fully implemented: data classes, Flask test client replay, StepPlayer layout setup, checkpoint assertion evaluation. `tests/test_scenario_runner.py` and `tests/test_scenarios.py` (parametrized auto-discovery) added. Sample scenario `tests/scenarios/sample-block-state.json` created.

*Last Updated: April 4, 2026*
