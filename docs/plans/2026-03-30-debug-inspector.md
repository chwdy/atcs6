# Debug Inspector Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a floating debug UI (separate page at `/debug`) that lets you browse and inspect any Python object in the backend at runtime — like Chrome DevTools for the railway simulation.

**Architecture:** A generic object inspector API (`/api/debug/inspect?path=network.trains`) that traverses the Python object graph and returns JSON, paired with a standalone HTML page with a tree-view UI for lazy browsing. No hardcoded per-entity endpoints — one generic endpoint handles everything.

**Tech Stack:** Python (Flask), vanilla JS, no dependencies.

---

## What GPT Got Wrong (and what we're changing)

| GPT's approach | Our approach |
|---|---|
| 25+ hardcoded endpoints, one per entity type | 1 generic inspector endpoint + 1 summary |
| 606-line routes/debug.py with 14 bugs | ~150-line `debug_inspector.py` with safe path traversal |
| No frontend UI at all | Standalone `/debug` page with tree view |
| Hallucinated method names (speed_kmh, etc.) | Introspects actual objects at runtime — no hardcoding |
| `api_logger.py` middleware on every request | Dropped — inspector is more useful, logger can be added later if needed |
| Coupled to private controller methods | Only accesses `AppState` and traverses from there |

## What we keep from GPT

- `BlockManager.history` recording (fix the occupied_by bug)
- `docs/DATA_STRUCTURES.md` (good reference doc, keep as-is)

## What we discard from GPT

- `api_logger.py` — premature, noisy (logs every `/api/` call including simulation ticks)
- `routes/debug.py` — replace entirely with generic inspector
- Changes to `routes/__init__.py` — revert the debug route registration
- Changes to `app_factory.py` — revert the logger middleware hookup

---

## Design

### Backend: Generic Object Inspector

Single endpoint: `GET /api/debug/inspect?path=<dotted.path>&depth=<N>`

**Path resolution:**
- Root object is `AppState` (the `state` variable)
- `path=network` → `state.network`
- `path=network.trains` → `state.network.trains` (returns dict keys)
- `path=network.trains.T1` → `state.network.trains["T1"]` (Train object)
- `path=network.trains.T1.position.head_tile` → the value

**Serialization rules (depth-limited):**
- Primitives (int, float, str, bool, None) → return value directly
- Enums → return `{"_type": "enum", "value": "FREE", "class": "BlockState"}`
- Dicts → return `{"_type": "dict", "length": N, "keys": [...]}` at depth=0, full entries at depth>0
- Lists/tuples → return `{"_type": "list", "length": N}` at depth=0, items at depth>0
- Sets/frozensets → return `{"_type": "set", "length": N, "items": [...]}` at depth=0
- Dataclass/object → return `{"_type": "object", "class": "Train", "fields": {"field_name": ...}}` with field values serialized recursively up to depth limit
- Circular references → return `{"_type": "circular", "repr": "..."}`
- Callables → return `{"_type": "method", "name": "occupy_block"}`
- Anything else → return `{"_type": "unknown", "repr": str(obj)[:200]}`

**Depth control:**
- `depth=0`: Show type info and keys/length only (no values for children)
- `depth=1` (default): Show one level of children with their types
- `depth=2`: Two levels deep (use sparingly)

**Safety:**
- Only traverses from `AppState` — no arbitrary Python eval
- Skips attributes starting with `__` (dunder)
- Skips modules and classes themselves (only instances)
- Max 1000 items per collection (truncate with `"_truncated": true`)
- Max depth=3 hard limit

### Additional endpoint: Summary

`GET /api/debug/summary` — Quick counts of all entities. This is the one curated endpoint worth keeping because it's the landing page.

### Additional endpoint: BlockManager history

`GET /api/debug/block-history?limit=50` — Recent block state transitions (from the history deque GPT added to BlockManager).

### Frontend: Standalone Debug Page

Served at `/debug` — completely separate HTML page, not loaded as part of the main app.

**Layout:**
```
┌─────────────────────────────────────────────┐
│  Debug Inspector          [Auto-refresh ▢]  │
├─────────────────────────────────────────────┤
│  Path: [network.trains.T1_____________] [→] │
├─────────────────────────────────────────────┤
│  Summary: 24 tracks, 6 blocks, 4 signals,  │
│           2 trains, sim=running             │
├─────────────────────────────────────────────┤
│  ▼ network          RailwayNetwork          │
│    ▼ trains         dict (2 items)          │
│      ▼ T1           Train                   │
│        ● train_id   "T1"                    │
│        ● state      TrainState.RUNNING      │
│        ▶ position   TrainPosition           │
│        ● current_speed  45.2                │
│      ▶ T2           Train                   │
│    ▶ blocks         dict (6 items)          │
│    ▶ signals        dict (4 items)          │
│  ▶ path_dispatcher  PathDispatcher          │
│  ▶ sim_controller   SimulationController    │
├─────────────────────────────────────────────┤
│  Block History (last 20)                    │
│  10:32:01 B1 FREE→RESERVED (T1)            │
│  10:32:02 B1 RESERVED→OCCUPIED (T1)        │
│  10:32:05 B1 OCCUPIED→CAUTION (T1)         │
└─────────────────────────────────────────────┘
```

**Behavior:**
- Click `▶` to expand a node (fetches `/api/debug/inspect?path=<full.path>&depth=1`)
- Click `▼` to collapse
- Primitives shown inline with value
- Path bar allows direct navigation (type a path, press Enter)
- Auto-refresh: when enabled, re-fetches currently expanded nodes every 2s
- Block history panel at the bottom, auto-refreshes

---

## Tasks

### Task 1: Revert GPT's changes and keep only BlockManager.history

**Files:**
- Revert: `app_factory.py` (remove api_logger imports)
- Revert: `routes/__init__.py` (remove debug route)
- Delete: `api_logger.py`
- Delete: `routes/debug.py`
- Keep: `block_manager.py` changes (history recording) — but fix the bug
- Keep: `docs/DATA_STRUCTURES.md`

**Step 1: Revert app_factory.py**

```bash
git checkout -- app_factory.py
```

**Step 2: Revert routes/__init__.py**

```bash
git checkout -- routes/__init__.py
```

**Step 3: Delete GPT's files**

```bash
rm api_logger.py routes/debug.py
```

**Step 4: Fix BlockManager.history bug**

In `block_manager.py`, the `clear_block` method records `block.occupied_by` AFTER setting it to `None`. Fix: capture `occupied_by` before clearing.

**Step 5: Run tests**

```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py -v
```

**Step 6: Commit**

```bash
git add -A && git commit -m "revert GPT debug scaffolding, keep BlockManager.history"
```

---

### Task 2: Build the generic object inspector backend

**Files:**
- Create: `debug_inspector.py` (~150 lines)
- Modify: `app_factory.py` (register debug routes)

**Step 1: Write tests for the inspector**

Create `tests/test_debug_inspector.py` with tests for:
- Path resolution: `""` → AppState, `"network"` → RailwayNetwork
- Dict traversal: `"network.trains"` → dict keys
- Dict key lookup: `"network.trains.T1"` → Train object
- Attribute access: `"network.trains.T1.train_id"` → string value
- Depth limiting: depth=0 shows types only, depth=1 shows values
- Enum serialization: BlockState.FREE → `{"_type": "enum", ...}`
- Circular reference handling
- Invalid path → 404
- Truncation for large collections

**Step 2: Run tests to verify they fail**

```bash
conda run -n general pytest tests/test_debug_inspector.py -v
```

**Step 3: Implement `debug_inspector.py`**

Core functions:
- `resolve_path(root, path_string)` → Python object
- `serialize_object(obj, depth, max_items)` → JSON-safe dict
- `register_debug_routes(app, state)` → Flask route registration

Routes:
- `GET /api/debug/inspect` — generic inspector
- `GET /api/debug/summary` — entity counts
- `GET /api/debug/block-history` — BlockManager history

**Step 4: Register routes in app_factory.py**

Add `from debug_inspector import register_debug_routes` and call it after `register_all`.

**Step 5: Run all tests**

```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py tests/test_debug_inspector.py -v
```

**Step 6: Manual smoke test**

```bash
python3 server.py &
curl -s http://localhost:5000/api/debug/summary | python3 -m json.tool
curl -s "http://localhost:5000/api/debug/inspect?path=network&depth=1" | python3 -m json.tool
```

**Step 7: Commit**

```bash
git add -A && git commit -m "feat: add generic debug object inspector API"
```

---

### Task 3: Build the floating debug UI page

**Files:**
- Create: `static/debug.html` (standalone page)
- Create: `static/js/debug-inspector.js` (tree view + API calls)
- Create: `static/css/debug.css` (styling for the debug page)
- Modify: `debug_inspector.py` (add route to serve the page)

**Step 1: Create the debug HTML page**

Standalone page at `/debug`. Includes:
- Path input bar
- Summary panel
- Tree view container
- Block history panel
- Auto-refresh toggle

**Step 2: Implement the tree view JS**

`debug-inspector.js`:
- `fetchInspect(path, depth)` — calls API
- `renderNode(data, path, container)` — renders a tree node
- `expandNode(path)` — fetches children and inserts into tree
- `collapseNode(path)` — removes children from DOM
- `refreshSummary()` — updates summary panel
- `refreshHistory()` — updates block history
- Auto-refresh timer (2s interval, toggleable)

**Step 3: Style the debug page**

Dark theme, monospace font, tree indentation with lines. Completely self-contained CSS (doesn't import main app styles).

**Step 4: Add route to serve the page**

In `debug_inspector.py`, add:
```python
@app.route("/debug")
def debug_page():
    return app.send_static_file("debug.html")
```

**Step 5: Run syntax and smoke tests**

```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py -v
```

**Step 6: Manual test**

Open `http://localhost:5000/debug` in browser, verify:
- Summary shows entity counts
- Can expand `network` → `trains` → individual trains
- Can type path in input bar
- Block history shows (if simulation has run)

**Step 7: Commit**

```bash
git add -A && git commit -m "feat: add floating debug inspector UI at /debug"
```

---

### Task 4: Final integration test and cleanup

**Step 1: Run full test suite**

```bash
conda run -n general pytest tests/test_javascript_syntax.py tests/test_app_smoke.py tests/test_backend.py tests/test_debug_inspector.py -v
```

**Step 2: Verify server starts**

```bash
python3 server.py
```

**Step 3: Test debug page end-to-end**

- Open `/debug`, verify tree browsing works
- Start simulation, verify auto-refresh shows live data
- Check block history updates as trains move

**Step 4: Commit if any final fixes needed**

---

## Future Enhancements (not in this plan)

These are explicitly NOT included — add them only if real need emerges:

- **API call logger** — GPT's idea, but add later if you actually need request replay
- **Variable watch list** — pin specific paths and monitor them
- **Value editing** — modify variables from the debug UI (dangerous, but powerful)
- **WebSocket for real-time** — replace polling with push updates
- **Breakpoints** — pause simulation when a condition is met
