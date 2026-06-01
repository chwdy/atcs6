// api.ts — Thin HTTP client over the Flask `/api` surface.
//
// Migrated from static/js/api.js (longterm_todo.md item #7, Wave 1 — foundation
// cluster). Behavior is unchanged from the classic file: each method is a thin
// wrapper over `_request`, the single fetch/JSON/error-normalization choke point.
//
// Part of the Vite bundle; the single client instance is imported as `{ api }`
// by the app, panels, and editor.

// Caller-supplied fetch options plus the custom `returnNullOn404` flag stripped
// before the request reaches fetch().
interface RequestOptions {
  method?: string;
  headers?: Record<string, string>;
  body?: unknown;
  returnNullOn404?: boolean;
  [key: string]: unknown;
}

interface RequestConfig {
  method?: string;
  headers: Record<string, string>;
  body?: unknown;
  returnNullOn404?: boolean;
  [key: string]: unknown;
}

export class API {
  baseUrl: string;

  constructor(baseUrl = '/api') {
    this.baseUrl = baseUrl;
  }

  /**
   * Centralized request handler to reduce boilerplate.
   * @param endpoint - API endpoint (e.g., '/network')
   * @param options - Fetch options (method, headers, body, etc.)
   * @param caller - Name of the calling method for logging
   * @returns JSON response or null
   */
  async _request(endpoint: string, options: RequestOptions = {}, caller = 'request'): Promise<any> {
    const url = `${this.baseUrl}${endpoint}`;
    const config: RequestConfig = {
      method: 'GET',
      headers: {},
      ...options,
    };

    // Handle body serialization and Content-Type
    if (config.body && typeof config.body !== 'string') {
      config.headers['Content-Type'] = 'application/json';
      config.body = JSON.stringify(config.body);
    }

    // Helper options (not passed to fetch)
    const returnNullOn404 = config.returnNullOn404;
    delete config.returnNullOn404;

    try {
      const response = await fetch(url, config as RequestInit);

      if (response.status === 404 && returnNullOn404) {
        return null;
      }

      if (!response.ok) {
        let errorMessage = `HTTP ${response.status}: ${response.statusText}`;
        let errorData: any = null;
        try {
          errorData = await response.json();
          if (errorData.error) errorMessage = errorData.error;
        } catch (e) {
          try {
            const text = await response.text();
            if (text) errorMessage = `HTTP ${response.status}: ${text}`;
          } catch (e2) {}
        }
        const error = new Error(errorMessage) as Error & { status?: number; data?: unknown };
        error.status = response.status;
        error.data = errorData;
        if (errorData) {
          Object.assign(error, errorData);
        }
        throw error;
      }

      if (response.status === 204) return null;

      return await response.json();
    } catch (e) {
      console.error(`[API] ${caller} failed:`, e);
      throw e;
    }
  }

  async getNetwork(): Promise<any> {
    return this._request('/network', {}, 'getNetwork');
  }

  /**
   * Get the inspector snapshot for a single train.
   * Returns null when the train was deleted between polls.
   */
  async getInspectorEntityTrain(trainId: string): Promise<any> {
    return this._request(
      `/inspector/entity/train/${encodeURIComponent(trainId)}`,
      { returnNullOn404: true },
      'getInspectorEntityTrain'
    );
  }

  /**
   * Get current network layout as StepRecorder steps.
   * Used by the scenario recorder to embed layout in scenario files.
   */
  async getNetworkSteps(): Promise<any> {
    const data = await this._request('/network/steps', {}, 'getNetworkSteps');
    return data?.steps || [];
  }

  // Individual track operations

  async getTrack(x: number, y: number): Promise<any> {
    return this._request(`/track/${x}/${y}`, {}, 'getTrack');
  }

  async updateTrack(x: number, y: number, updates: unknown): Promise<any> {
    return this._request(
      `/track/${x}/${y}`,
      {
        method: 'PATCH',
        body: updates,
      },
      'updateTrack'
    );
  }

  async deleteTrack(x: number, y: number): Promise<any> {
    return this._request(
      `/track/${x}/${y}`,
      {
        method: 'DELETE',
      },
      'deleteTrack'
    );
  }

  async addTracksBulk(tracks: unknown): Promise<any> {
    return this._request(
      '/tracks/bulk',
      {
        method: 'POST',
        body: { tracks },
      },
      'addTracksBulk'
    );
  }

  async deleteTracksBulk(positions: unknown, { force = false }: { force?: boolean } = {}): Promise<any> {
    return this._request(
      '/tracks/bulk-delete',
      {
        method: 'POST',
        body: { positions, force },
      },
      'deleteTracksBulk'
    );
  }

  async createTrain(trainData: unknown): Promise<any> {
    return this._request(
      '/trains',
      {
        method: 'POST',
        body: trainData,
      },
      'createTrain'
    );
  }

  /** Get all trains in the network. */
  async getTrains(): Promise<any> {
    const result = await this._request('/trains', {}, 'getTrains');
    return result.trains || [];
  }

  /**
   * Map of train_id -> assigned route_id (or null), from the /trains sibling
   * payload's `assigned_routes_by_train_id`. Used to tell which trains are
   * free for assignment.
   */
  async getAssignedRoutesByTrain(): Promise<any> {
    const result = await this._request('/trains', {}, 'getTrains');
    return result.assigned_routes_by_train_id || {};
  }

  /** Delete a train. */
  async deleteTrain(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}`,
      {
        method: 'DELETE',
      },
      'deleteTrain'
    );
  }

  /** Update train properties. */
  async updateTrain(trainId: string, updates: unknown): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}`,
      {
        method: 'PATCH',
        body: updates,
      },
      'updateTrain'
    );
  }

  /**
   * Control simulation state.
   * @param action - 'start', 'stop', 'reset', 'set_speed'
   * @param speed - Simulation speed multiplier
   */
  async controlSimulation(action: string, speed: number | null = null): Promise<any> {
    const data: { action: string; speed?: number } = { action };
    if (speed !== null) {
      data.speed = speed;
    }
    return this._request(
      '/simulation/control',
      {
        method: 'POST',
        body: data,
      },
      'controlSimulation'
    );
  }

  /**
   * Clear a fatal physical-entry conflict halt after the operator has
   * resolved the conflict. Returns { released, failed_runtime }; released
   * is false (HTTP still 200) when the conflict still stands.
   */
  async releaseFailedRuntime(): Promise<any> {
    return this._request(
      '/simulation/release-failed-runtime',
      {
        method: 'POST',
      },
      'releaseFailedRuntime'
    );
  }

  /**
   * Restart the server process. The server flushes autosave, then
   * replaces itself via os.execv. Caller should immediately switch
   * the UI into a reconnect-polling state.
   */
  async restartServer(): Promise<any> {
    return this._request(
      '/server/restart',
      {
        method: 'POST',
      },
      'restartServer'
    );
  }

  /**
   * Lightweight server health check. Used by the reconnect flow to
   * detect when a restarted server is back up.
   */
  async healthCheck(): Promise<any> {
    return this._request('/health', {}, 'healthCheck');
  }

  // === Block Management ===

  /** Get all blocks. */
  async getBlocks(): Promise<any> {
    return this._request('/blocks', {}, 'getBlocks');
  }

  /** Create a new block. */
  async createBlock(blockId: string | null = null, name = '', options: Record<string, unknown> = {}): Promise<any> {
    return this._request(
      '/blocks',
      {
        method: 'POST',
        body: {
          block_id: blockId,
          name,
          ...options,
        },
      },
      'createBlock'
    );
  }

  /** Get a single block by ID. */
  async getBlock(blockId: string): Promise<any> {
    return this._request(`/blocks/${encodeURIComponent(blockId)}`, {}, 'getBlock');
  }

  /**
   * Resolve what a block-relative direction means physically — the terminal
   * tile and compass exit heading — so the UI can render `F-(x,y)-E` labels.
   * @returns { terminal_tile: [x,y], terminal_heading: "N|E|S|W" }, or null when
   *   the block lacks geometry to disambiguate that direction.
   */
  async getBlockDirectionInfo(blockId: string, direction: string): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}/direction-info?direction=${encodeURIComponent(direction)}`,
      { returnNullOn404: true },
      'getBlockDirectionInfo'
    );
  }

  /** Update block properties. */
  async updateBlock(blockId: string, data: unknown): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}`,
      {
        method: 'PATCH',
        body: data,
      },
      'updateBlock'
    );
  }

  /** Delete a block. */
  async deleteBlock(blockId: string): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}`,
      {
        method: 'DELETE',
      },
      'deleteBlock'
    );
  }

  /** Add tracks to a block. */
  async addTracksToBlock(blockId: string, positions: unknown, force = false): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}/tracks`,
      {
        method: 'POST',
        body: { positions, force },
      },
      'addTracksToBlock'
    );
  }

  /**
   * Extend a block with new track positions.
   * Uses the /extend endpoint which supports signal removal with force=true.
   */
  async extendBlock(blockId: string, positions: unknown, force = false, forceOverwrite = false): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}/extend`,
      {
        method: 'POST',
        body: { positions, force, force_overwrite: forceOverwrite },
      },
      'extendBlock'
    );
  }

  /** Check if a block can be extended to a specific position. */
  async canExtendBlock(blockId: string, x: number, y: number): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}/can_extend`,
      {
        method: 'POST',
        body: { x, y },
      },
      'canExtendBlock'
    );
  }

  // === Signal Management ===

  /** Create a new signal. */
  async createSignal(
    x: number,
    y: number,
    detectionDirection: string,
    signalType = 'block',
    name: string | null = null,
    autoConnect = true
  ): Promise<any> {
    return this._request(
      '/signal',
      {
        method: 'POST',
        body: {
          x,
          y,
          detection_direction: detectionDirection,
          signal_type: signalType,
          name,
          auto_connect: autoConnect,
        },
      },
      'createSignal'
    );
  }

  /** Get a signal by ID. */
  async getSignal(signalId: string): Promise<any> {
    return this._request(`/signal/${encodeURIComponent(signalId)}`, {}, 'getSignal');
  }

  /** Get a signal at specific coordinates. */
  async getSignalAt(x: number, y: number): Promise<any> {
    return this._request(
      `/signal/${x}/${y}`,
      {
        returnNullOn404: true,
      },
      'getSignalAt'
    );
  }

  /** Update signal properties. */
  async updateSignal(signalId: string, updates: unknown): Promise<any> {
    return this._request(
      `/signal/${encodeURIComponent(signalId)}`,
      {
        method: 'PATCH',
        body: updates,
      },
      'updateSignal'
    );
  }

  /** Delete a signal. */
  async deleteSignal(signalId: string): Promise<any> {
    return this._request(
      `/signal/${encodeURIComponent(signalId)}`,
      {
        method: 'DELETE',
      },
      'deleteSignal'
    );
  }

  /** Delete signal at specific coordinates. */
  async deleteSignalAt(x: number, y: number): Promise<any> {
    return this._request(
      `/signal/${x}/${y}`,
      {
        method: 'DELETE',
      },
      'deleteSignalAt'
    );
  }

  /** Validate signal placement. */
  async validateSignalPlacement(x: number, y: number): Promise<any> {
    return this._request(`/signal/validate/${x}/${y}`, {}, 'validateSignalPlacement');
  }

  /** Get blocks reachable from a signal. */
  async getReachableBlocks(signalId: string): Promise<any> {
    return this._request(
      `/signal/${encodeURIComponent(signalId)}/reachable-blocks`,
      {},
      'getReachableBlocks'
    );
  }

  // === Station API Methods ===

  // === Platform API Methods (new naming, same physical entity) ===

  async createPlatform(data: unknown): Promise<any> {
    return this._request(
      '/platform',
      {
        method: 'POST',
        body: data,
      },
      'createPlatform'
    );
  }

  async getPlatform(platformId: string): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}`,
      {
        returnNullOn404: true,
      },
      'getPlatform'
    );
  }

  async updatePlatform(platformId: string, updates: unknown): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}`,
      {
        method: 'PATCH',
        body: updates,
      },
      'updatePlatform'
    );
  }

  async deletePlatform(platformId: string): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}`,
      {
        method: 'DELETE',
      },
      'deletePlatform'
    );
  }

  async validatePlatformPlacement(x: number, y: number): Promise<any> {
    return this._request(`/platform/validate/${x}/${y}`, {}, 'validatePlatformPlacement');
  }

  async extendPlatform(platformId: string, positions: unknown): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}/extend`,
      {
        method: 'POST',
        body: { positions },
      },
      'extendPlatform'
    );
  }

  async removePlatformTile(platformId: string, x: number, y: number): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}/remove_tile`,
      {
        method: 'POST',
        body: { x, y },
      },
      'removePlatformTile'
    );
  }

  async assignPlatformTileToBlock(platformId: string, x: number, y: number, blockId: string): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(platformId)}/assign_tile`,
      {
        method: 'POST',
        body: { x, y, block_id: blockId },
      },
      'assignPlatformTileToBlock'
    );
  }

  /** Legacy compatibility wrapper: get physical platform by ID. */
  async getStation(stationId: string): Promise<any> {
    return this._request(
      `/platform/${encodeURIComponent(stationId)}`,
      {
        returnNullOn404: true,
      },
      'getStation'
    );
  }

  // === Logical Station Group API Methods ===

  async createStationGroup(data: unknown): Promise<any> {
    return this._request(
      '/station',
      {
        method: 'POST',
        body: data,
      },
      'createStationGroup'
    );
  }

  async listStationGroups(): Promise<any> {
    return this._request('/station', {}, 'listStationGroups');
  }

  async deleteStationGroup(stationId: string): Promise<any> {
    return this._request(
      `/station/${encodeURIComponent(stationId)}`,
      {
        method: 'DELETE',
      },
      'deleteStationGroup'
    );
  }

  async updateStationGroup(stationId: string, patch: unknown): Promise<any> {
    return this._request(
      `/station/${encodeURIComponent(stationId)}`,
      {
        method: 'PATCH',
        body: patch,
      },
      'updateStationGroup'
    );
  }

  async addPlatformToStation(stationId: string, platformId: string): Promise<any> {
    return this._request(
      `/station/${encodeURIComponent(stationId)}/add-platform`,
      {
        method: 'POST',
        body: { platform_id: platformId },
      },
      'addPlatformToStation'
    );
  }

  async removePlatformFromStation(stationId: string, platformId: string): Promise<any> {
    return this._request(
      `/station/${encodeURIComponent(stationId)}/remove-platform`,
      {
        method: 'POST',
        body: { platform_id: platformId },
      },
      'removePlatformFromStation'
    );
  }

  /** Remove tiles from a block. */
  async removeBlockTiles(blockId: string, positions: unknown): Promise<any> {
    return this._request(
      `/blocks/${encodeURIComponent(blockId)}/tracks`,
      {
        method: 'DELETE',
        body: { positions },
      },
      'removeBlockTiles'
    );
  }

  /** Get a specific train. */
  async getTrain(trainId: string): Promise<any> {
    return this._request(`/trains/${encodeURIComponent(trainId)}`, {}, 'getTrain');
  }

  /** Get train lookahead data for the train monitor. */
  async getTrainLookahead(trainId: string): Promise<any> {
    return this._request(`/trains/${encodeURIComponent(trainId)}/lookahead`, {}, 'getTrainLookahead');
  }

  // === Train Control API ===

  /** Turn train power on. */
  async trainPowerOn(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/power-on`,
      {
        method: 'POST',
      },
      'trainPowerOn'
    );
  }

  /**
   * Turn train power off.
   *
   * In v1 power-off is latched: ``power_off_cmd()`` writes
   * ``phase_latches.power_off`` (and ``phase_latches.path_cancel_request``
   * when there is a live path), and Phase 3 commits the OFF transition on
   * a later tick. The response indicates the request was accepted; callers
   * must observe the controller sibling's ``power_on === false`` (e.g. via
   * ``stateManager.getController(trainId).power_on``) on a subsequent fetch
   * before treating the train as actually OFF.
   */
  async trainPowerOff(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/power-off`,
      {
        method: 'POST',
      },
      'trainPowerOff'
    );
  }

  /** Set active cab ('A' or 'B'). */
  async trainSetCab(trainId: string, cab: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/set-cab`,
      {
        method: 'POST',
        body: { cab },
      },
      'trainSetCab'
    );
  }

  // === OnboardComputer API (New Train Control Architecture) ===

  /**
   * Get OnboardComputer state for a train.
   * This is the single source of truth for train control state.
   */
  async getOnboardState(trainId: string): Promise<any> {
    return this._request(`/trains/${encodeURIComponent(trainId)}/onboard`, {}, 'getOnboardState');
  }

  /**
   * Set throttle/brake handle position.
   * @param position - Handle position: P1-P5, N, B1-B8, EB
   */
  async setHandle(trainId: string, position: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/handle`,
      {
        method: 'POST',
        body: { position },
      },
      'setHandle'
    );
  }

  /**
   * Set control mode (ATO or Manual) via OnboardComputer.
   * @param mode - 'ato' or 'manual'
   */
  async setControlMode(trainId: string, mode: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/mode`,
      {
        method: 'POST',
        body: { mode },
      },
      'setControlMode'
    );
  }

  /**
   * Set manual-mode submode for a train.
   * @param modeType - 'simulation' (handle) or 'power_percent'
   */
  async setManualModeType(trainId: string, modeType: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/manual-mode-type`,
      {
        method: 'POST',
        body: { mode_type: modeType },
      },
      'setManualModeType'
    );
  }

  /**
   * Set manual power-percent command (0..100) when manual_mode_type == power_percent.
   * @param percent - Integer 0..100
   */
  async setManualPowerPercent(trainId: string, percent: number): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/manual-power-percent`,
      {
        method: 'POST',
        body: { percent },
      },
      'setManualPowerPercent'
    );
  }

  /** Start ATO movement via OnboardComputer. */
  async atoGo(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/ato-go`,
      {
        method: 'POST',
      },
      'atoGo'
    );
  }

  /** Stop ATO movement (service stop) via OnboardComputer. */
  async atoStop(trainId: string): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/ato-stop`,
      {
        method: 'POST',
      },
      'atoStop'
    );
  }

  // === Schedule API ===

  /** Get all schedules. */
  async getSchedules(): Promise<any> {
    const result = await this._request('/schedules', {}, 'getSchedules');
    return result.schedules || [];
  }

  /** Delete a schedule. */
  async deleteSchedule(scheduleId: string): Promise<any> {
    return this._request(
      `/schedules/${encodeURIComponent(scheduleId)}`,
      {
        method: 'DELETE',
      },
      'deleteSchedule'
    );
  }

  // === Layout Persistence API ===

  /** Get list of saved layouts. */
  async getLayouts(): Promise<any> {
    const result = await this._request('/layouts', {}, 'getLayouts');
    return result.layouts || [];
  }

  /** Save current layout. */
  async saveLayout(name: string, description = ''): Promise<any> {
    return this._request(
      '/layouts',
      {
        method: 'POST',
        body: { name, description },
      },
      'saveLayout'
    );
  }

  /** Load a saved layout. */
  async loadLayout(name: string): Promise<any> {
    return this._request(
      `/layouts/${encodeURIComponent(name)}/load`,
      {
        method: 'POST',
      },
      'loadLayout'
    );
  }

  /** Delete a saved layout. */
  async deleteLayout(name: string): Promise<any> {
    return this._request(
      `/layouts/${encodeURIComponent(name)}`,
      {
        method: 'DELETE',
      },
      'deleteLayout'
    );
  }

  /** Clear the entire network. */
  async clearNetwork(): Promise<any> {
    return this._request(
      '/network/clear',
      {
        method: 'POST',
      },
      'clearNetwork'
    );
  }

  /** Get network statistics. */
  async getNetworkStats(): Promise<any> {
    return this._request('/network/stats', {}, 'getNetworkStats');
  }

  // === Scale Configuration API ===

  /** Get current scale configuration. */
  async getScale(): Promise<any> {
    return this._request('/scale', {}, 'getScale');
  }

  /** Set custom scale (e.g. 160). */
  async setScale(scale: number): Promise<any> {
    return this._request(
      '/scale',
      {
        method: 'PUT',
        body: { scale },
      },
      'setScale'
    );
  }

  // === Path Dispatcher API ===

  /**
   * Create a path for a train.
   *
   * The starting block is implicit (the train's current head block); the
   * server only needs the destination and direction.
   */
  async createPath({
    train_id,
    requested_destination_block_id,
    requested_destination_direction = 'forward',
    acquisition_mode = 'all_or_nothing',
  }: {
    train_id?: string;
    requested_destination_block_id?: string;
    requested_destination_direction?: string;
    acquisition_mode?: string;
  } = {}): Promise<any> {
    return this._request(
      '/paths',
      {
        method: 'POST',
        body: {
          train_id,
          requested_destination_block_id,
          requested_destination_direction,
          acquisition_mode,
        },
      },
      'createPath'
    );
  }

  /**
   * Get list of active paths.
   * @param filters - Optional filters: { train_id, block_id, status }
   */
  async getPaths(
    filters: { train_id?: string; block_id?: string; status?: string } = {}
  ): Promise<any> {
    const params = new URLSearchParams();
    if (filters.train_id) params.append('train_id', filters.train_id);
    if (filters.block_id) params.append('block_id', filters.block_id);
    if (filters.status) params.append('status', filters.status);

    const queryString = params.toString() ? `?${params}` : '';
    const result = await this._request(`/paths${queryString}`, {}, 'getPaths');
    return result.paths || [];
  }

  /** Get details of a specific path. */
  async getPath(pathId: string): Promise<any> {
    return this._request(`/paths/${encodeURIComponent(pathId)}`, {}, 'getPath');
  }

  /**
   * Request cancellation of a path.
   *
   * In v1 the backend latches API-driven cancellations: when the path has
   * a live train, the response resolves with ``status: "queued"`` once the
   * pending_path_cancel_request latch is written. The path is not actually
   * removed and the train has not yet stopped at that point — Phase 3 of
   * the simulation tick commits the cancel after the train brakes to a
   * stop. Callers should refresh from /api/paths or /api/network on the
   * next tick rather than mutating local state synchronously.
   *
   * Paths with no assigned train still cancel synchronously and resolve
   * with a CancellationResult (success, path_id, cancellation_kind,
   * reason, released_block_ids, released_switch_ids, train_id, notes).
   */
  async cancelPath(pathId: string): Promise<any> {
    return this._request(
      `/paths/${encodeURIComponent(pathId)}`,
      {
        method: 'DELETE',
      },
      'cancelPath'
    );
  }

  /** Extend an existing path to a new destination. */
  async extendPath(
    pathId: string,
    {
      requested_destination_block_id,
      requested_destination_direction = 'forward',
      acquisition_mode = 'all_or_nothing',
    }: {
      requested_destination_block_id?: string;
      requested_destination_direction?: string;
      acquisition_mode?: string;
    } = {}
  ): Promise<any> {
    return this._request(
      `/paths/${encodeURIComponent(pathId)}/extend`,
      {
        method: 'PUT',
        body: {
          requested_destination_block_id,
          requested_destination_direction,
          acquisition_mode,
        },
      },
      'extendPath'
    );
  }

  /** Get all blocks reachable from a starting block. */
  async getReachableEndpoints(blockId: string, includeStations = true): Promise<any> {
    return this._request(
      `/dispatcher/reachable/${encodeURIComponent(blockId)}?include_stations=${includeStations}`,
      {},
      'getReachableEndpoints'
    );
  }

  /**
   * Get all blocks reachable from a path's end in the FORWARD direction only.
   * Used for path extension to ensure users can only extend in the direction
   * the path is already heading (no backward extension).
   */
  async getPathReachableExtensions(pathId: string, includeStations = true): Promise<any> {
    return this._request(
      `/paths/${encodeURIComponent(pathId)}/reachable-extensions?include_stations=${includeStations}`,
      {},
      'getPathReachableExtensions'
    );
  }

  // === Switch Management API ===

  /** Get a specific switch. */
  async getSwitch(switchId: string): Promise<any> {
    return this._request(`/switches/${encodeURIComponent(switchId)}`, {}, 'getSwitch');
  }

  /** Update a switch. */
  async updateSwitch(switchId: string, updates: unknown): Promise<any> {
    return this._request(
      `/switches/${encodeURIComponent(switchId)}`,
      {
        method: 'PUT',
        body: updates,
      },
      'updateSwitch'
    );
  }

  /** Delete a switch. */
  async deleteSwitch(switchId: string): Promise<any> {
    return this._request(
      `/switches/${encodeURIComponent(switchId)}`,
      {
        method: 'DELETE',
      },
      'deleteSwitch'
    );
  }

  /**
   * Change switch state (MAIN=0, DIVERGE=1).
   * Fails with 409 Conflict if switch is locked by a path.
   */
  async setSwitchState(switchId: string, state: number): Promise<any> {
    return this._request(
      `/switches/${encodeURIComponent(switchId)}/state`,
      {
        method: 'PUT',
        body: { state },
      },
      'setSwitchState'
    );
  }

  // === Train Routes (Route Editor) ===

  /** Get all train routes. */
  async getRoutes(): Promise<any> {
    return this._request('/train-routes', {}, 'getRoutes');
  }

  /** Get a specific route. */
  async getRoute(routeId: string): Promise<any> {
    return this._request(`/train-routes/${encodeURIComponent(routeId)}`, {}, 'getRoute');
  }

  /** Create a new route. */
  async createRoute(routeData: unknown): Promise<any> {
    return this._request(
      '/train-routes',
      {
        method: 'POST',
        body: routeData,
      },
      'createRoute'
    );
  }

  /** Update a route. */
  async updateRoute(routeId: string, updates: unknown): Promise<any> {
    return this._request(
      `/train-routes/${encodeURIComponent(routeId)}`,
      {
        method: 'PUT',
        body: updates,
      },
      'updateRoute'
    );
  }

  /** Delete a route. */
  async deleteRoute(routeId: string): Promise<any> {
    return this._request(
      `/train-routes/${encodeURIComponent(routeId)}`,
      {
        method: 'DELETE',
      },
      'deleteRoute'
    );
  }

  /** Activate a route. */
  async activateRoute(routeId: string): Promise<any> {
    return this._request(
      `/train-routes/${encodeURIComponent(routeId)}/activate`,
      {
        method: 'POST',
      },
      'activateRoute'
    );
  }

  /** Deactivate a route. */
  async deactivateRoute(routeId: string): Promise<any> {
    return this._request(
      `/train-routes/${encodeURIComponent(routeId)}/deactivate`,
      {
        method: 'POST',
      },
      'deactivateRoute'
    );
  }

  /** Get route execution status. */
  async getRouteStatus(routeId: string): Promise<any> {
    return this._request(`/train-routes/${encodeURIComponent(routeId)}/status`, {}, 'getRouteStatus');
  }

  /**
   * Check viability between consecutive route-task target pairs.
   * @param pairs - Array of pair objects. Each pair is either
   *   ``{from, to, direction}`` (preferred) or the legacy ``[from, to]``
   *   list form (server treats that as direction="either").
   */
  async checkViability(pairs: unknown): Promise<any> {
    return this._request(
      '/train-routes/check-viability',
      {
        method: 'POST',
        body: { pairs },
      },
      'checkViability'
    );
  }

  /**
   * Blocks/stations reachable from a route-task target (for feasibility
   * highlighting when building waypoints).
   * @param type - 'block' | 'platform' | 'station'
   */
  async getReachableFrom(type: string, id: string): Promise<any> {
    return this._request(
      `/train-routes/reachable-from?type=${encodeURIComponent(type)}&id=${encodeURIComponent(id)}`,
      {},
      'getReachableFrom'
    );
  }

  /**
   * Read-only run history for a route (activation time + per-run task
   * completion timestamps), aggregated across all trains.
   */
  async getRouteHistory(routeId: string): Promise<any> {
    return this._request(`/train-routes/${encodeURIComponent(routeId)}/history`, {}, 'getRouteHistory');
  }

  /**
   * List every `RouteRun` across every route — live and historical.
   * Returns one summary per run (run_number, train, route, state,
   * direction, started_at, task counts, last_error).
   */
  async getRouteRuns(): Promise<any> {
    const result = await this._request('/route-runs', {}, 'getRouteRuns');
    return result?.route_runs || [];
  }

  /**
   * Get a single `RouteRun` by `run_number`, with per-task detail
   * (task_index, kind, target, resolved target, started/finished, delay
   * class, last_error).
   */
  async getRouteRun(runNumber: number): Promise<any> {
    return this._request(`/route-runs/${runNumber}`, {}, 'getRouteRun');
  }

  /**
   * Patch one or more per-run overrides on a currently RUNNING RouteRun.
   * Per design_new.md "RouteRun overrides" canonical home: accepts the
   * single-field shape `{field: value}` or the batched shape
   * `{overrides: {field: value, ...}}`.
   */
  async patchRouteRunOverrides(runNumber: number, body: unknown): Promise<any> {
    return this._request(
      `/route-runs/${runNumber}/overrides`,
      {
        method: 'PATCH',
        body,
      },
      'patchRouteRunOverrides'
    );
  }

  /**
   * Clear one per-run override on a currently RUNNING RouteRun. Restores
   * the shared-route value for that field via the effective_* helpers.
   *
   * @param field - One of `priority` / `acquisition_mode` /
   *     `action_on_waypoint_unreachable`.
   */
  async clearRouteRunOverride(runNumber: number, field: string): Promise<any> {
    return this._request(
      `/route-runs/${runNumber}/overrides/${encodeURIComponent(field)}`,
      { method: 'DELETE' },
      'clearRouteRunOverride'
    );
  }

  /**
   * Assign/unassign a train to/from a route.
   * @param routeId - Route ID or null to unassign
   */
  async assignRoute(trainId: string, routeId: string | null, options: { force?: boolean } = {}): Promise<any> {
    return this._request(
      `/trains/${encodeURIComponent(trainId)}/assign-route`,
      {
        method: 'PUT',
        body: {
          route_id: routeId,
          force: options.force || false,
        },
      },
      'assignRoute'
    );
  }

  async listTunnels(): Promise<any> {
    return this._request('/tunnels', {}, 'listTunnels');
  }

  async createTunnel(a: unknown, b: unknown): Promise<any> {
    return this._request(
      '/tunnels',
      {
        method: 'POST',
        body: { a, b },
      },
      'createTunnel'
    );
  }

  async deleteTunnel(x: number, y: number, edge: string): Promise<any> {
    return this._request(
      '/tunnels',
      {
        method: 'DELETE',
        body: { x, y, edge },
      },
      'deleteTunnel'
    );
  }
}

// The single API client instance — imported as `{ api }` by the app, panels,
// and editor.
export const api = new API();
