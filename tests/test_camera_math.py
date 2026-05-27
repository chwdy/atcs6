"""
Camera math tests.

Camera (static/js/camera.js) owns viewport state (panX, panY, scale,
centerX, centerY, gridSize) and the world↔screen projection used by
Renderer + handler files + Editor + App.

These tests exercise Camera through Node by loading the real camera.js
file and instantiating against a canvas stub. They guard the math
invariants every renderer relies on:

  1. screenToGrid(gridToScreen(x, y)) == (x, y) at any scale.
  2. Camera.panBy(dx, dy) shifts screen→world consistently.
  3. Camera.zoomAt anchors the world point under (sx, sy).
  4. Camera.centerOnContent places the bbox center at the visual center.
  5. Camera.resizeToCanvas updates centerX/Y but leaves panX/Y alone
     (Phase 1 visual-center invariant).
  6. onChange subscribers fire on mutator calls.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent
CAMERA_JS = PROJECT_ROOT / "static" / "js" / "camera.js"


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
const cameraSrc = fs.readFileSync(process.argv[1], 'utf8');
// Provide a window-like global so camera.js can attach itself.
globalThis.window = globalThis;
// Evaluate the file in the current realm. camera.js attaches Camera to window.
eval(cameraSrc);

function makeCanvas(width = 1200, height = 800) {
    return { width, height };
}

function assertEq(actual, expected, label) {
    const a = JSON.stringify(actual);
    const e = JSON.stringify(expected);
    if (a !== e) throw new Error(`${label}: expected ${e}, got ${a}`);
}

function assertClose(actual, expected, label, eps = 1e-9) {
    if (Math.abs(actual - expected) > eps) {
        throw new Error(`${label}: expected ${expected} (±${eps}), got ${actual}`);
    }
}

// --- 1. Roundtrip at multiple scales (mirror of Phase 2 test but via real Camera).
{
    for (const scale of [0.1, 0.5, 1.0, 2.5, 5.0]) {
        const cam = new Camera(makeCanvas());
        cam.resizeToCanvas();
        cam.scale = scale;
        for (const [gx, gy] of [[0,0],[1,0],[-3,5],[12,12],[100,1]]) {
            const sx = cam.gridToScreenX(gx, gy);
            const sy = cam.gridToScreenY(gx, gy);
            assertEq(
                [cam.screenToGridX(sx, sy), cam.screenToGridY(sx, sy)],
                [gx, gy],
                `roundtrip scale=${scale} tile=(${gx},${gy})`
            );
        }
    }
}

// --- 2. panBy shifts screen->world predictably.
{
    const cam = new Camera(makeCanvas());
    cam.resizeToCanvas();
    const sx = 720, sy = 350;
    const before = cam.screenToGrid(sx, sy);
    cam.panBy(50, -30);
    const after = cam.screenToGrid(sx, sy);
    // After panBy(50,-30), the world point under (sx,sy) is the same world
    // point that was under (sx-50, sy+30) before pan.
    cam.panBy(-50, 30);  // reset
    const baseline = cam.screenToGrid(sx - 50, sy + 30);
    cam.panBy(50, -30);
    const repeat = cam.screenToGrid(sx, sy);
    assertEq(repeat, baseline, 'panBy: world under (sx,sy) after pan == world under (sx-dx, sy-dy) before pan');
    // Also confirm before != after when pan is non-trivial.
    if (JSON.stringify(before) === JSON.stringify(after)) {
        throw new Error('panBy: expected world to shift, but it did not');
    }
}

// --- 3. zoomAt anchors the world point under the cursor across scales.
{
    for (const targetScale of [0.5, 2.0, 4.0]) {
        const cam = new Camera(makeCanvas());
        cam.resizeToCanvas();
        cam.panX = 137;
        cam.panY = -82;
        const anchorSx = 720, anchorSy = 350;
        const worldBefore = cam.screenToGrid(anchorSx, anchorSy);
        cam.zoomAt(anchorSx, anchorSy, targetScale);
        const worldAfter = cam.screenToGrid(anchorSx, anchorSy);
        assertEq(worldAfter, worldBefore, `zoomAt anchor at scale=${targetScale}`);
        assertClose(cam.scale, targetScale, `scale set after zoomAt(target=${targetScale})`);
    }
}

// --- 4. centerOnContent places bbox center at visual center.
{
    const cam = new Camera(makeCanvas());
    cam.resizeToCanvas();
    const tracks = [
        { x: -5, y: -3 }, { x: 4, y: -3 },
        { x: -5, y: 7 }, { x: 4, y: 7 },
    ];
    cam.centerOnContent(tracks);
    // bbox is x∈[-5,4], y∈[-3,7] → continuous center (-0.5+0.5)/2... actually:
    // (minX+maxX+1)/2 = (-5+4+1)/2 = 0; (minY+maxY+1)/2 = (-3+7+1)/2 = 2.5
    // World point (0, 2.5) should land at visual center.
    const tileSize = cam.gridSize * cam.scale;
    const expectedSX = cam.centerX + cam.panX + ((0 + 2.5 + 1) * tileSize / 2);
    const expectedSY = cam.centerY + cam.panY + ((0 - 2.5) * tileSize / 2);
    assertClose(expectedSX, cam.centerX, 'centerOnContent: bbox X-center at visual center');
    assertClose(expectedSY, cam.centerY, 'centerOnContent: bbox Y-center at visual center');
}

// --- 5. resizeToCanvas updates centerX/Y but leaves panX/Y alone.
{
    const canvas = makeCanvas(1200, 800);
    const cam = new Camera(canvas);
    cam.resizeToCanvas();
    cam.panX = 123;
    cam.panY = 456;
    const oldPanX = cam.panX, oldPanY = cam.panY;
    // Simulate sidebar opening: canvas-container shrinks.
    canvas.width = 850;
    canvas.height = 800;
    cam.resizeToCanvas();
    assertEq(cam.centerX, 425, 'centerX updated to new midpoint');
    assertEq(cam.panX, oldPanX, 'panX NOT adjusted on resize');
    assertEq(cam.panY, oldPanY, 'panY NOT adjusted on resize');
    // World coord at visual center is preserved.
    const cam2 = new Camera(makeCanvas(1200, 800));
    cam2.resizeToCanvas();
    cam2.panX = 123; cam2.panY = 456;
    const worldBefore = cam2.screenToGrid(cam2.centerX, cam2.centerY);
    cam2.canvas.width = 850;
    cam2.resizeToCanvas();
    const worldAfter = cam2.screenToGrid(cam2.centerX, cam2.centerY);
    assertEq(worldAfter, worldBefore, 'world at visual center preserved on resize');
}

// --- 6. onChange subscribers fire on mutators.
{
    const cam = new Camera(makeCanvas());
    cam.resizeToCanvas();
    let fired = 0;
    const unsub = cam.onChange(() => fired++);
    cam.panBy(10, 0);
    cam.zoomAt(100, 100, 2.0);
    cam.centerOnContent([{x:0,y:0},{x:1,y:1}]);
    cam.panToWorld(5, 5);
    cam.resizeToCanvas();
    if (fired < 5) throw new Error(`expected ≥5 onChange fires, got ${fired}`);
    unsub();
    const oldFired = fired;
    cam.panBy(1, 1);
    if (fired !== oldFired) throw new Error('unsub did not stop notifications');
}

// --- 7. panToWorld lands the given tile at the visual center.
{
    const cam = new Camera(makeCanvas());
    cam.resizeToCanvas();
    cam.scale = 1.5;
    cam.panToWorld(7, -3);
    // gridToScreen of (7,-3) should equal centerX, centerY.
    assertClose(cam.gridToScreenX(7, -3), cam.centerX, 'panToWorld lands gx at visual center X');
    assertClose(cam.gridToScreenY(7, -3), cam.centerY, 'panToWorld lands gy at visual center Y');
}

console.log("OK");
"""


def test_camera_math_via_node():
    """Load the actual camera.js via Node and exercise every public method."""
    assert NODE, "Node.js not found"
    assert CAMERA_JS.exists(), f"camera.js missing at {CAMERA_JS}"
    result = subprocess.run(
        [NODE, "-e", HARNESS, str(CAMERA_JS)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"Node exited with {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout, f"Expected OK, got stdout: {result.stdout}"


def test_camera_file_declares_class():
    """Source-level: camera.js exposes a Camera class on window."""
    src = CAMERA_JS.read_text()
    assert "class Camera" in src, "camera.js must declare Camera class"
    assert "window.Camera = Camera" in src, "camera.js must export Camera onto window"


def test_renderer_uses_camera():
    """Source-level: renderer.js wires up a Camera instance."""
    renderer = (PROJECT_ROOT / "static" / "js" / "renderer.js").read_text()
    assert "new Camera(" in renderer, "renderer.js must instantiate Camera"
    assert "this.camera" in renderer, "renderer.js must expose this.camera"
