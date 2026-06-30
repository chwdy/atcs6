// One front door for every "show me where this is on the layout" gesture.
//
// Before this, each panel reached into the renderer directly with its own
// method (flashTiles / setInspectHighlight / setReachableHighlights /
// highlightPath / setSelectedTunnel / setBoxSelection+setSelectionGroups), its
// own colour literal, its own auto-clear timer, and its own decision about
// whether to pan. The LayoutHighlighter collapses that into a single verb —
// `show(target, { channel, ... })` — plus `clear(channel?)`. Each channel maps
// onto one of the renderer's existing, independent highlight state fields, so
// the channels still coexist exactly as before; what changes is that callers no
// longer pick the renderer method, the colour, the duration, or the pan policy
// by hand.
//
// The renderer stays a dumb draw target: it keeps its per-channel state and its
// draw code untouched. This class only decides which field to set, with what
// colour/duration (from the theme palette), with what camera move, and who
// clears it.
//
// Channels:
//   selection  committed multi-select outline + fill (setBoxSelection +
//              setSelectionGroups). Persists until the selection changes.
//   peek       transient entity outline pointing at something the current panel
//              references (setInspectHighlight). Cleared on panel / tool swap.
//   locate     a timed flash that pulses where a thing is (flashTiles). Self
//              expiring; duration is caller-adjustable.
//   reachable  soft fill of a tile set for a live tool (setReachableHighlights).
//   path       every tile of one route/path (highlightPath). Panel-scoped.
//   pair       a tunnel's two mouths + connector (setSelectedTunnel).
//              Panel-scoped.

import { getCanvasPalette, rgbTriple } from '../lib/canvas-palette';

export type HighlightChannel =
  | 'selection'
  | 'peek'
  | 'locate'
  | 'reachable'
  | 'path'
  | 'pair';

/** Where the highlight lands. A channel that needs tiles/keys (selection, peek,
 *  locate, reachable) accepts any of `tiles` / `keys` / `entity`; `path` and
 *  `pair` carry their own shape straight through to the renderer. */
export type HighlightTarget =
  | { tiles: Array<[number, number]> }
  | { keys: Set<string> }
  | { entity: { kind: string; id: string } }
  | { path: unknown }
  | { pair: unknown };

/** How far the camera moves to bring the target into view. Defaults to `none`
 *  so a highlight never scrolls the map unless the caller asks. */
export type HighlightReveal = 'none' | 'ifOffscreen' | 'center';

export interface HighlightOptions {
  channel: HighlightChannel;
  reveal?: HighlightReveal;
  // `locate` only — the flash duration and look. durationMs is caller-adjustable
  // (e.g. a route-task locate lingers longer than a single-tile locate).
  durationMs?: number;
  fillAlpha?: number;
  highlightTrack?: boolean;
  blink?: boolean;
  mark?: 'x' | 'none';
}

/** Flash style the renderer's flashTiles understands. */
interface FlashTilesOpts {
  rgb?: string;
  fillAlpha?: number;
  durationMs?: number;
  blink?: boolean;
  mark?: 'x' | 'none';
  highlightTrack?: boolean;
}

/** The slice of the renderer the highlighter drives. Methods are optional and
 *  called defensively, mirroring the `?.` / `if (renderer.x)` guards the call
 *  sites used before this class existed (some renderers — and the test fakes —
 *  implement only part of the surface). */
export interface HighlighterRenderer {
  setBoxSelection?(keys: Set<string>): void;
  setSelectionGroups?(groups: unknown[]): void;
  setInspectHighlight?(groups: unknown[] | null): void;
  setReachableHighlights?(keys: Set<string> | null): void;
  highlightPath?(path: unknown): void;
  clearPathHighlight?(): void;
  setSelectedTunnel?(pair: unknown): void;
  flashTiles?(tiles: Array<[number, number]>, opts: FlashTilesOpts): void;
  requestRedraw?(opts?: unknown): void;
  inspectHighlightGroups?: unknown[];
  camera?: { panToWorld(x: number, y: number): void };
  canvas?: { getBoundingClientRect(): { width: number; height: number } };
  gridToScreenX?(x: number, y: number): number;
  gridToScreenY?(x: number, y: number): number;
}

export interface LayoutHighlighterHost {
  /** Read at call time so construction order vs the renderer never matters. */
  readonly renderer: HighlighterRenderer;
  /** Group a set of "x,y" keys into per-entity outline groups (the editor's
   *  _buildSelectionOutlineGroups). Used by the selection and peek channels. */
  buildOutlineGroups(keys: Set<string>): unknown[];
  /** Resolve an entity reference to its footprint keys. Only needed when a
   *  caller passes an `{ entity }` target; omit it and entity targets throw. */
  resolveEntityKeys?(kind: string, id: string): Set<string> | null;
}

/** Locate-flash defaults — preserve the values the call sites hard-coded before
 *  this class existed, so migrating them is a behaviour-preserving change. */
const LOCATE_DEFAULT_MS = 900;
const LOCATE_DEFAULT_ALPHA = 0.4;
/** Pixels from each canvas edge inside which a tile counts as "on screen" for
 *  reveal: 'ifOffscreen'. Matches the old _revealTunnelPartner margin. */
const REVEAL_MARGIN = 60;

export class LayoutHighlighter {
  private readonly host: LayoutHighlighterHost;

  constructor(host: LayoutHighlighterHost) {
    this.host = host;
  }

  private get renderer(): HighlighterRenderer {
    return this.host.renderer;
  }

  /** Show `target` on the layout through one channel. */
  show(target: HighlightTarget, opts: HighlightOptions): void {
    const r = this.renderer;
    if (!r) return;

    // Camera move first (so the world repaints at the new offset before the
    // overlay draws), matching the old locate sites.
    if (opts.reveal && opts.reveal !== 'none') {
      this.reveal(this.tilesOf(target), opts.reveal);
    }

    switch (opts.channel) {
      case 'selection': {
        const keys = this.keysOf(target);
        r.setBoxSelection?.(keys);
        r.setSelectionGroups?.(this.host.buildOutlineGroups(keys));
        break;
      }
      case 'peek': {
        r.setInspectHighlight?.(this.host.buildOutlineGroups(this.keysOf(target)));
        break;
      }
      case 'reachable': {
        r.setReachableHighlights?.(this.keysOf(target));
        break;
      }
      case 'locate': {
        const tiles = this.tilesOf(target);
        if (tiles.length) r.flashTiles?.(tiles, this.locateStyle(opts));
        break;
      }
      case 'path': {
        if ('path' in target) r.highlightPath?.(target.path);
        break;
      }
      case 'pair': {
        if ('pair' in target) r.setSelectedTunnel?.(target.pair);
        break;
      }
    }
  }

  /** Clear one channel, several channels, or — with no argument — every
   *  persistent channel. The `locate` channel is self-expiring, so it has
   *  nothing to clear. */
  clear(channel?: HighlightChannel | HighlightChannel[]): void {
    const r = this.renderer;
    if (!r) return;
    const targets: HighlightChannel[] = channel
      ? Array.isArray(channel)
        ? channel
        : [channel]
      : ['selection', 'peek', 'reachable', 'path', 'pair'];
    for (const c of targets) {
      switch (c) {
        case 'selection':
          r.setBoxSelection?.(new Set());
          r.setSelectionGroups?.([]);
          break;
        case 'peek':
          // Skip the redraw when nothing is peeked (parity with the old
          // clearInspectHighlight guard).
          if (r.inspectHighlightGroups?.length) r.setInspectHighlight?.([]);
          break;
        case 'reachable':
          r.setReachableHighlights?.(new Set());
          break;
        case 'path':
          r.clearPathHighlight?.();
          break;
        case 'pair':
          r.setSelectedTunnel?.(null);
          break;
        case 'locate':
          break;
      }
    }
  }

  /** The locate flash style: colour from the theme palette, the rest from the
   *  caller (or the preserved defaults). */
  private locateStyle(opts: HighlightOptions): FlashTilesOpts {
    return {
      rgb: rgbTriple(getCanvasPalette().highlightLocate),
      fillAlpha: opts.fillAlpha ?? LOCATE_DEFAULT_ALPHA,
      durationMs: opts.durationMs ?? LOCATE_DEFAULT_MS,
      highlightTrack: opts.highlightTrack ?? true,
      ...(opts.blink !== undefined ? { blink: opts.blink } : {}),
      ...(opts.mark !== undefined ? { mark: opts.mark } : {}),
    };
  }

  /** Move the camera so `tiles` come into view, per the reveal mode. */
  private reveal(tiles: Array<[number, number]>, mode: HighlightReveal): void {
    if (!tiles.length) return;
    const r = this.renderer;
    if (!r.camera) return;
    const [cx, cy] = this.center(tiles);
    if (mode === 'center') {
      r.camera.panToWorld(cx, cy);
      r.requestRedraw?.({ world: true, live: true, reason: 'highlight-reveal' });
      return;
    }
    // ifOffscreen: pan only when the centre sits outside the safe margin.
    if (
      r.canvas &&
      typeof r.gridToScreenX === 'function' &&
      typeof r.gridToScreenY === 'function'
    ) {
      const rect = r.canvas.getBoundingClientRect();
      const sx = r.gridToScreenX(cx, cy);
      const sy = r.gridToScreenY(cx, cy);
      const onScreen =
        sx >= REVEAL_MARGIN &&
        sx <= rect.width - REVEAL_MARGIN &&
        sy >= REVEAL_MARGIN &&
        sy <= rect.height - REVEAL_MARGIN;
      if (onScreen) return;
    }
    r.camera.panToWorld(cx, cy);
    r.requestRedraw?.({ world: true, live: true, reason: 'highlight-reveal' });
  }

  /** Centre of a tile list's bounding box (a single tile returns itself). */
  private center(tiles: Array<[number, number]>): [number, number] {
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const [x, y] of tiles) {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
    return [(minX + maxX) / 2, (minY + maxY) / 2];
  }

  /** Resolve a target to "x,y" keys (for the key-based channels). */
  private keysOf(target: HighlightTarget): Set<string> {
    if ('keys' in target) return target.keys;
    if ('tiles' in target) return new Set(target.tiles.map(([x, y]) => `${x},${y}`));
    if ('entity' in target) return this.resolveEntity(target.entity);
    return new Set();
  }

  /** Resolve a target to [x,y] tiles (for locate and reveal). */
  private tilesOf(target: HighlightTarget): Array<[number, number]> {
    if ('tiles' in target) return target.tiles;
    if ('keys' in target) return keysToTiles(target.keys);
    if ('entity' in target) return keysToTiles(this.resolveEntity(target.entity));
    return [];
  }

  private resolveEntity(entity: { kind: string; id: string }): Set<string> {
    if (!this.host.resolveEntityKeys) {
      throw new Error(
        `LayoutHighlighter: an { entity } target was passed but the host has no ` +
          `resolveEntityKeys (kind=${entity.kind}, id=${entity.id}).`,
      );
    }
    return this.host.resolveEntityKeys(entity.kind, entity.id) ?? new Set();
  }
}

/** Parse a set of "x,y" keys into [x,y] number pairs, dropping malformed ones. */
function keysToTiles(keys: Set<string>): Array<[number, number]> {
  const out: Array<[number, number]> = [];
  for (const k of keys) {
    const comma = k.indexOf(',');
    if (comma < 0) continue;
    const x = Number(k.slice(0, comma));
    const y = Number(k.slice(comma + 1));
    if (Number.isFinite(x) && Number.isFinite(y)) out.push([x, y]);
  }
  return out;
}
