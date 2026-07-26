// popover-anchor.ts — Anchor a floating element beside a set of grid tiles.
//
// Used by the block- and platform-creation popups so the dialog hovers next to
// the tiles being edited and follows them as the user pans/zooms the canvas,
// instead of sitting as a full-screen centered modal.
//
// The canvas→viewport projection and the beside-not-over placement rule both
// live in window-placement.ts, shared with every floating window, so a dialog
// and a window dodge the layout the same way.

import { placeClearOfLayout, tilesScreenRect } from './window-placement';

interface Tile {
  x: number;
  y: number;
}

/**
 * Position `el` beside the on-screen footprint of `tiles` and keep it there
 * while the viewport changes. `el` must be `position: fixed`; this writes its
 * `left`/`top`. Returns a cleanup function that detaches all listeners — call it
 * when the popup closes.
 *
 * If the renderer/camera/canvas are unavailable (or `tiles` is empty), this is a
 * no-op and the element keeps whatever position its CSS gives it.
 */
export function anchorToTiles(el: HTMLElement, tiles: Tile[]): () => void {
  const camera = (window as any).app?.renderer?.camera;
  const canvas = document.getElementById('rail-canvas') as HTMLCanvasElement | null;

  if (!camera || !canvas || !tiles || tiles.length === 0) {
    return () => {};
  }

  const reposition = () => {
    const w = el.offsetWidth || 320;
    const h = el.offsetHeight || 200;

    // The tiles' full screen footprint, not just their centroid: a dialog
    // centred on the centroid of a long block still covers most of it.
    const anchor = tilesScreenRect(tiles);
    if (!anchor) {
      return; // camera/canvas went away mid-edit — leave the last position
    }

    // Sit BESIDE the tiles being edited, level with them, rather than centred
    // over their midpoint — the point of anchoring is to keep the edit target
    // visible while the dialog is open, and the old "centred, preferring above"
    // rule covered the layout whenever the tiles were near the top of the view.
    // placeClearOfLayout applies the same right → left → below → above search
    // every floating window uses, so dialogs and windows dodge content alike.
    const { left, top } = placeClearOfLayout(
      { width: w, height: h },
      {
        avoid: anchor,
        preferred: { left: anchor.left, top: Math.round((anchor.top + anchor.bottom) / 2 - h / 2) },
      }
    );

    el.style.left = `${left}px`;
    el.style.top = `${top}px`;
  };

  reposition();
  const unsubscribe = camera.onChange(reposition);
  window.addEventListener('resize', reposition);
  // Capture-phase scroll so we react to any scrolling ancestor, not just window.
  window.addEventListener('scroll', reposition, true);

  return () => {
    try {
      unsubscribe?.();
    } catch {
      /* camera gone — ignore */
    }
    window.removeEventListener('resize', reposition);
    window.removeEventListener('scroll', reposition, true);
  };
}
