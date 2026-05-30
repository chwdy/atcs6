"""
Phase 7 — debug-flag caching tests.

`Renderer.debugFlags` is refreshed once per rAF flush, so hot paths
(_drawWorld, mouse hover handlers) read a cached object instead of
querying the DOM per draw call. This test asserts:

  1. renderer.js declares `debugFlags` on the Renderer instance.
  2. _drawWorld no longer reads `debug-show-blocks` / `debug-show-signals`
     directly via document.getElementById — those lookups moved to
     _refreshDebugFlags.
  3. _refreshDebugFlags exists and is called from _flushRedraw.
"""

import re
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent
RENDERER_JS = PROJECT_ROOT / "static" / "js" / "renderer.js"


def test_renderer_declares_debug_flags():
    src = RENDERER_JS.read_text()
    assert "this.debugFlags" in src, (
        "renderer.js must initialize this.debugFlags on the instance"
    )
    assert re.search(r"_refreshDebugFlags\s*\(\s*\)", src), (
        "renderer.js must declare _refreshDebugFlags"
    )


def test_flush_refreshes_debug_flags():
    """_flushRedraw must call _refreshDebugFlags before painting either
    layer, so debug-checkbox changes propagate by the next frame."""
    src = RENDERER_JS.read_text()
    # Extract just the body of _flushRedraw() by brace-counting from the
    # opening `{`. Stop at the matching close. Locate the method DEFINITION
    # (signature immediately followed by `{`), not a call-site like
    # `this._flushRedraw();` — resize() invokes it directly, so the first
    # textual `_flushRedraw()` in the file is a call, not the definition.
    sig = re.search(r"_flushRedraw\s*\(\s*\)\s*\{", src)
    assert sig is not None, "_flushRedraw not found"
    open_brace = src.find("{", sig.start())
    depth = 0
    i = open_brace
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = src[open_brace : i + 1]
    # Match the actual CALL patterns, not mentions in comments.
    refresh_idx = body.find("this._refreshDebugFlags(")
    draw_world_idx = body.find("this._drawWorld(")
    draw_live_idx = body.find("this._drawLive(")
    assert refresh_idx >= 0, "_flushRedraw must call this._refreshDebugFlags()"
    assert draw_world_idx >= 0, "_flushRedraw body must invoke this._drawWorld()"
    assert draw_live_idx >= 0, "_flushRedraw body must invoke this._drawLive()"
    assert draw_world_idx > refresh_idx, (
        "_refreshDebugFlags must run BEFORE _drawWorld"
    )
    assert draw_live_idx > refresh_idx, (
        "_refreshDebugFlags must run BEFORE _drawLive"
    )


def test_draw_world_no_direct_dom_reads():
    """_drawWorld must not read debug checkboxes via document.getElementById.
    Hot-path DOM lookups should go through debugFlags."""
    src = RENDERER_JS.read_text()
    dw_start = src.find("_drawWorld(network)")
    assert dw_start >= 0, "_drawWorld(network) not found"
    # Roughly the body of _drawWorld — first ~3500 chars after the signature.
    body = src[dw_start : dw_start + 3500]
    bad = re.findall(r"getElementById\(['\"]debug-show-", body)
    assert not bad, (
        f"_drawWorld must use this.debugFlags, not direct DOM reads. "
        f"Found {len(bad)} offending lookups."
    )


def test_mouse_handlers_no_direct_debug_reads():
    """Constructor mouse handlers also use debugFlags rather than reading
    DOM on every mousemove (Phase 7)."""
    src = RENDERER_JS.read_text()
    ctor_start = src.find("constructor(canvasId)")
    # Constructor body — roughly the first 5000 chars.
    body = src[ctor_start : ctor_start + 5000]
    # _drawWorld might still appear later in the file but not in this slice.
    bad = re.findall(r"getElementById\(['\"]debug-show-", body)
    assert not bad, (
        "Mouse handlers in constructor should read this.debugFlags, not DOM."
    )
