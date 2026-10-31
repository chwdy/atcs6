/**
 * Polling for a Logs panel tab at the refresh rate its own picker chose.
 *
 * Built on `pollWhileShown` (next/poll.ts): a tab starts asking the server
 * when it appears and stops when it goes away, so only the tab on show ever
 * polls, and a closed panel polls nothing.
 */
import { useEffect, useRef } from 'preact/hooks';

import { pollWhileShown } from '../poll';

/**
 * Call `poll` at `hz` while the calling component is on screen. At 0 Hz (the
 * "Paused" / "On-demand" choice) it reads once when the tab appears or the
 * rate changes, and after that only when the tab asks (its refresh button).
 * `minPeriodMs` caps how fast a high rate may go.
 */
export function useRatePoll(poll: () => void | Promise<void>, hz: number, minPeriodMs = 50): void {
  const latest = useRef(poll);
  latest.current = poll;
  useEffect(() => {
    if (hz > 0) {
      const period = Math.max(minPeriodMs, Math.round(1000 / hz));
      return pollWhileShown(() => latest.current(), period);
    }
    void latest.current();
    return undefined;
  }, [hz, minPeriodMs]);
}

/**
 * Wrap an async read so a second call while one is still running gets the
 * running one back instead of starting another (a refresh button pressed
 * during a timer read, say). The polling timer already skips overlapping
 * ticks; this covers the reads a tab starts itself.
 */
export function singleFlight(read: () => Promise<void>): () => Promise<void> {
  let running: Promise<void> | null = null;
  return () => {
    if (running) return running;
    running = read().finally(() => {
      running = null;
    });
    return running;
  };
}
