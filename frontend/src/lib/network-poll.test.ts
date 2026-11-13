import { describe, expect, it } from 'vitest';

import { NetworkPoller, withLiveFields } from './network-poll';

function fullReply(version: string, overrides: Record<string, any> = {}) {
  return {
    layout_version: version,
    layout_unchanged: false,
    simulation_time: 1,
    trains: [{ id: 'T1' }],
    tracks: [
      { x: 0, y: 0, type: 'track', is_switch: false, switch_state: null },
      { x: 1, y: 0, type: 'switch', is_switch: true, switch_state: 0 },
    ],
    blocks: {
      B1: { block_id: 'B1', ordered_tiles: [[0, 0, 'E']], reads_occupied: false, train_id: null },
      B2: { block_id: 'B2', ordered_tiles: [[1, 0, 'E']], reads_occupied: false, train_id: null },
    },
    switches: { SW1: { switch_id: 'SW1', x: 1, y: 0, position: 0 } },
    tunnels: [],
    ...overrides,
  };
}

function liveReply(version: string, overrides: Record<string, any> = {}) {
  return {
    layout_version: version,
    layout_unchanged: true,
    simulation_time: 2,
    trains: [{ id: 'T1' }],
    live_blocks: {
      B1: { reads_occupied: false, train_id: null },
      B2: { reads_occupied: false, train_id: null },
    },
    live_switches: { SW1: { position: 0 } },
    live_tracks: [{ x: 1, y: 0, switch_state: 0 }],
    ...overrides,
  };
}

function pollerWith(replies: any[]) {
  const versionsSent: (string | undefined)[] = [];
  const poller = new NetworkPoller(async (version) => {
    versionsSent.push(version);
    return replies.shift();
  });
  return { poller, versionsSent };
}

describe('withLiveFields', () => {
  it('keeps the same object when no live field changed', () => {
    const cached = { block_id: 'B1', state: 'free', reservation: { path_id: 'P' } };
    expect(withLiveFields(cached, { state: 'free', reservation: { path_id: 'P' } })).toBe(cached);
  });

  it('returns a new object with the live fields laid over when one changed', () => {
    const cached = { block_id: 'B1', state: 'free', ordered_tiles: [[0, 0, 'E']] };
    const merged = withLiveFields(cached, { state: 'occupied' });
    expect(merged).not.toBe(cached);
    expect(merged).toEqual({ block_id: 'B1', state: 'occupied', ordered_tiles: [[0, 0, 'E']] });
    expect(merged.ordered_tiles).toBe(cached.ordered_tiles);
  });
});

describe('NetworkPoller', () => {
  it('asks for the full network first, then passes the version it holds', async () => {
    const { poller, versionsSent } = pollerWith([fullReply('v1'), liveReply('v1')]);
    const first = await poller.poll();
    const second = await poller.poll();
    expect(versionsSent).toEqual([undefined, 'v1']);
    expect(first.layoutChanged).toBe(true);
    expect(second.layoutChanged).toBe(false);
  });

  it('rebuilds the full network shape from a live-only reply', async () => {
    const { poller } = pollerWith([
      fullReply('v1'),
      liveReply('v1', { live_blocks: { B1: { reads_occupied: true, train_id: 'T1' }, B2: { reads_occupied: false, train_id: null } } }),
    ]);
    await poller.poll();
    const { network } = await poller.poll();
    expect(network.simulation_time).toBe(2);
    expect(network.blocks.B1).toEqual({ block_id: 'B1', ordered_tiles: [[0, 0, 'E']], reads_occupied: true, train_id: 'T1' });
    expect(network.tunnels).toEqual([]);
    expect(network.tracks).toHaveLength(2);
    expect(network.live_blocks).toBeUndefined();
  });

  it('reuses unchanged entities so the change check can skip them', async () => {
    const { poller } = pollerWith([
      fullReply('v1'),
      liveReply('v1'),
      liveReply('v1', { live_tracks: [{ x: 1, y: 0, switch_state: 1, signals: [] }] }),
    ]);
    const full = (await poller.poll()).network;
    const same = (await poller.poll()).network;
    expect(same.blocks.B1).toBe(full.blocks.B1);
    expect(same.tracks).toBe(full.tracks);
    expect(same.platforms).toBe(full.platforms);

    const thrown = (await poller.poll()).network;
    expect(thrown.tracks).not.toBe(full.tracks);
    expect(thrown.tracks[0]).toBe(full.tracks[0]);
    expect(thrown.tracks[1].switch_state).toBe(1);
    expect(thrown.tracks[1].type).toBe('switch');
  });

  it('takes a full reply when the layout changed', async () => {
    const { poller, versionsSent } = pollerWith([
      fullReply('v1'),
      fullReply('v2', { platforms: [] }),
      liveReply('v2'),
    ]);
    await poller.poll();
    const changed = await poller.poll();
    expect(changed.layoutChanged).toBe(true);
    expect(changed.network.platforms).toEqual([]);
    await poller.poll();
    expect(versionsSent).toEqual([undefined, 'v1', 'v2']);
  });

  it('asks for the full network when a live-only reply names another version', async () => {
    const { poller, versionsSent } = pollerWith([fullReply('v1'), liveReply('v0'), fullReply('v1')]);
    await poller.poll();
    const result = await poller.poll();
    expect(versionsSent).toEqual([undefined, 'v1', undefined]);
    expect(result.layoutChanged).toBe(true);
    expect(result.network.blocks.B1.block_id).toBe('B1');
  });
});
