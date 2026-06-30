// district-color.ts — one deterministic color per power district.
//
// The power-district inspector swatch and the canvas color overlay must agree on
// a district's color without sharing any runtime state. Both derive the hue from
// the district id alone, so the same id always paints the same color regardless
// of insertion order or which view computes it first.

/** Deterministic hue (0–359) for a power-district id. */
export function districtHue(districtId: string): number {
  let h = 0;
  for (let i = 0; i < districtId.length; i++) {
    h = (h * 31 + districtId.charCodeAt(i)) >>> 0;
  }
  // Spread ids around the wheel with the golden angle so similar ids (D_001,
  // D_002) still land far apart and read as distinct colors.
  return Math.round((h * 137.508) % 360);
}

/** `hsla(...)` fill for a district id; `alpha` defaults to the overlay's 0.6. */
export function districtColorCss(districtId: string, alpha = 0.6): string {
  return `hsla(${districtHue(districtId)}, 70%, 50%, ${alpha})`;
}
