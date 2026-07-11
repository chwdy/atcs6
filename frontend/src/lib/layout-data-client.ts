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
//     (track geometry, blocks, platforms, stations, switches, signals, tunnels,
//     speed profiles, layout persistence, scale, and the network fetch the
//     renderer draws from). This is the contract the reusable component needs.
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
  getNetwork(): Promise<any>;
  getNetworkSteps(): Promise<any>;
  getNetworkStats(): Promise<any>;

  // --- Individual track geometry ---
  getTrack(x: number, y: number): Promise<any>;
  updateTrack(x: number, y: number, updates: unknown): Promise<any>;
  deleteTrack(x: number, y: number): Promise<any>;
  addTracksBulk(tracks: unknown): Promise<any>;
  deleteTracksBulk(positions: unknown, opts?: { force?: boolean }): Promise<any>;

  // --- Blocks ---
  getBlocks(): Promise<any>;
  createBlock(blockId?: string | null, name?: string, options?: Record<string, unknown>): Promise<any>;
  getBlock(blockId: string): Promise<any>;
  updateBlock(blockId: string, data: unknown): Promise<any>;
  deleteBlock(blockId: string): Promise<any>;
  addTracksToBlock(blockId: string, positions: unknown, force?: boolean): Promise<any>;
  extendBlock(blockId: string, positions: unknown, force?: boolean, forceOverwrite?: boolean): Promise<any>;
  canExtendBlock(blockId: string, x: number, y: number): Promise<any>;
  removeBlockTiles(blockId: string, positions: unknown): Promise<any>;

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

  // --- Power districts ---
  createPowerDistrict(data: unknown): Promise<any>;
  listPowerDistricts(): Promise<any>;
  getPowerDistrict(districtId: string): Promise<any>;
  updatePowerDistrict(districtId: string, patch: unknown): Promise<any>;
  deletePowerDistrict(districtId: string): Promise<any>;
  addBlockToDistrict(districtId: string, blockId: string): Promise<any>;
  addBlocksToDistrict(districtId: string, blockIds: string[]): Promise<any>;
  removeBlockFromDistrict(districtId: string, blockId: string): Promise<any>;

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
  trainSetCab(trainId: string, cab: string): Promise<any>;
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
    acquisition_mode?: string;
  }): Promise<any>;
  getPaths(filters?: { train_id?: string; block_id?: string; status?: string }): Promise<any>;
  getPath(pathId: string): Promise<any>;
  cancelPath(pathId: string): Promise<any>;
  extendPath(
    pathId: string,
    opts?: {
      requested_destination_block_id?: string;
      requested_destination_direction?: string;
      acquisition_mode?: string;
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
  activateRoute(routeId: string): Promise<any>;
  deactivateRoute(routeId: string): Promise<any>;
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
    options?: { force?: boolean; startMode?: string; startTaskIndex?: number | null }
  ): Promise<any>;

  // --- Speed profiles (train accel/brake curves) ---
  getSpeedProfiles(): Promise<any[]>;
  getSpeedProfile(profileId: string): Promise<any>;
  createSpeedProfile(data: unknown): Promise<any>;
  updateSpeedProfile(profileId: string, data: unknown): Promise<any>;
  deleteSpeedProfile(profileId: string): Promise<any>;
}

/** The full client the current host wires up — both halves in one object. */
export type FullDataClient = LayoutDataClient & SimClient;
