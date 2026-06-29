// camera.ts — Viewport + isometric projection.
//
// Migrated from static/js/camera.js (longterm_todo.md item #8, Phase 2). The
// math is unchanged and MUST stay in sync with tests/test_projection_roundtrip.py;
// it is covered natively by camera.test.ts (the Vitest port of the old
// Node-eval test_camera_math.py).
//
// Owns viewport state (panX, panY, scale, centerX, centerY, gridSize) and the
// world<->screen projection. Part of the Vite bundle; the Renderer imports it
// and constructs `new Camera(...)` directly.

/**
 * Minimal canvas shape the camera reads. HTMLCanvasElement satisfies it, and
 * unit tests can pass a plain `{ width, height }` stub.
 */
export interface CanvasLike {
  width: number;
  height: number;
}

export interface CameraOptions {
  gridSize?: number;
}

export interface GridPoint {
  x: number;
  y: number;
}

/** The four grid corners of a tile (x,y): used to project its full footprint. */
const TILE_CORNERS: ReadonlyArray<readonly [number, number]> = [
  [0, 0],
  [1, 0],
  [1, 1],
  [0, 1],
];

export class Camera {
  canvas: CanvasLike | null;
  gridSize: number;
  panX: number;
  panY: number;
  scale: number;
  centerX: number;
  centerY: number;
  private _subscribers: Array<() => void>;

  constructor(canvas: CanvasLike | null, { gridSize = 40 }: CameraOptions = {}) {
    this.canvas = canvas;
    this.gridSize = gridSize;

    // Pan offset (user dragging / fit / follow).
    this.panX = 0;
    this.panY = 0;

    // Zoom level (wheel zoom + buttons). 1.0 = native tile size.
    this.scale = 1.0;

    // Center of canvas in screen pixels (where world coord 0,0 appears when
    // pan = 0). Updated by resizeToCanvas().
    this.centerX = 0;
    this.centerY = 0;

    this._subscribers = [];
  }

  onChange(cb: () => void): () => void {
    this._subscribers.push(cb);
    return () => {
      const idx = this._subscribers.indexOf(cb);
      if (idx >= 0) this._subscribers.splice(idx, 1);
    };
  }

  private _emit(): void {
    for (const cb of this._subscribers) {
      try {
        cb();
      } catch (e) {
        console.error('[Camera] subscriber error:', e);
      }
    }
  }

  // ---- Projection ----------------------------------------------------------
  //
  // X+ goes down-right, Y+ goes up-right. Corner-based: tile (x,y) has corners
  // at (x,y), (x+1,y), (x+1,y+1), (x,y+1); tile center is (x+0.5, y+0.5), so we
  // add 1 to (gridX + gridY). The math here MUST mirror
  // tests/test_projection_roundtrip.py.

  tileSize(): number {
    return this.gridSize * this.scale;
  }

  gridToScreenX(gridX: number, gridY = 0): number {
    const tileSize = this.gridSize * this.scale;
    return this.centerX + this.panX + ((gridX + gridY + 1) * tileSize) / 2;
  }

  gridToScreenY(gridX: number, gridY = 0): number {
    const tileSize = this.gridSize * this.scale;
    return this.centerY + this.panY + ((gridX - gridY) * tileSize) / 2;
  }

  screenToGridX(screenX: number, screenY: number): number {
    const sx = screenX - this.centerX - this.panX;
    const sy = screenY - this.centerY - this.panY;
    const tileSize = this.gridSize * this.scale;
    return Math.floor((sx + sy) / tileSize);
  }

  screenToGridY(screenX: number, screenY: number): number {
    const sx = screenX - this.centerX - this.panX;
    const sy = screenY - this.centerY - this.panY;
    const tileSize = this.gridSize * this.scale;
    return Math.floor((sx - sy) / tileSize);
  }

  screenToGrid(screenX: number, screenY: number): GridPoint {
    return {
      x: this.screenToGridX(screenX, screenY),
      y: this.screenToGridY(screenX, screenY),
    };
  }

  // ---- Mutators ------------------------------------------------------------

  /**
   * Update centerX / centerY to the new canvas midpoint. panX / panY are
   * intentionally NOT adjusted: because screenToGrid subtracts centerX, the
   * world point at the visual center stays put under any centerX change —
   * preserving the visual center across sidebar opens.
   */
  resizeToCanvas(): void {
    const width = this.canvas?.width ?? 0;
    const height = this.canvas?.height ?? 0;
    this.centerX = Math.floor(width / 2);
    this.centerY = Math.floor(height / 2);
    this._emit();
  }

  panBy(dx: number, dy: number): void {
    if (dx === 0 && dy === 0) return;
    this.panX += dx;
    this.panY += dy;
    this._emit();
  }

  /**
   * Set absolute scale, then adjust pan so the world point under
   * (anchorScreenX, anchorScreenY) stays under the same screen position —
   * keeping the cursor's tile anchored across zoom.
   */
  zoomAt(anchorScreenX: number, anchorScreenY: number, newScale: number): void {
    const clamped = Math.max(0.1, Math.min(5, newScale));
    if (clamped === this.scale) return;
    const worldBefore = this.screenToGrid(anchorScreenX, anchorScreenY);
    this.scale = clamped;
    const newTileSize = this.gridSize * clamped;
    const projX =
      this.centerX + this.panX + ((worldBefore.x + worldBefore.y + 1) * newTileSize) / 2;
    const projY =
      this.centerY + this.panY + ((worldBefore.x - worldBefore.y) * newTileSize) / 2;
    this.panX += anchorScreenX - projX;
    this.panY += anchorScreenY - projY;
    this._emit();
  }

  /**
   * Centre the visual viewport on the bbox of the given tile list: sets
   * panX/panY so the bbox center lands at (centerX, centerY).
   */
  centerOnContent(tracks: ReadonlyArray<GridPoint>): void {
    if (!tracks || tracks.length === 0) return;
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const t of tracks) {
      if (t.x < minX) minX = t.x;
      if (t.x > maxX) maxX = t.x;
      if (t.y < minY) minY = t.y;
      if (t.y > maxY) maxY = t.y;
    }
    const cx = (minX + maxX + 1) / 2;
    const cy = (minY + maxY + 1) / 2;
    const tileSize = this.gridSize * this.scale;
    this.panX = -(((cx + cy + 1) * tileSize) / 2);
    this.panY = -(((cx - cy) * tileSize) / 2);
    this._emit();
  }

  /**
   * Zoom AND pan so the whole tile list fits tightly inside the canvas. Unlike
   * centerOnContent (which only re-centers at the current scale), this also
   * picks the scale.
   *
   * We fit the tiles' ACTUAL on-screen footprint, not their grid bounding box.
   * In the iso projection screen-X tracks (x + y) and screen-Y tracks (x - y)
   * (see gridToScreenX/Y). A real network is sparse — it never reaches all four
   * corners of its grid bbox at once — so sizing the bbox's diamond would zoom
   * to mostly-empty space and leave the layout floating small and off-centre.
   * Instead we project every tile corner (at scale=1, pan=0 — those are uniform
   * offsets that don't affect the span or the centre), take the real screen box,
   * fit each axis independently into the matching visible dimension (times
   * `margin` for breathing room), use the smaller scale so both axes fit, clamp
   * to the zoom range, then pan the footprint's centre to the visible centre.
   *
   * `insets` are pixels along each edge that floating chrome (the primary tool
   * rail on the left, etc.) covers over the canvas. The fit frames into the
   * region INSIDE those insets so the layout is not hidden behind the chrome.
   * They default to 0, i.e. the whole canvas. The caller measures live chrome
   * geometry and passes it in — Camera stays free of any DOM/UI knowledge.
   */
  fitToContent(
    tracks: ReadonlyArray<GridPoint>,
    margin = 0.9,
    insets: { left?: number; right?: number; top?: number; bottom?: number } = {},
  ): void {
    if (!tracks || tracks.length === 0) return;
    const canvasW = this.canvas?.width ?? 0;
    const canvasH = this.canvas?.height ?? 0;
    if (canvasW <= 0 || canvasH <= 0) return;

    // Clamp each inset so even a bogus/oversized value can never collapse the
    // visible region to zero (which would make the fit scale blow up).
    const insetL = Math.max(0, Math.min(insets.left ?? 0, canvasW - 1));
    const insetR = Math.max(0, Math.min(insets.right ?? 0, canvasW - 1 - insetL));
    const insetT = Math.max(0, Math.min(insets.top ?? 0, canvasH - 1));
    const insetB = Math.max(0, Math.min(insets.bottom ?? 0, canvasH - 1 - insetT));
    const availW = canvasW - insetL - insetR;
    const availH = canvasH - insetT - insetB;

    let minSX = Infinity;
    let maxSX = -Infinity;
    let minSY = Infinity;
    let maxSY = -Infinity;
    for (const t of tracks) {
      for (const [dx, dy] of TILE_CORNERS) {
        const gx = t.x + dx;
        const gy = t.y + dy;
        // gridToScreenX/Y with tileSize=1 and no centre/pan offset.
        const px = (gx + gy + 1) / 2;
        const py = (gx - gy) / 2;
        if (px < minSX) minSX = px;
        if (px > maxSX) maxSX = px;
        if (py < minSY) minSY = py;
        if (py > maxSY) maxSY = py;
      }
    }
    const footW = maxSX - minSX; // footprint width  in tileSize units
    const footH = maxSY - minSY; // footprint height in tileSize units
    if (footW > 0 && footH > 0) {
      const tileSize = Math.min((availW * margin) / footW, (availH * margin) / footH);
      this.scale = Math.max(0.1, Math.min(5, tileSize / this.gridSize));
    }
    // Pan so the footprint's centre lands at the centre of the visible region.
    // screenX = centerX + panX + px * ts lands the footprint centre at the
    // canvas centre when panX = -(footprint centre px) * ts; shifting the
    // viewport centre by the inset asymmetry moves it by (insetL - insetR)/2.
    const ts = this.gridSize * this.scale;
    this.panX = -((minSX + maxSX) / 2) * ts + (insetL - insetR) / 2;
    this.panY = -((minSY + maxSY) / 2) * ts + (insetT - insetB) / 2;
    this._emit();
  }

  /**
   * Pan such that the given world tile lands at the visual center. Used by
   * App._maybeFollowSelectedTrain when Follow Train is on.
   */
  panToWorld(gridX: number, gridY: number): void {
    const tileSize = this.gridSize * this.scale;
    this.panX = -(((gridX + gridY + 1) * tileSize) / 2);
    this.panY = -(((gridX - gridY) * tileSize) / 2);
    this._emit();
  }
}
