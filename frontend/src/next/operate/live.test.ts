import { afterEach, describe, expect, it } from 'vitest';

import { StateManager } from '../../lib/state-manager';
import { liveVersion, watchLiveState } from './live';

function managerWith(network: Record<string, any>): StateManager {
  const sm = new StateManager();
  sm.updateFromNetwork(network);
  return sm;
}

const NETWORK = {
  running_trains: { T_A: { block_id: 'B1' }, T_B: { block_id: 'B2' } },
  blocks: { B1: { reads_occupied: true }, B2: { reads_occupied: false } },
};

describe('live state', () => {
  let stop: (() => void) | null = null;
  afterEach(() => {
    stop?.();
    stop = null;
  });

  it('bumps liveVersion once for a poll that changes several entities', async () => {
    const sm = managerWith(NETWORK);
    stop = watchLiveState(sm);
    await Promise.resolve();
    const before = liveVersion.peek();
    sm.updateFromNetwork({
      ...NETWORK,
      blocks: { B1: { reads_occupied: false }, B2: { reads_occupied: true } },
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
