import { afterEach, describe, expect, it } from 'vitest';

import { RendererPlatformHandler, type PlatformHandlerRenderer } from './renderer-platforms';

// renderer-platforms.js had no test before the migration; this is fresh coverage
// for its pure geometry helpers (the canvas-drawing entry composes these).

function makeRenderer(): PlatformHandlerRenderer {
  return {
    ctx: {} as unknown as CanvasRenderingContext2D, // pure helpers under test don't draw
    scale: 1,
    gridSize: 40,
    currentPlatforms: [],
    trackMap: {},
    mouseX: -1,
    mouseY: -1,
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
    platformBounds: [],
    labelBounds: [],
    getNearbyTrackSegments: () => [],
    distRectToSegments: () => 100,
  };
}

const h = () => new RendererPlatformHandler(makeRenderer());

afterEach(() => {
  delete (globalThis as { TRACK_BITS?: unknown }).TRACK_BITS;
});

describe('RendererPlatformHandler pure helpers', () => {
  it('abbreviatePlatformName: initials for multi-word, passthrough for short', () => {
    expect(h().abbreviatePlatformName('Platform 1')).toBe('P1');
    expect(h().abbreviatePlatformName('AB')).toBe('AB');
    expect(h().abbreviatePlatformName('')).toBe('');
  });

  it('getSideMultiplier maps side+orientation, defaulting to 1', () => {
    expect(h().getSideMultiplier('east', 'NS')).toBe(1);
    expect(h().getSideMultiplier('west', 'NS')).toBe(-1);
    expect(h().getSideMultiplier('nope', 'NS')).toBe(1);
    expect(h().getSideMultiplier('east', undefined)).toBe(1);
  });

  it('shouldSwapEndpoints follows the orientation rule', () => {
    expect(h().shouldSwapEndpoints({ x: 0, y: 0 }, { x: 0, y: -5 }, 'NS')).toBe(true);
    expect(h().shouldSwapEndpoints({ x: 0, y: 0 }, { x: 0, y: 5 }, 'NS')).toBe(false);
    expect(h().shouldSwapEndpoints({ x: 0, y: 0 }, { x: 5, y: 0 }, 'EW')).toBe(true);
    expect(h().shouldSwapEndpoints({ x: 0, y: 0 }, { x: 5, y: 0 }, undefined)).toBe(false);
  });

  it('findLongestPair returns the farthest two points (null below two)', () => {
    expect(h().findLongestPair([{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 10, y: 0 }]))
      .toEqual([{ x: 0, y: 0 }, { x: 10, y: 0 }]);
    expect(h().findLongestPair([{ x: 0, y: 0 }])).toBeNull();
  });

  it('findEndpointTiles returns the farthest two tiles', () => {
    expect(h().findEndpointTiles([[0, 0], [1, 0], [5, 0]])).toEqual([[0, 0], [5, 0]]);
    expect(h().findEndpointTiles([])).toBeNull();
    expect(h().findEndpointTiles([[2, 2]])).toEqual([[2, 2], [2, 2]]);
  });

  it('getPathEndpoints yields up to two endpoints for the active track bits', () => {
    (globalThis as { TRACK_BITS?: unknown }).TRACK_BITS = { NS: 1, EW: 2, NE: 4, NW: 8, SE: 16, SW: 32 };
    expect(h().getPathEndpoints([0, 0], 1, 40)).toHaveLength(2); // NS
  });
});
