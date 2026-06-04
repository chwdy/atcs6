import { describe, expect, it } from 'vitest';

import { Renderer } from './renderer';

// Coverage for the single-tile block-direction overlay fix: a block of one tile
// has its forward (F) and reverse (R) terminal arrows trace the same track
// segment in opposite directions, so without a lane shift their arrows and F/R
// pills land exactly on top of each other. drawBlockDirectionOverlay slides each
// into its own lane; the shift is floored by the pill size so the labels can
// never overlap, at any zoom.
//
// We drive the real Renderer methods through a plain `this` stub (the methods
// only touch a small, stubbable surface) and a recording 2D context.

type Rect = { x: number; y: number; w: number; h: number };

function makeCtx() {
  const roundRects: Rect[] = [];
  const texts: Array<{ text: string; x: number; y: number }> = [];
  const strokes: Array<{ x1: number; y1: number; x2: number; y2: number }> = [];
  let penX = 0;
  let penY = 0;
  const ctx = {
    save() {},
    restore() {},
    beginPath() {},
    closePath() {},
    moveTo(x: number, y: number) {
      penX = x;
      penY = y;
    },
    lineTo(x: number, y: number) {
      strokes.push({ x1: penX, y1: penY, x2: x, y2: y });
      penX = x;
      penY = y;
    },
    stroke() {},
    fill() {},
    measureText: (t: string) => ({ width: 6 * t.length }),
    roundRect(x: number, y: number, w: number, h: number) {
      roundRects.push({ x, y, w, h });
    },
    fillRect(x: number, y: number, w: number, h: number) {
      roundRects.push({ x, y, w, h });
    },
    fillText(text: string, x: number, y: number) {
      texts.push({ text, x, y });
    },
    font: '',
    textAlign: '',
    textBaseline: '',
    fillStyle: '',
    strokeStyle: '',
    lineWidth: 0,
    lineCap: '',
    lineJoin: '',
    globalAlpha: 1,
  };
  return { ctx, roundRects, texts, strokes };
}

// Faithful copy of RendererTrackHandler.getEdgePoints — the four iso-tile corner
// offsets the real arrow geometry reads.
const trackHandler = {
  getEdgePoints(h: number) {
    return {
      N: { x: h / 2, y: -h / 2 },
      E: { x: h / 2, y: h / 2 },
      S: { x: -h / 2, y: h / 2 },
      W: { x: -h / 2, y: -h / 2 },
    };
  },
};

// Tile center in screen space; distinct per tile so multi-tile cases separate.
const gridToScreenX = (x: number, y: number) => 100 + (x + y) * 20;
const gridToScreenY = (x: number, y: number) => 100 + (x - y) * 20;

function makeStub(rec: ReturnType<typeof makeCtx>, extra: Record<string, unknown>) {
  return {
    ctx: rec.ctx,
    scale: 1,
    gridSize: 40,
    gridToScreenX,
    gridToScreenY,
    trackHandler,
    _drawTileForwardArrow: (Renderer.prototype as any)._drawTileForwardArrow,
    ...extra,
  } as any;
}

// Two unique pill rectangles, in draw order (forward first, then reverse). Each
// pill draws its box twice (fill + stroke) at the same coords, so dedupe.
function pillRects(roundRects: Rect[]): Rect[] {
  const out: Rect[] = [];
  for (const r of roundRects) {
    if (!out.some((o) => o.x === r.x && o.y === r.y)) out.push(r);
  }
  return out;
}

function disjoint(a: Rect, b: Rect): boolean {
  return a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y;
}

const center = (r: Rect) => ({ x: r.x + r.w / 2, y: r.y + r.h / 2 });

describe('drawBlockDirectionOverlay — single-tile blocks', () => {
  // track_bits 2 = EW straight (N/S clear); the canonical single-tile segment.
  function runSingleTile() {
    const rec = makeCtx();
    const stub = makeStub(rec, {
      debugFlags: { showBlockDirections: true },
      currentBlocks: {
        B1: { ordered_tiles: [[0, 0, 'W']], allowed_travel_directions: ['forward', 'reverse'] },
      },
      trackMap: { '0,0': { track_bits: 2 } },
    });
    (Renderer.prototype as any).drawBlockDirectionOverlay.call(stub);
    return rec;
  }

  it('draws both the F and R pills', () => {
    const { texts } = runSingleTile();
    expect(texts.map((t) => t.text).sort()).toEqual(['F', 'R']);
  });

  it('separates the two pills so they do not overlap', () => {
    const rects = pillRects(runSingleTile().roundRects);
    expect(rects).toHaveLength(2);
    expect(disjoint(rects[0], rects[1])).toBe(true);
  });

  it('places the pills symmetrically about the tile center', () => {
    const rects = pillRects(runSingleTile().roundRects);
    const a = center(rects[0]);
    const b = center(rects[1]);
    // Tile (0,0) center is (100,100); the two lanes are equal and opposite.
    expect((a.x + b.x) / 2).toBeCloseTo(100, 6);
    expect((a.y + b.y) / 2).toBeCloseTo(100, 6);
    // And genuinely apart, not both at center.
    expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThan(10);
  });
});

describe('_drawTileForwardArrow — no lane shift without an offset', () => {
  it('centers a labelled pill on the tile when no laneOffset is given (multi-tile / legacy path)', () => {
    const rec = makeCtx();
    const stub = makeStub(rec, {});
    (Renderer.prototype as any)._drawTileForwardArrow.call(stub, 0, 0, 'W', 'E', '#04d361', {
      letter: 'F',
    });
    const rects = pillRects(rec.roundRects);
    expect(rects).toHaveLength(1);
    const c = center(rects[0]);
    expect(c.x).toBeCloseTo(100, 6);
    expect(c.y).toBeCloseTo(100, 6);
  });

  it('keeps the plain per-tile arrow on the exact edge midpoints (no pill, no shift)', () => {
    const rec = makeCtx();
    const stub = makeStub(rec, {});
    (Renderer.prototype as any)._drawTileForwardArrow.call(stub, 0, 0, 'W', 'E', '#04d361', {});
    // No letter => no pill box drawn.
    expect(rec.roundRects).toHaveLength(0);
    // Shaft starts at the W edge midpoint of tile (0,0): (100,100)+(-10,-10).
    expect(rec.strokes[0].x1).toBeCloseTo(90, 6);
    expect(rec.strokes[0].y1).toBeCloseTo(90, 6);
  });
});
