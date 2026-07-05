import { describe, it, expect } from 'vitest';
import { pointInRect, resolveDropTarget, exceedsDragThreshold, type Rect } from './tab-drag';

const RECT: Rect = { left: 100, top: 100, right: 300, bottom: 250 };

describe('pointInRect', () => {
  it('is true for a point inside the rect', () => {
    expect(pointInRect(200, 175, RECT)).toBe(true);
  });

  it('is true exactly on each edge (inclusive)', () => {
    expect(pointInRect(100, 175, RECT)).toBe(true); // left edge
    expect(pointInRect(300, 175, RECT)).toBe(true); // right edge
    expect(pointInRect(200, 100, RECT)).toBe(true); // top edge
    expect(pointInRect(200, 250, RECT)).toBe(true); // bottom edge
  });

  it('is false just outside each edge', () => {
    expect(pointInRect(99, 175, RECT)).toBe(false);
    expect(pointInRect(301, 175, RECT)).toBe(false);
    expect(pointInRect(200, 99, RECT)).toBe(false);
    expect(pointInRect(200, 251, RECT)).toBe(false);
  });
});

describe('resolveDropTarget', () => {
  it('returns null when no candidate contains the point', () => {
    expect(resolveDropTarget(500, 500, [{ rect: RECT, value: 'a' }])).toBeNull();
  });

  it('returns the matching candidate value', () => {
    expect(resolveDropTarget(200, 175, [{ rect: RECT, value: 'a' }])).toBe('a');
  });

  it('returns the first match when candidates overlap — order is the caller’s z-order', () => {
    const overlapping: Rect = { left: 150, top: 120, right: 350, bottom: 260 };
    const candidates = [
      { rect: RECT, value: 'back' },
      { rect: overlapping, value: 'front' },
    ];
    // (200, 175) is inside both — the first-listed candidate wins.
    expect(resolveDropTarget(200, 175, candidates)).toBe('back');

    const reordered = [...candidates].reverse();
    expect(resolveDropTarget(200, 175, reordered)).toBe('front');
  });

  it('returns null for an empty candidate list', () => {
    expect(resolveDropTarget(200, 175, [])).toBeNull();
  });
});

describe('exceedsDragThreshold', () => {
  it('is false for movement under the threshold', () => {
    expect(exceedsDragThreshold(2, 2, 6)).toBe(false);
  });

  it('is true exactly at the threshold (Euclidean distance)', () => {
    expect(exceedsDragThreshold(6, 0, 6)).toBe(true);
    expect(exceedsDragThreshold(0, 6, 6)).toBe(true);
  });

  it('is true for movement well past the threshold', () => {
    expect(exceedsDragThreshold(100, 0, 6)).toBe(true);
  });

  it('uses Euclidean distance, not per-axis — a diagonal under either axis alone can still exceed it', () => {
    // dx=5, dy=5 individually look sub-threshold against a threshold of 6,
    // but the actual distance (~7.07) exceeds it.
    expect(exceedsDragThreshold(5, 5, 6)).toBe(true);
  });

  it('is false at the origin (no movement)', () => {
    expect(exceedsDragThreshold(0, 0, 6)).toBe(false);
  });
});
