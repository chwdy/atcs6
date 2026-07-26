import { describe, it, expect } from 'vitest';

import {
  computeFloatResize,
  computeFitSize,
  clampToViewport,
  computeOpenPlacement,
  type FloatRect,
} from './window-geometry';

// The floating inspector starts at left 200, top 100, 350×400 — so its right
// edge sits at 550 and its bottom at 500.
const START: FloatRect = { left: 200, top: 100, right: 550, bottom: 500 };
const VIEWPORT = { width: 1024, height: 768 };
const LIMITS = { minW: 260, minH: 180, margin: 6 };

function resize(dir: string, dx: number, dy: number) {
  return computeFloatResize(dir, START, dx, dy, VIEWPORT, LIMITS);
}

describe('computeFloatResize — floating window edge/corner math', () => {
  it('SE corner grows width and height from a fixed left/top', () => {
    expect(resize('se', 100, 60)).toEqual({ width: 450, height: 460 });
  });

  it('east edge changes width only, never position', () => {
    expect(resize('e', 80, 0)).toEqual({ width: 430 });
  });

  it('south edge changes height only', () => {
    expect(resize('s', 0, 50)).toEqual({ height: 450 });
  });

  it('west edge moves the left edge and widens, keeping the right edge fixed', () => {
    // Drag the left edge 50px left: left 200→150, width 350→400 (right stays 550).
    expect(resize('w', -50, 0)).toEqual({ left: 150, width: 400 });
  });

  it('north edge moves the top edge and grows height, keeping the bottom fixed', () => {
    // Drag the top edge 40px up: top 100→60, height 400→440 (bottom stays 500).
    expect(resize('n', 0, -40)).toEqual({ top: 60, height: 440 });
  });

  it('NW corner moves both left and top inward, growing the window', () => {
    expect(resize('nw', -30, -20)).toEqual({ left: 170, width: 380, top: 80, height: 420 });
  });

  it('clamps width to minW when the east edge is dragged far inward', () => {
    // Drag east edge 1000px left: would be -450, floored to left+minW = 460 → width 260.
    expect(resize('e', -1000, 0)).toEqual({ width: 260 });
  });

  it('clamps to minW from the west edge without crossing the right edge', () => {
    // Drag the left edge far right: left floored to right-minW = 290, width = 260.
    expect(resize('w', 1000, 0)).toEqual({ left: 290, width: 260 });
  });

  it('clamps to minH from the north edge', () => {
    // Drag top edge far down: top floored to bottom-minH = 320, height = 180.
    expect(resize('n', 1000, 1000)).toEqual({ top: 320, height: 180 });
  });

  it('clamps the right edge to the viewport margin', () => {
    // Drag east edge way out: right capped at viewport.width - margin = 1018 → width 818.
    expect(resize('e', 5000, 0)).toEqual({ width: 818 });
  });

  it('clamps the bottom edge to the viewport margin', () => {
    // Drag south edge way out: bottom capped at viewport.height - margin = 762 → height 662.
    expect(resize('s', 0, 5000)).toEqual({ height: 662 });
  });

  it('clamps the left edge to the viewport margin', () => {
    // Drag the left edge way out: left floored at margin = 6, width = 550 - 6 = 544.
    expect(resize('w', -5000, 0)).toEqual({ left: 6, width: 544 });
  });

  it('returns no fields the direction does not change', () => {
    // A pure-vertical drag must leave width/left absent so the caller never
    // writes them — and vice versa.
    expect(resize('s', 0, 30)).not.toHaveProperty('width');
    expect(resize('s', 0, 30)).not.toHaveProperty('left');
    expect(resize('e', 30, 0)).not.toHaveProperty('height');
    expect(resize('e', 30, 0)).not.toHaveProperty('top');
  });
});

describe('computeFitSize — double-click-to-fit math', () => {
  const ORIGIN = { left: 200, top: 100 };
  const FIT_LIMITS = { minW: 260, minH: 180, maxW: 900, maxH: 900, margin: 6 };

  it('fits exactly to natural size when it is well within all limits', () => {
    expect(computeFitSize(ORIGIN, { width: 400, height: 300 }, VIEWPORT, FIT_LIMITS)).toEqual({
      width: 400,
      height: 300,
    });
  });

  it('clamps to the available viewport space when content is larger than it', () => {
    // From left 200, available width is 1024 - 200 - 6 = 818.
    expect(computeFitSize(ORIGIN, { width: 2000, height: 300 }, VIEWPORT, FIT_LIMITS)).toEqual({
      width: 818,
      height: 300,
    });
    // From top 100, available height is 768 - 100 - 6 = 662.
    expect(computeFitSize(ORIGIN, { width: 400, height: 2000 }, VIEWPORT, FIT_LIMITS)).toEqual({
      width: 400,
      height: 662,
    });
  });

  it('clamps to minW/minH when content is smaller than the floor', () => {
    expect(computeFitSize(ORIGIN, { width: 50, height: 40 }, VIEWPORT, FIT_LIMITS)).toEqual({
      width: 260,
      height: 180,
    });
  });

  it('clamps to maxW/maxH when content exceeds an explicit cap, even with room in the viewport', () => {
    const roomyViewport = { width: 2000, height: 2000 };
    expect(computeFitSize({ left: 0, top: 0 }, { width: 1200, height: 1200 }, roomyViewport, FIT_LIMITS)).toEqual({
      width: 900,
      height: 900,
    });
  });

  it('never includes position — only width/height, so the anchor never moves', () => {
    const result = computeFitSize(ORIGIN, { width: 400, height: 300 }, VIEWPORT, FIT_LIMITS);
    expect(result).not.toHaveProperty('left');
    expect(result).not.toHaveProperty('top');
  });

  it('rounds fractional natural sizes', () => {
    expect(computeFitSize(ORIGIN, { width: 400.6, height: 300.2 }, VIEWPORT, FIT_LIMITS)).toEqual({
      width: 401,
      height: 300,
    });
  });
});

describe('clampToViewport', () => {
  const MARGIN = { margin: 8 };

  it('leaves a window that is already fully inside exactly where it is', () => {
    expect(clampToViewport({ left: 200, top: 100, width: 350, height: 400 }, VIEWPORT, MARGIN)).toEqual({
      left: 200,
      top: 100,
    });
  });

  it('pulls a window back in when the viewport shrinks past its right/bottom edge', () => {
    // 350×400 at (900, 600) overhangs a 1024×768 viewport on both axes.
    expect(clampToViewport({ left: 900, top: 600, width: 350, height: 400 }, VIEWPORT, MARGIN)).toEqual({
      left: 1024 - 350 - 8,
      top: 768 - 400 - 8,
    });
  });

  it('pulls a window back in from a negative left/top', () => {
    expect(clampToViewport({ left: -120, top: -40, width: 350, height: 400 }, VIEWPORT, MARGIN)).toEqual({
      left: 8,
      top: 8,
    });
  });

  it('pins a window wider than the viewport to the left margin rather than pushing it off', () => {
    const narrow = { width: 320, height: 768 };
    expect(clampToViewport({ left: 200, top: 100, width: 350, height: 400 }, narrow, MARGIN).left).toBe(8);
  });

  it('pins a window taller than the viewport to the top margin, keeping its header reachable', () => {
    const short = { width: 1024, height: 300 };
    expect(clampToViewport({ left: 200, top: 100, width: 350, height: 400 }, short, MARGIN).top).toBe(8);
  });

  it('does not resize — only position comes back', () => {
    const result = clampToViewport({ left: 900, top: 600, width: 350, height: 400 }, VIEWPORT, MARGIN);
    expect(result).not.toHaveProperty('width');
    expect(result).not.toHaveProperty('height');
  });
});

describe('computeOpenPlacement', () => {
  // Placement area: clear of the 108px tool rail on the left and the app header
  // above, inset 8px from the right/bottom.
  const BOUNDS: FloatRect = { left: 108, top: 60, right: 1016, bottom: 760 };
  const SIZE = { width: 350, height: 400 };
  const GAP = { gap: 12 };

  it('opens at the bounds origin when there is nothing to avoid', () => {
    expect(computeOpenPlacement(SIZE, BOUNDS, null, GAP)).toEqual({ left: 108, top: 60 });
  });

  it('opens to the right of the layout when there is room there', () => {
    const layout: FloatRect = { left: 150, top: 200, right: 500, bottom: 600 };
    expect(computeOpenPlacement(SIZE, BOUNDS, layout, GAP)).toEqual({ left: 512, top: 60 });
  });

  it('falls back to the left of the layout when the right has no room', () => {
    // Layout hugs the right edge, leaving 600-108 = 492px of clear space on the
    // left — enough for a 350px window, none on the right.
    const layout: FloatRect = { left: 600, top: 200, right: 1000, bottom: 600 };
    const placed = computeOpenPlacement(SIZE, BOUNDS, layout, GAP);
    expect(placed).toEqual({ left: 238, top: 60 });
    expect(placed.left + SIZE.width).toBeLessThanOrEqual(layout.left);
  });

  it('settles for least-overlap when the layout is too wide to clear on either side', () => {
    // 600px layout centred in 908px of bounds: no 350px window fits beside it.
    const layout: FloatRect = { left: 400, top: 200, right: 1000, bottom: 600 };
    const placed = computeOpenPlacement(SIZE, BOUNDS, layout, GAP);
    // Every candidate clips the layout, so the smallest clip wins: the "below"
    // anchor, clamped up to the bottom of the placement area.
    expect(placed).toEqual({ left: 108, top: 360 });
  });

  it('drops below the layout when neither side has room', () => {
    const layout: FloatRect = { left: 120, top: 60, right: 1000, bottom: 300 };
    expect(computeOpenPlacement(SIZE, BOUNDS, layout, GAP)).toEqual({ left: 108, top: 312 });
  });

  it('picks the least-overlapping candidate when the layout fills the viewport', () => {
    const layout: FloatRect = { left: 0, top: 0, right: 1024, bottom: 768 };
    const placed = computeOpenPlacement(SIZE, BOUNDS, layout, GAP);
    // Nothing can clear it, so the result must at least stay inside bounds
    // rather than escaping to a fixed corner off the placement area.
    expect(placed.left).toBeGreaterThanOrEqual(BOUNDS.left);
    expect(placed.top).toBeGreaterThanOrEqual(BOUNDS.top);
    expect(placed.left + SIZE.width).toBeLessThanOrEqual(BOUNDS.right);
    expect(placed.top + SIZE.height).toBeLessThanOrEqual(BOUNDS.bottom);
  });

  it('never places a window outside the supplied bounds', () => {
    const layout: FloatRect = { left: 900, top: 700, right: 1010, bottom: 755 };
    const placed = computeOpenPlacement(SIZE, BOUNDS, layout, GAP);
    expect(placed.left).toBeGreaterThanOrEqual(BOUNDS.left);
    expect(placed.top).toBeGreaterThanOrEqual(BOUNDS.top);
  });

  it('clamps a window larger than the placement area to the bounds origin', () => {
    const huge = { width: 2000, height: 2000 };
    expect(computeOpenPlacement(huge, BOUNDS, null, GAP)).toEqual({ left: 108, top: 60 });
  });
});
