"""
Static/dynamic canvas split tests (Phase 5).

Phase 5 introduces two stacked canvases:
  - World canvas (rail-world-canvas): static map shape — tracks, blocks,
    platforms, station labels, signals, debug overlays.
  - Live canvas (rail-canvas): dynamic state — routes, trains, hover
    overlay, box/lasso selection.

The Renderer routes painting via `_drawWorld` and `_drawLive`. `_flushRedraw`
honors the `world` / `live` flags on `requestRedraw` so hover (live-only)
no longer triggers a world repaint.

These tests assert the source contract and the flag-routing semantics.
A pure Node test exercises a stub that mirrors the flag logic; a separate
source check confirms the real renderer.js declares the helpers and the
two canvas elements live in index.html.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent
JS_DIR = PROJECT_ROOT / "static" / "js"
HTML = PROJECT_ROOT / "static" / "index.html"
CSS = PROJECT_ROOT / "static" / "css" / "layout" / "canvas.css"


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


JS_SNIPPET = r"""
let rafCallbacks = [];
globalThis.requestAnimationFrame = (cb) => { rafCallbacks.push(cb); return rafCallbacks.length; };

// Mirror of renderer.js requestRedraw / _flushRedraw with per-layer counters.
class Stub {
    constructor() {
        this.currentNetwork = { tracks: [] };
        this._pendingFlush = null;
        this._flushFrame = null;
        this.worldDrawCount = 0;
        this.liveDrawCount = 0;
    }
    requestRedraw(opts = {}) {
        const { world = true, live = true, reason = '', network } = opts;
        if (network !== undefined) this.currentNetwork = network;
        if (this._pendingFlush === null) this._pendingFlush = { world: false, live: false, reason: '' };
        if (world) this._pendingFlush.world = true;
        if (live) this._pendingFlush.live = true;
        if (reason) this._pendingFlush.reason = reason;
        if (this._flushFrame !== null) return;
        this._flushFrame = requestAnimationFrame(() => this._flushRedraw());
    }
    _flushRedraw() {
        this._flushFrame = null;
        const pending = this._pendingFlush;
        this._pendingFlush = null;
        if (pending === null) return;
        if (!this.currentNetwork) return;
        if (pending.world) this.worldDrawCount++;
        if (pending.live) this.liveDrawCount++;
    }
}

function flushRaf() {
    const cbs = rafCallbacks; rafCallbacks = [];
    for (const cb of cbs) cb();
}

function assertEq(a, e, label) {
    if (JSON.stringify(a) !== JSON.stringify(e)) throw new Error(`${label}: expected ${JSON.stringify(e)}, got ${JSON.stringify(a)}`);
}

// 1. live-only paints live but not world.
{
    const r = new Stub();
    r.requestRedraw({ live: true, world: false });
    flushRaf();
    assertEq(r.worldDrawCount, 0, 'live-only: world untouched');
    assertEq(r.liveDrawCount, 1, 'live-only: live painted');
}

// 2. world-only paints world but not live.
{
    const r = new Stub();
    r.requestRedraw({ live: false, world: true });
    flushRaf();
    assertEq(r.worldDrawCount, 1, 'world-only: world painted');
    assertEq(r.liveDrawCount, 0, 'world-only: live untouched');
}

// 3. live-only + world-only in same tick → BOTH painted (OR-merge).
{
    const r = new Stub();
    r.requestRedraw({ live: true, world: false });
    r.requestRedraw({ live: false, world: true });
    flushRaf();
    assertEq(r.worldDrawCount, 1, 'OR-merge: world painted');
    assertEq(r.liveDrawCount, 1, 'OR-merge: live painted');
}

// 4. Multiple live-only hovers in one frame collapse to ONE live paint.
{
    const r = new Stub();
    for (let i = 0; i < 20; i++) r.requestRedraw({ live: true, world: false });
    flushRaf();
    assertEq(r.worldDrawCount, 0, 'hover storm: world never painted');
    assertEq(r.liveDrawCount, 1, 'hover storm: live painted exactly once');
}

console.log("OK");
"""


def test_layer_flag_routing():
    """Layer flags route paints to the right counter."""
    assert NODE, "Node.js not found"
    r = subprocess.run([NODE, "-e", JS_SNIPPET], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"Node failed\nstdout: {r.stdout}\nstderr: {r.stderr}"
    assert "OK" in r.stdout, f"Expected OK in stdout: {r.stdout}"


def test_renderer_declares_layer_methods():
    """renderer.js has _drawWorld and _drawLive."""
    src = (PROJECT_ROOT / "frontend" / "src" / "renderer" / "renderer.ts").read_text()
    assert re.search(r"\b_drawWorld\s*\(", src), "renderer.ts must declare _drawWorld"
    assert re.search(r"\b_drawLive\s*\(", src), "renderer.js must declare _drawLive"
    assert "worldCtx" in src, "renderer.js must have worldCtx"
    assert "liveCtx" in src, "renderer.js must have liveCtx"


def test_two_canvases_in_html():
    """index.html declares both rail-world-canvas and rail-canvas."""
    html = HTML.read_text()
    assert 'id="rail-world-canvas"' in html, "index.html must declare rail-world-canvas"
    assert 'id="rail-canvas"' in html, "index.html must declare rail-canvas (live)"


def test_css_layers_canvases():
    """canvas.css positions both canvases at the same place with the live
    canvas on top (z-index)."""
    css = CSS.read_text()
    assert "#rail-world-canvas" in css, "canvas.css must style rail-world-canvas"
    assert "z-index" in css, "canvas.css must declare z-index for layering"
    assert "pointer-events: none" in css, (
        "world canvas must have pointer-events: none so clicks reach the live canvas above"
    )
