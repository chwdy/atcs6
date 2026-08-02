import { describe, it, expect } from 'vitest';

import { buildRenderSnapshot, type TrainController } from './render-snapshot';

// Vitest port of the old Node-eval tests/test_render_snapshot.py
// (test_snapshot_via_node). Same fixtures, same invariants, now exercised
// directly against the TypeScript buildRenderSnapshot.

const makeFixtures = () => {
  const network = {
    // T3 has no controller — it should be omitted.
    trains: [{ id: 'T1' }, { id: 'T2' }, { id: 'T3' }],
  };
  const controllers: Record<string, TrainController> = {
    T1: {
      cab: 'A',
      span: {
        occupied_tiles: [[0, 0], [1, 0]],
        leading_end: 'a',
        end_a: { tile: [0, 0], distance_cm: 10, entry_edge: 'W' },
        end_b: { tile: [1, 0], distance_cm: 90, entry_edge: 'E' },
      },
    },
    T2: {
      cab: null,
      span: {
        occupied_tiles: [[5, 5]],
        leading_end: 'b',
        end_a: { tile: [5, 5], distance_cm: 0, entry_edge: 'N' },
        end_b: { tile: [5, 5], distance_cm: 50, entry_edge: 'S' },
      },
    },
    // T3 absent.
  };
  const stateManager = {
    getController(id: string) {
      return controllers[id];
    },
  };
  return { network, controllers, stateManager };
};

describe('buildRenderSnapshot', () => {
  it('includes only trains with a live controller span (T1, T2; T3 omitted)', () => {
    const { network, stateManager } = makeFixtures();
    const snap = buildRenderSnapshot(network, stateManager);
    expect(Object.keys(snap.trains).sort()).toEqual(['T1', 'T2']);
  });

  it('pre-names span fields in camelCase for the renderer', () => {
    const { network, stateManager } = makeFixtures();
    const t1 = buildRenderSnapshot(network, stateManager).trains['T1'];
    expect(t1.id).toBe('T1');
    expect(t1.cab).toBe('A');
    expect(t1.span.occupiedTiles).toEqual([[0, 0], [1, 0]]);
    expect(t1.span.leadingEnd).toBe('a');
    expect(t1.span.endA).toEqual({ tile: [0, 0], distance_cm: 10, entry_edge: 'W' });
    expect(t1.span.endB).toEqual({ tile: [1, 0], distance_cm: 90, entry_edge: 'E' });
  });

  it('carries is_unidentified through as the isUnidentified render flag', () => {
    const { network, stateManager } = makeFixtures();
    (network.trains as Array<{ id: string; is_unidentified?: boolean }>)[1].is_unidentified = true;
    const snap = buildRenderSnapshot(network, stateManager);
    expect(snap.trains['T1'].isUnidentified).toBe(false);
    expect(snap.trains['T2'].isUnidentified).toBe(true);
  });

  it('passes through a null cab unchanged', () => {
    const { network, stateManager } = makeFixtures();
    const snap = buildRenderSnapshot(network, stateManager);
    expect(snap.trains['T2'].cab).toBeNull();
  });

  it('does not mutate its inputs', () => {
    const { network, controllers, stateManager } = makeFixtures();
    buildRenderSnapshot(network, stateManager);
    expect(network.trains.length).toBe(3);
    // The source controller still carries the original snake_case span fields.
    expect(controllers.T1.span?.occupied_tiles).toEqual([[0, 0], [1, 0]]);
    expect(controllers.T1.cab).toBe('A');
  });

  it('returns an empty trains map when stateManager is missing', () => {
    const { network } = makeFixtures();
    expect(buildRenderSnapshot(network, null).trains).toEqual({});
  });

  it('omits trains whose controller has no span', () => {
    const { network } = makeFixtures();
    const noSpanCtrl = { getController: () => ({ cab: 'A', span: null }) };
    expect(buildRenderSnapshot(network, noSpanCtrl).trains).toEqual({});
  });

  it('omits trains whose span has no occupied tiles', () => {
    const { network } = makeFixtures();
    const emptyTilesCtrl = {
      getController(id: string) {
        if (id === 'T1') return { cab: 'A', span: { occupied_tiles: [] } };
        return null;
      },
    };
    expect(buildRenderSnapshot(network, emptyTilesCtrl).trains).toEqual({});
  });

  // Coverage the old Python harness lacked: the dispatcher-progress path and
  // facing pass-through.
  it('surfaces facing and the dispatcher waiting-for-block id when present', () => {
    const network = { trains: [{ id: 'T1' }] };
    const stateManager = {
      getController: () => ({
        cab: 'A',
        facing_direction: 'NORTH',
        span: { occupied_tiles: [[0, 0]] },
      }),
      getDispatcherProgress: () => ({ waiting_for_block_id: 'B7' }),
    };
    const t1 = buildRenderSnapshot(network, stateManager).trains['T1'];
    expect(t1.facing).toBe('NORTH');
    expect(t1.waitingForBlockId).toBe('B7');
  });
});
