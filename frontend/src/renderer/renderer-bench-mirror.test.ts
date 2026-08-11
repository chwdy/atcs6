// Covers the Occupancy Bench overlay: the block-state rewrite the renderer
// applies while the bench holds the device (renderer.ts `_withBenchOccupancy`,
// lib/bench-occupancy-mirror).

import { describe, it, expect, afterEach } from 'vitest';
import { _withBenchOccupancy } from './renderer';
import {
  setBenchOccupancyMirror,
  benchOccupancyMirror,
  benchOccupancyMirrorActive,
} from '../lib/bench-occupancy-mirror';

afterEach(() => setBenchOccupancyMirror(null));

describe('bench occupancy mirror', () => {
  it('starts inactive and publishes nothing', () => {
    expect(benchOccupancyMirrorActive()).toBe(false);
    expect(benchOccupancyMirror().size).toBe(0);
  });

  it('clears back to inactive on null', () => {
    setBenchOccupancyMirror([['BLK_A', true]]);
    expect(benchOccupancyMirrorActive()).toBe(true);
    setBenchOccupancyMirror(null);
    expect(benchOccupancyMirrorActive()).toBe(false);
  });
});

describe('_withBenchOccupancy', () => {
  const blocks = () => ({
    BLK_A: { block_id: 'BLK_A', state: 'free' },
    BLK_B: { block_id: 'BLK_B', state: 'occupied' },
  });

  it('returns the payload untouched when no bench is publishing', () => {
    const input = blocks();
    // Same object, not just an equal one: the no-bench path is the one that
    // runs on every 200 ms refresh forever, and it must not allocate.
    expect(_withBenchOccupancy(input)).toBe(input);
  });

  it('paints a block the bench calls occupied', () => {
    setBenchOccupancyMirror([['BLK_A', true]]);
    expect(_withBenchOccupancy(blocks()).BLK_A.state).toBe('occupied');
  });

  it('clears a block the bench calls free, overriding a stale committed state', () => {
    // BLK_B was committed occupied before the handover. The runtime is not
    // detecting any more, so the bench's verdict is the only live one.
    setBenchOccupancyMirror([['BLK_B', false]]);
    expect(_withBenchOccupancy(blocks()).BLK_B.state).toBe('free');
  });

  it('leaves blocks the bench says nothing about alone', () => {
    setBenchOccupancyMirror([['BLK_A', true]]);
    expect(_withBenchOccupancy(blocks()).BLK_B.state).toBe('occupied');
  });

  it('ignores a verdict for a block this layout does not have', () => {
    setBenchOccupancyMirror([['BLK_GONE', true]]);
    const out = _withBenchOccupancy(blocks());
    expect(out.BLK_GONE).toBeUndefined();
    expect(Object.keys(out).sort()).toEqual(['BLK_A', 'BLK_B']);
  });

  it('never mutates the payload it was handed', () => {
    // The same block objects are held by stateManager and the editor's cached
    // network — a diagnostic overlay written in place would reach both.
    const input = blocks();
    const before = input.BLK_A;
    setBenchOccupancyMirror([['BLK_A', true]]);
    _withBenchOccupancy(input);
    expect(input.BLK_A).toBe(before);
    expect(input.BLK_A.state).toBe('free');
  });

  it('preserves availability so disabled/manual still win at paint time', () => {
    const input = { BLK_A: { block_id: 'BLK_A', state: 'free', disabled: true } };
    setBenchOccupancyMirror([['BLK_A', true]]);
    const out = _withBenchOccupancy(input);
    expect(out.BLK_A.disabled).toBe(true);
    expect(out.BLK_A.state).toBe('occupied');
  });
});
