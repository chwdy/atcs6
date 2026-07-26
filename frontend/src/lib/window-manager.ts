// FloatingWindow — the shared chrome for every draggable/resizable panel:
// header + drag, 8-direction resize, double-click-header-to-fit, DPI
// re-clamp, z-order via WindowManager. Owns no content semantics — a caller
// supplies a WindowContent to mount, and gets floating/dock-free chrome for
// free. This is the primitive that replaces two independent, ~90%-duplicated
// hand-rolled implementations (the secondary sidebar's floating mode and the
// diagnostics panel's drag/resize).
//
// WindowManager is the registry: it tracks open windows for z-order/focus
// (clicking any window brings it to the top of a narrow z-index band that
// always stays under --z-modal, closing the "floating inspector outranks
// modals" gap) and lets callers enumerate what's open.

import { computeFloatResize, computeFitSize, clampToViewport, type FloatRect } from './window-geometry';
import { watchDprChange, isCoarsePointer, coarsePointerScale } from './dpi';

/** One `resize` listener for every open window, rAF-coalesced.
 *
 *  Per-window listeners would each force their own layout read on every resize
 *  event, and a resize drag fires them continuously; batching into a single
 *  frame keeps that to one pass over the open windows no matter how many there
 *  are. Installed on the first subscriber and removed with the last, so a
 *  no-DOM context (this project's vitest has no jsdom) never touches `window`. */
const viewportListeners = new Set<() => void>();
let viewportFrame = 0;

function flushViewportListeners(): void {
  viewportFrame = 0;
  viewportListeners.forEach((fn) => fn());
}

function onViewportResize(): void {
  if (viewportFrame) {
    return;
  }
  viewportFrame = requestAnimationFrame(flushViewportListeners);
}

function watchViewportResize(cb: () => void): () => void {
  if (typeof window === 'undefined') {
    return () => {};
  }
  if (viewportListeners.size === 0) {
    window.addEventListener('resize', onViewportResize);
  }
  viewportListeners.add(cb);
  return () => {
    viewportListeners.delete(cb);
    if (viewportListeners.size === 0) {
      window.removeEventListener('resize', onViewportResize);
    }
  };
}

/** Persisted-and-restorable geometry for one window. Any field left null uses
 *  the browser's default flow position/size until the window is first moved
 *  or resized. */
export interface WindowGeometry {
  left: number | null;
  top: number | null;
  width: number | null;
  height: number | null;
}

/** What kind of window this is — informs persistence (Phase 6) and which
 *  windows may dock (only 'home'). Not a closed set: a spawned tab-group
 *  window gets a generated id but is still kind 'tab-group'. */
export type WindowKind = 'home' | 'tab-group' | 'diagnostics' | string;

/** What FloatingWindow needs from its content. Implemented by TabGroup for
 *  tab-hosting windows, and by a thin adapter for a single-content window
 *  like diagnostics. FloatingWindow never inspects tabs, entities, or
 *  anything content-shaped — only calls these two hooks. */
export interface WindowContent {
  /** Called once, right after the window's DOM exists. Mount content into
   *  contentEl (a plain flex column filling the space under the header —
   *  see floating-window.css's .floating-window-content). */
  mount(contentEl: HTMLElement): void;
  /** Best-effort cleanup (subscriptions, timers) when the window is
   *  destroyed. Not called when a window is merely hidden. */
  unmount?(): void;
}

export interface FloatingWindowOptions {
  /** Unique id — becomes part of the persistence key in a later phase. */
  id: string;
  kind: WindowKind;
  manager: WindowManager;
  title: string;
  /** Omit when adopting a window whose content isn't a single mountable
   *  block — the home sidebar's TabGroup writes directly into its own
   *  title/content elements by id instead (see adopt below). */
  content?: WindowContent;
  /** Whether this window may be docked into the page's left-flow layout
   *  slot. False for every non-adopted window (diagnostics, a spawned
   *  split-off window); the home secondary sidebar is the only
   *  dockable: true window — there is exactly one docked slot in the
   *  layout, so nothing else can use it. */
  dockable?: boolean;
  /** Adopt existing DOM instead of creating fresh markup — used only by the
   *  home secondary sidebar, whose header already has its own richer button
   *  set (parent-link, pin, tearoff, close) wired by its owner, not by
   *  FloatingWindow. When provided, FloatingWindow creates no title or close
   *  button of its own — it only wires drag/resize/fit/DPR-reclamp onto the
   *  supplied elements and appends its 8 resize handles into `root`. */
  adopt?: { root: HTMLElement; header: HTMLElement };
  minW?: number;
  minH?: number;
  maxW?: number;
  maxH?: number;
  initialGeometry?: Partial<WindowGeometry>;
  initialHidden?: boolean;
  /** Only meaningful when dockable — starts floating instead of docked. */
  initialFloating?: boolean;
  /** Where a dockable window's float starts the first time it floats with no
   *  previously-persisted position. Ignored for non-dockable windows (their
   *  position comes from initialGeometry / their own create-mode default).
   *
   *  A thunk, not a constant, because it is consulted at the moment the window
   *  actually floats — a placement that reads the live layout and chrome (see
   *  lib/window-placement.ts) would be measuring an unlaid-out page if it ran at
   *  construction. It receives the window's current measured size so placement
   *  can reason about the box it is positioning. */
  defaultFloatPosition?: (size: { width: number; height: number }) => { left: number; top: number };
  /** The header's close (×) button was clicked. Only fires for a
   *  FloatingWindow-created header (not adopt mode, whose owner wires its own
   *  close button). FloatingWindow does not decide what "close" means —
   *  diagnostics hides itself (it's a permanent singleton toggled elsewhere);
   *  a spawned split-off window (Phase 5) will destroy itself. */
  onCloseRequested?: () => void;
  /** Fires after a drag ends, a resize ends, a fit, or a dock/float toggle —
   *  the persistence hook a later phase wires to localStorage. This class
   *  does no storage I/O itself. */
  onGeometryChange?: (geo: WindowGeometry) => void;
}

const DEFAULT_MIN_W = 260;
const DEFAULT_MIN_H = 180;
/** Width at which a window is wide enough to lay its content out horizontally —
 *  today that means the working-set navigator flipping from a vertical tree to
 *  a horizontal tab strip (see `.window--wide` in layout/shell.css). Set just
 *  past `.secondary-sidebar`'s 500px docked max-width, so the docked inspector
 *  always stays narrow-form and only a deliberately widened floating window
 *  crosses over. */
const WIDE_WINDOW_WIDTH = 520;
const VIEWPORT_MARGIN = 8;
const RESIZE_MARGIN = 6;
const RESIZE_DIRECTIONS = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];

/** Must match --z-window in static/css/base/tokens.css — every open window's
 *  z-index lives in the band starting here, ordered by WindowManager's
 *  z-order (see WindowManager.applyZOrder). Deliberately under --z-picker
 *  (1000) and --z-modal (1100): a modal always outranks every floating
 *  window now, closing the divergence the old literal z-index: 9000 caused. */
const BASE_Z = 900;

export class FloatingWindow {
  readonly id: string;
  readonly kind: WindowKind;

  private manager: WindowManager;
  private content?: WindowContent;
  private dockable: boolean;
  private isFloatingState: boolean;
  private defaultFloatPosition: (size: { width: number; height: number }) => { left: number; top: number };
  private minW: number;
  private minH: number;
  private maxW: number;
  private maxH: number;
  private onCloseRequested?: () => void;
  private onGeometryChange?: (geo: WindowGeometry) => void;

  private rootEl!: HTMLElement;
  private headerEl!: HTMLElement;
  private titleEl: HTMLElement | null = null;
  private contentEl: HTMLElement | null = null;

  private hidden = false;
  private unwatchDpr: () => void = () => {};
  private unwatchViewport: () => void = () => {};
  private widthObserver: ResizeObserver | null = null;

  constructor(opts: FloatingWindowOptions) {
    this.id = opts.id;
    this.kind = opts.kind;
    this.manager = opts.manager;
    this.content = opts.content;
    this.dockable = !!opts.dockable;
    this.isFloatingState = this.dockable ? !!opts.initialFloating : true;
    this.defaultFloatPosition = opts.defaultFloatPosition ?? (() => ({ left: 40, top: 40 }));
    this.minW = opts.minW ?? DEFAULT_MIN_W;
    this.minH = opts.minH ?? DEFAULT_MIN_H;
    this.maxW = opts.maxW ?? Infinity;
    this.maxH = opts.maxH ?? Infinity;
    this.onCloseRequested = opts.onCloseRequested;
    this.onGeometryChange = opts.onGeometryChange;

    this.buildDom(opts.title, opts.adopt);
    if (opts.adopt) {
      // Adopted DOM doesn't ship with the .floating-window chrome class —
      // the home sidebar's static markup doesn't have it, and a spawned
      // window's freshly built root doesn't either. Set it to match this
      // window's actual floating state now (always true when non-dockable);
      // toggleDock's own class toggle keeps a dockable window in sync with
      // it from here on. Create-mode already ships the class in buildDom.
      this.rootEl.classList.toggle('floating-window', this.isFloatingState);
    }
    if (opts.initialGeometry) {
      this.setGeometry(opts.initialGeometry);
    } else if (this.dockable && this.isFloatingState) {
      this.setGeometry(this.measuredDefaultFloatPosition());
    }
    if (opts.content && this.contentEl) {
      opts.content.mount(this.contentEl);
    }
    this.setupDrag();
    this.setupResize();
    this.setupFitOnDblClick();
    this.rootEl.addEventListener('pointerdown', () => this.focus());
    this.unwatchDpr = watchDprChange(() => this.reclampToViewport());
    this.unwatchViewport = watchViewportResize(() => this.reclampToViewport());
    this.setupWidthClass();
    this.applyCoarsePointerSizing();
    if (opts.initialHidden) {
      this.setHidden(true);
    }
    this.manager.register(this);
    // Restored geometry is written for whatever viewport it was saved in, so a
    // layout saved on a large monitor comes back off-screen on a laptop. Pull
    // it in once the browser has laid the window out — reclamp measures, and at
    // constructor time the element has no box yet (a hidden or not-yet-`visible`
    // window measures 0×0 and reclamp early-returns).
    if (opts.initialGeometry && !opts.initialHidden && typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => this.reclampToViewport());
    }
  }

  /** Evaluate the default-float-position thunk against the window's size as it
   *  measures right now. A window that has never been laid out (still docked
   *  behind `display: none`) reports 0×0; fall back to the class's own minimums
   *  so placement is asked to fit a plausible box rather than a point. */
  private measuredDefaultFloatPosition(): { left: number; top: number } {
    const r = this.rootEl.getBoundingClientRect();
    return this.defaultFloatPosition({
      width: r.width || this.minW,
      height: r.height || this.minH,
    });
  }

  /** Whether drag/resize/fit should currently act — always true for a
   *  non-dockable window, only while actually floating for a dockable one
   *  (docked mode has a fixed layout position/width; the CSS-hidden resize
   *  handles are inert there too, but this is the authoritative gate since
   *  pointer events fire regardless of display). */
  private canFloat(): boolean {
    return !this.dockable || this.isFloatingState;
  }

  // ---- DOM construction ----

  private buildDom(title: string, adopt?: { root: HTMLElement; header: HTMLElement }): void {
    if (adopt) {
      this.rootEl = adopt.root;
      this.headerEl = adopt.header;
      this.rootEl.dataset.windowId = this.id;
      this.rootEl.dataset.windowKind = this.kind;
    } else {
      const root = document.createElement('div');
      root.className = 'floating-window';
      root.dataset.windowId = this.id;
      root.dataset.windowKind = this.kind;

      const header = document.createElement('div');
      header.className = 'floating-window-header';

      const titleEl = document.createElement('span');
      titleEl.className = 'floating-window-title';
      titleEl.textContent = title;

      const closeBtn = document.createElement('button');
      closeBtn.type = 'button';
      closeBtn.className = 'floating-window-close';
      closeBtn.title = 'Close';
      closeBtn.setAttribute('aria-label', 'Close');
      closeBtn.textContent = '×';
      closeBtn.addEventListener('click', () => this.onCloseRequested?.());

      header.appendChild(titleEl);
      header.appendChild(closeBtn);

      const content = document.createElement('div');
      content.className = 'floating-window-content';

      root.appendChild(header);
      root.appendChild(content);
      document.body.appendChild(root);

      this.rootEl = root;
      this.headerEl = header;
      this.titleEl = titleEl;
      this.contentEl = content;
    }

    // Resize handles are always created fresh and appended to the root,
    // whether that root was just created or adopted — the home sidebar's
    // handles were already dynamically created (not static markup) before
    // this migration, so this isn't new for it either.
    for (const dir of RESIZE_DIRECTIONS) {
      const handle = document.createElement('div');
      handle.className = 'floating-window-resizer';
      handle.dataset.resizeDir = dir;
      this.rootEl.appendChild(handle);
    }
  }

  // ---- Drag ----

  private setupDrag(): void {
    let offset: { x: number; y: number } | null = null;

    this.headerEl.addEventListener('pointerdown', (e: PointerEvent) => {
      if (!this.canFloat()) return;
      if ((e.target as HTMLElement).closest('button')) return;
      this.headerEl.setPointerCapture(e.pointerId);
      const r = this.rootEl.getBoundingClientRect();
      offset = { x: e.clientX - r.left, y: e.clientY - r.top };
    });

    this.headerEl.addEventListener('pointermove', (e: PointerEvent) => {
      if (!offset) return;
      const x = Math.max(0, Math.min(window.innerWidth - 80, e.clientX - offset.x));
      const y = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - offset.y));
      this.rootEl.style.left = `${x}px`;
      this.rootEl.style.top = `${y}px`;
      this.rootEl.style.right = 'auto';
      this.rootEl.classList.add('floating-window--dragging');
    });

    const end = (): void => {
      if (!offset) return;
      offset = null;
      this.rootEl.classList.remove('floating-window--dragging');
      this.notifyGeometryChange();
    };
    this.headerEl.addEventListener('pointerup', end);
    this.headerEl.addEventListener('pointercancel', end);
  }

  // ---- Resize ----

  private setupResize(): void {
    let active: { dir: string } & FloatRect & { startX: number; startY: number } | null = null;

    const onMove = (e: PointerEvent): void => {
      if (!active) return;
      const res = computeFloatResize(
        active.dir,
        active,
        e.clientX - active.startX,
        e.clientY - active.startY,
        { width: window.innerWidth, height: window.innerHeight },
        { minW: this.minW, minH: this.minH, margin: RESIZE_MARGIN }
      );
      if (res.left !== undefined) this.rootEl.style.left = `${res.left}px`;
      if (res.top !== undefined) this.rootEl.style.top = `${res.top}px`;
      if (res.width !== undefined) this.rootEl.style.width = `${Math.min(res.width, this.maxW)}px`;
      if (res.height !== undefined) {
        this.rootEl.style.height = `${Math.min(res.height, this.maxH)}px`;
        // A vertical drag sets an explicit height; lift any auto-height cap
        // so the explicit height governs from here on (mirrors the previous
        // per-consumer resize code this replaces).
        this.rootEl.style.maxHeight = 'none';
      }
    };

    const onEnd = (): void => {
      if (!active) return;
      active = null;
      this.rootEl.classList.remove('floating-window--resizing');
      document.body.style.userSelect = '';
      this.notifyGeometryChange();
    };

    this.rootEl.querySelectorAll<HTMLElement>('.floating-window-resizer').forEach((handle) => {
      const dir = handle.dataset.resizeDir as string;
      handle.addEventListener('pointerdown', (e: PointerEvent) => {
        if (!this.canFloat()) return;
        e.preventDefault();
        e.stopPropagation();
        handle.setPointerCapture(e.pointerId);
        const r = this.rootEl.getBoundingClientRect();
        active = { dir, startX: e.clientX, startY: e.clientY, left: r.left, top: r.top, right: r.right, bottom: r.bottom };
        this.rootEl.classList.add('floating-window--resizing');
        document.body.style.userSelect = 'none';
      });
      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onEnd);
      handle.addEventListener('pointercancel', onEnd);
    });
  }

  // ---- Double-click-to-fit ----

  private setupFitOnDblClick(): void {
    this.headerEl.addEventListener('dblclick', (e: MouseEvent) => {
      if (!this.canFloat()) return;
      if ((e.target as HTMLElement).closest('button')) return;
      this.fitToContent();
    });
  }

  /** Resize to the content's natural size (see computeFitSize) without
   *  moving the window's top-left anchor. Measures by temporarily lifting
   *  size constraints — the same stash/measure/restore shape used elsewhere
   *  in this codebase for uncontrolled measurement — then restoring them
   *  before applying the computed fit. */
  fitToContent(): void {
    const rect = this.rootEl.getBoundingClientRect();

    const prevWidth = this.rootEl.style.width;
    const prevHeight = this.rootEl.style.height;
    const prevMaxHeight = this.rootEl.style.maxHeight;
    this.rootEl.style.width = 'auto';
    this.rootEl.style.height = 'auto';
    this.rootEl.style.maxHeight = 'none';
    const natural = { width: this.rootEl.scrollWidth, height: this.rootEl.scrollHeight };
    this.rootEl.style.width = prevWidth;
    this.rootEl.style.height = prevHeight;
    this.rootEl.style.maxHeight = prevMaxHeight;

    const fit = computeFitSize(
      { left: rect.left, top: rect.top },
      natural,
      { width: window.innerWidth, height: window.innerHeight },
      { minW: this.minW, minH: this.minH, maxW: this.maxW, maxH: this.maxH, margin: VIEWPORT_MARGIN }
    );
    this.rootEl.style.width = `${fit.width}px`;
    this.rootEl.style.height = `${fit.height}px`;
    this.rootEl.style.maxHeight = 'none';
    this.notifyGeometryChange();
  }

  // ---- Geometry ----

  setGeometry(geo: Partial<WindowGeometry>): void {
    if (geo.left !== undefined && geo.left !== null) this.rootEl.style.left = `${geo.left}px`;
    if (geo.top !== undefined && geo.top !== null) this.rootEl.style.top = `${geo.top}px`;
    if (geo.width !== undefined && geo.width !== null) this.rootEl.style.width = `${geo.width}px`;
    if (geo.height !== undefined && geo.height !== null) {
      this.rootEl.style.height = `${geo.height}px`;
      this.rootEl.style.maxHeight = 'none';
    }
  }

  getGeometry(): WindowGeometry {
    return {
      left: this.rootEl.style.left ? parseFloat(this.rootEl.style.left) : null,
      top: this.rootEl.style.top ? parseFloat(this.rootEl.style.top) : null,
      width: this.rootEl.style.width ? parseFloat(this.rootEl.style.width) : null,
      height: this.rootEl.style.height ? parseFloat(this.rootEl.style.height) : null,
    };
  }

  private notifyGeometryChange(): void {
    this.onGeometryChange?.(this.getGeometry());
  }

  /** Keep the window reachable after a DPR change, a browser-window resize, or
   *  a geometry restore. The clamp rule itself lives in clampToViewport (pure,
   *  unit-tested); this only measures and writes back. */
  reclampToViewport(): void {
    if (this.hidden || !this.canFloat()) return;
    const r = this.rootEl.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return; // not laid out yet
    const { left, top } = clampToViewport(
      {
        left: parseFloat(this.rootEl.style.left) || r.left,
        top: parseFloat(this.rootEl.style.top) || r.top,
        width: r.width,
        height: r.height,
      },
      { width: window.innerWidth, height: window.innerHeight },
      { margin: VIEWPORT_MARGIN }
    );
    this.rootEl.style.left = `${left}px`;
    this.rootEl.style.top = `${top}px`;
  }

  /** Keep `.window--wide` in sync with the window's own width, so content can
   *  lay itself out by the room it actually has rather than by the viewport.
   *  Deliberately a class and not a CSS container query: `container-type:
   *  inline-size` implies `contain: layout`, which would make this window the
   *  containing block for its `position: fixed` descendants — the hardware port
   *  picker renders inside the inspector and anchors against the viewport. */
  private setupWidthClass(): void {
    const apply = (width: number): void => {
      this.rootEl.classList.toggle('window--wide', width >= WIDE_WINDOW_WIDTH);
    };
    if (typeof ResizeObserver === 'undefined') {
      apply(this.rootEl.getBoundingClientRect().width);
      return;
    }
    this.widthObserver = new ResizeObserver((entries) => {
      for (const entry of entries) {
        apply(entry.contentRect.width);
      }
    });
    this.widthObserver.observe(this.rootEl);
  }

  private applyCoarsePointerSizing(): void {
    const scale = coarsePointerScale(isCoarsePointer());
    this.rootEl.style.setProperty('--floating-window-handle-scale', String(scale));
  }

  // ---- Visibility ----

  setHidden(hidden: boolean): void {
    this.hidden = hidden;
    this.rootEl.classList.toggle('floating-window--hidden', hidden);
    if (!hidden) {
      this.reclampToViewport();
      this.focus();
    }
  }

  isHidden(): boolean {
    return this.hidden;
  }

  /** No-op when adopting a header with its own title element (the home
   *  sidebar manages its own #secondary-sidebar-title directly). */
  setTitle(title: string): void {
    if (this.titleEl) this.titleEl.textContent = title;
  }

  // ---- Docking ----

  isDockable(): boolean {
    return this.dockable;
  }

  isFloating(): boolean {
    return this.isFloatingState;
  }

  /** Toggle between docked (in-flow, page layout governs position/width) and
   *  floating (position: fixed, draggable/resizable like every other
   *  window). No-op for a non-dockable window. Returns the new floating
   *  state. */
  toggleDock(): boolean {
    if (!this.dockable) return this.isFloatingState;
    this.setFloating(!this.isFloatingState);
    return this.isFloatingState;
  }

  private setFloating(floating: boolean): void {
    this.isFloatingState = floating;
    this.rootEl.classList.toggle('floating-window', floating);
    if (floating) {
      if (!this.rootEl.style.left) {
        this.setGeometry(this.measuredDefaultFloatPosition());
      }
      this.reclampToViewport();
      this.focus();
    } else {
      // Drop the floating position/height so the docked layout's own CSS
      // governs; width is left intact (carried over from the floating
      // window's last size) — mirrors the sidebar's original dock-toggle
      // contract exactly.
      this.rootEl.style.left = '';
      this.rootEl.style.top = '';
      this.rootEl.style.height = '';
      this.rootEl.style.maxHeight = '';
    }
    this.notifyGeometryChange();
  }

  // ---- Z-order ----

  focus(): void {
    this.manager.focus(this);
  }

  setZIndex(z: number): void {
    this.rootEl.style.zIndex = String(z);
  }

  /** Current z-index, e.g. for a content-owned element (diagnostics' name
   *  tooltip) that must always render just above this window regardless of
   *  where WindowManager currently ranks it in the stack. */
  getZIndex(): number {
    return parseInt(this.rootEl.style.zIndex || '0', 10);
  }

  // ---- Content mount point ----

  /** Null for an adopted window with no single mountable content block (the
   *  home sidebar — its TabGroup writes directly into its own elements). */
  getContentElement(): HTMLElement | null {
    return this.contentEl;
  }

  getRootElement(): HTMLElement {
    return this.rootEl;
  }

  // ---- Lifecycle ----

  /** Remove this window entirely (DOM + registration). Not the same as
   *  hiding — a window that should merely toggle visibility (diagnostics,
   *  the home sidebar) never calls this; it's for windows that should stop
   *  existing (a spawned split-off window losing its last tab, Phase 5+). */
  destroy(): void {
    this.unwatchDpr();
    this.unwatchViewport();
    this.widthObserver?.disconnect();
    this.content?.unmount?.();
    this.rootEl.remove();
    this.manager.unregister(this);
  }
}

/** Registry of open windows: z-order/focus and enumeration. Persistence
 *  (which windows exist across a reload) is a later phase's concern, layered
 *  on top via subscribe() — WindowManager itself does no storage I/O, same
 *  separation ThemeChannel keeps from ThemeEnv. */
export class WindowManager {
  private windows = new Map<string, FloatingWindow>();
  private zOrder: string[] = [];
  private listeners = new Set<() => void>();

  register(win: FloatingWindow): void {
    this.windows.set(win.id, win);
    this.zOrder.push(win.id);
    this.applyZOrder();
    this.notify();
  }

  unregister(win: FloatingWindow): void {
    this.windows.delete(win.id);
    this.zOrder = this.zOrder.filter((id) => id !== win.id);
    this.notify();
  }

  get(id: string): FloatingWindow | undefined {
    return this.windows.get(id);
  }

  /** All open windows, back-to-front (last = topmost). */
  all(): FloatingWindow[] {
    return this.zOrder.map((id) => this.windows.get(id)).filter((w): w is FloatingWindow => !!w);
  }

  /** Bring a window to the top of the z-order band. */
  focus(win: FloatingWindow): void {
    if (!this.windows.has(win.id)) return;
    this.zOrder = this.zOrder.filter((id) => id !== win.id);
    this.zOrder.push(win.id);
    this.applyZOrder();
    this.notify();
  }

  private applyZOrder(): void {
    this.zOrder.forEach((id, index) => {
      this.windows.get(id)?.setZIndex(BASE_Z + index);
    });
  }

  /** Subscribe to open/close/focus changes. Returns an unsubscribe
   *  function. */
  subscribe(cb: () => void): () => void {
    this.listeners.add(cb);
    return () => {
      this.listeners.delete(cb);
    };
  }

  private notify(): void {
    this.listeners.forEach((cb) => cb());
  }
}

/** The app-wide singleton every window (diagnostics, the home sidebar, any
 *  spawned tab-group window) registers with, so z-order/focus is consistent
 *  across all of them — mirrors the gridPreference singleton pattern. */
export const windowManager = new WindowManager();
