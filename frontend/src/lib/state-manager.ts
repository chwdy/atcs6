// state-manager.ts — Centralized state cache with a pub/sub event system.
//
// Migrated from static/js/state-manager.js (longterm_todo.md item #7, Wave 1 —
// foundation cluster). Behavior is unchanged from the classic file.
//
// This is NOT the source of truth (backend is). It is a cache that:
//   1. Mirrors backend state
//   2. Provides a subscription mechanism for panels
//   3. Diffs incoming data to detect changes
//   4. Notifies subscribers only when their entity changes
//
// Part of the Vite bundle; the single instance is imported as `{ stateManager }`
// by the app, renderer, panels, and editor.

// The cache holds arbitrary backend JSON per entity, so the map value type is
// intentionally permissive.
type EntityData = any;
type SubscriberCallback = (newData: EntityData, oldData: EntityData, entityId: string) => void;
type ClockSubscriber = (simulationTime: number) => void;

interface PendingNotification {
  type: string;
  id: string;
  newData: EntityData;
  oldData: EntityData;
}

export class StateManager {
  state: Record<string, Map<string, EntityData>>;
  subscribers: Record<string, Map<string, Set<SubscriberCallback>>>;
  lastUpdate: Map<string, number>;
  version: number;
  _notificationsPaused: boolean;
  _pendingNotifications: PendingNotification[];
  simulationTime: number;
  _clockSubscribers: Set<ClockSubscriber>;

  constructor() {
    // ═══════════════════════════════════════════════════════════════
    // STATE STORAGE
    // ═══════════════════════════════════════════════════════════════

    // Canonical cache - mirrors backend state
    // Using Map for O(1) lookup and easy iteration
    //
    // Per design_new.md "Data Flow To UI" (~line 2065), train runtime
    // state is split across three sibling maps owned by separate
    // backend entities. Consumers subscribe to each type independently.
    this.state = {
      trains: new Map(), // trainId → persistent train data
      controllers: new Map(), // trainId → TrainController.to_dict()
      dispatcherProgress: new Map(), // trainId → dispatcher progress dict | null
      assignedRoutes: new Map(), // trainId → assigned route_id | null
      operatorStatus: new Map(), // trainId → {ato_status, pending_actions, hold_reason}
      blocks: new Map(), // blockId → block data object
      signals: new Map(), // signalId → signal data object
      switches: new Map(), // switchId → switch data object
      stations: new Map(), // stationId → station data object
      tracks: new Map(), // "x,y" string → track data object
    };

    // ═══════════════════════════════════════════════════════════════
    // SUBSCRIBER MANAGEMENT
    // ═══════════════════════════════════════════════════════════════

    // Subscribers organized by type and entity ID
    // Structure: Map<entityId, Set<callback>>
    // Special key '*' means "all entities of this type"
    this.subscribers = {
      trains: new Map(),
      controllers: new Map(),
      dispatcherProgress: new Map(),
      assignedRoutes: new Map(),
      operatorStatus: new Map(),
      blocks: new Map(),
      signals: new Map(),
      switches: new Map(),
      stations: new Map(),
      tracks: new Map(),
    };

    // ═══════════════════════════════════════════════════════════════
    // METADATA
    // ═══════════════════════════════════════════════════════════════

    // Track when each entity was last updated (for debugging/staleness)
    this.lastUpdate = new Map(); // "type:id" → timestamp

    // Global version counter (increments on any change)
    this.version = 0;

    // Flag to pause all notifications (e.g., during bulk load)
    this._notificationsPaused = false;

    // Pending notifications during pause
    this._pendingNotifications = [];

    // Simulation clock (seconds since start) — refreshed from
    // /api/network payload. Subscribers registered via subscribeSimClock()
    // are called on every update so panels can track the clock even when
    // no entity changes in that poll cycle.
    this.simulationTime = 0.0;
    this._clockSubscribers = new Set();
  }

  // ═══════════════════════════════════════════════════════════════════
  // SUBSCRIPTION API
  // ═══════════════════════════════════════════════════════════════════

  /**
   * Subscribe to simulation clock updates.
   * The callback receives the new simulationTime (seconds) on every poll
   * cycle, even when no entity data has changed. This allows clock displays
   * to stay live after the train reaches a stable resting state.
   *
   * @returns Unsubscribe function
   */
  subscribeSimClock(callback: ClockSubscriber): () => void {
    this._clockSubscribers.add(callback);
    return () => this._clockSubscribers.delete(callback);
  }

  /**
   * Subscribe to changes for a specific entity.
   *
   * @param type - Entity type: 'trains', 'blocks', 'signals', 'switches',
   *               'stations', 'tracks'
   * @param id - Entity ID, or '*' for all entities of this type
   * @param callback - Called with (newData, oldData, entityId); newData is
   *                   null if the entity was deleted
   * @param options.immediate - If true, call callback immediately with current
   *                            data (default: true)
   * @returns Unsubscribe function - MUST be called on cleanup
   */
  subscribe(
    type: string,
    id: string,
    callback: SubscriberCallback,
    options: { immediate?: boolean } = {}
  ): () => void {
    const { immediate = true } = options;

    // Validate type
    if (!this.subscribers[type]) {
      console.error(`[StateManager] Unknown entity type: ${type}`);
      console.error(`[StateManager] Valid types: ${Object.keys(this.subscribers).join(', ')}`);
      return () => {}; // Return no-op unsubscribe
    }

    // Validate callback
    if (typeof callback !== 'function') {
      console.error(`[StateManager] Callback must be a function`);
      return () => {};
    }

    // Initialize subscriber set for this id if needed
    if (!this.subscribers[type].has(id)) {
      this.subscribers[type].set(id, new Set());
    }

    // Add callback to subscribers
    this.subscribers[type].get(id)!.add(callback);

    // Immediately call with current data if requested and data exists
    if (immediate && id !== '*') {
      const currentData = this.state[type].get(id);
      if (currentData !== undefined) {
        try {
          callback(currentData, null, id);
        } catch (e) {
          console.error(`[StateManager] Subscriber callback error:`, e);
        }
      }
    }

    // Return unsubscribe function
    return () => {
      const subscribers = this.subscribers[type].get(id);
      if (subscribers) {
        subscribers.delete(callback);
        // Cleanup empty sets to prevent memory leak
        if (subscribers.size === 0) {
          this.subscribers[type].delete(id);
        }
      }
    };
  }

  // ═══════════════════════════════════════════════════════════════════
  // DATA ACCESS API
  // ═══════════════════════════════════════════════════════════════════

  /** Get current cached data for an entity (or undefined if not found). */
  get(type: string, id: string): EntityData | undefined {
    return this.state[type]?.get(id);
  }

  /** Get all entities of a type. */
  getAll(type: string): EntityData[] {
    const map = this.state[type];
    if (!map) return [];
    return Array.from(map.values());
  }

  /** Check if an entity exists in cache. */
  has(type: string, id: string): boolean {
    return this.state[type]?.has(id) ?? false;
  }

  // ═══════════════════════════════════════════════════════════════════
  // TRAIN SIBLING-PAYLOAD ACCESSORS
  // ═══════════════════════════════════════════════════════════════════

  /**
   * Get the live TrainController snapshot for a train (or undefined).
   * Source: `controllers_by_train_id` sibling map. Contains state,
   * current_speed, power_on, span, ato_info, lookahead, etc.
   */
  getController(trainId: string): EntityData | undefined {
    return this.state.controllers.get(trainId);
  }

  /**
   * Get the dispatcher path-progress projection for a train (or null/undefined).
   * Source: `dispatcher_progress_by_train_id`. Contains path_id, status,
   * current_resource_index, authority_end_block_id, resource_sequence.
   */
  getDispatcherProgress(trainId: string): EntityData | undefined {
    return this.state.dispatcherProgress.get(trainId);
  }

  /**
   * Get the assigned route id for a train (or null/undefined).
   * Source: `assigned_routes_by_train_id`. Owned by RouteExecutor.
   */
  getAssignedRouteId(trainId: string): EntityData | undefined {
    return this.state.assignedRoutes.get(trainId);
  }

  /**
   * Get the operator status read model for a train (or undefined).
   * Source: `operator_status_by_train_id`: `{ato_status, pending_actions,
   * hold_reason}` (design_new.md "Data Flow To UI").
   */
  getOperatorStatus(trainId: string): EntityData | undefined {
    return this.state.operatorStatus.get(trainId);
  }

  // ═══════════════════════════════════════════════════════════════════
  // DATA UPDATE API
  // ═══════════════════════════════════════════════════════════════════

  /**
   * Update a single entity and notify subscribers if changed.
   * @returns True if data changed, false if identical
   */
  updateEntity(type: string, id: string, data: EntityData): boolean {
    if (!this.state[type]) {
      console.error(`[StateManager] Unknown entity type: ${type}`);
      return false;
    }

    const oldData = this.state[type].get(id);

    // Deep equality check - skip if no change
    if (this._isEqual(oldData, data)) {
      return false;
    }

    // Update state
    this.state[type].set(id, data);
    this.lastUpdate.set(`${type}:${id}`, Date.now());
    this.version++;

    // Notify subscribers
    this._notify(type, id, data, oldData);

    return true;
  }

  /**
   * Remove an entity from cache and notify subscribers.
   * @returns True if entity existed and was removed
   */
  removeEntity(type: string, id: string): boolean {
    if (!this.state[type]) {
      console.error(`[StateManager] Unknown entity type: ${type}`);
      return false;
    }

    const oldData = this.state[type].get(id);
    if (!oldData) {
      return false; // Entity didn't exist
    }

    // Remove from state
    this.state[type].delete(id);
    this.lastUpdate.delete(`${type}:${id}`);
    this.version++;

    // Notify subscribers with null as new data (signals deletion)
    this._notify(type, id, null, oldData);

    return true;
  }

  /**
   * Bulk update from full network fetch.
   * Handles additions, updates, and deletions automatically.
   *
   * @param network - Full network data from /api/network
   */
  updateFromNetwork(network: Record<string, any>): void {
    // Pause notifications during bulk update to avoid spam
    this._notificationsPaused = true;
    this._pendingNotifications = [];

    // Update simulation clock scalar before bulk entity refresh so any
    // subscriber callback firing during the flush sees the new clock.
    if (typeof network.simulation_time === 'number') {
      this.simulationTime = network.simulation_time;
      for (const cb of this._clockSubscribers) {
        try {
          cb(this.simulationTime);
        } catch (e) {
          console.error('[StateManager] Clock subscriber error:', e);
        }
      }
    }

    try {
      // ─────────────────────────────────────────────────────────
      // UPDATE TRAINS (persistent only)
      // ─────────────────────────────────────────────────────────
      const currentTrainIds = new Set(this.state.trains.keys());
      for (const train of network.trains || []) {
        this.updateEntity('trains', train.id, train);
        currentTrainIds.delete(train.id);
      }
      // Remove trains that no longer exist
      for (const deletedId of currentTrainIds) {
        this.removeEntity('trains', deletedId);
      }

      // ─────────────────────────────────────────────────────────
      // UPDATE CONTROLLERS (live runtime per train)
      // ─────────────────────────────────────────────────────────
      const currentControllerIds = new Set(this.state.controllers.keys());
      for (const [tid, controller] of Object.entries(network.controllers_by_train_id || {})) {
        this.updateEntity('controllers', tid, controller);
        currentControllerIds.delete(tid);
      }
      for (const deletedId of currentControllerIds) {
        this.removeEntity('controllers', deletedId);
      }

      // ─────────────────────────────────────────────────────────
      // UPDATE DISPATCHER PROGRESS (path projection per train)
      // ─────────────────────────────────────────────────────────
      const currentDispatcherIds = new Set(this.state.dispatcherProgress.keys());
      for (const [tid, progress] of Object.entries(network.dispatcher_progress_by_train_id || {})) {
        this.updateEntity('dispatcherProgress', tid, progress);
        currentDispatcherIds.delete(tid);
      }
      for (const deletedId of currentDispatcherIds) {
        this.removeEntity('dispatcherProgress', deletedId);
      }

      // ─────────────────────────────────────────────────────────
      // UPDATE ASSIGNED ROUTES (RouteExecutor-derived per train)
      // ─────────────────────────────────────────────────────────
      const currentAssignedRouteIds = new Set(this.state.assignedRoutes.keys());
      for (const [tid, routeId] of Object.entries(network.assigned_routes_by_train_id || {})) {
        this.updateEntity('assignedRoutes', tid, routeId);
        currentAssignedRouteIds.delete(tid);
      }
      for (const deletedId of currentAssignedRouteIds) {
        this.removeEntity('assignedRoutes', deletedId);
      }

      // ─────────────────────────────────────────────────────────
      // UPDATE OPERATOR STATUS (read model per train)
      // ─────────────────────────────────────────────────────────
      const currentOperatorStatusIds = new Set(this.state.operatorStatus.keys());
      for (const [tid, status] of Object.entries(network.operator_status_by_train_id || {})) {
        this.updateEntity('operatorStatus', tid, status);
        currentOperatorStatusIds.delete(tid);
      }
      for (const deletedId of currentOperatorStatusIds) {
        this.removeEntity('operatorStatus', deletedId);
      }

      // ─────────────────────────────────────────────────────────
      // UPDATE BLOCKS
      // ─────────────────────────────────────────────────────────
      const currentBlockIds = new Set(this.state.blocks.keys());
      for (const [id, block] of Object.entries(network.blocks || {})) {
        this.updateEntity('blocks', id, block);
        currentBlockIds.delete(id);
      }
      for (const deletedId of currentBlockIds) {
        this.removeEntity('blocks', deletedId);
      }

      // ─────────────────────────────────────────────────────────
      // UPDATE SIGNALS
      // ─────────────────────────────────────────────────────────
      const currentSignalIds = new Set(this.state.signals.keys());
      for (const [id, signal] of Object.entries(network.signals || {})) {
        this.updateEntity('signals', id, signal);
        currentSignalIds.delete(id);
      }
      for (const deletedId of currentSignalIds) {
        this.removeEntity('signals', deletedId);
      }

      // ─────────────────────────────────────────────────────────
      // UPDATE SWITCHES
      // ─────────────────────────────────────────────────────────
      const currentSwitchIds = new Set(this.state.switches.keys());
      for (const [id, sw] of Object.entries(network.switches || {})) {
        this.updateEntity('switches', id, sw);
        currentSwitchIds.delete(id);
      }
      for (const deletedId of currentSwitchIds) {
        this.removeEntity('switches', deletedId);
      }

      // ─────────────────────────────────────────────────────────
      // UPDATE PLATFORMS (stored in stations map for compatibility)
      // ─────────────────────────────────────────────────────────
      const currentStationIds = new Set(this.state.stations.keys());
      for (const platform of network.platforms || []) {
        this.updateEntity('stations', platform.platform_id, platform);
        currentStationIds.delete(platform.platform_id);
      }
      for (const deletedId of currentStationIds) {
        this.removeEntity('stations', deletedId);
      }

      // ─────────────────────────────────────────────────────────
      // UPDATE TRACKS
      // ─────────────────────────────────────────────────────────
      const currentTrackKeys = new Set(this.state.tracks.keys());
      for (const track of network.tracks || []) {
        const key = `${track.x},${track.y}`;
        this.updateEntity('tracks', key, track);
        currentTrackKeys.delete(key);
      }
      for (const deletedKey of currentTrackKeys) {
        this.removeEntity('tracks', deletedKey);
      }
    } finally {
      // Resume notifications and flush pending
      this._notificationsPaused = false;
      this._flushPendingNotifications();
    }
  }

  // ═══════════════════════════════════════════════════════════════════
  // INTERNAL METHODS
  // ═══════════════════════════════════════════════════════════════════

  /** Notify subscribers of a change. */
  _notify(type: string, id: string, newData: EntityData, oldData: EntityData): void {
    if (this._notificationsPaused) {
      this._pendingNotifications.push({ type, id, newData, oldData });
      return;
    }

    // Notify entity-specific subscribers
    const specificSubscribers = this.subscribers[type].get(id);
    if (specificSubscribers) {
      for (const callback of specificSubscribers) {
        try {
          callback(newData, oldData, id);
        } catch (e) {
          console.error(`[StateManager] Subscriber callback error for ${type}:${id}:`, e);
        }
      }
    }

    // Notify wildcard subscribers (subscribed to all of this type)
    const wildcardSubscribers = this.subscribers[type].get('*');
    if (wildcardSubscribers) {
      for (const callback of wildcardSubscribers) {
        try {
          callback(newData, oldData, id);
        } catch (e) {
          console.error(`[StateManager] Wildcard subscriber error for ${type}:`, e);
        }
      }
    }
  }

  /** Flush pending notifications after bulk update. */
  _flushPendingNotifications(): void {
    const pending = this._pendingNotifications;
    this._pendingNotifications = [];

    for (const { type, id, newData, oldData } of pending) {
      this._notify(type, id, newData, oldData);
    }
  }

  /**
   * Deep equality check for change detection.
   * Uses JSON serialization - simple but sufficient for our data.
   */
  _isEqual(a: EntityData, b: EntityData): boolean {
    if (a === b) return true;
    if (a === null || b === null) return false;
    if (a === undefined || b === undefined) return false;
    return JSON.stringify(a) === JSON.stringify(b);
  }

  // ═══════════════════════════════════════════════════════════════════
  // DEBUGGING API
  // ═══════════════════════════════════════════════════════════════════

  /** Get debug info about current state. Useful for console debugging. */
  debug(): Record<string, unknown> {
    return {
      version: this.version,
      counts: {
        trains: this.state.trains.size,
        controllers: this.state.controllers.size,
        dispatcherProgress: this.state.dispatcherProgress.size,
        assignedRoutes: this.state.assignedRoutes.size,
        operatorStatus: this.state.operatorStatus.size,
        blocks: this.state.blocks.size,
        signals: this.state.signals.size,
        switches: this.state.switches.size,
        stations: this.state.stations.size,
        tracks: this.state.tracks.size,
      },
      subscriberCounts: {
        trains: this._countSubscribers('trains'),
        controllers: this._countSubscribers('controllers'),
        dispatcherProgress: this._countSubscribers('dispatcherProgress'),
        assignedRoutes: this._countSubscribers('assignedRoutes'),
        operatorStatus: this._countSubscribers('operatorStatus'),
        blocks: this._countSubscribers('blocks'),
        signals: this._countSubscribers('signals'),
        switches: this._countSubscribers('switches'),
        stations: this._countSubscribers('stations'),
        tracks: this._countSubscribers('tracks'),
      },
    };
  }

  _countSubscribers(type: string): number {
    let count = 0;
    for (const [, callbacks] of this.subscribers[type]) {
      count += callbacks.size;
    }
    return count;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// SINGLETON INSTANCE
// ═══════════════════════════════════════════════════════════════════════════

// The single StateManager instance — imported as `{ stateManager }` by the app,
// renderer, panels, and editor.
export const stateManager = new StateManager();
