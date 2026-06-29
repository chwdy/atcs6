import { describe, it, expect } from 'vitest';

import { Camera } from './camera';

// Vitest port of the old Node-eval tests/test_camera_math.py. Same invariants,
// now exercised directly against the TypeScript Camera.
const makeCanvas = (width = 1200, height = 800) => ({ width, height });

describe('Camera projection + viewport math', () => {
  it('roundtrips screenToGrid(gridToScreen(x, y)) at any scale', () => {
    for (const scale of [0.1, 0.5, 1.0, 2.5, 5.0]) {
      const cam = new Camera(makeCanvas());
      cam.resizeToCanvas();
      cam.scale = scale;
      for (const [gx, gy] of [[0, 0], [1, 0], [-3, 5], [12, 12], [100, 1]]) {
        const sx = cam.gridToScreenX(gx, gy);
        const sy = cam.gridToScreenY(gx, gy);
        expect([cam.screenToGridX(sx, sy), cam.screenToGridY(sx, sy)]).toEqual([gx, gy]);
      }
    }
  });

  it('panBy shifts the world under a fixed screen point predictably', () => {
    const cam = new Camera(makeCanvas());
    cam.resizeToCanvas();
    const sx = 720;
    const sy = 350;
    const before = cam.screenToGrid(sx, sy);
    cam.panBy(50, -30);
    const after = cam.screenToGrid(sx, sy);
    cam.panBy(-50, 30); // reset
    const baseline = cam.screenToGrid(sx - 50, sy + 30);
    cam.panBy(50, -30);
    expect(cam.screenToGrid(sx, sy)).toEqual(baseline);
    expect(after).not.toEqual(before);
  });

  it('zoomAt keeps the world point under the cursor fixed across scales', () => {
    for (const targetScale of [0.5, 2.0, 4.0]) {
      const cam = new Camera(makeCanvas());
      cam.resizeToCanvas();
      cam.panX = 137;
      cam.panY = -82;
      const anchorSx = 720;
      const anchorSy = 350;
      const worldBefore = cam.screenToGrid(anchorSx, anchorSy);
      cam.zoomAt(anchorSx, anchorSy, targetScale);
      expect(cam.screenToGrid(anchorSx, anchorSy)).toEqual(worldBefore);
      expect(cam.scale).toBeCloseTo(targetScale, 9);
    }
  });

  it('centerOnContent lands the bbox center at the visual center', () => {
    const cam = new Camera(makeCanvas());
    cam.resizeToCanvas();
    cam.centerOnContent([
      { x: -5, y: -3 },
      { x: 4, y: -3 },
      { x: -5, y: 7 },
      { x: 4, y: 7 },
    ]);
    // bbox center is (0, 2.5); that world point should land at visual center.
    const tileSize = cam.gridSize * cam.scale;
    const expectedSX = cam.centerX + cam.panX + ((0 + 2.5 + 1) * tileSize) / 2;
    const expectedSY = cam.centerY + cam.panY + ((0 - 2.5) * tileSize) / 2;
    expect(expectedSX).toBeCloseTo(cam.centerX, 9);
    expect(expectedSY).toBeCloseTo(cam.centerY, 9);
  });

  it('resizeToCanvas updates center but leaves pan (visual-center invariant)', () => {
    const canvas = makeCanvas(1200, 800);
    const cam = new Camera(canvas);
    cam.resizeToCanvas();
    cam.panX = 123;
    cam.panY = 456;
    canvas.width = 850;
    canvas.height = 800;
    cam.resizeToCanvas();
    expect(cam.centerX).toBe(425);
    expect(cam.panX).toBe(123);
    expect(cam.panY).toBe(456);

    const cam2 = new Camera(makeCanvas(1200, 800));
    cam2.resizeToCanvas();
    cam2.panX = 123;
    cam2.panY = 456;
    const worldBefore = cam2.screenToGrid(cam2.centerX, cam2.centerY);
    cam2.canvas!.width = 850;
    cam2.resizeToCanvas();
    expect(cam2.screenToGrid(cam2.centerX, cam2.centerY)).toEqual(worldBefore);
  });

  it('onChange fires on mutators and stops after unsubscribe', () => {
    const cam = new Camera(makeCanvas());
    cam.resizeToCanvas();
    let fired = 0;
    const unsub = cam.onChange(() => {
      fired += 1;
    });
    cam.panBy(10, 0);
    cam.zoomAt(100, 100, 2.0);
    cam.centerOnContent([{ x: 0, y: 0 }, { x: 1, y: 1 }]);
    cam.panToWorld(5, 5);
    cam.resizeToCanvas();
    expect(fired).toBeGreaterThanOrEqual(5);
    unsub();
    const old = fired;
    cam.panBy(1, 1);
    expect(fired).toBe(old);
  });

  it('panToWorld lands the given tile at the visual center', () => {
    const cam = new Camera(makeCanvas());
    cam.resizeToCanvas();
    cam.scale = 1.5;
    cam.panToWorld(7, -3);
    expect(cam.gridToScreenX(7, -3)).toBeCloseTo(cam.centerX, 9);
    expect(cam.gridToScreenY(7, -3)).toBeCloseTo(cam.centerY, 9);
  });

  it('fitToContent frames the whole bbox inside the canvas and centers it', () => {
    const canvas = makeCanvas(1200, 800);
    const cam = new Camera(canvas);
    cam.resizeToCanvas();
    cam.scale = 4; // start zoomed in too tight to show everything
    // A bbox the size of the demo_6 network (x: -22..43, y: -31..30).
    const minX = -22;
    const maxX = 43;
    const minY = -31;
    const maxY = 30;
    cam.fitToContent([
      { x: minX, y: minY },
      { x: maxX, y: minY },
      { x: minX, y: maxY },
      { x: maxX, y: maxY },
    ]);

    // Every corner of the footprint projects inside the canvas.
    for (const [gx, gy] of [
      [minX, minY],
      [maxX + 1, minY],
      [minX, maxY + 1],
      [maxX + 1, maxY + 1],
    ]) {
      const sx = cam.gridToScreenX(gx, gy);
      const sy = cam.gridToScreenY(gx, gy);
      expect(sx).toBeGreaterThanOrEqual(0);
      expect(sx).toBeLessThanOrEqual(canvas.width);
      expect(sy).toBeGreaterThanOrEqual(0);
      expect(sy).toBeLessThanOrEqual(canvas.height);
    }

    // The bbox center still lands at the visual center.
    const cx = (minX + maxX + 1) / 2;
    const cy = (minY + maxY + 1) / 2;
    expect(cam.gridToScreenX(cx, cy)).toBeCloseTo(cam.centerX, 6);
    expect(cam.gridToScreenY(cx, cy)).toBeCloseTo(cam.centerY, 6);

    // Scale stays inside the allowed zoom range and actually zoomed out.
    expect(cam.scale).toBeGreaterThanOrEqual(0.1);
    expect(cam.scale).toBeLessThanOrEqual(5);
    expect(cam.scale).toBeLessThan(4);
  });

  it('fitToContent tightly frames a sparse layout that does not fill its bbox', () => {
    const canvas = makeCanvas(1200, 800);
    const cam = new Camera(canvas);
    cam.resizeToCanvas();
    cam.scale = 4; // start zoomed in too tight to show everything

    // A diagonal line of tiles along y = x. Its grid bbox is a fat 0..10 square,
    // but the iso projection collapses it to a thin horizontal bar on screen
    // (x - y is constant, so screen-Y barely moves). The old bbox-diamond fit
    // zoomed to the empty square and left the bar tiny and floating; the
    // footprint fit must zoom to the bar itself.
    const tiles: Array<{ x: number; y: number }> = [];
    for (let i = 0; i <= 10; i++) tiles.push({ x: i, y: i });
    cam.fitToContent(tiles);

    // Measure the actual on-screen footprint of every tile corner.
    let minSx = Infinity;
    let maxSx = -Infinity;
    let minSy = Infinity;
    let maxSy = -Infinity;
    for (const t of tiles) {
      for (const [dx, dy] of [[0, 0], [1, 0], [1, 1], [0, 1]]) {
        const sx = cam.gridToScreenX(t.x + dx, t.y + dy);
        const sy = cam.gridToScreenY(t.x + dx, t.y + dy);
        if (sx < minSx) minSx = sx;
        if (sx > maxSx) maxSx = sx;
        if (sy < minSy) minSy = sy;
        if (sy > maxSy) maxSy = sy;
      }
    }

    // The whole footprint stays on-canvas.
    expect(minSx).toBeGreaterThanOrEqual(0);
    expect(maxSx).toBeLessThanOrEqual(canvas.width);
    expect(minSy).toBeGreaterThanOrEqual(0);
    expect(maxSy).toBeLessThanOrEqual(canvas.height);

    // The limiting axis (width, for this bar) fills the canvas to the margin —
    // i.e. it is genuinely tight. The old bbox fit only reached ~0.6 here.
    const widthFill = (maxSx - minSx) / canvas.width;
    expect(widthFill).toBeCloseTo(0.9, 6);

    // The footprint is centred on the canvas on both axes.
    expect((minSx + maxSx) / 2).toBeCloseTo(cam.centerX, 6);
    expect((minSy + maxSy) / 2).toBeCloseTo(cam.centerY, 6);

    expect(cam.scale).toBeGreaterThanOrEqual(0.1);
    expect(cam.scale).toBeLessThanOrEqual(5);
  });

  it('fitToContent frames into the visible region, clear of a left chrome inset', () => {
    const canvas = makeCanvas(1200, 800);
    const cam = new Camera(canvas);
    cam.resizeToCanvas();

    // A diagonal bar (width-limited on screen), so the available WIDTH is the
    // limiting axis — that makes the inset's effect on scale observable.
    const tiles: Array<{ x: number; y: number }> = [];
    for (let i = 0; i <= 10; i++) tiles.push({ x: i, y: i });

    const insetL = 200; // pretend the tool rail covers the left 200px
    cam.fitToContent(tiles, 0.9, { left: insetL });

    let minSx = Infinity;
    let maxSx = -Infinity;
    let minSy = Infinity;
    let maxSy = -Infinity;
    for (const t of tiles) {
      for (const [dx, dy] of [[0, 0], [1, 0], [1, 1], [0, 1]]) {
        const sx = cam.gridToScreenX(t.x + dx, t.y + dy);
        const sy = cam.gridToScreenY(t.x + dx, t.y + dy);
        if (sx < minSx) minSx = sx;
        if (sx > maxSx) maxSx = sx;
        if (sy < minSy) minSy = sy;
        if (sy > maxSy) maxSy = sy;
      }
    }

    // The whole footprint clears the inset and stays on-canvas.
    expect(minSx).toBeGreaterThanOrEqual(insetL);
    expect(maxSx).toBeLessThanOrEqual(canvas.width);
    expect(minSy).toBeGreaterThanOrEqual(0);
    expect(maxSy).toBeLessThanOrEqual(canvas.height);

    // The footprint is centred in the VISIBLE region (canvas minus the inset),
    // not on the raw canvas.
    const availW = canvas.width - insetL;
    expect((minSx + maxSx) / 2).toBeCloseTo(insetL + availW / 2, 6);
    expect((minSy + maxSy) / 2).toBeCloseTo(cam.centerY, 6);

    // The limiting axis fills the AVAILABLE width to the margin — the inset
    // shrank the usable space, so the layout is scaled smaller than a full-canvas
    // fit would have produced.
    expect((maxSx - minSx) / availW).toBeCloseTo(0.9, 6);
  });
});
