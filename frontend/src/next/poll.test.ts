import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { pollWhileShown } from './poll';

describe('pollWhileShown', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('polls at once, then every period, until stopped', async () => {
    const poll = vi.fn();
    const stop = pollWhileShown(poll, 1000, { visibilityState: 'visible' });
    expect(poll).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(poll).toHaveBeenCalledTimes(3);
    stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(poll).toHaveBeenCalledTimes(3);
  });

  it('skips ticks while the browser tab is hidden', async () => {
    const doc = { visibilityState: 'hidden' as DocumentVisibilityState };
    const poll = vi.fn();
    const stop = pollWhileShown(poll, 1000, doc);
    await vi.advanceTimersByTimeAsync(3000);
    expect(poll).not.toHaveBeenCalled();
    doc.visibilityState = 'visible';
    await vi.advanceTimersByTimeAsync(1000);
    expect(poll).toHaveBeenCalledTimes(1);
    stop();
  });

  it('never starts a call while the previous one is still running', async () => {
    let finish: () => void = () => {};
    const poll = vi.fn(() => new Promise<void>((resolve) => (finish = resolve)));
    const stop = pollWhileShown(poll, 1000, { visibilityState: 'visible' });
    await vi.advanceTimersByTimeAsync(3000);
    expect(poll).toHaveBeenCalledTimes(1);
    finish();
    await vi.advanceTimersByTimeAsync(1000);
    expect(poll).toHaveBeenCalledTimes(2);
    stop();
  });
});
