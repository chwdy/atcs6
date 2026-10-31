/**
 * Polling for the panels that need data the network snapshot does
 * not carry (event log, diagnostics, benches, route runs).
 *
 * A panel starts polling when it appears and stops when it goes away, so a
 * panel nobody can see never asks the server for anything. This replaces the
 * one-timer-per-panel pattern of today's panels (docs/ui-layout-rewrite-plan.md
 * §10).
 */
import { useEffect, useRef } from 'preact/hooks';

/**
 * Call `poll` now and then every `periodMs`. A tick is skipped while the
 * browser tab is hidden, and while the previous call is still running, so a
 * slow server never piles requests up. Returns a function that stops it.
 */
export function pollWhileShown(
  poll: () => void | Promise<void>,
  periodMs: number,
  doc: Pick<Document, 'visibilityState'> = document
): () => void {
  let stopped = false;
  let running = false;
  const tick = async () => {
    if (stopped || running || doc.visibilityState === 'hidden') return;
    running = true;
    try {
      await poll();
    } finally {
      running = false;
    }
  };
  void tick();
  const timer = setInterval(() => void tick(), periodMs);
  return () => {
    stopped = true;
    clearInterval(timer);
  };
}

/** Poll while the calling component is on screen. `poll` may change between
 *  renders; the latest one is called. */
export function usePoll(poll: () => void | Promise<void>, periodMs: number): void {
  const latest = useRef(poll);
  latest.current = poll;
  useEffect(() => pollWhileShown(() => latest.current(), periodMs), [periodMs]);
}
