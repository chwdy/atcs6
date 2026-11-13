// renderer-overlay.ts — the hook by which optional layers draw on top of the
// core layout render.
//
// The core renderer draws only the layout (tracks, blocks, tunnels). The
// Operate screen registers a `RunningTrainOverlay` (running-train-overlay.ts)
// that draws each running train on top of it. With no overlay registered the
// renderer is a pure layout viewer.
//
// Overlays draw at named z-order points within the live layer. Each point is
// named for its position in `Renderer._drawLive`, not for what draws there.

import type { NetworkPayload } from './payload-types';
import type { Renderer } from './renderer';

/**
 * A z-order point in the live layer where overlays may draw:
 *  - `afterBlockDirection`  — above the block-direction overlay, below the
 *                             inspection debug arrows (where trains go).
 *  - `top`                  — above the editor build previews.
 */
export type OverlayPhase = 'afterBlockDirection' | 'top';

/** A draw layer the host registers on the renderer via `registerOverlay`. */
export interface RendererOverlay {
  /** Paint this overlay's contribution for the given z-order point. */
  draw(phase: OverlayPhase, network: NetworkPayload, renderer: Renderer): void;
}
