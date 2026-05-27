"""
Renderer redraw-coalescing tests.

Phase 3 of the canvas refactor introduces a rAF-coalesced redraw scheduler:
many synchronous calls to `Renderer.requestRedraw(...)` in one tick must
collapse to a single paint on the next animation frame.

This test inlines a small Renderer-like stub in Node whose
`requestRedraw` / `_flushRedraw` mirror the implementation in
`static/js/renderer.js`. We use a deterministic `requestAnimationFrame`
stub so we can flush on demand and count actual paints.

It also asserts the source contract: the implementation in renderer.js
exposes `requestRedraw` and `_flushRedraw`, and external JS files no
longer call `renderer.drawNetwork(...)` directly (those calls must route
through the coalescer).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent
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


JS_SNIPPET = r"""
let rafCallbacks = [];
globalThis.requestAnimationFrame = (cb) => {
    rafCallbacks.push(cb);
    return rafCallbacks.length;
};

class Stub {
    constructor() {
        this.currentNetwork = { tracks: [] };
        this._pendingFlush = null;
        this._flushFrame = null;
        this._lastFlushReason = '';
        this.drawCount = 0;
        this.lastFlushFlags = null;
    }

    // Mirror of renderer.js requestRedraw / _flushRedraw.
    requestRedraw(opts = {}) {
        const { world = true, live = true, reason = '', network } = opts;
        if (network !== undefined) this.currentNetwork = network;
        if (this._pendingFlush === null) {
            this._pendingFlush = { world: false, live: false, reason: '' };
        }
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
        this.lastFlushFlags = { world: pending.world, live: pending.live };
        this._lastFlushReason = pending.reason || '';
        this.drawCount++;
    }
}

function flushRaf() {
    const cbs = rafCallbacks;
    rafCallbacks = [];
    for (const cb of cbs) cb();
}

function assertEq(actual, expected, label) {
    if (JSON.stringify(actual) !== JSON.stringify(expected)) {
        throw new Error(`${label}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
    }
}

// 1. Ten synchronous requestRedraw() calls collapse to one paint.
const r1 = new Stub();
for (let i = 0; i < 10; i++) r1.requestRedraw({ reason: `call-${i}` });
assertEq(r1.drawCount, 0, 'no paint before rAF');
flushRaf();
assertEq(r1.drawCount, 1, 'one paint after rAF');
assertEq(r1.lastFlushFlags, { world: true, live: true }, 'both flags set');

// 2. world-only + live-only OR together.
const r2 = new Stub();
r2.requestRedraw({ world: true, live: false });
r2.requestRedraw({ world: false, live: true });
flushRaf();
assertEq(r2.drawCount, 1, 'still one paint after mixed flags');
assertEq(r2.lastFlushFlags, { world: true, live: true }, 'flags OR-merged');

// 3. live-only stays live-only when no other call upgrades it.
const r3 = new Stub();
r3.requestRedraw({ world: false, live: true });
flushRaf();
assertEq(r3.lastFlushFlags, { world: false, live: true }, 'live-only preserved');

// 4. After a flush, the next requestRedraw schedules a new rAF.
const r4 = new Stub();
r4.requestRedraw();
flushRaf();
r4.requestRedraw();
flushRaf();
assertEq(r4.drawCount, 2, 'two distinct frames each produce one paint');

// 5. network option updates currentNetwork before flush.
const r5 = new Stub();
const fresh = { tracks: [{ x: 7, y: 7 }] };
r5.requestRedraw({ network: fresh });
assertEq(r5.currentNetwork, fresh, 'network applied synchronously');
flushRaf();
assertEq(r5.drawCount, 1, 'paint happens once after rAF');

console.log("OK");
"""


def test_redraw_coalescing_semantics():
    """Run the inlined Renderer-like stub through Node and verify the
    coalescer behaves as documented in `static/js/renderer.js`.
    """
    assert NODE, "Node.js not found"
    result = subprocess.run(
        [NODE, "-e", JS_SNIPPET], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, (
        f"Node returned {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout, f"Expected OK in stdout, got: {result.stdout}"


def test_renderer_exposes_request_redraw():
    """Source-level guarantee: renderer.js declares requestRedraw and
    _flushRedraw. If these get renamed/removed, every caller breaks
    silently — catch that here."""
    src = (JS_DIR / "renderer.js").read_text()
    assert re.search(r"\brequestRedraw\s*\(", src), (
        "renderer.js must define requestRedraw — Phase 3 contract."
    )
    assert re.search(r"\b_flushRedraw\s*\(", src), (
        "renderer.js must define _flushRedraw — Phase 3 contract."
    )


def test_no_external_drawNetwork_calls():
    """Source-level guarantee: outside renderer.js's own _flushRedraw,
    no JS file calls renderer.drawNetwork(...) or renderer.draw() directly.
    All paints route through requestRedraw.
    """
    offenders = []
    for js_file in JS_DIR.glob("*.js"):
        if js_file.name == "renderer.js":
            continue  # internal calls in _flushRedraw are allowed
        src = js_file.read_text()
        # Match renderer.drawNetwork(...) or renderer.draw() — both must be gone.
        for m in re.finditer(
            r"\brenderer\.(drawNetwork\s*\(|draw\s*\(\s*\))", src
        ):
            line_num = src[: m.start()].count("\n") + 1
            offenders.append(f"{js_file.name}:{line_num} — {m.group(0)}")
    assert not offenders, (
        "Found external drawNetwork/draw() calls (Phase 3: all paints must "
        "route through renderer.requestRedraw):\n  "
        + "\n  ".join(offenders)
    )
