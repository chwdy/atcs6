"""
RenderSnapshot tests (Phase 6).

`render-snapshot.js`'s `buildRenderSnapshot(network, stateManager)` produces
a per-frame immutable view of sim state. Renderers consume it instead of
reaching into `window.stateManager.getController(...)` directly.

These tests load the real render-snapshot.js via Node, feed it a hand-built
network + StateManager stub, and check:
  1. Each train with a live controller span lands in the snapshot.
  2. Span fields are pre-named in camelCase (occupiedTiles, leadingEnd,
     endA, endB) so the renderer has one shape to consume.
  3. Trains without a controller (e.g., not yet active) are omitted.
  4. buildRenderSnapshot does NOT mutate its inputs.
  5. A null / missing stateManager yields an empty trains map.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent
SNAP_JS = PROJECT_ROOT / "static" / "js" / "render-snapshot.js"
JS_DIR = PROJECT_ROOT / "static" / "js"


def _node_path():
    nvm_sh = Path(os.path.expanduser("~/.nvm/nvm.sh"))
    if nvm_sh.exists():
        r = subprocess.run(
            ["bash", "-c", f"source {nvm_sh} && nvm which 22"],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            p = r.stdout.strip()
            if p and Path(p).exists():
                return p
    return shutil.which("node")


NODE = _node_path()


HARNESS = r"""
const fs = require('fs');
const snapSrc = fs.readFileSync(process.argv[1], 'utf8');
globalThis.window = globalThis;
eval(snapSrc);

function assertEq(a, e, label) {
    if (JSON.stringify(a) !== JSON.stringify(e)) {
        throw new Error(`${label}: expected ${JSON.stringify(e)}, got ${JSON.stringify(a)}`);
    }
}

// Build a minimal network + stateManager stub.
const network = {
    trains: [
        { id: 'T1' },
        { id: 'T2' },
        { id: 'T3' },  // no controller — should be omitted
    ],
};

const controllers = {
    T1: {
        cab: 'A',
        span: {
            occupied_tiles: [[0, 0], [1, 0]],
            leading_end: 'a',
            end_a: { tile: [0, 0], distance_cm: 10, entry_edge: 'W' },
            end_b: { tile: [1, 0], distance_cm: 90, entry_edge: 'E' },
        },
    },
    T2: {
        cab: null,
        span: {
            occupied_tiles: [[5, 5]],
            leading_end: 'b',
            end_a: { tile: [5, 5], distance_cm: 0, entry_edge: 'N' },
            end_b: { tile: [5, 5], distance_cm: 50, entry_edge: 'S' },
        },
    },
    // T3 absent
};

const stateManager = {
    getController(id) { return controllers[id]; },
};

const snap = buildRenderSnapshot(network, stateManager);

// 1. T1 + T2 present, T3 omitted.
assertEq(Object.keys(snap.trains).sort(), ['T1', 'T2'], 'expected T1/T2 only');

// 2. T1 snapshot has camelCase span fields.
const t1 = snap.trains['T1'];
assertEq(t1.id, 'T1', 'T1 id');
assertEq(t1.cab, 'A', 'T1 cab');
assertEq(t1.span.occupiedTiles, [[0, 0], [1, 0]], 'T1 occupiedTiles');
assertEq(t1.span.leadingEnd, 'a', 'T1 leadingEnd');
assertEq(t1.span.endA, { tile: [0, 0], distance_cm: 10, entry_edge: 'W' }, 'T1 endA');
assertEq(t1.span.endB, { tile: [1, 0], distance_cm: 90, entry_edge: 'E' }, 'T1 endB');

// 3. T2 has cab: null (no cab selected).
assertEq(snap.trains['T2'].cab, null, 'T2 cab is null');

// 4. Inputs not mutated.
assertEq(network.trains.length, 3, 'network.trains length unchanged');
assertEq('occupied_tiles' in controllers.T1.span, true, 'controller span still has snake_case');
assertEq(controllers.T1.cab, 'A', 'controller cab unchanged');

// 5. Missing stateManager → empty trains.
const empty = buildRenderSnapshot(network, null);
assertEq(empty.trains, {}, 'null stateManager → empty trains');

// 6. Train with no controller-span → omitted.
const noSpanCtrl = { getController() { return { cab: 'A', span: null }; } };
const empty2 = buildRenderSnapshot(network, noSpanCtrl);
assertEq(empty2.trains, {}, 'no span → omitted');

// 7. Train with empty occupied_tiles → omitted.
const emptyTilesCtrl = {
    getController(id) {
        if (id === 'T1') return { cab: 'A', span: { occupied_tiles: [] } };
        return null;
    },
};
const empty3 = buildRenderSnapshot(network, emptyTilesCtrl);
assertEq(empty3.trains, {}, 'empty occupied_tiles → omitted');

console.log("OK");
"""


def test_snapshot_via_node():
    assert NODE, "Node.js not found"
    assert SNAP_JS.exists(), f"render-snapshot.js missing at {SNAP_JS}"
    r = subprocess.run(
        [NODE, "-e", HARNESS, str(SNAP_JS)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert r.returncode == 0, (
        f"Node exited {r.returncode}\nstdout: {r.stdout}\nstderr: {r.stderr}"
    )
    assert "OK" in r.stdout, f"Expected OK, got stdout: {r.stdout}"


def test_render_snapshot_module_exists():
    src = SNAP_JS.read_text()
    assert "function buildRenderSnapshot" in src
    assert "window.buildRenderSnapshot = buildRenderSnapshot" in src


def test_renderer_trains_no_direct_state_manager_reach():
    """renderer-trains.js's main drawTrain entry must consume the snapshot
    first. The fallback to stateManager is allowed (for the public
    drawNetwork() entry called outside the rAF flush), but the snapshot
    path must be tried first.
    """
    src = (JS_DIR / "renderer-trains.js").read_text()
    assert "currentSnapshot" in src, (
        "renderer-trains.js must read from renderer.currentSnapshot — Phase 6 contract"
    )
    # Check the order: drawTrain reads snapshot before falling back.
    draw_idx = src.find("drawTrain(train)")
    snap_idx = src.find("currentSnapshot", draw_idx)
    state_mgr_idx = src.find("window.stateManager", draw_idx)
    assert draw_idx >= 0, "drawTrain method not found"
    assert snap_idx >= 0, "drawTrain must reference currentSnapshot"
    assert state_mgr_idx > snap_idx, (
        "drawTrain must check snapshot BEFORE falling back to stateManager"
    )
