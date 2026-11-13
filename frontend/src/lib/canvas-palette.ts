// Canvas palette — the single typed source for every color the layout renderer
// draws. The values live in CSS (static/css/base/layout-themes.css), one block
// per canvas theme keyed by the data-canvas-theme attribute on <html>. This
// module resolves those tokens ONCE into a plain object and caches it, because
// the renderer draws every frame and calling getComputedStyle ~35 times per
// frame would be far too slow.
//
// The cache is invalidated when the canvas theme changes (the canvas channel in
// theme.ts calls invalidateCanvasPalette() on switch, then forces a redraw).
//
// Alpha tints (the rgba(...) fills/strokes the renderer used to hardcode) are
// computed from the base tokens via withAlpha(), which is memoized so repeated
// per-frame calls do not re-parse.

/** Every base color the renderer needs. Alpha variants are derived via
 *  withAlpha(); only opaque base colors are tokens. */
export interface CanvasPalette {
  // ground / structure
  bg: string;
  grid: string;
  axis: string;
  origin: string;
  // track linework
  track: string;
  trackDim: string;
  // shared status hues (blocks, routes, signals, switches, previews)
  success: string;
  successBright: string;
  caution: string;
  warning: string;
  danger: string;
  accent: string;
  accentBright: string;
  info: string;
  hold: string;
  lock: string;
  // neutrals / ink
  text: string;
  textDim: string;
  ink: string;
  onAccent: string;
  // info panels drawn on the canvas (block/train/station labels)
  overlayBg: string;
  overlayBorder: string;
  // block-state palette (consumed via getBlockStateColor)
  blockFree: string;
  blockReserved: string;
  blockOccupied: string;
  blockCaution: string;
  blockDisabled: string;
  blockManual: string;
  // train bodies (the outline reuses `ink`)
  trainBody: string;
  trainBodyManual: string;
  trainBodyTeardown: string;
  trainBodyEb: string;
  trainBodyUnidentified: string;
  // v2 train line, by train state
  trainLineMoving: string;
  trainLineWaiting: string;
  trainLineStopped: string;
  trainLineLost: string;
  // highlight overlays driven by the LayoutHighlighter. Only the "locate" flash
  // needs its own hue; peek/selection outlines take entity-chip colours and the
  // reachable fill reuses `warning`, so they have no dedicated token here.
  highlightLocate: string;
}

/** CSS custom property backing each palette key. */
const TOKENS: Record<keyof CanvasPalette, string> = {
  bg: '--canvas-bg',
  grid: '--canvas-grid',
  axis: '--canvas-axis',
  origin: '--canvas-origin',
  track: '--canvas-track',
  trackDim: '--canvas-track-dim',
  success: '--canvas-success',
  successBright: '--canvas-success-bright',
  caution: '--canvas-caution',
  warning: '--canvas-warning',
  danger: '--canvas-danger',
  accent: '--canvas-accent',
  accentBright: '--canvas-accent-bright',
  info: '--canvas-info',
  hold: '--canvas-hold',
  lock: '--canvas-lock',
  text: '--canvas-text',
  textDim: '--canvas-text-dim',
  ink: '--canvas-ink',
  onAccent: '--canvas-on-accent',
  overlayBg: '--canvas-overlay-bg',
  overlayBorder: '--canvas-overlay-border',
  blockFree: '--block-free',
  blockReserved: '--block-reserved',
  blockOccupied: '--block-occupied',
  blockCaution: '--block-caution',
  blockDisabled: '--block-disabled',
  blockManual: '--block-manual',
  trainBody: '--train-body',
  trainBodyManual: '--train-body-manual',
  trainBodyTeardown: '--train-body-teardown',
  trainBodyEb: '--train-body-eb',
  trainBodyUnidentified: '--train-body-unidentified',
  trainLineMoving: '--train-line-moving',
  trainLineWaiting: '--train-line-waiting',
  trainLineStopped: '--train-line-stopped',
  trainLineLost: '--train-line-lost',
  highlightLocate: '--canvas-highlight-locate',
};

/** Dark-theme values, used when a token resolves empty (no stylesheet yet, or
 *  the node test environment where getComputedStyle is unavailable). Mirrors the
 *  dark block in layout-themes.css. */
const FALLBACK: CanvasPalette = {
  bg: '#0a0a0c',
  grid: '#2a2a34',
  axis: '#4a4a5a',
  origin: '#8257e5',
  track: '#666666',
  trackDim: '#444444',
  success: '#04d361',
  successBright: '#00ff88',
  caution: '#ffd93d',
  warning: '#ff9500',
  danger: '#e83f5b',
  accent: '#8257e5',
  accentBright: '#c8a2ff',
  info: '#00d4ff',
  hold: '#8b4513',
  lock: '#ff8c00',
  text: '#ffffff',
  textDim: '#999999',
  ink: '#333333',
  onAccent: '#000000',
  overlayBg: '#0d1020',
  overlayBorder: '#646496',
  blockFree: '#00d4ff',
  blockReserved: '#04d361',
  blockOccupied: '#ef4444',
  blockCaution: '#fde047',
  blockDisabled: '#6c6c78',
  blockManual: '#ff9500',
  trainBody: '#ffffff',
  trainBodyManual: '#ffe066',
  trainBodyTeardown: '#c8a2ff',
  trainBodyEb: '#8257e5',
  trainBodyUnidentified: '#8a8a99',
  trainLineMoving: '#ffffff',
  trainLineWaiting: '#ffd93d',
  trainLineStopped: '#a3a3b5',
  trainLineLost: '#c8a2ff',
  highlightLocate: '#ffd93d',
};

let cache: CanvasPalette | null = null;

function resolvePalette(): CanvasPalette {
  if (typeof document === 'undefined' || typeof getComputedStyle === 'undefined') {
    return FALLBACK;
  }
  const cs = getComputedStyle(document.documentElement);
  const out = {} as CanvasPalette;
  (Object.keys(TOKENS) as (keyof CanvasPalette)[]).forEach((key) => {
    const value = cs.getPropertyValue(TOKENS[key]).trim();
    out[key] = value || FALLBACK[key];
  });
  return out;
}

/** The current canvas palette. Resolved lazily and cached; the renderer reads
 *  plain properties off this every frame. */
export function getCanvasPalette(): CanvasPalette {
  if (!cache) cache = resolvePalette();
  return cache;
}

/** Drop the cache so the next getCanvasPalette() re-reads the tokens. Called by
 *  the canvas theme channel after data-canvas-theme changes. */
export function invalidateCanvasPalette(): void {
  cache = null;
}

const alphaCache = new Map<string, string>();

/** Return `color` at the given alpha as an rgba() string. Accepts #rgb / #rrggbb
 *  / rgb() / rgba() input. alpha === 1 returns the input unchanged. Memoized. */
export function withAlpha(color: string, alpha: number): string {
  if (alpha === 1) return color;
  const key = `${color}@${alpha}`;
  const hit = alphaCache.get(key);
  if (hit !== undefined) return hit;
  const rgb = parseRgb(color);
  const out = rgb ? `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${alpha})` : color;
  alphaCache.set(key, out);
  return out;
}

/** Return `color` as a bare `"r, g, b"` triple (no alpha, no wrapper). Accepts
 *  the same inputs as withAlpha. Used where a sink composes its own
 *  `rgba(${triple}, ${alpha})` — e.g. the renderer flash style. Falls back to
 *  the input string when it cannot be parsed. */
export function rgbTriple(color: string): string {
  const rgb = parseRgb(color);
  return rgb ? `${rgb.r}, ${rgb.g}, ${rgb.b}` : color;
}

function parseRgb(color: string): { r: number; g: number; b: number } | null {
  const c = color.trim();
  if (c.startsWith('#')) {
    let h = c.slice(1);
    if (h.length === 3)
      h = h
        .split('')
        .map((x) => x + x)
        .join('');
    if (h.length !== 6) return null;
    return {
      r: parseInt(h.slice(0, 2), 16),
      g: parseInt(h.slice(2, 4), 16),
      b: parseInt(h.slice(4, 6), 16),
    };
  }
  const m = c.match(/rgba?\(([^)]+)\)/);
  if (m) {
    const parts = m[1].split(',').map((x) => parseFloat(x));
    if (parts.length >= 3) return { r: parts[0], g: parts[1], b: parts[2] };
  }
  return null;
}
