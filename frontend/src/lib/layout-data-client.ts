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
//     (track geometry, segments, blocks, platforms, stations, switches,
//     signals, tunnels, speed profiles, layout persistence, scale, and the
//     network fetch the renderer draws from). The track model is tile ->
//     segment -> block: a `Segment` carries geometry, a `Block` groups segments
//     and carries power / occupancy. This is the contract the reusable
//     component needs.
//   - `SimClient` — everything that drives or inspects the *simulation* (trains,
//     control handles, ATO, paths, routes, route-runs, ops). The sim is a plugin
//     on top of the layout component; only the sim plugin needs this surface.
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
  getNetworkSteps(): Promise<any>;
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
  extendSegment(
    segmentId: string,
    positions: unknown,
    force?: boolean,
    forceOverwrite?: boolean
  ): Promise<any>;
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

  // --- Signals (placement / geometry) ---
  createSignal(
    x: number,
    y: number,
    detectionDirection: string,
    signalType?: string,
    name?: string | null,
    autoConnect?: boolean
  ): Promise<any>;
  getSignal(signalId: string): Promise<any>;
  getSignalAt(x: number, y: number): Promise<any>;
  updateSignal(signalId: string, updates: unknown): Promise<any>;
  deleteSignal(signalId: string): Promise<any>;
  deleteSignalAt(x: number, y: number): Promise<any>;
  validateSignalPlacement(x: number, y: number): Promise<any>;
  getReachableBlocks(signalId: string): Promise<any>;

  // --- Platforms ---
  createPlatform(data: unknown): Promise<any>;
  getPlatform(platformId: string): Promise<any>;
  updatePlatform(platformId: string, updates: unknown): Promise<any>;
  deletePlatform(platformId: string): Promise<any>;
  validatePlatformPlacement(x: number, y: number): Promise<any>;
  extendPlatform(platformId: string, positions: unknown): Promise<any>;
  removePlatformTile(platformId: string, x: number, y: number): Promise<any>;
  assignPlatformTileToBlock(platformId: string, x: number, y: number, blockId: string): Promise<any>;
  getStation(stationId: string): Promise<any>;

  // --- Logical station groups ---
  createStationGroup(data: unknown): Promise<any>;
  listStationGroups(): Promise<any>;
  deleteStationGroup(stationId: string): Promise<any>;
  updateStationGroup(stationId: string, patch: unknown): Promise<any>;
  addPlatformToStation(stationId: string, platformId: string, force?: boolean): Promise<any>;
  removePlatformFromStation(stationId: string, platformId: string): Promise<any>;

  // --- Switches (geometry / config) ---
  getSwitches(): Promise<any>;
  getSwitch(switchId: string): Promise<any>;
  updateSwitch(switchId: string, updates: unknown): Promise<any>;
  deleteSwitch(switchId: string): Promise<any>;

  // --- Tunnels ---
  listTunnels(): Promise<any>;
  createTunnel(a: unknown, b: unknown): Promise<any>;
  deleteTunnel(x: number, y: number, edge: string): Promise<any>;

  // --- Layout persistence + scale ---
  getLayouts(): Promise<any>;
  saveLayout(name: string, description?: string): Promise<any>;
  loadLayout(name: string): Promise<any>;
  deleteLayout(name: string): Promise<any>;
  clearNetwork(): Promise<any>;
  getScale(): Promise<any>;
  setScale(scale: number): Promise<any>;
}

/**
 * The simulation data surface: drive and inspect trains, paths, routes, and
 * run control. Only the sim plugin needs this — the reusable layout component
 * does not.
 */
export interface SimClient {
  // --- Train CRUD + inspection ---
  createTrain(trainData: unknown): Promise<any>;
  getTrains(): Promise<any>;
  getAssignedRoutesByTrain(): Promise<any>;
  deleteTrain(trainId: string): Promise<any>;
  updateTrain(trainId: string, updates: unknown): Promise<any>;
  getTrain(trainId: string): Promise<any>;
  getTrainLookahead(trainId: string): Promise<any>;
  getInspectorEntityTrain(trainId: string): Promise<any>;
  getOnboardState(trainId: string): Promise<any>;

  // --- Train control ---
  trainPowerOn(trainId: string): Promise<any>;
  trainPowerOff(trainId: string): Promise<any>;
  setHandle(trainId: string, position: string): Promise<any>;
  releaseEmergency(trainId: string): Promise<any>;
  setControlMode(trainId: string, mode: string): Promise<any>;
  setManualModeType(trainId: string, modeType: string): Promise<any>;
  setManualPowerPercent(trainId: string, percent: number): Promise<any>;
  atoGo(trainId: string): Promise<any>;
  atoStop(trainId: string): Promise<any>;

  // --- Simulation + server ops ---
  controlSimulation(action: string, speed?: number | null): Promise<any>;
  restartServer(): Promise<any>;
  healthCheck(): Promise<any>;

  // --- Paths (dispatcher) ---
  createPath(opts?: {
    train_id?: string;
    requested_destination_block_id?: string;
    requested_destination_direction?: string;
  }): Promise<any>;
  getPaths(filters?: { train_id?: string; block_id?: string; status?: string }): Promise<any>;
  getPath(pathId: string): Promise<any>;
  cancelPath(pathId: string): Promise<any>;
  extendPath(
    pathId: string,
    opts?: {
      requested_destination_block_id?: string;
      requested_destination_direction?: string;
    }
  ): Promise<any>;
  getReachableEndpoints(blockId: string, includeStations?: boolean): Promise<any>;
  getReachableEndpointsForTrain(trainId: string, includeStations?: boolean): Promise<any>;
  getPathReachableExtensions(pathId: string, includeStations?: boolean): Promise<any>;

  // --- Switch operational state (throw) ---
  setSwitchState(switchId: string, state: number): Promise<any>;

  // --- Routes ---
  getRoutes(): Promise<any>;
  getRoute(routeId: string): Promise<any>;
  createRoute(routeData: unknown): Promise<any>;
  updateRoute(routeId: string, updates: unknown): Promise<any>;
  deleteRoute(routeId: string): Promise<any>;
  getRouteStatus(routeId: string): Promise<any>;
  checkViability(pairs: unknown): Promise<any>;
  getReachableFrom(type: string, id: string): Promise<any>;
  getRouteHistory(routeId: string): Promise<any>;

  // --- Route runs ---
  getRouteRuns(): Promise<any>;
  getRouteRun(runNumber: number): Promise<any>;
  patchRouteRunOverrides(runNumber: number, body: unknown): Promise<any>;
  clearRouteRunOverride(runNumber: number, field: string): Promise<any>;
  assignRoute(
    trainId: string,
    routeId: string | null,
    options?: { startMode?: string; startTaskIndex?: number | null }
  ): Promise<any>;

  // --- Speed profiles: the Default profile, sets by model, train instances ---
  getSpeedProfiles(): Promise<any[]>;
  getSpeedLibrary(): Promise<{ profiles: any[]; models: any[] }>;
  getSpeedProfile(profileId: number | string): Promise<any>;
  updateSpeedProfile(profileId: number | string, data: unknown): Promise<any>;
  deleteSpeedProfile(profileId: number | string): Promise<any>;
  renameLocomotiveModel(modelId: number, name: string): Promise<any>;
  deleteLocomotiveModel(modelId: number): Promise<any>;
}

/** The full client the current host wires up — both halves in one object. */
export type FullDataClient = LayoutDataClient & SimClient;
