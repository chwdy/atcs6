# Frontend Architecture

## Overview

The frontend is a vanilla JavaScript (ES6+) single-page application with no build tools, no module bundler, and no transpilation. All 25+ JS files are loaded via `<script>` tags in `static/index.html` in strict dependency order.

The UI renders a railway network on an HTML5 Canvas using isometric projection, with editing tools, a simulation monitor, and property panels.

## Script Loading Order

Scripts must load in this exact order (later scripts depend on earlier ones):

```
constants.js        # Color/size constants, EDGES map
helpers.js          # UI utility functions
api.js              # API class - backend HTTP communication
state-manager.js    # StateManager - centralized client cache + pub/sub
pathfinders.js      # Construction and existing-track pathfinders

renderer.js         # Renderer (coordinator)
renderer-tracks.js  # Track tile rendering
renderer-trains.js  # Train rendering with path interpolation
renderer-blocks.js  # Block overlay rendering
renderer-signals.js # Signal rendering
renderer-stations.js# Station rendering
renderer-platforms.js # Platform rendering
renderer-routes.js  # Route overlay rendering
renderer-debug.js   # Debug overlay rendering

editor.js           # Editor (coordinator + God Object)
editor-ui.js        # Shared editor UI helpers
editor-blocks.js    # Block editor panel
editor-signals.js   # Signal editor panel
editor-stations.js  # Station editor panel
editor-switches.js  # Switch editor panel
editor-trains.js    # Train editor panel
editor-routes.js    # Route editor panel
editor-paths.js     # Path editor panel

train-monitor.js    # Train status monitoring bar

app.js              # App class - initialization + main loop
```

**Why this order matters:** There is no module system (`import`/`export`). Each file defines classes or functions on the global scope. Later files instantiate or reference classes defined in earlier files. Misordering causes `ReferenceError` at load time.

## Core Architecture Pattern

### Composition via Handler Sub-Classes

Both `Renderer` and `Editor` use a delegation pattern where the main class instantiates specialized handler objects:

```
App
├── API              (backend communication)
├── StateManager     (client state cache + pub/sub)
├── Renderer
│   ├── TrackRenderer
│   ├── TrainRenderer
│   ├── BlockRenderer
│   ├── SignalRenderer
│   ├── StationRenderer
│   ├── PlatformRenderer
│   ├── RouteRenderer
│   └── DebugRenderer
├── Editor
│   ├── BlockEditor
│   ├── SignalEditor
│   ├── StationEditor
│   ├── SwitchEditor
│   ├── TrainEditor
│   ├── RouteEditor
│   └── PathEditor
└── TrainMonitor
```

Handler sub-classes receive a reference to their parent (e.g., `this.editor = editor`) and access shared state through it.

### Global Singletons

The application relies on global singletons stored on `window`:

```javascript
window.app       // App instance
window.api       // API instance (also app.api)
window.editor    // Editor instance (also app.editor)
window.renderer  // Renderer instance (also app.renderer)
```

Many sub-handlers access these globals directly (e.g., `window.app.stateManager`) rather than through injected dependencies.

## State Management

### StateManager (`state-manager.js`)

A centralized cache with pub/sub notifications:

```javascript
class StateManager {
    // Cached data (refreshed from backend)
    network = null;        // Full network snapshot
    blocks = [];           // Block list
    signals = [];          // Signal list
    stations = [];         // Station list
    trains = [];           // Train list
    switches = [];         // Switch list

    // Pub/sub
    subscribe(event, callback)
    notify(event, data)
}
```

**Key events:** `networkChanged`, `blocksChanged`, `signalsChanged`, `stationsChanged`, `trainsChanged`, `switchesChanged`, `simulationStateChanged`

**Data flow:**
1. User action or simulation tick triggers an API call
2. API response updates `StateManager` cache
3. `StateManager.notify()` fires subscriber callbacks
4. Subscribers (Editor panels, Renderer, TrainMonitor) re-render

### Backend as Source of Truth

The backend is the single source of truth. The frontend never computes derived state locally -- it always fetches from the backend after mutations.

**Polling model:**
- During simulation: full network poll every ~200ms via `GET /api/network`
- During editing: on-demand fetches after each user action
- No WebSocket or delta-update protocol

## Communication Patterns

The frontend uses four communication mechanisms (inconsistently):

| Pattern | Where Used | Example |
|---------|-----------|---------|
| **Direct method calls** | App → Editor, App → Renderer | `this.editor.handleClick(e)` |
| **Pub/sub via StateManager** | Editor panels, TrainMonitor | `stateManager.subscribe('blocksChanged', fn)` |
| **DOM CustomEvents** | Some inter-component signals | `document.dispatchEvent(new CustomEvent(...))` |
| **window.* globals** | Cross-cutting access | `window.app.stateManager.getBlocks()` |

This mix evolved organically. New code should prefer StateManager pub/sub for loose coupling.

## Rendering Pipeline

The `Renderer` class manages an HTML5 Canvas with isometric projection:

1. **Coordinate transform:** Grid (x, y) → isometric screen (px, py)
2. **Render order:** Background → tracks → blocks → stations → platforms → signals → trains → routes → debug overlay
3. **Render loop:** `requestAnimationFrame` during simulation; manual `render()` call during editing
4. **Camera:** Pan (drag) and zoom (scroll wheel) with matrix transforms

Train rendering uses smooth path interpolation:
- Backend provides `path_tiles` and `path_tile_info` arrays
- `TrainRenderer` interpolates head/tail positions between tile entry/exit points
- Car positions are calculated by walking backward from the head along the path

## API Communication (`api.js`)

The `API` class wraps all `fetch()` calls to the backend:

```javascript
class API {
    async getNetwork()                     // GET /api/network
    async updateTrack(x, y, updates)       // PUT /api/track/{x}/{y}
    async createBlock(data)                // POST /api/blocks
    async startSimulation()                // POST /api/simulation/control
    // ... ~50+ methods covering all endpoints
}
```

- All methods use `async/await` with `try/catch`
- Errors logged with `[API]` prefix via `console.error`
- No request queuing or retry logic
- No request deduplication (rapid clicks can fire duplicate requests)

## Known Anti-Patterns

These are documented for awareness, not as immediate action items:

1. **editor.js as God Object** (~2300+ lines): Handles track placement, selection, context menus, keyboard shortcuts, drag operations, and coordinates all editor panels. Should be decomposed further.

2. **No module system:** All files share global scope. Name collisions are avoided only by convention. No tree-shaking or dead code elimination.

3. **Mixed communication patterns:** Four different inter-component communication mechanisms make data flow hard to trace.

4. **Full network polling:** Every 200ms during simulation, the entire network JSON is fetched. For large layouts this creates unnecessary bandwidth and parsing overhead. Delta updates would be more efficient.

5. **No error boundary:** A JavaScript error in any file prevents the entire application from loading (all scripts share one execution context).

## File Size Reference

Largest frontend files (indicators of complexity):

| File | Size | Notes |
|------|------|-------|
| `editor.js` | ~87KB | God Object - needs decomposition |
| `editor-trains.js` | ~64KB | Train editing is complex |
| `app.js` | ~52KB | Main loop + initialization |
| `train-monitor.js` | ~47KB | Monitoring panel |
| `api.js` | ~29KB | ~50+ endpoint methods |
| `renderer-trains.js` | ~22KB | Path interpolation logic |

## Adding New Frontend Code

When adding new JavaScript functionality:

1. **Create a new file** following the naming pattern (`editor-*.js`, `renderer-*.js`)
2. **Add the `<script>` tag** in `static/index.html` in the correct position (after dependencies, before dependents)
3. **Wire it up** in the parent class constructor (e.g., `this.myHandler = new MyHandler(this)`)
4. **Use StateManager** for cross-component communication (subscribe/notify)
5. **Run `test_javascript_syntax.py`** to catch syntax errors before testing in browser
6. **Use CSS classes** from `STYLE_GUIDE.md` (now in `docs/`) -- never inline styles for static values
