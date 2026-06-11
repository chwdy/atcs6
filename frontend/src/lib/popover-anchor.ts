// popover-anchor.ts — Anchor a floating element over a set of grid tiles.
//
// Used by the block- and platform-creation popups so the dialog hovers over the
// tiles being edited and follows them as the user pans/zooms the canvas, instead
// of sitting as a full-screen centered modal.
//
// The main world/live canvas is sized in CSS pixels (renderer.ts sets
// canvas.width = parent.clientWidth with no devicePixelRatio multiply), so the
// camera's gridToScreenX/Y return CSS-pixel offsets relative to the canvas
// top-left. We add the canvas bounding rect to land in viewport coordinates for
// a `position: fixed` element.

interface Tile {
  x: number;
  y: number;
}

/**
 * Position `el` over the on-screen centroid of `tiles` and keep it there while
 * the viewport changes. `el` must be `position: fixed`; this writes its
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
    // On-screen centroid of the tile set, in canvas-local CSS pixels.
    let sx = 0;
    let sy = 0;
    for (const t of tiles) {
      sx += camera.gridToScreenX(t.x, t.y);
      sy += camera.gridToScreenY(t.x, t.y);
    }
    sx /= tiles.length;
    sy /= tiles.length;

    const rect = canvas.getBoundingClientRect();
    const anchorX = rect.left + sx; // viewport coords of the centroid
    const anchorY = rect.top + sy;

    const w = el.offsetWidth || 320;
    const h = el.offsetHeight || 200;
    const margin = 12; // keep this far from the viewport edges
    const gap = 18; // distance between the anchor point and the card edge

    // Prefer the card above the tiles (so it doesn't cover them); drop below
    // when there isn't room above.
    let left = anchorX - w / 2;
    let top = anchorY - h - gap;
    if (top < margin) {
      top = anchorY + gap;
    }

    left = Math.max(margin, Math.min(left, window.innerWidth - w - margin));
    top = Math.max(margin, Math.min(top, window.innerHeight - h - margin));

    el.style.left = `${Math.round(left)}px`;
    el.style.top = `${Math.round(top)}px`;
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
