"""
Projection round-trip tests.

These guard the invariant `screenToGrid(gridToScreen(gx, gy)) == (gx, gy)`
across the supported zoom range. If this breaks, click hit-testing
and any other screen->world conversion stops landing on the right tile
when the user is zoomed in or out.

The math under test lives in `static/js/renderer.js`:
- `gridToScreenX/Y` at lines 298-307
- `screenToGridX/Y` at lines 311-328

The transcription below must stay in sync with that JS code.

Phase 2 of the canvas refactor introduced `Editor._zoomAt`, which exercises
both directions of the projection (it calls `screenToGrid` for the world
point under the cursor, then re-projects via the same formula). Without
these tests, a future refactor of Camera math could silently shift
hit-testing off by one tile at non-unity scale.
"""

import math

import pytest


def _grid_to_screen(gx, gy, *, pan_x, pan_y, center_x, center_y, grid_size, scale):
    """Mirror of renderer.js:298-307 (gridToScreenX/Y). Returns the screen
    position of the CENTER of tile (gx, gy)."""
    tile_size = grid_size * scale
    sx = center_x + pan_x + ((gx + gy + 1) * tile_size / 2)
    sy = center_y + pan_y + ((gx - gy) * tile_size / 2)
    return (sx, sy)


def _screen_to_grid(sx, sy, *, pan_x, pan_y, center_x, center_y, grid_size, scale):
    """Mirror of renderer.js:311-328 (screenToGridX/Y)."""
    tile_size = grid_size * scale
    local_x = sx - center_x - pan_x
    local_y = sy - center_y - pan_y
    gx = math.floor((local_x + local_y) / tile_size)
    gy = math.floor((local_x - local_y) / tile_size)
    return (gx, gy)


SCALES = [0.1, 0.5, 1.0, 2.5, 5.0]
SAMPLE_TILES = [
    (0, 0),
    (1, 0),
    (0, 1),
    (-3, 5),
    (7, -2),
    (12, 12),
    (-15, -8),
    (100, 1),
    (1, 100),
]


@pytest.mark.parametrize("scale", SCALES)
@pytest.mark.parametrize("gx,gy", SAMPLE_TILES)
def test_grid_to_screen_to_grid_roundtrip(scale, gx, gy):
    """For every sample tile at every supported zoom, projecting to screen
    and back must return the same tile coordinate."""
    # Default viewport: no pan, canvas 1200x800 centered.
    params = dict(
        pan_x=0,
        pan_y=0,
        center_x=600,
        center_y=400,
        grid_size=40,
        scale=scale,
    )
    sx, sy = _grid_to_screen(gx, gy, **params)
    rx, ry = _screen_to_grid(sx, sy, **params)
    # The screen point returned by gridToScreen is the diamond center, which
    # screenToGrid floors back to the owning tile. Identity must hold.
    assert (rx, ry) == (gx, gy), (
        f"Roundtrip failed at scale={scale} tile=({gx},{gy}): "
        f"screen=({sx},{sy}), got=({rx},{ry})"
    )


@pytest.mark.parametrize("scale", SCALES)
def test_roundtrip_with_pan(scale):
    """Same invariant under non-zero pan."""
    params = dict(
        pan_x=137,
        pan_y=-249,
        center_x=600,
        center_y=400,
        grid_size=40,
        scale=scale,
    )
    for gx, gy in SAMPLE_TILES:
        sx, sy = _grid_to_screen(gx, gy, **params)
        rx, ry = _screen_to_grid(sx, sy, **params)
        assert (rx, ry) == (gx, gy), (
            f"Pan+scale roundtrip failed at scale={scale} tile=({gx},{gy})"
        )


@pytest.mark.parametrize("scale", SCALES)
def test_zoom_anchor_invariant(scale):
    """The world coord under a given screen position before zoom must
    equal the world coord under the same screen position after Editor._zoomAt
    has run. This is what makes "zoom around cursor" feel right.

    This re-implements the _zoomAt body in Python so the math is unit-
    tested independent of the browser.
    """
    # Pre-zoom viewport.
    grid_size = 40
    initial = dict(
        pan_x=50,
        pan_y=30,
        center_x=600,
        center_y=400,
        grid_size=grid_size,
        scale=1.0,
    )
    # Screen position the user's cursor is hovering.
    anchor_sx, anchor_sy = 720, 350

    world_before = _screen_to_grid(anchor_sx, anchor_sy, **initial)

    # Apply the _zoomAt formula in Python (editor.js _zoomAt body).
    target_scale = scale
    new_tile_size = grid_size * target_scale
    screen_x_after_scale = initial["center_x"] + initial["pan_x"] + \
        ((world_before[0] + world_before[1] + 1) * new_tile_size / 2)
    screen_y_after_scale = initial["center_y"] + initial["pan_y"] + \
        ((world_before[0] - world_before[1]) * new_tile_size / 2)
    new_pan_x = initial["pan_x"] + (anchor_sx - screen_x_after_scale)
    new_pan_y = initial["pan_y"] + (anchor_sy - screen_y_after_scale)

    final = dict(initial)
    final["scale"] = target_scale
    final["pan_x"] = new_pan_x
    final["pan_y"] = new_pan_y

    world_after = _screen_to_grid(anchor_sx, anchor_sy, **final)
    assert world_after == world_before, (
        f"Zoom anchor drifted at scale={scale}: "
        f"pre={world_before}, post={world_after}"
    )
