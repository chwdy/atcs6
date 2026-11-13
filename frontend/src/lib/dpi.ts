// DPI awareness for window chrome — a small, narrowly-scoped utility.
//
// Canvas rendering already scales its backing store by devicePixelRatio in a
// few places (the tile editor's track-bits compass) for crisp drawing. Nothing does the equivalent for
// floating-window chrome: resize-handle hit areas, drag handles, and window
// geometry stay in plain CSS px today, and nothing re-checks them if the
// browser moves to a different-density monitor. This module answers exactly
// two questions — "what's the current pixel ratio" and "should hit-targets be
// bigger" — and nothing else. It does not implement zoom, does not compute a
// layout scale factor, and does not touch font sizes; CSS px already do the
// right thing for layout, this is purely a crispness/hit-target concern.
//
// Mirrors the ThemeEnv / GridEnv env-injection idiom (see theme.ts,
// grid-preference.ts) so it unit-tests without a real browser.

/** What a MediaQueryList needs to provide for watchDprChange's re-subscribe
 *  trick. Matches the real MediaQueryList surface we use. */
export interface DpiMediaQueryList {
  readonly matches: boolean;
  addEventListener(type: 'change', listener: () => void): void;
  removeEventListener(type: 'change', listener: () => void): void;
}

/** Environment DPI helpers need, injected so they unit-test without a real
 *  window (see memoryDpiEnv and dpi.test.ts). browserDpiEnv() binds it to
 *  window.devicePixelRatio + window.matchMedia. */
export interface DpiEnv {
  devicePixelRatio(): number;
  /** Returns null when matchMedia is unavailable (older browsers, non-DOM
   *  contexts) — callers treat that as "can't watch, but can still read." */
  matchMedia(query: string): DpiMediaQueryList | null;
}

/** DpiEnv backed by the real window. devicePixelRatio defaults to 1 when
 *  absent (non-browser contexts); matchMedia is wrapped because it doesn't
 *  exist in every environment this code can run in. */
export function browserDpiEnv(): DpiEnv {
  return {
    devicePixelRatio: () => (typeof window !== 'undefined' ? window.devicePixelRatio || 1 : 1),
    matchMedia: (query) => {
      if (typeof window === 'undefined' || !window.matchMedia) {
        return null;
      }
      return window.matchMedia(query);
    },
  };
}

/** In-memory DpiEnv for tests. Its matchMedia understands two query shapes —
 *  `(resolution: <n>dppx)` (the exact query watchDprChange subscribes with)
 *  and `(pointer: coarse)` — enough to exercise both real call sites without
 *  a DOM. setDpr()/setCoarsePointer() simulate the environment changing and
 *  fire any listeners a resolution query's matches flipped for. */
export function memoryDpiEnv(initial?: {
  dpr?: number;
  coarsePointer?: boolean;
}): DpiEnv & { setDpr(next: number): void; setCoarsePointer(next: boolean): void } {
  let dpr = initial?.dpr ?? 1;
  let coarsePointer = initial?.coarsePointer ?? false;
  const resolutionQueries = new Set<{ target: number; matches: boolean; listeners: Set<() => void> }>();

  const setDpr = (next: number): void => {
    dpr = next;
    resolutionQueries.forEach((q) => {
      const nowMatches = q.target === dpr;
      if (nowMatches !== q.matches) {
        q.matches = nowMatches;
        q.listeners.forEach((fn) => fn());
      }
    });
  };

  return {
    devicePixelRatio: () => dpr,
    matchMedia: (query: string) => {
      const resolutionMatch = /resolution:\s*([\d.]+)dppx/.exec(query);
      if (resolutionMatch) {
        const state = { target: parseFloat(resolutionMatch[1]), matches: false, listeners: new Set<() => void>() };
        state.matches = state.target === dpr;
        resolutionQueries.add(state);
        return {
          get matches() {
            return state.matches;
          },
          addEventListener: (_type: 'change', fn: () => void) => {
            state.listeners.add(fn);
          },
          removeEventListener: (_type: 'change', fn: () => void) => {
            state.listeners.delete(fn);
          },
        };
      }
      if (query.includes('pointer: coarse')) {
        return {
          matches: coarsePointer,
          addEventListener: () => {},
          removeEventListener: () => {},
        };
      }
      return null;
    },
    setDpr,
    setCoarsePointer: (next: boolean) => {
      coarsePointer = next;
    },
  };
}

/** Current device pixel ratio, defaulting to 1 when unavailable. Same value
 *  the existing canvas-crispness call sites read from window.devicePixelRatio
 *  directly — this is a convenience wrapper for chrome code that also wants
 *  it, not a replacement for the canvas usages. */
export function currentDpr(env: DpiEnv = browserDpiEnv()): number {
  return env.devicePixelRatio();
}

/** True when the primary pointer is coarse (touch/stylus — the expected input
 *  for a hardware control panel's touchscreen), via matchMedia('(pointer:
 *  coarse)'). Used to size hit-targets up, not to change layout wholesale. */
export function isCoarsePointer(env: DpiEnv = browserDpiEnv()): boolean {
  return env.matchMedia('(pointer: coarse)')?.matches ?? false;
}

/** Scale factor for a base hit-target size (resize handles, drag handles)
 *  under a coarse pointer. Pure — takes the already-measured boolean, not an
 *  env, so it colocated-tests as a trivial table. Doubling is a starting
 *  heuristic (today's 6-14px handles vs. ~44px touch-target guidance); tune
 *  this once real hardware panels exist to measure against. */
export function coarsePointerScale(isCoarse: boolean): number {
  return isCoarse ? 2 : 1;
}

/** Subscribe to a DPI *change* — e.g. the browser window is dragged from a 1x
 *  monitor to a 2x monitor, or a hardware panel's display mode switches.
 *
 *  Implemented with the standard matchMedia(`(resolution: ${dpr}dppx)`)
 *  re-subscribe trick: a resolution media query keeps matching only while the
 *  ratio it names is the current one, so it fires a `change` event the
 *  instant the real ratio moves away from that value. On each fire we read
 *  the new ratio, notify the caller, and re-subscribe at the new value so the
 *  *next* change fires too (a single query only ever flips once, from true to
 *  false, so without re-subscribing this would only ever fire once).
 *
 *  Returns an unsubscribe function. No-ops (returns a no-op unsubscribe) when
 *  matchMedia is unavailable. */
export function watchDprChange(onChange: (dpr: number) => void, env: DpiEnv = browserDpiEnv()): () => void {
  let activeQuery: DpiMediaQueryList | null = null;
  let activeListener: (() => void) | null = null;
  let stopped = false;

  const subscribeAtCurrentRatio = (): void => {
    const dpr = env.devicePixelRatio();
    const query = env.matchMedia(`(resolution: ${dpr}dppx)`);
    if (!query) {
      return;
    }
    activeQuery = query;
    activeListener = () => {
      if (stopped) {
        return;
      }
      activeQuery?.removeEventListener('change', activeListener!);
      onChange(env.devicePixelRatio());
      subscribeAtCurrentRatio();
    };
    activeQuery.addEventListener('change', activeListener);
  };

  subscribeAtCurrentRatio();

  return () => {
    stopped = true;
    if (activeQuery && activeListener) {
      activeQuery.removeEventListener('change', activeListener);
    }
  };
}
