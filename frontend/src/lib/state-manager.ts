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
  /** The operating environment the last snapshot named, or null before one. */
  operationMode: string | null = null;

  constructor() {
    // ═══════════════════════════════════════════════════════════════
    // STATE STORAGE
    // ═══════════════════════════════════════════════════════════════

    // Canonical cache - mirrors the snapshot (design_v2.md "Data flow to
    // the page"). Using Map for O(1) lookup and easy iteration.
    this.state = {
      runningTrains: new Map(), // trainId → train entry (design_v2.md "Live fields")
      blocks: new Map(), // blockId → block data object
      switches: new Map(), // switchId → switch data object
      tracks: new Map(), // "x,y" string → track data object
    };

    // ═══════════════════════════════════════════════════════════════
    // SUBSCRIBER MANAGEMENT
    // ═══════════════════════════════════════════════════════════════

    // Subscribers organized by type and entity ID
    // Structure: Map<entityId, Set<callback>>
    // Special key '*' means "all entities of this type"
    this.subscribers = {
      runningTrains: new Map(),
      blocks: new Map(),
      switches: new Map(),
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
  }

  // ═══════════════════════════════════════════════════════════════════
  // SUBSCRIPTION API
  // ═══════════════════════════════════════════════════════════════════

  /**
   * Subscribe to changes for a specific entity.
   *
   * @param type - Entity type: 'runningTrains', 'blocks', 'switches', 'tracks'
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
      // Keep the newer, equal object without notifying anyone. The network
      // poll hands back the object it cached from the last full reply while an
      // entity is unchanged, so holding that same object lets later polls skip
      // it on identity instead of comparing it again (design_v2.md "Layout
      // version").
      if (oldData !== data) this.state[type].set(id, data);
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

    try {
      // ─────────────────────────────────────────────────────────
      // UPDATE RUNNING TRAINS (design_v2.md "Live fields", A train entry)
      // ─────────────────────────────────────────────────────────
      if (typeof network.operation_mode === 'string') {
        this.operationMode = network.operation_mode;
      }
      const currentRunningIds = new Set(this.state.runningTrains.keys());
      for (const [tid, entry] of Object.entries(network.running_trains || {})) {
        this.updateEntity('runningTrains', tid, entry);
        currentRunningIds.delete(tid);
      }
      for (const deletedId of currentRunningIds) {
        this.removeEntity('runningTrains', deletedId);
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
      counts: Object.fromEntries(Object.entries(this.state).map(([k, m]) => [k, m.size])),
      subscriberCounts: Object.fromEntries(Object.keys(this.subscribers).map((k) => [k, this._countSubscribers(k)])),
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
