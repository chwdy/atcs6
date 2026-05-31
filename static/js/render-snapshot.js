// render-snapshot.js — per-frame immutable view of sim state for renderers
// ============================================================================
// Built once per rAF flush by Renderer._flushRedraw. Renderers consume the
// snapshot instead of reaching into window.stateManager or mutable network
// fields directly. The decoupling lets renderer handlers be tested with
// hand-built fixtures and makes "what does the renderer see this frame?"
// a single inspectable object.
//
// Phase 6 of the canvas refactor. Current scope: train controller spans
// (the previous reach-in at renderer-trains.js:19). Track / signal / block
// state already flow through `network.*` so they don't need snapshotting
// yet; future expansion can pre-resolve their color categories here.
// ============================================================================

/**
 * Build a render snapshot from the current network payload and the
 * StateManager. Pure function — does not mutate either argument.
 *
 * Shape:
 *   {
 *     trains: { [trainId]: { id, cab, span: { occupiedTiles, leadingEnd, endA, endB } } }
 *   }
 *
 * Trains without a live controller span are omitted (renderers should
 * gracefully skip them — same as the legacy `if (!controller?.span?...)` guard).
 */
function buildRenderSnapshot(network, stateManager) {
    const snap = { trains: {} };
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

if (typeof window !== 'undefined') {
    window.buildRenderSnapshot = buildRenderSnapshot;
}
