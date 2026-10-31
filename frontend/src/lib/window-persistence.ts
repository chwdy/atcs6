// window-persistence.ts — where each floating window keeps its geometry and
// settings in localStorage: one state blob per window, keyed by id. The two
// right-panel pop-outs, the Train Monitor and the floating Virtual Bench use
// it.
//
// Mirrors the ThemeEnv / GridEnv env-injection idiom (see lib/theme.ts,
// lib/grid-preference.ts) so it unit-tests without a real browser. Does no
// window-lifecycle orchestration itself — each window calls load/save at its
// own lifecycle points, the same separation WindowManager keeps from storage
// entirely.

import type { WindowGeometry } from './window-manager';

export interface WindowPersistenceEnv {
  getStored(key: string): string | null;
  setStored(key: string, value: string): void;
}

/** WindowPersistenceEnv backed by real localStorage. Storage access is
 *  wrapped because it throws in private-mode browsers. */
export function browserWindowPersistenceEnv(): WindowPersistenceEnv {
  return {
    getStored: (key) => {
      try {
        return localStorage.getItem(key);
      } catch {
        return null;
      }
    },
    setStored: (key, value) => {
      try {
        localStorage.setItem(key, value);
      } catch {
        /* storage unavailable (private mode) — state applies this session only */
      }
    },
  };
}

/** In-memory WindowPersistenceEnv for tests and no-DOM contexts. */
export function memoryWindowPersistenceEnv(initial?: Record<string, string>): WindowPersistenceEnv {
  const store = new Map<string, string>(Object.entries(initial ?? {}));
  return {
    getStored: (key) => store.get(key) ?? null,
    setStored: (key, value) => {
      store.set(key, value);
    },
  };
}

const windowStateKey = (id: string): string => `window:${id}`;

/** Per-window state. */
export interface WindowState {
  geometry: WindowGeometry;
  floating: boolean;
  /** Only meaningful for a window that toggles visibility rather than being
   *  destroyed when "closed" (the Virtual Bench) — whether it was open the
   *  last time state was saved. */
  hidden?: boolean;
  /** Only for the Virtual Bench — its toolbar settings. */
  virtualBench?: {
    refreshHz: number;
    filter: string;
    showRaw: boolean;
  };
}

export function loadWindowState(env: WindowPersistenceEnv, id: string): WindowState | null {
  try {
    const raw = env.getStored(windowStateKey(id));
    if (!raw) {
      return null;
    }
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

export function saveWindowState(env: WindowPersistenceEnv, id: string, state: WindowState): void {
  env.setStored(windowStateKey(id), JSON.stringify(state));
}
