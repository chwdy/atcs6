import { describe, expect, it, vi } from 'vitest';
import { TRACK_BEND_RADIUS } from '../lib/constants';
import { traceTrackLine } from './track-path';

function makeCtx() {
  return { moveTo: vi.fn(), lineTo: vi.fn(), arcTo: vi.fn(), closePath: vi.fn() };
}
const trace = (ctx: ReturnType<typeof makeCtx>, points: number[][], closed = false, scale = 1) =>
  traceTrackLine(ctx as unknown as CanvasRenderingContext2D, points.map(([x, y]) => ({ x, y })), closed, scale);

describe('traceTrackLine', () => {
  it('draws a straight run with no arcs, through every point', () => {
    const ctx = makeCtx();
    trace(ctx, [[0, 0], [20, 0], [40, 0]]);
    expect(ctx.moveTo).toHaveBeenCalledWith(0, 0);
    expect(ctx.arcTo).not.toHaveBeenCalled();
    expect(ctx.lineTo.mock.calls).toEqual([[20, 0], [40, 0]]);
  });

  it('rounds a bend with the track radius when the pieces are long enough', () => {
    const ctx = makeCtx();
    trace(ctx, [[0, 0], [100, 0], [200, 100]]);
    expect(ctx.arcTo).toHaveBeenCalledWith(100, 0, 200, 100, TRACK_BEND_RADIUS);
  });

  it('uses at most half of the shorter piece beside a bend', () => {
    // A right-angle bend between two 20-long pieces: the arc may start 10
    // back from the corner, which for a right angle is a radius of 10.
    const ctx = makeCtx();
    trace(ctx, [[0, 0], [20, 0], [20, 20]]);
    expect(ctx.arcTo.mock.calls[0][4]).toBeCloseTo(10);
  });

  it('closes a loop and rounds every corner of it', () => {
    const ctx = makeCtx();
    trace(ctx, [[0, 0], [100, 0], [100, 100], [0, 100]], true);
    expect(ctx.moveTo).toHaveBeenCalledWith(0, 50);
    expect(ctx.arcTo).toHaveBeenCalledTimes(4);
    expect(ctx.closePath).toHaveBeenCalledTimes(1);
  });

  it('skips a repeated point, which has no direction', () => {
    const ctx = makeCtx();
    trace(ctx, [[0, 0], [20, 0], [20, 0], [40, 0]]);
    expect(ctx.lineTo.mock.calls).toEqual([[20, 0], [40, 0]]);
  });
});
