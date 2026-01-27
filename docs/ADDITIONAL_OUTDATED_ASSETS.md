# Additional Outdated Code and Documentation Analysis

> **Generated:** 2026-01-27
> **Purpose:** Identify remaining outdated code and documentation that doesn't match current implementation

---

## Reference Documents Needing Archival or Updates

### Reference_Vehicle_Pathfinding.md

**Status:** Outdated / Needs Archival

**Evidence:** 
- Document describes A* pathfinding for vehicles (`route_t::intern_calc_route`)
- Current train system uses **routes + dispatcher** (see `train_route.py`, `path_dispatcher.py`), NOT dynamic A* pathfinding
- `pathfinder.py` exists but is **only imported by tests** (`tests/test_pathfinding.py`), not production code
- Trains follow pre-assigned routes from the dispatcher

**Recommendation:** Archive to `archive/Reference_Vehicle_Pathfinding.md`

**Reasoning:** The document describes a pathfinding approach that was replaced by the route+dispatcher system. This is similar to the `Reference_TrainMover_ATC.md` that was already archived.

---

### Reference_UI_Interaction.md

**Status:** Outdated / Needs Archival

**Evidence:**
- Document describes Simutrans tool architecture (`tool_t`, `work`, `move` methods)
- Actual implementation in `static/js/editor.js` uses different patterns:
  - Canvas event handlers (`mousedown`, `mousemove`, `mouseup`)
  - State-based UI with tool selection
  - Not the two-click tool pattern described
- Document notes it's based on C++ code (`src/simutrans/tool/simtool.h`)

**Recommendation:** Archive to `archive/Reference_UI_Interaction.md`

**Reasoning:** The document describes Simutrans C++ patterns that don't match our JavaScript implementation. While the concepts are interesting for reference, they don't reflect how our editor actually works.

---

### Reference_Construction_Pathfinding.md

**Status:** Partially Accurate / Keep as Reference

**Evidence:**
- Describes Simutrans C++ implementation (`wegbauer.cc`)
- JavaScript implementation in `static/js/pathfinders.js` follows similar A* patterns:
  - `ConstructionPathfinder` class with `findPath()` method
  - Similar cost factors (`useExisting: 0.2`, `extend: 0.5`, `diagonal: 1`, etc.)
  - `pathToDiagonalTracks()` for diagonal build mode (matches document)
- Document correctly notes: "For actual implementation, see `static/js/editor.js`"

**Recommendation:** Keep as-is (标注为外部参考)

**Reasoning:** The document is clearly labeled as describing Simutrans patterns, not our implementation. It provides useful context for understanding why certain algorithms were chosen. The JavaScript code implements similar concepts.

---

### Reference_Track_Display.md

**Status:** Accurate / Keep

**Evidence:**
- Describes the 6-bit track system (`track_bits` 0-63)
- Rendering implementation in `static/js/renderer-tracks.js` matches:
  - Edge masks (N=13, E=22, S=49, W=42)
  - `drawGridSegment()` primitive with edge offsets
  - Forbidden track configurations
  - Switch rendering with active/inactive paths

**Recommendation:** Keep as-is (标注为核心参考)

**Reasoning:** This document accurately describes our internal implementation and is useful for developers working on track rendering.

---

## Dead Code Paths Identified

### pathfinder.py (RailwayPathfinder)

**Status:** Dead Code / UI-Only Legacy Code

**Evidence:**
```python
# tests/test_pathfinding.py:18
from pathfinder import RailwayPathfinder, RailPathNode
```

- Only imported by `tests/test_pathfinding.py`
- No production imports found in: `server.py`, `network.py`, `simulation.py`, `train.py`, `route_executor.py`
- Documented as A* for trains, but trains use routes + dispatcher

**Recommendation:** Keep for tests, but add deprecation warning

**Reasoning:**
1. Tests exist that use this code - removing would break tests
2. The code may be useful for future features (e.g., dynamic re-routing)
3. Document in module docstring that this is legacy UI-only code

**Action:** Add deprecation notice to `pathfinder.py`:
```python
"""
DEPRECATED: This module is kept for testing purposes only.
The RailwayPathfinder A* implementation is NOT used by production code.
Trains use route+dispatcher system instead of dynamic pathfinding.
"""
```

---

### train_movement.py (File Check)

**Status:** N/A - File Does Not Exist

**Evidence:**
- Search for `train_movement.py` returns no results
- No imports of `TrainMover` class from any file
- `Reference_TrainMover_ATC.md` was already archived to `archive/Reference_TrainMover_ATC.md`

**Recommendation:** No action needed

**Reasoning:** The TrainMover design was either never fully implemented or was refactored into other modules (`train_controller.py`, `simulation.py`).

---

## Duplicate Functionality

### Construction Pathfinding (Frontend vs Backend)

**Backend:** `pathfinder.py` - `RailwayPathfinder` class
- A* pathfinding for trains
- NOT used in production

**Frontend:** `static/js/pathfinders.js` - `ConstructionPathfinder` class
- A* pathfinding for track construction
- Actively used by `editor.js` for drag-to-build tracks

**Assessment:** No duplication - different purposes
- Backend version was intended for train navigation (superseded by routes)
- Frontend version is for construction/path preview (actively used)

**Recommendation:** Keep both, document their purposes

---

### Block Validation Logic

**Backend:** `block_validation.py` - `is_crossing()`, `validate_block_tile()`
**Frontend:** `static/js/pathfinders.js` - `validateBlockTile()`, `isCrossingForBlock()`

**Assessment:** Necessary duplication for UI feedback
- Frontend needs validation for immediate user feedback
- Backend needs validation for API security
- Both should remain synchronized

**Recommendation:** Keep both, ensure parity in tests

---

## Other Outdated Items

### openttd_reference/ Directory

**Contents:**
- `construction_vs_operation.md` - Research notes
- `tile_bit_system.md` - Track geometry research
- `track_configuration_and_signals.md` - Signal research
- `track_display_analysis.md` - Rendering research
- `track_placement_analysis.md` - Placement rules research
- `wayfinding_analysis.md` - Pathfinding research

**Status:** Research Notes / Archive Candidate

**Recommendation:** Move to `archive/openttd_reference/`

**Reasoning:** These are research notes from studying OpenTTD's implementation. They provided useful context during development but are not implementation documentation. Keep for historical reference.

---

### docs/plans/ Directory

**Contents (9 files, all dated 2026-01):**

| File | Status | Notes |
|------|--------|-------|
| `2026-01-13-sim-controls-implementation.md` | Implemented | Control panel features exist |
| `2026-01-13-sim-controls-refactor.md` | Implemented | Refactoring completed |
| `2026-01-13-train-path-refactor-design.md` | Implemented | `path_tiles` exists in `train.py` |
| `2026-01-13-train-path-refactor.md` | Partially Implemented | Path-based rendering exists |
| `2026-01-14-train-path-fix.md` | Implemented | Path fixes applied |
| `2026-01-16-editor-switch-toast-fixes.md` | Implemented | Toast notifications exist |
| `2026-01-25-train-monitor-controller-lookahead-design.md` | Implemented | Train monitor uses path_tile_info |
| `2026-01-26-ato-path-completion-design.md` | Implemented | ATO features exist |
| `2026-01-26-ato-path-completion-implementation.md` | Implemented | ATO implementation exists |

**Status:** Mixed - Most implemented, some may have evolved

**Recommendation:** Keep all plan documents

**Reasoning:**
1. Documents capture design intent at the time
2. Useful for understanding why features were implemented
3. Some details may have evolved - good historical record
4. Low overhead (9 markdown files)

---

## Summary of Recommended Actions

| Item | Action | Priority |
|------|--------|----------|
| `Reference_Vehicle_Pathfinding.md` | Archive to `archive/` | High |
| `Reference_UI_Interaction.md` | Archive to `archive/` | High |
| `openttd_reference/` | Move to `archive/openttd_reference/` | Medium |
| `pathfinder.py` | Add deprecation notice | Low |
| `Reference_Construction_Pathfinding.md` | Keep (标注外部参考) | N/A |
| `Reference_Track_Display.md` | Keep (标注核心参考) | N/A |
| `docs/plans/` | Keep all | N/A |

---

## Archival Command (for reference)

```bash
# Archive reference documents
git mv Reference_Vehicle_Pathfinding.md archive/
git mv Reference_UI_Interaction.md archive/

# Archive openttd_reference
git mv openttd_reference archive/

# Commit
git commit -m "docs: archive outdated reference documents"
```
