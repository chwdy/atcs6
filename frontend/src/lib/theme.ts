// Theme manager — two independent theme axes ("channels"):
//
//   chrome  → the editor UI: panels, buttons, surfaces, text. Backed by the
//             data-theme attribute on <html> and the colors in themes.css.
//   canvas  → the layout renderer: ground, track, blocks, trains. Backed by the
//             data-canvas-theme attribute and the colors in layout-themes.css.
//
// Each axis is a ThemeChannel: its own registry of themes, its own attribute,
// its own localStorage key, switched independently of the other. The top
// bar's View menu (frontend/src/next/top-bar.tsx) drives both; this module
// owns the state, the persistence, and the side effects (a canvas switch
// invalidates the renderer's color cache and forces a redraw).
//
// A tiny pre-paint stub in index.html sets BOTH attributes before the
// stylesheets apply, so there is no flash of the wrong theme. It shares the two
// storage keys and the per-channel defaults below; on load this module
// reconcile()s each channel in case the stub honored a name that is not
// registered.
//
// Adding a theme is two steps and touches no other file: add a CSS block (in
// themes.css or layout-themes.css), then register it on the matching channel.

import { invalidateCanvasPalette } from './canvas-palette';
import { gridPreference } from './grid-preference';

const CHROME_ATTR = 'data-theme';
const CHROME_KEY = 'atcs6-theme';
const CANVAS_ATTR = 'data-canvas-theme';
const CANVAS_KEY = 'atcs6-canvas-theme';

/** One registered theme on a channel. `name` is the attribute value; `label`
 *  and `icon` (a full Tabler class, e.g. 'ti ti-moon') drive the UI. */
export interface ThemeDefinition {
  name: string;
  label: string;
  icon: string;
}

/** The operations a channel needs from its environment. Injecting this keeps
 *  ThemeChannel testable without a DOM — see memoryEnv() and the unit tests.
 *  browserEnv() binds it to the real document / localStorage for one attribute
 *  + storage key. */
export interface ThemeEnv {
  getAttribute(): string | null;
  setAttribute(value: string): void;
  getStored(): string | null;
  setStored(value: string): void;
  prefersLight(): boolean;
}

/** Per-channel behavior: whether the channel follows the OS color scheme
 *  when nothing is saved, and its default. Both shipped channels default to
 *  dark and do not follow the OS. */
export interface ChannelOptions {
  followOsPref?: boolean;
  defaultTheme?: string;
}

export type ThemeListener = (name: string) => void;

/** ThemeEnv backed by the real document / localStorage / matchMedia, for one
 *  attribute + storage key. Storage access is wrapped because it throws in
 *  private-mode browsers. */
export function browserEnv(attribute: string, storageKey: string): ThemeEnv {
  return {
    getAttribute: () => document.documentElement.getAttribute(attribute),
    setAttribute: (value) => document.documentElement.setAttribute(attribute, value),
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
        /* storage unavailable (private mode) — theme still applies this session */
      }
    },
    prefersLight: () =>
      typeof window !== 'undefined' &&
      !!window.matchMedia &&
      window.matchMedia('(prefers-color-scheme: light)').matches,
  };
}

/** In-memory ThemeEnv for tests and no-DOM contexts. */
export function memoryEnv(initial?: { attribute?: string; stored?: string; prefersLight?: boolean }): ThemeEnv {
  let attribute = initial?.attribute ?? null;
  let stored = initial?.stored ?? null;
  const prefersLight = initial?.prefersLight ?? false;
  return {
    getAttribute: () => attribute,
    setAttribute: (value) => {
      attribute = value;
    },
    getStored: () => stored,
    setStored: (value) => {
      stored = value;
    },
    prefersLight: () => prefersLight,
  };
}

/** One independent theme axis: a registry of themes plus the active selection,
 *  applied to one DOM attribute and persisted under one storage key. */
export class ThemeChannel {
  private readonly env: ThemeEnv;
  private readonly opts: ChannelOptions;
  private readonly defs = new Map<string, ThemeDefinition>();
  private readonly order: string[] = [];
  private readonly listeners = new Set<ThemeListener>();

  constructor(env: ThemeEnv, opts: ChannelOptions = {}) {
    this.env = env;
    this.opts = opts;
  }

  /** Register a theme. Re-registering a name updates its definition without
   *  moving it in the cycle order. Returns `this` so calls chain. */
  register(def: ThemeDefinition): this {
    if (!this.defs.has(def.name)) this.order.push(def.name);
    this.defs.set(def.name, def);
    return this;
  }

  has(name: string): boolean {
    return this.defs.has(name);
  }

  get(name: string): ThemeDefinition | undefined {
    return this.defs.get(name);
  }

  /** All registered themes, in registration order. */
  themes(): ThemeDefinition[] {
    return this.order.map((name) => this.defs.get(name)!);
  }

  /** Registered theme names, in registration order. */
  names(): string[] {
    return [...this.order];
  }

  /** The theme that should be active: saved preference, else (for chrome) the
   *  OS preference, else the channel default, else the first registered theme.
   *  Only ever returns a registered name (assuming at least one is registered). */
  preferred(): string {
    const saved = this.env.getStored();
    if (saved && this.has(saved)) return saved;
    if (this.opts.followOsPref) {
      const os = this.env.prefersLight() ? 'light' : 'dark';
      if (this.has(os)) return os;
    }
    const def = this.opts.defaultTheme ?? 'dark';
    if (this.has(def)) return def;
    return this.order[0] ?? def;
  }

  /** The active theme name. Falls back to preferred() when the attribute is
   *  unset or holds an unregistered value. */
  current(): string {
    const attr = this.env.getAttribute();
    return attr && this.has(attr) ? attr : this.preferred();
  }

  /** Activate a theme: set the attribute, persist it, notify subscribers.
   *  Unknown (unregistered) names are ignored. */
  set(name: string): void {
    if (!this.has(name)) return;
    this.env.setAttribute(name);
    this.env.setStored(name);
    this.listeners.forEach((cb) => cb(name));
  }

  /** Switch to the next registered theme, wrapping around. */
  toggle(): void {
    this.set(this.next().name);
  }

  /** The theme toggle() would switch to — drives a toggle button's icon/title. */
  next(): ThemeDefinition {
    const i = this.order.indexOf(this.current());
    const name = this.order[(i + 1) % this.order.length];
    return this.defs.get(name)!;
  }

  /** Subscribe to changes on this channel; returns an unsubscribe function. */
  subscribe(cb: ThemeListener): () => void {
    this.listeners.add(cb);
    return () => {
      this.listeners.delete(cb);
    };
  }

  /** Correct the DOM if the pre-paint stub set an unregistered theme. A no-op
   *  when the current attribute is already a registered theme. */
  reconcile(): void {
    const attr = this.env.getAttribute();
    if (!attr || !this.has(attr)) this.set(this.preferred());
  }
}

/** Aggregates the two channels and owns boot-time wiring. */
export class ThemeManager {
  readonly chrome: ThemeChannel;
  readonly canvas: ThemeChannel;

  constructor(chrome: ThemeChannel, canvas: ThemeChannel) {
    this.chrome = chrome;
    this.canvas = canvas;
  }

  /** Reconcile both channels with the registry and wire side effects. Call once
   *  after the DOM is ready. */
  init(): void {
    this.chrome.reconcile();
    this.canvas.reconcile();
    // A canvas switch changes colors the renderer reads from CSS: drop the
    // cached palette and force one redraw so the layout repaints immediately.
    this.canvas.subscribe(() => {
      invalidateCanvasPalette();
      redrawLayout();
    });
    // Toggling the background grid changes no colors, so no palette invalidation
    // — just repaint the world layer so the grid appears / disappears at once.
    gridPreference.subscribe(() => redrawLayout());
  }
}

/** Force the layout renderer to repaint (after a canvas-theme change, or a
 *  screen change that swaps the screen's block colours). Safe before the app
 *  exists. */
export function redrawLayout(): void {
  if (typeof window === 'undefined') return;
  const app = (window as unknown as { app?: { renderer?: { requestRedraw?: (o: { reason: string }) => void } } }).app;
  app?.renderer?.requestRedraw?.({ reason: 'canvas-theme' });
}

/** Reconcile both channels and wire side effects. Call once after the DOM is
 *  ready. */
export function initThemeManager(mgr: ThemeManager = themeManager): void {
  mgr.init();
}

function buildDefaultManager(): ThemeManager {
  const hasDom = typeof document !== 'undefined';
  // Both channels start dark until the operator picks a theme; neither follows
  // the computer's light/dark setting.
  const chrome = new ThemeChannel(hasDom ? browserEnv(CHROME_ATTR, CHROME_KEY) : memoryEnv(), {
    followOsPref: false,
    defaultTheme: 'dark',
  })
    .register({ name: 'dark', label: 'Dark', icon: 'ti ti-moon' })
    .register({ name: 'light', label: 'Light', icon: 'ti ti-sun' });
  const canvas = new ThemeChannel(hasDom ? browserEnv(CANVAS_ATTR, CANVAS_KEY) : memoryEnv(), {
    followOsPref: false,
    defaultTheme: 'dark',
  })
    .register({ name: 'dark', label: 'Dark', icon: 'ti ti-square' })
    .register({ name: 'light', label: 'Light', icon: 'ti ti-square-half-filled' })
    .register({ name: 'blueprint', label: 'Blueprint', icon: 'ti ti-ruler-2' });
  return new ThemeManager(chrome, canvas);
}

/** The app-wide singleton. chrome = dark/light; canvas = dark/light/blueprint. */
export const themeManager = buildDefaultManager();

declare global {
  interface Window {
    themeManager?: ThemeManager;
  }
}

// In the browser, expose the manager and self-initialize once the DOM is ready.
// Module scripts are deferred, so this typically runs after the document is
// parsed (readyState "interactive") and before DOMContentLoaded — the controls
// already exist, so init runs immediately in that case.
if (typeof document !== 'undefined') {
  if (typeof window !== 'undefined') window.themeManager = themeManager;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => initThemeManager());
  } else {
    initThemeManager();
  }
}
