import { describe, expect, it, vi } from 'vitest';

import {
  buildStarterLoop,
  freeTrainId,
  ROUTE_SIDE_ORDER,
  starterLoopSides,
  TRAIN_START,
  type StarterLoopApi,
} from './starter-loop';

// The edges each track bit joins, and where each edge leads (north is y + 1).
const EDGES: Record<number, string[]> = { 1: ['N', 'S'], 2: ['E', 'W'], 4: ['N', 'E'], 8: ['N', 'W'], 16: ['S', 'E'], 32: ['S', 'W'] };
const STEP: Record<string, [number, number]> = { N: [0, 1], S: [0, -1], E: [1, 0], W: [-1, 0] };
const OPPOSITE: Record<string, string> = { N: 'S', S: 'N', E: 'W', W: 'E' };

function edgeToward(a: { x: number; y: number }, b: { x: number; y: number }): string | undefined {
  return Object.keys(STEP).find((e) => a.x + STEP[e][0] === b.x && a.y + STEP[e][1] === b.y);
}

describe('starterLoopSides', () => {
  it('is one closed loop: every tile joins the next, and the last joins the first', () => {
    const tiles = starterLoopSides().flatMap((s) => s.tiles);
    const keys = new Set(tiles.map((t) => `${t.x},${t.y}`));
    expect(keys.size).toBe(tiles.length);
    tiles.forEach((tile, i) => {
      const next = tiles[(i + 1) % tiles.length];
      const out = edgeToward(tile, next);
      expect(out, `tile ${tile.x},${tile.y} is next to ${next.x},${next.y}`).toBeDefined();
      expect(EDGES[tile.track_bits]).toContain(out);
      expect(EDGES[next.track_bits]).toContain(OPPOSITE[out!]);
    });
  });

  it('gives every side a length, since a model layout has no default one', () => {
    for (const side of starterLoopSides()) expect(side.length_cm).toBeGreaterThan(0);
  });

  it('starts the train on the top side, so the route goes round from the next side', () => {
    const top = starterLoopSides().find((s) => s.name === 'Loop top')!;
    expect(top.tiles.some((t) => t.x === TRAIN_START.x && t.y === TRAIN_START.y)).toBe(true);
    expect(ROUTE_SIDE_ORDER[ROUTE_SIDE_ORDER.length - 1]).toBe('Loop top');
  });
});

describe('freeTrainId', () => {
  it('takes the first LOOP_n not already used', () => {
    expect(freeTrainId([])).toBe('LOOP_1');
    expect(freeTrainId(['LOOP_1', 'LOOP_2', 'T9'])).toBe('LOOP_3');
  });
});

function fakeApi(): StarterLoopApi & { calls: string[] } {
  const calls: string[] = [];
  let n = 0;
  return {
    calls,
    addTracksBulk: vi.fn(async (tracks) => {
      calls.push(`tracks:${tracks.length}`);
    }),
    createSegment: vi.fn(async (_id, _positions, name) => {
      n += 1;
      calls.push(`segment:${name}`);
      return { segment_id: `SEG_${n}`, block_id: `BLK_${n}` };
    }),
    updateSegment: vi.fn(async (id, data) => {
      calls.push(`length:${id}:${data.length_cm}`);
    }),
    createTrain: vi.fn(async (train: any) => {
      calls.push(`train:${train.id}`);
    }),
    createRoute: vi.fn(async () => {
      calls.push('route');
      return { status: 'success', route: { route_id: 'ROUTE-1' } };
    }),
    assignRoute: vi.fn(async (trainId, routeId) => {
      calls.push(`assign:${trainId}:${routeId}`);
    }),
  };
}

describe('buildStarterLoop', () => {
  it('lays the track, makes and measures each side, places a train, and gives it the loop route', async () => {
    const api = fakeApi();
    const result = await buildStarterLoop(api, ['LOOP_1']);

    expect(result).toEqual({ trainId: 'LOOP_2', routeId: 'ROUTE-1' });
    expect(api.calls).toEqual([
      'tracks:40',
      'segment:Loop left', 'length:SEG_1:100',
      'segment:Loop top', 'length:SEG_2:200',
      'segment:Loop right', 'length:SEG_3:100',
      'segment:Loop bottom', 'length:SEG_4:200',
      'train:LOOP_2',
      'route',
      'assign:LOOP_2:ROUTE-1',
    ]);
    const route = (api.createRoute as any).mock.calls[0][0];
    expect(route.mode).toBe('circular');
    // Right, bottom, left, top: the blocks made for those sides, in that order.
    expect(route.tasks.map((t: any) => t.task_location.block_id)).toEqual(['BLK_3', 'BLK_4', 'BLK_1', 'BLK_2']);
    expect(route.tasks.every((t: any) => t.task_kind === 'waypoint')).toBe(true);
  });

  it('says which step failed', async () => {
    const api = fakeApi();
    api.createTrain = vi.fn(async () => {
      throw new Error('position is not on track');
    });
    await expect(buildStarterLoop(api)).rejects.toThrow('Could not place the train: position is not on track');
  });
});
