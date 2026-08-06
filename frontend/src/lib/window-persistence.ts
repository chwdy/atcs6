// window-persistence.ts — the unified localStorage scheme for every floating
// window: a top-level registry (which windows exist + their kind) plus one
// state blob per window (geometry/floating/hidden + tab-group-or-diagnostics
// payload), keyed by id. This is what makes a spawned window (Phase 5)
// survive a reload, and replaces the two separate, ad-hoc keys the sidebar
// and diagnostics panel used before this system existed
// (secondaryInspectorStateV1, diagnosticsPanelStateV1) — both are abandoned
// outright. This is disposable UI-preference state, not user data, so a
// clean cutover is the right call; there is no migration shim.
//
// Mirrors the ThemeEnv / GridEnv env-injection idiom (see lib/theme.ts,
// lib/grid-preference.ts) so it unit-tests without a real browser. Does no
// window-lifecycle orchestration itself — EditorUIHandler (the home window +
// every spawned window) and panels/diagnostics.ts (the one non-tab-group
// consumer) each call load/save at their own lifecycle points, the same
// separation WindowManager keeps from storage entirely.

import type { WindowGeometry } from './window-manager';
import type { SerializedTabGroup } from '../editor/tab-group';

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

const REGISTRY_KEY = 'windowRegistryV1';
const windowStateKey = (id: string): string => `window:${id}`;

/** 'home' is the one dockable window; 'diagnostics' and 'train-monitor' are the
 *  non-tab-group singletons (both hide rather than destroy on close, and both
 *  always exist regardless of the registry); 'tab-group' is every window
 *  spawned by splitting a tab (Phase 5) — always floating, always holds a
 *  TabGroup. */
export type PersistedWindowKind =
  | 'home'
  | 'diagnostics'
  | 'tab-group'
  | 'train-monitor'
  | 'occupancy-monitor'
  | 'occupancy-bench'
  | 'virtual-bench'
  | 'event-log';

export interface WindowRegistryEntry {
  id: string;
  kind: PersistedWindowKind;
}

export interface WindowRegistry {
  windows: WindowRegistryEntry[];
}

export function loadRegistry(env: WindowPersistenceEnv): WindowRegistry {
  try {
    const raw = env.getStored(REGISTRY_KEY);
    if (!raw) {
      return { windows: [] };
    }
    const parsed = JSON.parse(raw);
    const windows = Array.isArray(parsed?.windows) ? parsed.windows : [];
    return {
      windows: windows.filter(
        (w: any): w is WindowRegistryEntry => !!w && typeof w.id === 'string' && typeof w.kind === 'string'
      ),
    };
  } catch {
    return { windows: [] };
  }
}

export function saveRegistry(env: WindowPersistenceEnv, registry: WindowRegistry): void {
  env.setStored(REGISTRY_KEY, JSON.stringify(registry));
}

/** Per-window state. `tabGroup` is present for 'home' and 'tab-group' kinds;
 *  `diagnostics` is present for the 'diagnostics' kind — never both, but
 *  both are optional rather than a discriminated union so a caller can read
 *  `state.tabGroup` without a kind check first. */
export interface WindowState {
  geometry: WindowGeometry;
  floating: boolean;
  /** Only meaningful for a window that toggles visibility rather than being
   *  destroyed when "closed" (diagnostics) — whether it was open the last
   *  time state was saved. */
  hidden?: boolean;
  tabGroup?: SerializedTabGroup;
  diagnostics?: {
    trainId: string | null;
    refreshHz: number;
    sortMode: 'asc' | 'desc' | null;
    collapsedGroups: string[];
  };
  /** Only for the 'occupancy-monitor' kind — its toolbar settings. */
  occupancyMonitor?: {
    refreshHz: number;
    filter: string;
  };
  /** Only for the 'virtual-bench' kind — its toolbar settings. */
  virtualBench?: {
    refreshHz: number;
    filter: string;
    showRaw: boolean;
  };
  /** Only for the 'event-log' kind — its toolbar settings. The entity and
   *  free-text filters are deliberately not persisted: they are a drill-down
   *  into one investigation, and restoring one silently would show a filtered
   *  stream that looks like an empty one. */
  eventLog?: {
    refreshHz: number;
    level: string;
    persisted: boolean;
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

/** Register a window in the top-level registry if it isn't already listed —
 *  idempotent, safe to call on every save. */
export function ensureRegistered(env: WindowPersistenceEnv, id: string, kind: PersistedWindowKind): void {
  const registry = loadRegistry(env);
  if (registry.windows.some((w) => w.id === id)) {
    return;
  }
  registry.windows.push({ id, kind });
  saveRegistry(env, registry);
}

/** Drop a window from the registry — called when a spawned window is
 *  destroyed, so its stale `window:<id>` entry (left in storage, harmless
 *  but unbounded otherwise) is never read again. */
export function unregister(env: WindowPersistenceEnv, id: string): void {
  const registry = loadRegistry(env);
  const next = registry.windows.filter((w) => w.id !== id);
  if (next.length !== registry.windows.length) {
    saveRegistry(env, { windows: next });
  }
}
