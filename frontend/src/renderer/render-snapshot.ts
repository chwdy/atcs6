// render-snapshot.ts — per-frame immutable view of sim state for renderers.
//
// Migrated from static/js/render-snapshot.js (longterm_todo.md item #7, the
// first renderer-cluster slice). Behavior is unchanged and covered natively by
// render-snapshot.test.ts (the Vitest port of the old Node-eval
// tests/test_render_snapshot.py).
//
// Built once per rAF flush by Renderer._flushRedraw. Renderers consume the
// snapshot instead of reaching into the StateManager or mutable network
// fields directly. The decoupling lets renderer handlers be tested with
// hand-built fixtures and makes "what does the renderer see this frame?"
// a single inspectable object.
//
// Phase 6 of the canvas refactor. Current scope: train controller spans
// (the previous reach-in at renderer-trains.js:19). Track / signal / block
// state already flow through `network.*` so they don't need snapshotting
// yet; future expansion can pre-resolve their color categories here.
//
// Part of the Vite bundle; Renderer._flushRedraw imports and calls it directly.

/** A controller span as the renderer reads it (snake_case from the payload). */
export interface ControllerSpan {
  occupied_tiles?: readonly unknown[] | null;
  leading_end?: unknown;
  end_a?: unknown;
  end_b?: unknown;
}

export interface TrainController {
  cab?: unknown;
  facing_direction?: unknown;
  control_mode?: unknown;
  route_teardown_pending?: unknown;
  eb_active?: unknown;
  presence_unknown?: unknown;
  span?: ControllerSpan | null;
}

export interface DispatcherProgress {
  waiting_for_block_id?: unknown;
}

/** The slice of StateManager buildRenderSnapshot reads (both methods optional). */
export interface SnapshotStateManager {
  getController?(trainId: string): TrainController | null | undefined;
  getDispatcherProgress?(trainId: string): DispatcherProgress | null | undefined;
}

export interface SnapshotNetwork {
  trains?: ReadonlyArray<{ id: string; is_unidentified?: boolean }> | null;
}

/** Span fields pre-named in camelCase so the renderer has one shape to consume. */
export interface RenderSnapshotSpan {
  occupiedTiles: readonly unknown[];
  leadingEnd: unknown;
  endA: unknown;
  endB: unknown;
}

export interface RenderSnapshotTrain {
  id: string;
  cab: unknown;
  facing: unknown;
  /** 'manual' | 'ato' (or null). Drives the manual-mode body tint. */
  controlMode: unknown;
  /** True while an operator route teardown (abort/deactivate) is latched. */
  teardownPending: boolean;
  /** True while the emergency brake is active. Drives the purple body tint. */
  ebActive: boolean;
  /** True for a runtime-detected unidentified train the app cannot drive. Drives the
   *  steel body tint and the dashed outline that mark it apart. */
  isUnidentified: boolean;
  /** True while no wired block backs the calculated position — the train may
   *  physically be gone. Drives the question-mark badge at the head. */
  presenceUnknown: boolean;
  span: RenderSnapshotSpan;
  waitingForBlockId: unknown;
}

export interface RenderSnapshot {
  trains: Record<string, RenderSnapshotTrain>;
}

/**
 * Build a render snapshot from the current network payload and the
 * StateManager. Pure function — does not mutate either argument.
 *
 * Trains without a live controller span are omitted (renderers should
 * gracefully skip them — same as the legacy `if (!controller?.span?...)` guard).
 */
export function buildRenderSnapshot(
  network: SnapshotNetwork | null | undefined,
  stateManager: SnapshotStateManager | null | undefined,
): RenderSnapshot {
  const snap: RenderSnapshot = { trains: {} };
  if (!network || !network.trains || !stateManager) return snap;

  for (const train of network.trains) {
    const controller = stateManager.getController?.(train.id);
    if (!controller?.span) continue;
    const span = controller.span;
    if (!span.occupied_tiles || span.occupied_tiles.length === 0) continue;

    // Shallow-copy span fields the renderer reads. Cheap because each
    // train carries O(few-tens) of occupied tiles.
    // Dispatcher-owned diagnostic for the "Show stuck" overlay: the block
    // this train is held waiting on, or null when it is not stuck.
    const progress = stateManager.getDispatcherProgress?.(train.id);

    snap.trains[train.id] = {
      id: train.id,
      cab: controller.cab ?? null,
      facing: controller.facing_direction ?? null,
      controlMode: controller.control_mode ?? null,
      teardownPending: controller.route_teardown_pending === true,
      ebActive: controller.eb_active === true,
      isUnidentified: train.is_unidentified === true,
      presenceUnknown: controller.presence_unknown === true,
      span: {
        occupiedTiles: span.occupied_tiles,
        leadingEnd: span.leading_end,
        endA: span.end_a,
        endB: span.end_b,
      },
      waitingForBlockId: progress?.waiting_for_block_id ?? null,
    };
  }
  return snap;
}
