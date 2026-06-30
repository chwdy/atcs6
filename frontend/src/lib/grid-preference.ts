// Background-grid visibility — a single persisted boolean controlling whether
// the layout renderer paints its diamond background grid. This is NOT a theme
// (no colors, no registry of named options), so it is deliberately not a
// ThemeChannel; it is its own concept with its own localStorage key. It mirrors
// the theme channels' env-injection so it unit-tests without a DOM.
//
// Data flow: the renderer reads gridPreference.visible() when it paints the
// world layer; the Display popover flips it via .toggle(); theme.ts subscribes a
// renderer redraw so a toggle repaints the layout immediately. Hiding the grid
// hides the whole coordinate layer together — the repeating diamond lines, the
// origin axes, and the origin marker.

const GRID_KEY = 'atcs6-canvas-grid';
const VISIBLE = 'visible';
const HIDDEN = 'hidden';

/** Storage the preference needs from its environment. Injecting this keeps the
 *  preference testable without a DOM (see memoryGridEnv and grid-preference.test). */
export interface GridEnv {
  getStored(): string | null;
  setStored(value: string): void;
}

/** GridEnv backed by real localStorage. Storage access is wrapped because it
 *  throws in private-mode browsers and the global is absent in node tests; in
 *  both cases the grid falls back to visible. */
export function browserGridEnv(storageKey: string = GRID_KEY): GridEnv {
  return {
    getStored: () => {
      try {
        return localStorage.getItem(storageKey);
      } catch {
        return null;
      }
    },
    setStored: (value) => {
      try {
        localStorage.setItem(storageKey, value);
      } catch {
        /* storage unavailable (private mode) — applies for this session only */
      }
    },
  };
}

/** In-memory GridEnv for tests and no-DOM contexts. */
export function memoryGridEnv(initial?: string | null): GridEnv {
  let stored = initial ?? null;
  return {
    getStored: () => stored,
    setStored: (value) => {
      stored = value;
    },
  };
}

export type GridListener = (visible: boolean) => void;

/** The grid-visibility preference: a persisted boolean plus subscribers. */
export class GridPreference {
  private readonly env: GridEnv;
  private value: boolean;
  private readonly listeners = new Set<GridListener>();

  constructor(env: GridEnv) {
    this.env = env;
    // Default visible: only the explicit "hidden" sentinel hides the grid, so a
    // missing or unrecognized stored value shows it.
    this.value = this.env.getStored() !== HIDDEN;
  }

  /** Whether the background grid should be drawn. */
  visible(): boolean {
    return this.value;
  }

  /** Set visibility, persist it, and notify subscribers. */
  set(visible: boolean): void {
    this.value = visible;
    this.env.setStored(visible ? VISIBLE : HIDDEN);
    this.listeners.forEach((cb) => cb(visible));
  }

  /** Flip visibility. */
  toggle(): void {
    this.set(!this.value);
  }

  /** Subscribe to changes; returns an unsubscribe function. */
  subscribe(cb: GridListener): () => void {
    this.listeners.add(cb);
    return () => {
      this.listeners.delete(cb);
    };
  }
}

/** The app-wide singleton, backed by localStorage. */
export const gridPreference = new GridPreference(browserGridEnv());
