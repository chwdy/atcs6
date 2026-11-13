import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { NetworkPoller } from './network-poll';
import { NetworkStream, type EventSourceLike } from './network-stream';

function fullReply(version: string, state = 'free') {
  return {
    layout_version: version,
    layout_unchanged: false,
    trains: [{ id: 'T1' }],
    tracks: [{ x: 0, y: 0, is_switch: false }],
    blocks: { B1: { block_id: 'B1', ordered_tiles: [[0, 0, 'E']], state, occupied_by: null } },
    switches: {},
    tunnels: [],
  };
}

function liveReply(version: string, state = 'free') {
  return {
    layout_version: version,
    layout_unchanged: true,
    trains: [{ id: 'T1' }],
    live_blocks: { B1: { state, occupied_by: state === 'occupied' ? 'T1' : null } },
    live_switches: {},
    live_tracks: [],
  };
}

class FakeSource implements EventSourceLike {
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  closed = false;
  constructor(readonly url: string) {}
  close() {
    this.closed = true;
  }
  send(reply: unknown) {
    this.onmessage?.({ data: JSON.stringify(reply) });
  }
  fail() {
    this.onerror?.(new Event('error'));
  }
}

function fakeDoc() {
  const listeners = new Set<() => void>();
  return {
    visibilityState: 'visible' as DocumentVisibilityState,
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
    show(state: DocumentVisibilityState) {
      this.visibilityState = state;
      for (const fn of listeners) fn();
    },
  };
}

function setup() {
  const sources: FakeSource[] = [];
  const poller = new NetworkPoller(async () => fullReply('v1'));
  const onMessage = vi.fn();
  const pollOnce = vi.fn();
  const doc = fakeDoc();
  const stream = new NetworkStream({
    poller,
    onMessage,
    pollOnce,
    doc: doc as any,
    openEventSource: (url) => {
      const source = new FakeSource(url);
      sources.push(source);
      return source;
    },
  });
  const latest = () => sources[sources.length - 1];
  return { sources, latest, poller, onMessage, pollOnce, doc, stream };
}

describe('NetworkStream', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('applies each pushed message over the cached layout', async () => {
    const { latest, onMessage, pollOnce, stream } = setup();
    stream.start();
    expect(latest().url).toBe('/api/network/stream');

    latest().send(fullReply('v1'));
    expect(onMessage).toHaveBeenLastCalledWith(
      expect.objectContaining({ layoutChanged: true })
    );

    latest().send(liveReply('v1', 'occupied'));
    const { network, layoutChanged } = onMessage.mock.calls[1][0];
    expect(layoutChanged).toBe(false);
    expect(network.tracks).toHaveLength(1);
    expect(network.blocks.B1).toMatchObject({
      block_id: 'B1',
      ordered_tiles: [[0, 0, 'E']],
      state: 'occupied',
      occupied_by: 'T1',
    });
    expect(network.live_blocks).toBeUndefined();

    await vi.advanceTimersByTimeAsync(1000);
    expect(pollOnce).not.toHaveBeenCalled();
    expect(stream.isPolling).toBe(false);
  });

  it('polls while the stream is down and stops once a new stream delivers', async () => {
    const { sources, latest, onMessage, pollOnce, stream } = setup();
    stream.start();
    latest().send(fullReply('v1'));

    latest().fail();
    expect(sources[0].closed).toBe(true);
    expect(stream.isPolling).toBe(true);
    expect(pollOnce).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(pollOnce).toHaveBeenCalledTimes(6);

    // A new stream after 2 s, naming the layout already held.
    await vi.advanceTimersByTimeAsync(1000);
    expect(sources).toHaveLength(2);
    expect(latest().url).toBe('/api/network/stream?layout_version=v1');

    latest().send(liveReply('v1', 'occupied'));
    expect(stream.isPolling).toBe(false);
    expect(onMessage).toHaveBeenCalledTimes(2);
    const polls = pollOnce.mock.calls.length;
    await vi.advanceTimersByTimeAsync(1000);
    expect(pollOnce).toHaveBeenCalledTimes(polls);
  });

  it('keeps polling and keeps retrying while new streams fail', async () => {
    const { sources, latest, pollOnce, stream } = setup();
    stream.start();
    latest().fail();
    await vi.advanceTimersByTimeAsync(2000);
    expect(sources).toHaveLength(2);
    latest().fail();
    await vi.advanceTimersByTimeAsync(2000);
    expect(sources).toHaveLength(3);
    expect(stream.isPolling).toBe(true);
    expect(pollOnce.mock.calls.length).toBeGreaterThanOrEqual(20);
  });

  it('counts a stream that goes silent for 5 s as dropped', async () => {
    const { sources, latest, pollOnce, stream } = setup();
    stream.start();
    latest().send(fullReply('v1'));
    await vi.advanceTimersByTimeAsync(4900);
    expect(pollOnce).not.toHaveBeenCalled();
    latest().send(liveReply('v1'));
    await vi.advanceTimersByTimeAsync(4900);
    expect(pollOnce).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(200);
    expect(sources[0].closed).toBe(true);
    expect(stream.isPolling).toBe(true);
    expect(pollOnce).toHaveBeenCalled();
  });

  it('falls back to polling when the stream cannot even be created', async () => {
    const pollOnce = vi.fn();
    const stream = new NetworkStream({
      poller: new NetworkPoller(async () => fullReply('v1')),
      onMessage: vi.fn(),
      pollOnce,
      doc: fakeDoc() as any,
      openEventSource: () => {
        throw new Error('EventSource is not available');
      },
    });
    stream.start();
    expect(stream.isPolling).toBe(true);
    expect(pollOnce).toHaveBeenCalledTimes(1);
  });

  it('reads the full network for a live-only message about another layout', async () => {
    const { latest, onMessage, pollOnce, stream } = setup();
    stream.start();
    latest().send(fullReply('v1'));
    latest().send(liveReply('v2'));
    expect(onMessage).toHaveBeenCalledTimes(1);
    expect(pollOnce).toHaveBeenCalledTimes(1);
    // That is one read, not the fallback poll.
    expect(stream.isPolling).toBe(false);
  });

  it('shares the cache with one-off reads', async () => {
    const { latest, poller, onMessage, stream } = setup();
    await poller.poll();
    expect(poller.heldLayoutVersion).toBe('v1');
    stream.start();
    expect(latest().url).toBe('/api/network/stream?layout_version=v1');
    latest().send(liveReply('v1', 'occupied'));
    expect(onMessage.mock.calls[0][0].network.blocks.B1.state).toBe('occupied');
  });

  it('closes the stream while the tab is hidden and opens a new one when it shows', async () => {
    const { sources, latest, pollOnce, doc, stream } = setup();
    stream.start();
    latest().send(fullReply('v1'));

    doc.show('hidden');
    expect(sources[0].closed).toBe(true);
    await vi.advanceTimersByTimeAsync(10000);
    expect(sources).toHaveLength(1);
    expect(pollOnce).not.toHaveBeenCalled();

    doc.show('visible');
    expect(sources).toHaveLength(2);
    expect(latest().url).toBe('/api/network/stream?layout_version=v1');
  });

  it('opens nothing until the tab shows when started hidden', async () => {
    const { sources, doc, stream } = setup();
    doc.visibilityState = 'hidden';
    stream.start();
    expect(sources).toHaveLength(0);
    doc.show('visible');
    expect(sources).toHaveLength(1);
  });

  it('stop closes the stream, the polling and the retry', async () => {
    const { sources, latest, pollOnce, doc, stream } = setup();
    stream.start();
    latest().fail();
    expect(stream.isPolling).toBe(true);
    stream.stop();
    expect(stream.isPolling).toBe(false);
    const polls = pollOnce.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10000);
    expect(pollOnce).toHaveBeenCalledTimes(polls);
    expect(sources).toHaveLength(1);
    doc.show('visible');
    expect(sources).toHaveLength(1);
  });

  it('ignores events from a stream it already closed', async () => {
    const { sources, latest, onMessage, stream } = setup();
    stream.start();
    const first = latest();
    first.fail();
    await vi.advanceTimersByTimeAsync(2000);
    expect(sources).toHaveLength(2);
    first.onmessage = null;
    first.send(fullReply('v1'));
    expect(onMessage).not.toHaveBeenCalled();
  });
});
