// renderer-overlay.ts — the hook by which optional layers draw on top of the
// core layout render.
//
// The core renderer draws only the layout (tracks, blocks, platforms, stations,
// signals, tunnels). Anything that is *not* layout — trains, active routes, the
// train-placement ghost — is drawn by a registered `RendererOverlay`. With no
// overlay registered the renderer is a pure layout viewer; the sim app registers
// `SimRendererOverlay` (sim-overlay.ts) to restore the full simulation view.
//
// Overlays draw at named z-order points within the live layer rather than in one
// block, because sim draws are interleaved with layout/editor draws (route wash
// below the train, trains below the debug arrows, ghost on top). Each point is
// named for its position in `Renderer._drawLive`, not for what draws there.

import type { NetworkPayload } from './payload-types';
import type { Renderer } from './renderer';

/**
 * A z-order point in the live layer where overlays may draw:
 *  - `afterStations`        — above platforms/stations, below the block-direction
 *                             overlay and trains (where route overlays go).
 *  - `afterBlockDirection`  — above the block-direction overlay, below the
 *                             inspection debug arrows (where trains go).
 *  - `top`                  — above the editor build/hover previews (where the
 *                             train-placement ghost goes).
 */
export type OverlayPhase = 'afterStations' | 'afterBlockDirection' | 'top';

/** A draw layer the host registers on the renderer via `registerOverlay`. */
export interface RendererOverlay {
  /** Paint this overlay's contribution for the given z-order point. */
  draw(phase: OverlayPhase, network: NetworkPayload, renderer: Renderer): void;
}
