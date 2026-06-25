// Theme manager — two independent theme axes ("channels"):
//
//   chrome  → the editor UI: panels, buttons, surfaces, text. Backed by the
//             data-theme attribute on <html> and the colors in themes.css.
//   canvas  → the layout renderer: ground, track, blocks, trains. Backed by the
//             data-canvas-theme attribute and the colors in layout-themes.css.
//
// Each axis is a ThemeChannel: its own registry of themes, its own attribute,
// its own localStorage key, switched independently of the other. The Display
// popover (built in app.ts) drives both; this module owns the state, the
// persistence, and the side effects (a canvas switch invalidates the renderer's
// color cache and forces a redraw).
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

/** Per-channel behavior. chrome follows the OS color scheme when nothing is
 *  saved; canvas just defaults to its drawing-board dark. */
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
    wireDisplayMenu(this);
    // A canvas switch changes colors the renderer reads from CSS: drop the
    // cached palette and force one redraw so the layout repaints immediately.
    this.canvas.subscribe(() => {
      invalidateCanvasPalette();
      redrawLayout();
    });
  }
}

const DISPLAY_BUTTON_ID = 'btn-display';

// The six primary-color tokens previewed per theme, per channel. Read live from
// CSS (not hardcoded), so a swatch always matches the theme: background, accent,
// text, error, warning, info.
const CHROME_PREVIEW_TOKENS = ['--bg-panel', '--accent-color', '--text-primary', '--danger-color', '--warning-color', '--info-color'];
const CANVAS_PREVIEW_TOKENS = ['--canvas-bg', '--canvas-accent', '--canvas-text', '--canvas-danger', '--canvas-warning', '--canvas-info'];

/** Build and wire the Display popover behind the header #btn-display button. One
 *  row per channel (Editor theme / Layout theme); each row lists that channel's
 *  registered themes. Each theme is a 2x3 grid of its six primary colors (see
 *  the *_PREVIEW_TOKENS above), with a check on the active one. A newly
 *  registered theme appears here with no extra wiring. Clicking a swatch
 *  switches that channel only. */
function wireDisplayMenu(mgr: ThemeManager): void {
  const btn = document.getElementById(DISPLAY_BUTTON_ID);
  if (!btn) return;

  const sections = [
    { channel: mgr.chrome, label: 'Editor theme', attr: CHROME_ATTR, tokens: CHROME_PREVIEW_TOKENS },
    { channel: mgr.canvas, label: 'Layout theme', attr: CANVAS_ATTR, tokens: CANVAS_PREVIEW_TOKENS },
  ];
  const options: { channel: ThemeChannel; name: string; el: HTMLButtonElement }[] = [];
  const root = document.documentElement;

  // Read a theme's token colors WITHOUT activating it: set the channel's
  // attribute on <html>, read getComputedStyle, restore. Synchronous, so the
  // browser never paints the intermediate state.
  const readColors = (attr: string, name: string, tokens: string[]): string[] => {
    const prev = root.getAttribute(attr);
    root.setAttribute(attr, name);
    const cs = getComputedStyle(root);
    const colors = tokens.map((t) => cs.getPropertyValue(t).trim() || 'transparent');
    if (prev === null) root.removeAttribute(attr);
    else root.setAttribute(attr, prev);
    return colors;
  };

  let menu: HTMLDivElement | null = null;

  const syncActive = () => {
    for (const o of options) o.el.setAttribute('aria-pressed', String(o.channel.current() === o.name));
  };

  // Built lazily on first open, so every stylesheet (including the @imported
  // theme files) has loaded before we sample colors.
  const build = () => {
    const el = document.createElement('div');
    el.className = 'display-menu';
    el.hidden = true;
    el.setAttribute('role', 'menu');

    for (const { channel, label, attr, tokens } of sections) {
      const row = document.createElement('div');
      row.className = 'display-menu-row';
      const heading = document.createElement('div');
      heading.className = 'display-menu-row-label';
      heading.textContent = label;
      row.appendChild(heading);

      const tiles = document.createElement('div');
      tiles.className = 'display-menu-tiles';
      for (const theme of channel.themes()) {
        const tile = document.createElement('button');
        tile.type = 'button';
        tile.className = 'display-menu-tile';
        tile.setAttribute('role', 'menuitemradio');
        tile.title = theme.label;

        const swatch = document.createElement('span');
        swatch.className = 'display-menu-swatch';
        for (const color of readColors(attr, theme.name, tokens)) {
          const cell = document.createElement('span');
          cell.className = 'display-menu-cell';
          cell.style.background = color;
          swatch.appendChild(cell);
        }
        const check = document.createElement('i');
        check.className = 'ti ti-check display-menu-check';
        check.setAttribute('aria-hidden', 'true');
        swatch.appendChild(check);

        const tileLabel = document.createElement('span');
        tileLabel.className = 'display-menu-tile-label';
        tileLabel.textContent = theme.label;

        tile.appendChild(swatch);
        tile.appendChild(tileLabel);
        tile.addEventListener('click', () => channel.set(theme.name));
        tiles.appendChild(tile);
        options.push({ channel, name: theme.name, el: tile });
      }
      row.appendChild(tiles);
      el.appendChild(row);
    }
    document.body.appendChild(el);
    menu = el;
    mgr.chrome.subscribe(syncActive);
    mgr.canvas.subscribe(syncActive);
    syncActive();
  };

  const place = () => {
    if (!menu) return;
    const r = btn.getBoundingClientRect();
    menu.style.top = `${r.bottom + 6}px`;
    menu.style.left = 'auto';
    menu.style.right = `${Math.max(8, window.innerWidth - r.right)}px`;
  };
  const open = () => {
    if (!menu) build();
    place();
    menu!.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
  };
  const close = () => {
    if (menu) menu.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
  };

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!menu || menu.hidden) open();
    else close();
  });
  document.addEventListener('click', (e) => {
    if (menu && !menu.hidden && !menu.contains(e.target as Node) && e.target !== btn) close();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && menu && !menu.hidden) close();
  });
  window.addEventListener('resize', () => {
    if (menu && !menu.hidden) place();
  });
}

/** Force the layout renderer to repaint (after a canvas-theme change). Safe
 *  before the app exists. */
function redrawLayout(): void {
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
  const chrome = new ThemeChannel(hasDom ? browserEnv(CHROME_ATTR, CHROME_KEY) : memoryEnv(), {
    followOsPref: true,
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
