// block-color.ts — one deterministic color per block (the grouping unit).
//
// The block inspector swatch and the canvas grouping overlay must agree on a
// block's color without sharing any runtime state. Both derive the hue from the
// block id alone, so the same id always paints the same color regardless of
// insertion order or which view computes it first.
//
// This is the id-keyed counterpart to the segment overlay's golden-angle cache:
// segments take a color from draw order, blocks take one from their id.

/** Deterministic hue (0–359) for a block id. */
export function blockHue(blockId: string): number {
  let h = 0;
  for (let i = 0; i < blockId.length; i++) {
    h = (h * 31 + blockId.charCodeAt(i)) >>> 0;
  }
  // Spread ids around the wheel with the golden angle so similar ids (BLK_001,
  // BLK_002) still land far apart and read as distinct colors.
  return Math.round((h * 137.508) % 360);
}

/** `hsla(...)` fill for a block id; `alpha` defaults to the overlay's 0.6. */
export function blockColorCss(blockId: string, alpha = 0.6): string {
  return `hsla(${blockHue(blockId)}, 70%, 50%, ${alpha})`;
}
