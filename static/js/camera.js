// camera.js — Viewport + isometric projection
// ============================================================================
// Owns all viewport state (pan, zoom, canvas center) and the world↔screen
// projection. Pulled out of Renderer in Phase 4 of the canvas refactor so:
//   - viewport math is testable in isolation (no DOM dependencies)
//   - two canvases (Phase 5) can share one camera instance
//   - editor / app mutate viewport via Camera methods, not direct field writes
//
// Single source of truth for: panX, panY, scale, centerX, centerY, gridSize.
// External callers reach these via `renderer.camera.<field>` (read) and via
// Camera methods (write). Direct `renderer.<field>` access is gone.
// ============================================================================

class Camera {
    constructor(canvas, { gridSize = 40 } = {}) {
        this.canvas = canvas;
        this.gridSize = gridSize;

        // Pan offset (user dragging / fit / follow).
        this.panX = 0;
        this.panY = 0;

        // Zoom level (wheel zoom + buttons). 1.0 = native tile size.
        this.scale = 1.0;

        // Center of canvas in screen pixels (where world coord 0,0 appears
        // when pan = 0). Updated by resizeToCanvas() and on canvas mutate.
        this.centerX = 0;
        this.centerY = 0;

        // viewportChanged subscribers — wired up by Renderer (Phase 5 wants
        // this to drive layer-aware redraw flags). Camera itself stays
        // ignorant of who paints what.
        this._subscribers = [];
    }

    onChange(cb) {
        this._subscribers.push(cb);
        return () => {
            const idx = this._subscribers.indexOf(cb);
            if (idx >= 0) this._subscribers.splice(idx, 1);
        };
    }

    _emit() {
        for (const cb of this._subscribers) {
            try { cb(); } catch (e) { console.error('[Camera] subscriber error:', e); }
        }
    }

    // ---- Projection ---------------------------------------------------------
    //
    // Updated convention: X+ goes down-right, Y+ goes up-right.
    // Corner-based: tile (x,y) has corners at (x,y), (x+1,y), (x+1,y+1), (x,y+1).
    // Tile center is at (x+0.5, y+0.5), so we add 1 to (gridX + gridY).
    //
    // The math here MUST mirror tests/test_projection_roundtrip.py.

    tileSize() {
        return this.gridSize * this.scale;
    }

    gridToScreenX(gridX, gridY = 0) {
        const tileSize = this.gridSize * this.scale;
        return this.centerX + this.panX + ((gridX + gridY + 1) * tileSize / 2);
    }

    gridToScreenY(gridX, gridY = 0) {
        const tileSize = this.gridSize * this.scale;
        return this.centerY + this.panY + ((gridX - gridY) * tileSize / 2);
    }

    screenToGridX(screenX, screenY) {
        const sx = screenX - this.centerX - this.panX;
        const sy = screenY - this.centerY - this.panY;
        const tileSize = this.gridSize * this.scale;
        return Math.floor((sx + sy) / tileSize);
    }

    screenToGridY(screenX, screenY) {
        const sx = screenX - this.centerX - this.panX;
        const sy = screenY - this.centerY - this.panY;
        const tileSize = this.gridSize * this.scale;
        return Math.floor((sx - sy) / tileSize);
    }

    screenToGrid(screenX, screenY) {
        return {
            x: this.screenToGridX(screenX, screenY),
            y: this.screenToGridY(screenX, screenY),
        };
    }

    // ---- Mutators -----------------------------------------------------------

    /**
     * Update centerX / centerY to the new canvas midpoint. panX / panY are
     * intentionally NOT adjusted: because screenToGrid subtracts centerX,
     * the world point at the visual center stays put under any centerX
     * change. This is the contract Phase 1 introduced — preserving the
     * visual center across sidebar opens.
     */
    resizeToCanvas() {
        const width = this.canvas?.width ?? 0;
        const height = this.canvas?.height ?? 0;
        this.centerX = Math.floor(width / 2);
        this.centerY = Math.floor(height / 2);
        this._emit();
    }

    panBy(dx, dy) {
        if (dx === 0 && dy === 0) return;
        this.panX += dx;
        this.panY += dy;
        this._emit();
    }

    /**
     * Set absolute scale, then adjust pan so the world point under
     * (anchorScreenX, anchorScreenY) stays under the same screen position.
     * This is the math used by the wheel handler and the +/- buttons —
     * keeping the cursor's tile anchored across zoom is the "feels right"
     * invariant tested in tests/test_projection_roundtrip.py.
     */
    zoomAt(anchorScreenX, anchorScreenY, newScale) {
        const clamped = Math.max(0.1, Math.min(5, newScale));
        if (clamped === this.scale) return;
        const worldBefore = this.screenToGrid(anchorScreenX, anchorScreenY);
        this.scale = clamped;
        // Where the world point would now land at the new scale (no pan fix).
        const newTileSize = this.gridSize * clamped;
        const projX = this.centerX + this.panX +
            ((worldBefore.x + worldBefore.y + 1) * newTileSize / 2);
        const projY = this.centerY + this.panY +
            ((worldBefore.x - worldBefore.y) * newTileSize / 2);
        // Shift pan so it lands back at the anchor.
        this.panX += anchorScreenX - projX;
        this.panY += anchorScreenY - projY;
        this._emit();
    }

    /**
     * Centre the visual viewport on the bbox of the given tile list.
     * Sets panX/panY so the bbox center lands at (centerX, centerY).
     */
    centerOnContent(tracks) {
        if (!tracks || tracks.length === 0) return;
        let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
        for (const t of tracks) {
            if (t.x < minX) minX = t.x;
            if (t.x > maxX) maxX = t.x;
            if (t.y < minY) minY = t.y;
            if (t.y > maxY) maxY = t.y;
        }
        const cx = (minX + maxX + 1) / 2;
        const cy = (minY + maxY + 1) / 2;
        const tileSize = this.gridSize * this.scale;
        this.panX = -((cx + cy + 1) * tileSize / 2);
        this.panY = -((cx - cy) * tileSize / 2);
        this._emit();
    }

    /**
     * Pan such that the given world tile lands at the visual center.
     * Used by App._maybeFollowSelectedTrain when Follow Train is on.
     */
    panToWorld(gridX, gridY) {
        const tileSize = this.gridSize * this.scale;
        this.panX = -((gridX + gridY + 1) * tileSize / 2);
        this.panY = -((gridX - gridY) * tileSize / 2);
        this._emit();
    }
}

// Browser-global export to match the rest of static/js/ which doesn't use
// modules. Renderer reads this via `new Camera(...)` after camera.js loads.
if (typeof window !== 'undefined') {
    window.Camera = Camera;
}
