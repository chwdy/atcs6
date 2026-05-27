"""
Renderer resize math tests.

These cover the invariant introduced in Phase 1 of the canvas refactor:
when the canvas pixel buffer is resized (e.g., when the secondary sidebar
opens and shrinks .canvas-container.clientWidth), the world coordinate
that was at the visual center of the old canvas should remain at the
visual center of the new canvas.

The fix lives in `static/js/renderer.js` — `Renderer.resize()` no longer
adjusts `panX` / `panY` on canvas resize. Because `screenToGridX/Y`
subtracts `centerX` / `centerY` from the input screen coordinate, the
world coord at the visual center depends only on `(panX, panY, scale,
gridSize)` — not on `(centerX, centerY)`. Updating centerX/Y alone
preserves the visual-center invariant for free.

Two tests:

1. Pure-Python re-implementation of the projection math validates the
   invariant across several pan/scale combinations.
2. A source check on `renderer.js` confirms the buggy
   `panX += (oldCenterX - centerX)` adjustment is gone.
"""

import math
import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent
RENDERER_JS = PROJECT_ROOT / "static" / "js" / "renderer.js"


def _screen_to_grid(sx, sy, *, pan_x, pan_y, center_x, center_y, grid_size, scale):
    """Mirror of renderer.js:311-328 (screenToGridX/Y).

    Must stay in sync with the JS implementation. If the JS math changes,
    this transcription must be updated and the tests rerun.
    """
    tile_size = grid_size * scale
    local_x = sx - center_x - pan_x
    local_y = sy - center_y - pan_y
    gx = math.floor((local_x + local_y) / tile_size)
    gy = math.floor((local_x - local_y) / tile_size)
    return (gx, gy)


@pytest.mark.parametrize(
    "pan_x,pan_y,scale,old_size,new_size",
    [
        # Identity: no pan, sidebar opens (1200 -> 850 wide).
        (0, 0, 1.0, (1200, 800), (850, 800)),
        # User panned right and down, sidebar opens.
        (175, -90, 1.0, (1200, 800), (850, 800)),
        # User zoomed in, sidebar opens.
        (40, 40, 2.5, (1400, 900), (1050, 900)),
        # User zoomed out, sidebar closes (850 -> 1200 wide).
        (-100, 200, 0.5, (850, 800), (1200, 800)),
        # No-op size change (ResizeObserver spurious fire).
        (50, 50, 1.0, (1200, 800), (1200, 800)),
        # Asymmetric resize (height also changes).
        (123, 456, 1.5, (1600, 1000), (1100, 700)),
    ],
)
def test_visual_center_preserved_under_resize(
    pan_x, pan_y, scale, old_size, new_size
):
    """The world coord at the visual center before resize must equal the
    world coord at the visual center after resize, given that panX/panY/scale
    do not change across the resize. This is the contract Renderer.resize()
    must honor (and now does, per Phase 1)."""
    grid_size = 40

    old_w, old_h = old_size
    new_w, new_h = new_size

    old_center_x = old_w // 2
    old_center_y = old_h // 2
    new_center_x = new_w // 2
    new_center_y = new_h // 2

    before = _screen_to_grid(
        old_center_x,
        old_center_y,
        pan_x=pan_x,
        pan_y=pan_y,
        center_x=old_center_x,
        center_y=old_center_y,
        grid_size=grid_size,
        scale=scale,
    )
    after = _screen_to_grid(
        new_center_x,
        new_center_y,
        pan_x=pan_x,  # unchanged across resize (the Phase 1 contract)
        pan_y=pan_y,
        center_x=new_center_x,
        center_y=new_center_y,
        grid_size=grid_size,
        scale=scale,
    )

    assert before == after, (
        f"Visual center drifted on resize: pre={before}, post={after}. "
        f"Phase 1 invariant: panX/panY must not be adjusted on resize."
    )


def test_resize_does_not_adjust_pan():
    """Source-level check: the buggy `panX += (oldCenterX - centerX)` block
    is gone from Renderer.resize(). If it returns, the visual-center
    invariant breaks and the sidebar-open shift comes back."""
    src = RENDERER_JS.read_text()
    # Locate the resize() method body. Cheap regex: find `resize() {` and
    # everything up to the next top-level `}` that closes the method. To
    # avoid brace-matching complexity, just look at the next ~2000 chars.
    match = re.search(r"\bresize\s*\(\s*\)\s*\{", src)
    assert match, "Could not find resize() method in renderer.js"
    body = src[match.end() : match.end() + 2000]

    # The old buggy block adjusted panX/panY by a centerX delta.
    buggy_patterns = [
        r"this\.panX\s*\+=\s*\(?\s*oldCenterX",
        r"this\.panY\s*\+=\s*\(?\s*oldCenterY",
        r"oldCenterX\s*-\s*this\.centerX",
        r"oldCenterY\s*-\s*this\.centerY",
    ]
    for pat in buggy_patterns:
        assert not re.search(pat, body), (
            f"renderer.js resize() still contains the Phase-1-removed "
            f"pan-adjustment pattern: /{pat}/. "
            f"This breaks the visual-center invariant when the sidebar opens."
        )


def test_resize_observer_installed():
    """Source-level check: ResizeObserver is wired up on the canvas's
    parent so sidebar-driven dimension changes actually trigger resize().
    Without this, the canvas pixel buffer never reflows and the right
    portion gets clipped by overflow:hidden."""
    src = RENDERER_JS.read_text()
    assert "ResizeObserver" in src, (
        "renderer.js must install a ResizeObserver — window.resize alone "
        "does not fire when the sidebar opens via CSS class toggle."
    )
    assert "this.canvas.parentElement" in src, (
        "ResizeObserver must observe the canvas's parent (.canvas-container) "
        "since that's the element whose width changes when the sidebar opens."
    )
