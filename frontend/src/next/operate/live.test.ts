import { afterEach, describe, expect, it } from 'vitest';

import { StateManager } from '../../lib/state-manager';
import { liveVersion, trainViews, trainsOnTile, watchLiveState } from './live';

function managerWith(network: Record<string, any>): StateManager {
  const sm = new StateManager();
  sm.updateFromNetwork(network);
  return sm;
}

const NETWORK = {
  trains: [{ id: 'T_B' }, { id: 'T_A' }],
  controllers_by_train_id: {
    T_A: { train_status: 'manual', span: { occupied_tiles: [[1, 2], [2, 2]] } },
    T_B: { train_status: 'off', span: null },
  },
  operator_status_by_train_id: { T_A: { ato_status: 'OFF', hold_reason: null } },
  assigned_routes_by_train_id: { T_A: 'R1', T_B: null },
};

describe('live trains', () => {
  let stop: (() => void) | null = null;
  afterEach(() => {
    stop?.();
    stop = null;
  });

  it('gathers each train from its sibling maps, ordered by id', () => {
    const views = trainViews(managerWith(NETWORK));
    expect(views.map((v) => v.id)).toEqual(['T_A', 'T_B']);
    expect(views[0].controller.train_status).toBe('manual');
    expect(views[0].assignedRouteId).toBe('R1');
    expect(views[1].assignedRouteId).toBeNull();
  });

  it('finds the train whose span covers a tile', () => {
    const sm = managerWith(NETWORK);
    expect(trainsOnTile(2, 2, sm)).toEqual(['T_A']);
    expect(trainsOnTile(5, 5, sm)).toEqual([]);
  });

  it('bumps liveVersion once for a poll that changes several entities', async () => {
    const sm = managerWith(NETWORK);
    stop = watchLiveState(sm);
    await Promise.resolve();
    const before = liveVersion.peek();
    sm.updateFromNetwork({
      ...NETWORK,
      controllers_by_train_id: {
        T_A: { train_status: 'ato', span: null },
        T_B: { train_status: 'manual', span: null },
      },
    });
    await Promise.resolve();
    expect(liveVersion.peek()).toBe(before + 1);
  });

  it('does not bump for a poll that changes nothing', async () => {
    const sm = managerWith(NETWORK);
    stop = watchLiveState(sm);
    await Promise.resolve();
    const before = liveVersion.peek();
    sm.updateFromNetwork(NETWORK);
    await Promise.resolve();
    expect(liveVersion.peek()).toBe(before);
  });
});
