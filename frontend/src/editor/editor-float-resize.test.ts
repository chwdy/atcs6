import { describe, it, expect } from 'vitest';

import { computeFloatResize, type FloatRect } from './editor-ui';

// The floating inspector starts at left 200, top 100, 350×400 — so its right
// edge sits at 550 and its bottom at 500.
const START: FloatRect = { left: 200, top: 100, right: 550, bottom: 500 };
const VIEWPORT = { width: 1024, height: 768 };
const LIMITS = { minW: 260, minH: 180, margin: 6 };

function resize(dir: string, dx: number, dy: number) {
  return computeFloatResize(dir, START, dx, dy, VIEWPORT, LIMITS);
}

describe('computeFloatResize — floating inspector edge/corner math', () => {
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
