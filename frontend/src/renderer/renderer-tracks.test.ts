import { afterEach, describe, expect, it, vi } from 'vitest';

// renderer-tracks imports getActiveSegments/isCrossing from ../lib/constants;
// mock just those two so the drawing is tested in isolation from the real
// bit math (the rest of constants stays real).
const mocks = vi.hoisted(() => ({
  getActiveSegments: vi.fn((): Array<[string, string]> => [['N', 'S']]),
  isCrossing: vi.fn((): boolean => false),
}));
vi.mock('../lib/constants', async (importActual) => {
  const actual = await importActual<typeof import('../lib/constants')>();
  return { ...actual, getActiveSegments: mocks.getActiveSegments, isCrossing: mocks.isCrossing };
});

// blockTrackColor now delegates to the --block-* token consumer; mock it so the
// unit test verifies the delegation without depending on getComputedStyle.
vi.mock('../lib/helpers', () => ({
  getBlockStateColor: (state: string) => `token:${state}`,
}));

import { TRACK_LINE_WIDTH, TRACK_STATUS_WIDTH, TRACK_WIDTH } from '../lib/constants';
import { RendererTrackHandler, blockTrackColor, linkPieces, switchMark, type TrackHandlerRenderer } from './renderer-tracks';

// renderer-tracks.js had no test before the migration; this is fresh coverage
// for the shared geometry (getEdgePoints/drawGridSegment) and bit-driven draw.

function makeCtx() {
  return {
    save: vi.fn(),
    restore: vi.fn(),
    translate: vi.fn(),
    beginPath: vi.fn(),
    moveTo: vi.fn(),
    lineTo: vi.fn(),
    closePath: vi.fn(),
    arcTo: vi.fn(),
    clip: vi.fn(),
    fill: vi.fn(),
    stroke: vi.fn(),
    arc: vi.fn(),
    fillText: vi.fn(),
    setLineDash: vi.fn(),
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 0,
    lineCap: '',
    lineJoin: '',
    font: '',
    textAlign: '',
    textBaseline: '',
  };
}

function makeRenderer(ctx: ReturnType<typeof makeCtx>): TrackHandlerRenderer {
  return {
    ctx: ctx as unknown as CanvasRenderingContext2D,
    scale: 1,
    gridSize: 40,
    currentBlocks: {},
    currentNetwork: {},
    trackMap: {},
    gridToScreenX: (x) => x * 10,
    gridToScreenY: (_x, y) => y * 10,
    debugHandler: { highlightDeadEnds: vi.fn() },
  };
}

const EXTENT_STYLE = {
  tileFill: 'fill', glow: 'glow', core: 'core', cap: 'cap',
};

afterEach(() => {
  mocks.getActiveSegments.mockReset();
  mocks.getActiveSegments.mockReturnValue([['N', 'S']]);
  mocks.isCrossing.mockReset();
  mocks.isCrossing.mockReturnValue(false);
});

describe('RendererTrackHandler', () => {
  it('getEdgePoints returns the four tile-corner points', () => {
    const ep = new RendererTrackHandler(makeRenderer(makeCtx())).getEdgePoints(40);
    expect(ep.N).toEqual({ x: 20, y: -20 });
    expect(ep.E).toEqual({ x: 20, y: 20 });
    expect(ep.S).toEqual({ x: -20, y: 20 });
    expect(ep.W).toEqual({ x: -20, y: -20 });
  });

  it('drawGridSegment fills one quadrilateral', () => {
    const ctx = makeCtx();
    new RendererTrackHandler(makeRenderer(ctx)).drawGridSegment(
      { x: 0, y: 0 }, { x: 10, y: 0 }, 4, 'N', 'S', '#666',
    );
    expect(ctx.fill).toHaveBeenCalledTimes(1);
    expect(ctx.lineTo).toHaveBeenCalledTimes(3);
  });

  describe('blockTrackColor', () => {
    it('returns null for a missing block (caller keeps its default)', () => {
      expect(blockTrackColor(undefined)).toBeNull();
      expect(blockTrackColor(null)).toBeNull();
    });

    it('colours a block from its live fields through the --block-* token consumer', () => {
      expect(blockTrackColor({ reads_occupied: true })).toBe('token:occupied');
      expect(blockTrackColor({ train_id: 'T1' })).toBe('token:free'); // the detector decides
      expect(blockTrackColor({ claimed_by_train_id: 'T1' })).toBe('token:reserved');
      expect(blockTrackColor({ reads_occupied: false, train_id: null })).toBe('token:free');
      expect(blockTrackColor({})).toBe('token:free');
    });
  });

});

/** Record every stroke: its colour, its width, and the points of its path. */
function recordStrokes(ctx: ReturnType<typeof makeCtx>) {
  const strokes: Array<{ style: string; width: number; starts: number[][]; points: number[][] }> = [];
  let starts: number[][] = [];
  let points: number[][] = [];
  ctx.beginPath = vi.fn(() => { starts = []; points = []; });
  ctx.moveTo = vi.fn((x: number, y: number) => { starts.push([x, y]); points.push([x, y]); });
  ctx.lineTo = vi.fn((x: number, y: number) => { points.push([x, y]); });
  // A bend is drawn as an arc round its corner point.
  ctx.arcTo = vi.fn((x: number, y: number) => { points.push([x, y]); });
  ctx.stroke = vi.fn(() => {
    strokes.push({ style: ctx.strokeStyle, width: ctx.lineWidth, starts, points });
  });
  return Object.assign(strokes, { current: () => points });
}

describe('linkPieces', () => {
  const at = (x: number, y: number) => ({ x, y });

  it('links pieces that meet on a shared edge into one line with two free ends', () => {
    const lines = linkPieces([
      { a: 'e1', b: 'e2', pa: at(0, 0), pb: at(10, 0), tile: 't1' },
      { a: 'e3', b: 'e2', pa: at(20, 10), pb: at(10, 0), tile: 't2' },
    ]);
    expect(lines).toHaveLength(1);
    expect(lines[0].closed).toBe(false);
    expect(lines[0].startFree && lines[0].endFree).toBe(true);
    expect(lines[0].points).toHaveLength(3);
    expect(lines[0].points[1]).toEqual(at(10, 0));
  });

  it('returns a loop of track as one closed line', () => {
    const lines = linkPieces([
      { a: 'e1', b: 'e2', pa: at(0, 0), pb: at(10, 0), tile: 't1' },
      { a: 'e2', b: 'e3', pa: at(10, 0), pb: at(10, 10), tile: 't2' },
      { a: 'e3', b: 'e1', pa: at(10, 10), pb: at(0, 0), tile: 't3' },
    ]);
    expect(lines).toHaveLength(1);
    expect(lines[0].closed).toBe(true);
    expect(lines[0].points).toHaveLength(3);
  });

  it('carries each line on into the neighbouring tile where three pieces meet', () => {
    // Tile t1 has two pieces leaving edge `fork`; tile t2 has one arriving.
    const lines = linkPieces([
      { a: 'fork', b: 'e1', pa: at(0, 0), pb: at(-10, 0), tile: 't1' },
      { a: 'fork', b: 'e2', pa: at(0, 0), pb: at(-10, 10), tile: 't1' },
      { a: 'fork', b: 'e3', pa: at(0, 0), pb: at(10, 0), tile: 't2' },
    ]);
    expect(lines).toHaveLength(3);
    for (const line of lines) {
      // Every line passes THROUGH the meeting point, so the canvas joins it
      // there instead of ending square.
      expect(line.points).toHaveLength(3);
      expect(line.points[1]).toEqual(at(0, 0));
      expect(line.startFree).toBe(false);
    }
  });
});

describe('RendererTrackHandler.drawTracks', () => {
  it('draws all track in the track colour, then the block status as a thin line pulled back at the block ends', () => {
    mocks.getActiveSegments.mockReturnValue([['N', 'S']]);
    const ctx = makeCtx();
    const strokes = recordStrokes(ctx);
    const r = makeRenderer(ctx);
    r.currentBlocks = { B: { reads_occupied: true } };
    new RendererTrackHandler(r).drawTracks([
      { x: 0, y: 0, track_bits: 1, block_section_id: 'B' },
      { x: 0, y: 1, track_bits: 1, block_section_id: 'B' },
    ]);
    expect(strokes.map((s) => s.style)).toEqual(['#3a3a44', 'token:occupied']);
    // The track at its full thickness and the status as a thin line along
    // its middle, one line each: the two tiles are linked, not drawn as two
    // pieces.
    expect(strokes.map((s) => s.width)).toEqual([TRACK_LINE_WIDTH, TRACK_STATUS_WIDTH]);
    expect(strokes[0].starts).toHaveLength(1);
    expect(strokes[1].starts).toHaveLength(1);
    expect(strokes[1].points).toHaveLength(3);
    // The status line stops TRACK_WIDTH short of where the track ends.
    const [gx, gy] = strokes[0].starts[0];
    const [cx, cy] = strokes[1].starts[0];
    expect(Math.hypot(cx - gx, cy - gy)).toBeCloseTo(TRACK_WIDTH);
  });

  it('draws no colour line for track that is in no block', () => {
    mocks.getActiveSegments.mockReturnValue([['N', 'S']]);
    const ctx = makeCtx();
    const strokes = recordStrokes(ctx);
    new RendererTrackHandler(makeRenderer(ctx)).drawTracks([{ x: 0, y: 0, track_bits: 1 }]);
    expect(strokes.map((s) => s.style)).toEqual(['#3a3a44']);
  });

  it('draws a switch with its selected route whole and the leg not taken dim, short of that route', () => {
    // Regular turnout EW | NE: at switch_state 0 the straight EW segment is
    // the selected route and the NE segment is not. Both leave by the E edge.
    mocks.getActiveSegments.mockReturnValue([['E', 'W'], ['N', 'E']]);
    const ctx = makeCtx();
    const strokes = recordStrokes(ctx);
    const fills: Array<{ style: string; points: number[][] }> = [];
    ctx.fill = vi.fn(() => { fills.push({ style: ctx.fillStyle, points: strokes.current() }); });
    const r = makeRenderer(ctx);
    r.currentBlocks = { B: { reads_occupied: true } };
    new RendererTrackHandler(r).drawTracks([{
      x: 0, y: 0, track_bits: 6, block_section_id: 'B',
      is_switch: true, switch_state: 0, switch_points: { E: ['N', 'W'] },
    }]);
    const [grey, colour] = strokes;
    expect(grey.style).toBe('#3a3a44');
    expect(grey.starts).toHaveLength(2); // the selected route + under the leg not taken
    // The leg not taken is filled in the dim colour, as thick as the grey,
    // from the N edge (10, -10) toward the E edge (10, 10), where it would
    // meet the selected route.
    expect(fills).toHaveLength(1);
    expect(fills[0].style).toBe('#25252b');
    const corners = fills[0].points;
    expect(corners.slice(0, 2)).toEqual([[15, -10], [5, -10]]);
    // The selected route runs from E (10, 10) to W (-10, -10): the line
    // x = y. The leg's end is cut parallel to it, so both end corners are the
    // same distance from the middle of that route: half its width plus the gap.
    const clear = TRACK_LINE_WIDTH / 2 + TRACK_WIDTH / 2;
    for (const [x, y] of corners.slice(2)) {
      expect(Math.abs(x - y) / Math.SQRT2).toBeCloseTo(clear);
    }
    // Only the selected route takes the status line.
    expect(colour.style).toBe('token:occupied');
    expect(colour.starts).toHaveLength(1);
    expect(colour.width).toBe(TRACK_STATUS_WIDTH);
  });

  it('draws both legs whole while alternate arrivals are on, and both dim until the position is known', () => {
    mocks.getActiveSegments.mockReturnValue([['E', 'W'], ['N', 'E']]);
    const tile = {
      x: 0, y: 0, track_bits: 6, is_switch: true, switch_state: 0,
      switch_id: 'SW', switch_points: { E: ['N', 'W'] },
    };
    const draw = (condition: object) => {
      const ctx = makeCtx();
      const strokes = recordStrokes(ctx);
      const r = makeRenderer(ctx);
      r.currentNetwork = { switches: { SW: condition } };
      new RendererTrackHandler(r).drawTracks([tile]);
      return { strokes, fills: ctx.fill.mock.calls.length };
    };
    // Alternate arrivals on: the runtime picks the leg, so neither is dim.
    const alternate = draw({ alternate_next: 0 });
    expect(alternate.strokes[0].style).toBe('#3a3a44');
    expect(alternate.strokes[0].points).toHaveLength(3); // both legs, linked at E
    expect(alternate.fills).toBe(0);
    // Position not known: no leg is drawn as set.
    const unknown = draw({ position_known: false });
    expect(unknown.strokes).toHaveLength(0);
    expect(unknown.fills).toBe(2);
  });

  it('marks a switch by the condition that matters most to someone about to throw it', () => {
    expect(switchMark(undefined)).toBeNull();
    expect(switchMark({ held: false, manual: false, alternate_next: null, position_known: true })).toBeNull();
    expect(switchMark({ manual: true })).toBe('manual');
    expect(switchMark({ alternate_next: 1 })).toBe('alternate');
    expect(switchMark({ position_known: false, manual: true })).toBe('unknown');
    expect(switchMark({ held: true, manual: true, position_known: false })).toBe('held');
  });

  it('draws a badge beside a switch that is not plain, off the track', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.currentNetwork = { switches: { A: { held: true }, B: {} } };
    // Track leaves tile A by E, W and N, so the badge goes on the S side:
    // down and to the left of the tile's centre at (0, 0).
    new RendererTrackHandler(r).drawSwitchMarks([
      { x: 0, y: 0, track_bits: 6, is_switch: true, switch_id: 'A' },
      { x: 5, y: 5, track_bits: 6, is_switch: true, switch_id: 'B' },
    ]);
    expect(ctx.arc).toHaveBeenCalledTimes(1); // the plain switch has no badge
    const [x, y] = ctx.arc.mock.calls[0];
    expect(x).toBeLessThan(0);
    expect(y).toBeGreaterThan(0);
    expect(ctx.fillText).toHaveBeenCalledTimes(1);
  });

  it('marks a plain crossing with a dot', () => {
    mocks.getActiveSegments.mockReturnValue([['N', 'S'], ['E', 'W']]);
    mocks.isCrossing.mockReturnValue(true);
    const ctx = makeCtx();
    new RendererTrackHandler(makeRenderer(ctx)).drawTracks([{ x: 0, y: 0, track_bits: 3 }]);
    expect(ctx.arc).toHaveBeenCalledTimes(1);
    expect(ctx.fill).toHaveBeenCalledTimes(1);
  });
});

describe('RendererTrackHandler.drawPreviewTracks', () => {
  it('draws the track about to be laid as one line as thick as the track, in one stroke', () => {
    mocks.getActiveSegments.mockReturnValue([['N', 'S']]);
    const ctx = makeCtx();
    const strokes = recordStrokes(ctx);
    new RendererTrackHandler(makeRenderer(ctx)).drawPreviewTracks([
      { x: 0, y: 0, track_bits: 1 },
      { x: 0, y: 1, track_bits: 1 },
      { x: 0, y: 2, track_bits: 0 }, // nothing to lay on this tile
    ]);
    // One stroke, so the see-through colour is not doubled where tiles meet.
    expect(strokes).toHaveLength(1);
    expect(strokes[0].width).toBe(TRACK_LINE_WIDTH);
    expect(strokes[0].starts).toHaveLength(1); // the two tiles are one line
  });

  it('draws nothing for an empty preview', () => {
    const ctx = makeCtx();
    new RendererTrackHandler(makeRenderer(ctx)).drawPreviewTracks([]);
    new RendererTrackHandler(makeRenderer(ctx)).drawPreviewTracks(null);
    expect(ctx.stroke).not.toHaveBeenCalled();
  });
});

describe('RendererTrackHandler.drawTileExtentHighlight', () => {
  it('lights the track stroke and caps the run ends (no cell fill for track tiles)', () => {
    // Two stacked NS tiles. getActiveSegments is mocked to [['N','S']];
    // trackBitsToEdges(1) is real -> {N, S}. (0,0)'s N neighbor is (0,1) and
    // (0,1)'s S neighbor is (0,0) — both in the set — so only the two outer
    // ends of the run get a cap.
    mocks.getActiveSegments.mockReturnValue([['N', 'S']]);
    const ctx = makeCtx();
    const r = makeRenderer(ctx);
    r.trackMap = { '0,0': { track_bits: 1 }, '0,1': { track_bits: 1 } };
    new RendererTrackHandler(r).drawTileExtentHighlight(new Set(['0,0', '0,1']), EXTENT_STYLE);
    expect(ctx.fill).not.toHaveBeenCalled(); // track tiles show as rail, not a diamond
    // 2 glow + 2 core segment strokes, then 2 end-cap ticks (run ends only).
    expect(ctx.stroke).toHaveBeenCalledTimes(6);
  });

  it('draws a dim diamond (and no stroke) only for a selected tile with no track', () => {
    const ctx = makeCtx();
    const r = makeRenderer(ctx); // trackMap is empty -> 3,3 carries no track
    new RendererTrackHandler(r).drawTileExtentHighlight(new Set(['3,3']), EXTENT_STYLE);
    expect(ctx.fill).toHaveBeenCalledTimes(1);
    expect(ctx.stroke).not.toHaveBeenCalled();
  });

  it('draws nothing for an empty set', () => {
    const ctx = makeCtx();
    new RendererTrackHandler(makeRenderer(ctx)).drawTileExtentHighlight(new Set(), EXTENT_STYLE);
    expect(ctx.fill).not.toHaveBeenCalled();
    expect(ctx.stroke).not.toHaveBeenCalled();
  });
});
