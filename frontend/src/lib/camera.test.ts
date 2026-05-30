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
});
