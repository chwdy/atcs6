// layout-data-client.ts — the data contract the layout editor + renderer depend on.
//
// The layout editor/renderer never talk to a concrete backend directly; they
// talk to an object that satisfies `LayoutDataClient`. The current Flask-backed
// `API` class (lib/api.ts) is one implementation; a future host (e.g. a separate
// layout-editor MVP on a different backend) provides its own implementation of
// the same interface and the editor code is reused unchanged.
//
// The surface is split in two:
//   - `LayoutDataClient` — everything needed to view and edit the *layout*
//     (track geometry, segments, blocks, switches, tunnels, layout persistence,
//     and the network fetch the renderer draws from). The track model is tile ->
//     segment -> block: a `Segment` carries geometry, a `Block` groups segments
//     and carries power / occupancy.
//   - `SimClient` — the few running requests the editor also sends (remove a
//     train, server ops, throw a switch).
//
// `API implements LayoutDataClient, SimClient` (in lib/api.ts) makes the
// compiler verify the concrete client satisfies both halves.

/**
 * The layout-only data surface: view + edit track geometry and the entities
 * that live on it. A reusable layout-editor host only needs to implement this.
 */
export interface LayoutDataClient {
  // --- Network fetch (the payload the renderer draws) + layout export ---
  getNetwork(layoutVersion?: string): Promise<any>;
  getNetworkStats(): Promise<any>;

  // --- Individual track geometry ---
  getTrack(x: number, y: number): Promise<any>;
  updateTrack(x: number, y: number, updates: unknown): Promise<any>;
  deleteTrack(x: number, y: number): Promise<any>;
  addTracksBulk(tracks: unknown): Promise<any>;
  deleteTracksBulk(positions: unknown, opts?: { force?: boolean }): Promise<any>;

  // --- Segments (the geometry axis; ids here are SEGMENT ids) ---
  getSegments(): Promise<any>;
  getSegment(segmentId: string): Promise<any>;
  createSegment(
    segmentId?: string | null,
    positions?: unknown,
    name?: string,
    options?: Record<string, unknown>
  ): Promise<any>;
  updateSegment(segmentId: string, data: unknown): Promise<any>;
  deleteSegment(segmentId: string): Promise<any>;
  addTracksToSegment(segmentId: string, positions: unknown, force?: boolean): Promise<any>;
  removeSegmentTiles(segmentId: string, positions: unknown): Promise<any>;
  extendSegment(segmentId: string, positions: unknown, forceOverwrite?: boolean): Promise<any>;
  canExtendSegment(segmentId: string, x: number, y: number): Promise<any>;
  getSegmentEndTiles(segmentId: string): Promise<any>;

  // --- Blocks (grouping + power + occupancy; ids here are BLOCK ids) ---
  getBlocks(): Promise<any>;
  createBlock(segmentIds: string[], name?: string, blockId?: string | null): Promise<any>;
  getBlock(blockId: string): Promise<any>;
  updateBlock(blockId: string, data: unknown): Promise<any>;
  deleteBlock(blockId: string): Promise<any>;
  addSegmentToBlock(blockId: string, segmentId: string): Promise<any>;
  removeSegmentFromBlock(blockId: string, segmentId: string): Promise<any>;
  getBlockVoltage(blockId: string): Promise<any>;

  // --- Switches (geometry / config) ---
  getSwitches(): Promise<any>;
  getSwitch(switchId: string): Promise<any>;
  updateSwitch(switchId: string, updates: unknown): Promise<any>;
  deleteSwitch(switchId: string): Promise<any>;

  // --- Tunnels ---
  listTunnels(): Promise<any>;
  createTunnel(a: unknown, b: unknown): Promise<any>;
  deleteTunnel(x: number, y: number, edge: string): Promise<any>;

  // --- Layout persistence ---
  getLayouts(): Promise<any>;
  saveLayout(name: string, description?: string): Promise<any>;
  loadLayout(name: string): Promise<any>;
  deleteLayout(name: string): Promise<any>;
  clearNetwork(): Promise<any>;
}

/** The running requests the editor also sends. */
export interface SimClient {
  // --- Server ops ---
  restartServer(): Promise<any>;
  healthCheck(): Promise<any>;

  // --- Switch operational state (throw) ---
  setSwitchState(switchId: string, state: number): Promise<any>;
}

/** The full client the current host wires up — both halves in one object. */
export type FullDataClient = LayoutDataClient & SimClient;
