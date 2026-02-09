/**
 * StateManager - Centralized state cache with pub/sub event system
 * 
 * This is NOT the source of truth (backend is).
 * This is a cache that:
 * 1. Mirrors backend state
 * 2. Provides subscription mechanism for panels
 * 3. Diffs incoming data to detect changes
 * 4. Notifies subscribers only when their entity changes
 */
class StateManager {
    constructor() {
        // ═══════════════════════════════════════════════════════════════
        // STATE STORAGE
        // ═══════════════════════════════════════════════════════════════
        
        // Canonical cache - mirrors backend state
        // Using Map for O(1) lookup and easy iteration
        this.state = {
            trains: new Map(),      // trainId → train data object
            blocks: new Map(),      // blockId → block data object
            signals: new Map(),     // signalId → signal data object
            switches: new Map(),    // switchId → switch data object
            stations: new Map(),    // stationId → station data object
            tracks: new Map(),      // "x,y" string → track data object
        };
        
        // ═══════════════════════════════════════════════════════════════
        // SUBSCRIBER MANAGEMENT
        // ═══════════════════════════════════════════════════════════════
        
        // Subscribers organized by type and entity ID
        // Structure: Map<entityId, Set<callback>>
        // Special key '*' means "all entities of this type"
        this.subscribers = {
            trains: new Map(),
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
        this.lastUpdate = new Map();  // "type:id" → timestamp
        
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
     * @param {string} type - Entity type: 'trains', 'blocks', 'signals', 
     *                        'switches', 'stations', 'tracks'
     * @param {string} id - Entity ID, or '*' for all entities of this type
     * @param {Function} callback - Called with (newData, oldData, entityId)
     *                              newData is null if entity was deleted
     * @param {Object} options - Optional settings
     * @param {boolean} options.immediate - If true, call callback immediately 
     *                                      with current data (default: true)
     * @returns {Function} Unsubscribe function - MUST be called on cleanup
     * 
     * @example
     * // Subscribe to a specific train
     * const unsub = stateManager.subscribe('trains', 'train-001', 
     *     (train, oldTrain) => {
     *         console.log('Train updated:', train);
     *     }
     * );
     * 
     * // Later, cleanup
     * unsub();
     * 
     * @example
     * // Subscribe to ALL block changes
     * const unsub = stateManager.subscribe('blocks', '*', 
     *     (block, oldBlock, blockId) => {
     *         console.log(`Block ${blockId} changed`);
     *     }
     * );
     */
    subscribe(type, id, callback, options = {}) {
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
        this.subscribers[type].get(id).add(callback);
        
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

    /**
     * Check if there are any subscribers for an entity.
     * Useful for debugging and optimization.
     */
    hasSubscribers(type, id) {
        const specific = this.subscribers[type]?.get(id)?.size > 0;
        const wildcard = this.subscribers[type]?.get('*')?.size > 0;
        return specific || wildcard;
    }

    // ═══════════════════════════════════════════════════════════════════
    // DATA ACCESS API
    // ═══════════════════════════════════════════════════════════════════

    /**
     * Get current cached data for an entity.
     * 
     * @param {string} type - Entity type
     * @param {string} id - Entity ID
     * @returns {Object|undefined} Entity data or undefined if not found
     * 
     * @example
     * const train = stateManager.get('trains', 'train-001');
     * if (train) {
     *     console.log(train.current_speed);
     * }
     */
    get(type, id) {
        return this.state[type]?.get(id);
    }

    /**
     * Get all entities of a type.
     * 
     * @param {string} type - Entity type
     * @returns {Array<Object>} Array of all entities
     * 
     * @example
     * const allTrains = stateManager.getAll('trains');
     * allTrains.forEach(train => console.log(train.id));
     */
    getAll(type) {
        const map = this.state[type];
        if (!map) return [];
        return Array.from(map.values());
    }

    /**
     * Get all entity IDs of a type.
     * 
     * @param {string} type - Entity type
     * @returns {Array<string>} Array of entity IDs
     */
    getAllIds(type) {
        const map = this.state[type];
        if (!map) return [];
        return Array.from(map.keys());
    }

    /**
     * Check if an entity exists in cache.
     */
    has(type, id) {
        return this.state[type]?.has(id) ?? false;
    }

    /**
     * Get timestamp of last update for an entity.
     * Returns undefined if entity was never updated.
     */
    getLastUpdate(type, id) {
        return this.lastUpdate.get(`${type}:${id}`);
    }

    // ═══════════════════════════════════════════════════════════════════
    // DATA UPDATE API
    // ═══════════════════════════════════════════════════════════════════

    /**
     * Update a single entity and notify subscribers if changed.
     * 
     * @param {string} type - Entity type
     * @param {string} id - Entity ID
     * @param {Object} data - New entity data
     * @returns {boolean} True if data changed, false if identical
     * 
     * @example
     * // After API response
     * const response = await api.updateTrain(trainId, { power: 'on' });
     * stateManager.updateEntity('trains', trainId, response.train);
     * // Subscribers are notified automatically
     */
    updateEntity(type, id, data) {
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
     * 
     * @param {string} type - Entity type
     * @param {string} id - Entity ID
     * @returns {boolean} True if entity existed and was removed
     * 
     * @example
     * await api.deleteTrain(trainId);
     * stateManager.removeEntity('trains', trainId);
     * // Subscribers receive (null, oldData, id)
     */
    removeEntity(type, id) {
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
     * @param {Object} network - Full network data from /api/network
     * 
     * @example
     * const network = await api.getNetwork();
     * stateManager.updateFromNetwork(network);
     */
    updateFromNetwork(network) {
        // Pause notifications during bulk update to avoid spam
        this._notificationsPaused = true;
        this._pendingNotifications = [];
        
        try {
            // ─────────────────────────────────────────────────────────
            // UPDATE TRAINS
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

    /**
     * Notify subscribers of a change.
     * @private
     */
    _notify(type, id, newData, oldData) {
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

    /**
     * Flush pending notifications after bulk update.
     * @private
     */
    _flushPendingNotifications() {
        const pending = this._pendingNotifications;
        this._pendingNotifications = [];
        
        for (const { type, id, newData, oldData } of pending) {
            this._notify(type, id, newData, oldData);
        }
    }

    /**
     * Deep equality check for change detection.
     * Uses JSON serialization - simple but sufficient for our data.
     * @private
     */
    _isEqual(a, b) {
        if (a === b) return true;
        if (a === null || b === null) return false;
        if (a === undefined || b === undefined) return false;
        return JSON.stringify(a) === JSON.stringify(b);
    }

    // ═══════════════════════════════════════════════════════════════════
    // DEBUGGING API
    // ═══════════════════════════════════════════════════════════════════

    /**
     * Get debug info about current state.
     * Useful for console debugging.
     */
    debug() {
        return {
            version: this.version,
            counts: {
                trains: this.state.trains.size,
                blocks: this.state.blocks.size,
                signals: this.state.signals.size,
                switches: this.state.switches.size,
                stations: this.state.stations.size,
                tracks: this.state.tracks.size,
            },
            subscriberCounts: {
                trains: this._countSubscribers('trains'),
                blocks: this._countSubscribers('blocks'),
                signals: this._countSubscribers('signals'),
                switches: this._countSubscribers('switches'),
                stations: this._countSubscribers('stations'),
                tracks: this._countSubscribers('tracks'),
            },
        };
    }

    _countSubscribers(type) {
        let count = 0;
        for (const [_, callbacks] of this.subscribers[type]) {
            count += callbacks.size;
        }
        return count;
    }

    /**
     * Log all subscribers for debugging.
     */
    debugSubscribers() {
        console.group('[StateManager] Active Subscribers');
        for (const [type, subs] of Object.entries(this.subscribers)) {
            if (subs.size > 0) {
                console.group(type);
                for (const [id, callbacks] of subs) {
                    console.log(`  ${id}: ${callbacks.size} subscriber(s)`);
                }
                console.groupEnd();
            }
        }
        console.groupEnd();
    }
}

// ═══════════════════════════════════════════════════════════════════════════
// SINGLETON INSTANCE
// ═══════════════════════════════════════════════════════════════════════════

// Create and expose global instance
window.stateManager = new StateManager();
