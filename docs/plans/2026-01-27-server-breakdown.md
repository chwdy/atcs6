# Server Breakdown Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Break `server.py` side effects and routes into clear modules while preserving default demo startup behavior (unless `--no-demo`), and clean up stale TODOs/docs.

**Architecture:** Introduce an app factory and explicit runtime/bootstrap modules so imports are side‑effect free; move API routes into domain modules and wire them with shared state injection; keep the current demo‑by‑default behavior behind CLI flags.

**Tech Stack:** Python 3, Flask, pytest, vanilla JS (no build).

---

## Task 1: Add an explicit app factory and shared state container

**Files:**
- Create: `app_factory.py`
- Create: `app_state.py`
- Modify: `server.py`

**Step 1: Write a failing test (if needed)**

If no tests cover app creation semantics, skip adding new tests and rely on existing smoke tests.

**Step 2: Implement shared state container**

Create a lightweight dataclass to hold global objects currently in `server.py`.

```python
# app_state.py
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from network import RailwayNetwork
from path_dispatcher import PathDispatcher
from persistence import PersistenceManager
from simulation import SimulationController


@dataclass
class AppState:
    network: RailwayNetwork
    path_dispatcher: PathDispatcher
    sim_controller: SimulationController
    persistence_manager: PersistenceManager
    simulation_lock: object
    simulation_running: bool
    simulation_speed: float
    last_update_time: datetime
```

**Step 3: Implement app factory**

`create_app(state: AppState) -> Flask` that registers converters/CORS and routes.

```python
# app_factory.py
from flask import Flask
from flask_cors import CORS
from werkzeug.routing import IntegerConverter

from app_state import AppState
from routes import blocks, signals, stations, tracks, trains, paths, misc


class SignedIntConverter(IntegerConverter):
    regex = r"-?\d+"


def create_app(state: AppState) -> Flask:
    app = Flask(__name__, static_folder="static")
    CORS(app)
    app.url_map.converters["sint"] = SignedIntConverter

    tracks.register(app, state)
    blocks.register(app, state)
    signals.register(app, state)
    stations.register(app, state)
    trains.register(app, state)
    paths.register(app, state)
    misc.register(app, state)

    return app
```

**Step 4: Update server entrypoint to use the app factory**

Add a `main()` in `server.py` that creates `AppState`, then calls `create_app()` and `app.run()`.

**Step 5: Run tests**

Run the required suites:

```bash
source ~/.nvm/nvm.sh && nvm use 22
conda run -n general pytest tests/test_javascript_syntax.py -v
conda run -n general pytest tests/test_app_smoke.py -v
conda run -n general pytest tests/test_backend.py -v
```

**Step 6: Commit**

```bash
git add app_factory.py app_state.py server.py
git commit -m "refactor: introduce app factory and app state"
```

---

## Task 2: Move API routes into domain modules

**Files:**
- Create: `routes/__init__.py`
- Create: `routes/tracks.py`
- Create: `routes/blocks.py`
- Create: `routes/signals.py`
- Create: `routes/stations.py`
- Create: `routes/trains.py`
- Create: `routes/paths.py`
- Create: `routes/misc.py`
- Modify: `server.py`

**Step 1: Extract routes with a consistent register() API**

Each module exports `register(app, state)` and uses `state.network` / `state.sim_controller`.

```python
# routes/tracks.py
from flask import jsonify, request

def register(app, state):
    network = state.network

    @app.route("/api/track/<sint:x>/<sint:y>", methods=["GET"])
    def get_track(x, y):
        ...
```

**Step 2: Move helper functions used by routes**

Keep helpers that are only used by routes in the `routes/` module or a `routes/helpers.py` file.

**Step 3: Ensure shared locks are used consistently**

Any route that currently uses `simulation_lock` should read it from `state`.

**Step 4: Remove route definitions from `server.py`**

`server.py` should only build state, set up runtime, and call `create_app()`.

**Step 5: Run tests**

```bash
source ~/.nvm/nvm.sh && nvm use 22
conda run -n general pytest tests/test_javascript_syntax.py -v
conda run -n general pytest tests/test_app_smoke.py -v
conda run -n general pytest tests/test_backend.py -v
```

**Step 6: Commit**

```bash
git add routes server.py
git commit -m "refactor: split api routes into modules"
```

---

## Task 3: Move startup tests, demo boot, and threads behind CLI flags

**Files:**
- Create: `bootstrap.py`
- Create: `runtime.py`
- Modify: `server.py`

**Step 1: Create bootstrap module**

`bootstrap.py` should build and return `AppState` based on flags:

- Default: demo boot (current behavior)
- `--no-demo`: start empty network

**Step 2: Create runtime module**

`runtime.py` should provide `start_simulation_thread(state)` and `start_autosave_thread(state)`.

**Step 3: Parse CLI flags in server.py**

Add flags:

- `--no-demo`
- `--skip-startup-tests`
- `--no-sim`
- `--no-autosave`

Only run startup tests and start threads inside `main()`.

**Step 4: Run tests**

```bash
source ~/.nvm/nvm.sh && nvm use 22
conda run -n general pytest tests/test_javascript_syntax.py -v
conda run -n general pytest tests/test_app_smoke.py -v
conda run -n general pytest tests/test_backend.py -v
```

**Step 5: Commit**

```bash
git add bootstrap.py runtime.py server.py
git commit -m "refactor: move bootstrapping and runtime behind main"
```

---

## Task 4: Clean up stale TODOs and unused parameters

**Files:**
- Modify: `train_physics.py`
- Modify: `train.py`
- Modify: `route_executor.py`
- Modify: `path_utils.py`

**Step 1: Update TrainPhysics TODOs**

Remove TODOs that are already complete (tests + migration) or mark them as done.

**Step 2: is_opposite_polarity_safe**

If we keep the simplified behavior, remove the TODO and add a note in the docstring that it only validates single‑unit consists.

**Step 3: RouteExecutor.handle_path_completion**

Rename parameter to `_event_time` if unused, or store it in `RouteExecutionState` for later metrics.

**Step 4: _positions_to_path_tiles**

Remove the unused `network` parameter and update call sites/tests.

**Step 5: Run tests**

```bash
source ~/.nvm/nvm.sh && nvm use 22
conda run -n general pytest tests/test_javascript_syntax.py -v
conda run -n general pytest tests/test_app_smoke.py -v
conda run -n general pytest tests/test_backend.py -v
```

**Step 6: Commit**

```bash
git add train_physics.py train.py route_executor.py path_utils.py tests
git commit -m "chore: remove stale todo items and unused params"
```

---

## Task 5: Update docs for accurate module references

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Modify: `Reference_Train_System_v2.md`

**Step 1: Replace obsolete file references**

Update module lists to remove `train_movement.py` and `train_atc.py`, replacing with `train_physics.py` and `train_controller.py`.

**Step 2: Document current safety limitation**

Add a brief note describing the simplified opposite‑polarity safety check, if we keep it.

**Step 3: Run tests (doc-only changes can skip)**

If only docs changed, tests can be skipped. If code changed too, run all three suites.

**Step 4: Commit**

```bash
git add README.md AGENTS.md Reference_Train_System_v2.md
git commit -m "docs: align module list with current architecture"
```

---

## Task 6: Smoke-check server startup behavior

**Files:**
- None (runtime verification)

**Step 1: Verify default demo boot still works**

```bash
python3 server.py
```

**Step 2: Verify new flags**

```bash
python3 server.py --no-demo --no-sim --no-autosave --skip-startup-tests
```

**Step 3: Document any behavior differences**

If any CLI change affects existing usage, add a short note to `README.md`.

---

## Notes / Decisions

- Default startup remains demo‑boot unless `--no-demo` is passed (per user choice).
- No new tests are required unless a functional behavior changes; we’ll rely on existing suites.
- Keep refactor steps small and independently testable.

---

## Required Commands Summary

```bash
source ~/.nvm/nvm.sh && nvm use 22
conda run -n general pytest tests/test_javascript_syntax.py -v
conda run -n general pytest tests/test_app_smoke.py -v
conda run -n general pytest tests/test_backend.py -v
```
