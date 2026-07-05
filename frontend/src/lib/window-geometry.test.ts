import { describe, it, expect } from 'vitest';

import { computeFloatResize, computeFitSize, type FloatRect } from './window-geometry';

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
