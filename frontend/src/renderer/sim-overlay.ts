// sim-overlay.ts — the simulation draw layer registered on the core renderer.
//
// This is the one place that knows how to draw simulation state on top of the
// layout: active route overlays, trains, and the train-placement ghost. It
// reuses the existing `RendererRouteHandler` and `RendererTrainHandler` (still
// owned by the Renderer) — it only decides *when* in the z-order they draw, and
// it owns the per-frame snapshot build that used to live in `Renderer.render`.
//
// Because the snapshot is the renderer's only dependency on the `stateManager`
// singleton, building it here (with stateManager injected) is what lets the core
// renderer drop its `stateManager` import entirely.

import { buildRenderSnapshot, type SnapshotStateManager } from './render-snapshot';
import type { OverlayPhase, RendererOverlay } from './renderer-overlay';
import type { NetworkPayload } from './payload-types';
import type { Renderer } from './renderer';
import type { TrainSnap } from './renderer-trains';

export class SimRendererOverlay implements RendererOverlay {
  private readonly stateManager: SnapshotStateManager | null | undefined;

  constructor(stateManager: SnapshotStateManager | null | undefined) {
    this.stateManager = stateManager;
  }

  draw(phase: OverlayPhase, network: NetworkPayload, renderer: Renderer): void {
    switch (phase) {
      case 'afterStations':
        // Route overlays (reachable highlights, active routes, conflict flash).
        renderer.routeHandler.drawRouteOverlays();
        break;

      case 'afterBlockDirection':
        // Build the per-frame sim snapshot so trains read one coherent view,
        // then draw the trains on top of the route wash. The span endpoints are
        // richer at runtime than render-snapshot types them, hence the widen.
        renderer.currentSnapshot = buildRenderSnapshot(network, this.stateManager) as unknown as {
          trains?: Record<string, TrainSnap>;
        };
        (network.trains || []).forEach((train) => {
          renderer.trainHandler.drawTrain(train);
        });
        break;

      case 'top':
        // Train-placement ghost sits above the editor build/hover previews.
        renderer._drawTrainGhost();
        break;
    }
  }
}
