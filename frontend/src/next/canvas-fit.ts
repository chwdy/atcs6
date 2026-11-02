/**
 * Fit the canvas to the whole layout after a screen changed its size: the
 * Locomotives screen during a calibration run, and the Logs screen, both of
 * which show the canvas at 40% of the width.
 */

/** The App parts a fit needs. */
export interface CanvasFitHost {
  renderer?: { fitToContent(tracks: any[], margin?: number, insets?: { left: number; bottom: number }): void; requestRedraw(opts?: any): void };
  currentNetwork?: any;
  _canvasChromeInsets?(): { left: number; bottom: number };
}

/** Run `fn` once the browser has laid the page out again (the canvas has its new size). */
export function afterLayout(fn: () => void): void {
  requestAnimationFrame(() => requestAnimationFrame(fn));
}

export function fitCanvas(host: CanvasFitHost): void {
  const tracks = host.currentNetwork?.tracks;
  if (!host.renderer || !tracks?.length) return;
  host.renderer.fitToContent(tracks, undefined, host._canvasChromeInsets?.() ?? { left: 0, bottom: 0 });
  host.renderer.requestRedraw({ reason: 'fit-view' });
}
