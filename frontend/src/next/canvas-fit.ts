/**
 * Fit the canvas to the whole layout after a screen changed its size: the
 * Logs screen shows the canvas at 40% of the width.
 */

/** The App parts a fit needs. */
export interface CanvasFitHost {
  renderer?: { fitToContent(tracks: any[], margin?: number, insets?: { left: number; bottom: number }): void; requestRedraw(opts?: any): void };
  currentNetwork?: any;
}

/** Run `fn` once the browser has laid the page out again (the canvas has its new size). */
export function afterLayout(fn: () => void): void {
  requestAnimationFrame(() => requestAnimationFrame(fn));
}

export function fitCanvas(host: CanvasFitHost): void {
  const tracks = host.currentNetwork?.tracks;
  if (!host.renderer || !tracks?.length) return;
  host.renderer.fitToContent(tracks);
  host.renderer.requestRedraw({ reason: 'fit-view' });
}
