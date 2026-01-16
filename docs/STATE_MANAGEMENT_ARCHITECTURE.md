# Frontend State Management Architecture

## Document Info

| | |
|---|---|
| **Status** | Proposed |
| **Created** | 2026-01-15 |
| **Last Updated** | 2026-01-15 |

---

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Proposed Architecture](#proposed-architecture)
3. [StateManager Design](#statemanager-design)
4. [Panel Categories and Patterns](#panel-categories-and-patterns)
5. [Data Flow Patterns](#data-flow-patterns)
6. [Canvas and Renderer Integration](#canvas-and-renderer-integration)
7. [Handling User Editing State](#handling-user-editing-state)
8. [Backend API Endpoints](#backend-api-endpoints)
9. [Migration Plan](#migration-plan)
10. [File Changes Summary](#file-changes-summary)
11. [Implementation Details](#implementation-details)
12. [Open Questions](#open-questions)
13. [Appendix](#appendix)

---

## Problem Statement

### Current Pain Points

1. **Monolithic data fetch** - Every 200ms during simulation, we fetch the entire network (`GET /api/network`) even though panels only care about specific entities.

2. **No event system** - Components poll directly and manually extract their data from a giant blob. There's no notification when specific data changes.

3. **Competing update paths** - Train control panel has special handling in `app.js` to avoid race conditions with `refreshCurrentEditor()`. This creates fragile, hard-to-maintain code.

4. **Manual wiring** - Each panel must know how to:
   - Find its entity in `currentNetwork`
   - Detect if the entity changed
   - Update its own DOM elements

5. **Confusing updates** - During simulation, panels update from polling even when the user didn't interact. This can cause:
   - Input fields resetting while user is typing
   - Unexpected value changes mid-edit
   - No visual indication of why data changed

6. **Unclear data ownership** - Is `app.currentNetwork` the source of truth? What about `editor.cachedNetwork`? Multiple caches lead to synchronization bugs.

### Current Data Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         CURRENT ARCHITECTURE                                │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Backend                             Frontend                               │
│  ───────                             ────────                               │
│                                                                             │
│  GET /api/network ──────────────────► app.currentNetwork (giant blob)       │
│  (returns EVERYTHING:                        │                              │
│   tracks, trains, blocks,                    ├──► editor.cachedNetwork      │
│   signals, stations, switches)               │    (another copy!)           │
│                                              │                              │
│                      ┌───────────────────────┼───────────────────────┐      │
│                      │                       │                       │      │
│                      ▼                       ▼                       ▼      │
│               Train Control           Block Editor            Debug Panel   │
│               Panel                   Panel                                 │
│               │                       │                       │             │
│               │ Special handling      │ refreshCurrentEditor  │ Manual      │
│               │ in app.js polling     │ rebuilds entire       │ extraction  │
│               │ to avoid race         │ panel HTML            │ from blob   │
│               │ conditions            │                       │             │
│               └───────────────────────┴───────────────────────┘             │
│                                                                             │
│  Problems:                                                                  │
│  - Every panel processes the entire network                                 │
│  - No way to know if YOUR entity changed                                    │
│  - Panels fight over update timing                                          │
│  - Multiple copies of "truth"                                               │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Current Panel Update Mechanisms

| Panel | File | How It Gets Data | Update Trigger |
|-------|------|------------------|----------------|
| Train Control | `editor-trains.js` | `currentNetwork.trains.find(t => t.id === id)` | Special path in `startSimulationPolling()` |
| Block Editor | `editor-blocks.js` | `cachedNetwork.blocks[id]` | `refreshCurrentEditor()` rebuilds HTML |
| Signal Editor | `editor-signals.js` | `cachedNetwork.signals[id]` | `refreshCurrentEditor()` rebuilds HTML |
| Station Editor | `editor-stations.js` | `cachedNetwork.stations.find()` | `refreshCurrentEditor()` rebuilds HTML |
| Switch Editor | `editor-switches.js` | `cachedNetwork.switches[id]` | `refreshCurrentEditor()` rebuilds HTML |
| Properties Panel | `editor.js` | `cachedNetwork.tracks[]` | `selectElement()` |
| Debug Panel | `app.js` | `currentNetwork.trains.find()` | `updateTrainDebugPanel()` |
| Train Monitor | `train-monitor.js` | Passed data from `app.js` | `trainMonitor.update(train)` |

---

## Proposed Architecture

### Core Principles

1. **Backend is the ONLY source of truth**
   - Frontend StateManager is a cache, not a database
   - All writes go to backend first
   - Cache updates from backend responses or polling

2. **Write-through cache pattern**
   - User action → API call → Backend response → Update StateManager → Notify subscribers
   - Never update cache optimistically (local app, no latency concern)

3. **Subscription-based updates**
   - Panels subscribe to specific entities they display
   - StateManager notifies subscribers only when their entity changes
   - Panels don't need to poll or diff manually

4. **Single update path**
   - All data flows through StateManager
   - No more `currentNetwork` vs `cachedNetwork` confusion
   - One place to debug data issues

5. **Respect user editing state**
   - Don't update panel while user is actively editing
   - Visual indication when external changes occur
   - Save button panels: local state until save
   - Action button panels: immediate updates

### Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────────────────┐
│                            NEW ARCHITECTURE                                       │
├──────────────────────────────────────────────────────────────────────────────────┤
│                                                                                   │
│  BACKEND (Source of Truth)          FRONTEND                                     │
│  ─────────────────────────          ────────                                     │
│                                                                                   │
│                                     ┌────────────────────────────────────────┐   │
│  Polling (200ms during sim)         │           StateManager                 │   │
│  GET /api/network ─────────────────►│           (Cache + Event Bus)          │   │
│                                     │                                        │   │
│                                     │  ┌──────────────────────────────────┐  │   │
│  User Actions                       │  │  state: {                        │  │   │
│  PUT /api/trains/<id> ─────────┐    │  │    trains: Map<id, data>         │  │   │
│  PUT /api/blocks/<id> ─────────┤    │  │    blocks: Map<id, data>         │  │   │
│  PUT /api/signals/<id> ────────┼───►│  │    signals: Map<id, data>        │  │   │
│  DELETE /api/... ──────────────┘    │  │    switches: Map<id, data>       │  │   │
│         │                           │  │    stations: Map<id, data>       │  │   │
│         │                           │  │    tracks: Map<"x,y", data>      │  │   │
│         ▼                           │  │  }                               │  │   │
│  Response with updated entity ─────►│  └──────────────────────────────────┘  │   │
│                                     │                                        │   │
│                                     │  ┌──────────────────────────────────┐  │   │
│                                     │  │  subscribers: {                  │  │   │
│                                     │  │    trains: Map<id, Set<callback>>│  │   │
│                                     │  │    blocks: Map<id, Set<callback>>│  │   │
│                                     │  │    ...                           │  │   │
│                                     │  │  }                               │  │   │
│                                     │  └──────────────────────────────────┘  │   │
│                                     │                                        │   │
│                                     │  Methods:                              │   │
│                                     │  - subscribe(type, id, callback)      │   │
│                                     │  - updateEntity(type, id, data)       │   │
│                                     │  - updateFromNetwork(networkData)     │   │
│                                     │  - get(type, id) / getAll(type)       │   │
│                                     └─────────────────┬──────────────────────┘   │
│                                                       │                          │
│                                                       │ Notifies only on CHANGE  │
│                                                       │                          │
│          ┌────────────────┬───────────────┬──────────┴─────────┬─────────────┐  │
│          │                │               │                    │             │  │
│          ▼                ▼               ▼                    ▼             ▼  │
│   ┌────────────┐  ┌────────────┐  ┌────────────────┐  ┌────────────┐  ┌───────┐ │
│   │  Canvas    │  │  Main      │  │  Secondary     │  │  Debug     │  │ Train │ │
│   │  Renderer  │  │  Editor    │  │  Editor Panel  │  │  Panel     │  │Monitor│ │
│   │            │  │  Panel     │  │                │  │            │  │       │ │
│   │ getAll()   │  │ subscribe  │  │ subscribe      │  │ subscribe  │  │ sub.  │ │
│   │ on each    │  │ to selected│  │ to entity      │  │ to train   │  │ to    │ │
│   │ draw call  │  │ track      │  │ being edited   │  │            │  │ train │ │
│   └────────────┘  └────────────┘  └────────────────┘  └────────────┘  └───────┘ │
│                                                                                  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### What Changes, What Stays the Same

| Aspect | Current | New |
|--------|---------|-----|
| Polling frequency | 200ms full network | 200ms full network (same) |
| Data storage | `app.currentNetwork` + `editor.cachedNetwork` | `stateManager.state` (single source) |
| Panel data access | Manual extraction from blob | `stateManager.get()` or subscription callback |
| Change detection | None (rebuild every time) | StateManager diffs, notifies on change |
| Update trigger | `refreshCurrentEditor()` rebuilds HTML | Subscription callback updates values only |
| Race conditions | Special handling per panel | Single update path eliminates races |

---

## StateManager Design

### Class Definition

```javascript
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
            // UPDATE STATIONS
            // ─────────────────────────────────────────────────────────
            const currentStationIds = new Set(this.state.stations.keys());
            for (const station of network.stations || []) {
                this.updateEntity('stations', station.station_id, station);
                currentStationIds.delete(station.station_id);
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
```

### Entity Types Reference

| Type | Key Format | Source in Network | Example ID |
|------|------------|-------------------|------------|
| `trains` | `train.id` | `network.trains[]` | `"train-001"` |
| `blocks` | `block.block_id` | `network.blocks{}` | `"BLK-A"` |
| `signals` | `signal.signal_id` | `network.signals{}` | `"SIG-1"` |
| `switches` | `switch.switch_id` | `network.switches{}` | `"SW-1"` |
| `stations` | `station.station_id` | `network.stations[]` | `"STATION-A"` |
| `tracks` | `"x,y"` | `network.tracks[]` | `"5,10"` |

### Subscription Callback Signature

```javascript
/**
 * @callback SubscriptionCallback
 * @param {Object|null} newData - New entity data, or null if deleted
 * @param {Object|null} oldData - Previous data, or null if new entity
 * @param {string} entityId - The entity ID that changed
 */
function onEntityChange(newData, oldData, entityId) {
    if (newData === null) {
        // Entity was deleted
        console.log(`Entity ${entityId} was deleted`);
        return;
    }
    
    if (oldData === null) {
        // New entity
        console.log(`New entity: ${entityId}`);
    } else {
        // Updated entity
        console.log(`Entity ${entityId} updated`);
    }
}
```

---

## Panel Categories and Patterns

The architecture supports three panel complexity levels, each with its own pattern.

### Category 1: Simple Panels (Signal Editor, Switch Editor)

**Characteristics:**
- Mostly text inputs, numbers, dropdowns
- Few fields, predictable layout
- Can potentially use metadata-driven rendering
- Has Save button for committing changes

**Files:** `editor-signals.js`, `editor-switches.js`

**Pattern:**

```javascript
class SimpleEditorHandler {
    constructor(editor) {
        this.editor = editor;
        this._unsubscribe = null;
        this._entityId = null;
        this._localState = null;  // Copy for local edits
        this._isDirty = false;    // True if user has unsaved changes
    }

    showEditor(entityId) {
        // 1. Cleanup previous subscription
        this._cleanup();
        this._entityId = entityId;
        
        // 2. Get current data from cache
        const data = window.stateManager.get('signals', entityId);
        if (!data) {
            console.error(`Signal ${entityId} not found`);
            return;
        }
        
        // 3. Create local copy for editing
        this._localState = JSON.parse(JSON.stringify(data));
        this._isDirty = false;
        
        // 4. Render the panel
        this._renderPanel(entityId, data);
        
        // 5. Subscribe to future updates
        this._unsubscribe = window.stateManager.subscribe(
            'signals', 
            entityId, 
            (newData, oldData) => this._onDataUpdate(newData, oldData)
        );
    }

    _onDataUpdate(newData, oldData) {
        if (!newData) {
            // Entity was deleted - close panel
            window.showToast('Signal was deleted', 'warning');
            this.hideEditor();
            return;
        }
        
        if (this._isDirty) {
            // User has unsaved changes - show indicator but don't overwrite
            this._showExternalChangeIndicator();
            return;
        }
        
        // No local changes - safe to update
        this._localState = JSON.parse(JSON.stringify(newData));
        this._updatePanelValues(newData);
    }

    _onFieldChange(field, value) {
        this._localState[field] = value;
        this._isDirty = true;
        this._updateSaveButtonState();
    }

    async saveChanges() {
        try {
            const response = await window.api.updateSignal(
                this._entityId, 
                this._localState
            );
            
            // Update cache from response (source of truth)
            window.stateManager.updateEntity('signals', this._entityId, response.signal);
            
            this._isDirty = false;
            this._updateSaveButtonState();
            window.showToast('Signal saved', 'success');
        } catch (e) {
            console.error('Failed to save signal:', e);
            window.showToast('Failed to save: ' + e.message, 'error');
        }
    }

    hideEditor() {
        this._cleanup();
        // ... hide panel DOM ...
    }

    _cleanup() {
        if (this._unsubscribe) {
            this._unsubscribe();
            this._unsubscribe = null;
        }
        this._entityId = null;
        this._localState = null;
        this._isDirty = false;
    }
}
```

### Category 2: Tile-Based Panels (Block Editor, Station Editor)

**Characteristics:**
- Visual tile picker/editor component
- Complex custom UI that can't be auto-generated
- Local state during editing with Save button
- May need to show related entities (e.g., tracks in block)

**Files:** `editor-blocks.js`, `editor-stations.js`

**Pattern:**

```javascript
class TileBasedEditorHandler {
    constructor(editor) {
        this.editor = editor;
        this._unsubscribe = null;
        this._entityId = null;
        this._localState = null;
        this._isDirty = false;
        this._isUserInteracting = false;  // True during drag operations
    }

    async showEditor(blockId) {
        this._cleanup();
        this._entityId = blockId;
        
        // Get block data
        const block = window.stateManager.get('blocks', blockId);
        if (!block) {
            console.error(`Block ${blockId} not found`);
            return;
        }
        
        // Create local copy
        this._localState = JSON.parse(JSON.stringify(block));
        this._isDirty = false;
        
        // Render panel with tile picker
        this._renderPanel(blockId, block);
        this._initTilePicker(block);
        
        // Subscribe for updates
        this._unsubscribe = window.stateManager.subscribe(
            'blocks', 
            blockId, 
            (newData, oldData) => this._onDataUpdate(newData, oldData)
        );
    }

    _onDataUpdate(newData, oldData) {
        if (!newData) {
            window.showToast('Block was deleted', 'warning');
            this.hideEditor();
            return;
        }
        
        // Don't interrupt user during drag operations
        if (this._isUserInteracting) {
            return;
        }
        
        if (this._isDirty) {
            // Show indicator that server has different data
            this._showExternalChangeIndicator();
            return;
        }
        
        // Update local state and UI
        this._localState = JSON.parse(JSON.stringify(newData));
        this._updatePanelValues(newData);
        this._refreshTilePicker(newData);
    }

    _onTileDragStart() {
        this._isUserInteracting = true;
    }

    _onTileDragEnd() {
        this._isUserInteracting = false;
    }

    _onTileAdded(x, y) {
        this._localState.positions.push([x, y]);
        this._isDirty = true;
        this._updateSaveButtonState();
    }

    _onTileRemoved(x, y) {
        this._localState.positions = this._localState.positions.filter(
            p => !(p[0] === x && p[1] === y)
        );
        this._isDirty = true;
        this._updateSaveButtonState();
    }

    async saveChanges() {
        try {
            const response = await window.api.updateBlock(
                this._entityId, 
                this._localState
            );
            
            window.stateManager.updateEntity('blocks', this._entityId, response.block);
            
            this._isDirty = false;
            this._updateSaveButtonState();
            window.showToast('Block saved', 'success');
        } catch (e) {
            window.showToast('Failed to save: ' + e.message, 'error');
        }
    }

    discardChanges() {
        // Revert to cached data
        const current = window.stateManager.get('blocks', this._entityId);
        this._localState = JSON.parse(JSON.stringify(current));
        this._isDirty = false;
        this._updatePanelValues(current);
        this._refreshTilePicker(current);
        this._updateSaveButtonState();
    }

    // ... cleanup similar to Simple pattern ...
}
```

### Category 3: Complex Control Panels (Train Control)

**Characteristics:**
- Real-time status display (speed, position, state)
- Multiple action buttons (power, direction, depart, stop)
- Live updates during simulation - NO save concept
- Each button is an immediate action, not a "edit and save" flow
- Most complex panel with custom layout

**Files:** `editor-trains.js`

**Pattern:**

```javascript
class TrainControlHandler {
    constructor(editor) {
        this.editor = editor;
        this._unsubscribe = null;
        this._trainId = null;
        this._isActionInProgress = false;  // Prevent double-clicks
    }

    showTrainControlPanel(trainId) {
        this._cleanup();
        this._trainId = trainId;
        
        // Get current train data
        const train = window.stateManager.get('trains', trainId);
        if (!train) {
            console.error(`Train ${trainId} not found`);
            return;
        }
        
        // Render the full control panel
        this._renderControlPanel(trainId, train);
        this._initControlHandlers(trainId);
        
        // Subscribe for live updates
        this._unsubscribe = window.stateManager.subscribe(
            'trains', 
            trainId, 
            (train, oldTrain) => this._onTrainUpdate(train, oldTrain)
        );
    }

    _onTrainUpdate(train, oldTrain) {
        if (!train) {
            // Train was deleted
            window.showToast('Train was deleted', 'warning');
            this.hidePanel();
            return;
        }
        
        // Always update display values - this is a live view
        // (No "dirty" concept for control panels)
        this._updateLiveValues(train);
        this._updateButtonStates(train);
        
        // Optional: highlight changed values briefly
        if (oldTrain) {
            this._highlightChanges(train, oldTrain);
        }
    }

    _updateLiveValues(train) {
        // Update state display
        const stateEl = document.querySelector('.train-state-display');
        if (stateEl) {
            stateEl.textContent = (train.state || 'off').toUpperCase();
            stateEl.style.color = this._getStateColor(train.state);
        }
        
        // Update speed display
        const speedEl = document.querySelector('.train-speed-display');
        if (speedEl) {
            speedEl.innerHTML = `${(train.current_speed || 0).toFixed(1)} 
                <span class="text-muted">/ ${train.max_speed || 100} km/h</span>`;
        }
        
        // Update position display
        const posEl = document.querySelector('.train-pos-display');
        if (posEl && train.position?.end_a) {
            const head = train.position.leading_end === 'a' 
                ? train.position.end_a 
                : train.position.end_b;
            posEl.textContent = `(${head.tile[0]}, ${head.tile[1]})`;
        }
        
        // ... more value updates ...
    }

    _updateButtonStates(train) {
        const state = train.state;
        
        // Power buttons
        const powerOnBtn = document.getElementById('train-power-on-btn');
        const powerOffBtn = document.getElementById('train-power-off-btn');
        if (powerOnBtn) powerOnBtn.disabled = state !== 'off';
        if (powerOffBtn) powerOffBtn.disabled = state === 'off';
        
        // Direction buttons
        ['north', 'east', 'south', 'west'].forEach(dir => {
            const btn = document.getElementById(`train-dir-${dir}-btn`);
            if (btn) btn.disabled = state !== 'idle';
        });
        
        // ... more button state logic ...
    }

    // ─────────────────────────────────────────────────────────────────
    // ACTION HANDLERS
    // ─────────────────────────────────────────────────────────────────

    async handlePowerOn() {
        if (this._isActionInProgress) return;
        this._isActionInProgress = true;
        
        try {
            const response = await window.api.updateTrain(this._trainId, { 
                power: 'on' 
            });
            
            // Update cache from response
            window.stateManager.updateEntity('trains', this._trainId, response.train);
            // UI updates automatically via subscription callback
            
        } catch (e) {
            window.showToast('Failed to power on: ' + e.message, 'error');
        } finally {
            this._isActionInProgress = false;
        }
    }

    async handlePowerOff() {
        if (this._isActionInProgress) return;
        this._isActionInProgress = true;
        
        try {
            const response = await window.api.updateTrain(this._trainId, { 
                power: 'off' 
            });
            window.stateManager.updateEntity('trains', this._trainId, response.train);
        } catch (e) {
            window.showToast('Failed to power off: ' + e.message, 'error');
        } finally {
            this._isActionInProgress = false;
        }
    }

    async handleSetDirection(direction) {
        if (this._isActionInProgress) return;
        this._isActionInProgress = true;
        
        try {
            const response = await window.api.updateTrain(this._trainId, { 
                cab_direction: direction 
            });
            window.stateManager.updateEntity('trains', this._trainId, response.train);
        } catch (e) {
            window.showToast('Failed to set direction: ' + e.message, 'error');
        } finally {
            this._isActionInProgress = false;
        }
    }

    async handleDepart() {
        // ... similar pattern ...
    }

    async handleStop() {
        // ... similar pattern ...
    }

    // ... cleanup similar to other patterns ...
}
```

### Pattern Comparison Summary

| Aspect | Simple Panel | Tile-Based Panel | Control Panel |
|--------|--------------|------------------|---------------|
| Local state? | Yes, copy for editing | Yes, copy for editing | No, live view |
| Save button? | Yes | Yes | No |
| Dirty tracking? | Yes | Yes | No |
| External update handling | Show indicator if dirty | Show indicator if dirty | Always apply |
| User interaction pause? | On input focus | On drag operations | Never |
| Action style | Edit → Save | Edit → Save | Immediate |

---

## Data Flow Patterns

### Pattern 1: Initial Application Load

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ APP STARTUP                                                                 │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. App constructor runs                                                    │
│     │                                                                       │
│     ├── new StateManager() created as window.stateManager                   │
│     │                                                                       │
│     ├── new Renderer() created                                              │
│     │                                                                       │
│     ├── new Editor() created (with handler instances)                       │
│     │                                                                       │
│     └── this.refreshNetwork() called                                        │
│              │                                                              │
│              ▼                                                              │
│  2. refreshNetwork()                                                        │
│     │                                                                       │
│     ├── const network = await api.getNetwork()                              │
│     │              │                                                        │
│     │              └── GET /api/network → full network data                 │
│     │                                                                       │
│     ├── stateManager.updateFromNetwork(network)                             │
│     │              │                                                        │
│     │              ├── For each train: updateEntity('trains', id, data)     │
│     │              ├── For each block: updateEntity('blocks', id, data)     │
│     │              ├── For each signal: updateEntity('signals', id, data)   │
│     │              ├── ...                                                  │
│     │              └── (No subscribers yet, so no callbacks fired)          │
│     │                                                                       │
│     └── this.renderer.drawNetwork(network)                                  │
│                      │                                                      │
│                      └── Draws canvas from network data                     │
│                                                                             │
│  3. App is ready, waiting for user interaction                              │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Pattern 2: User Opens an Editor Panel

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ USER CLICKS ON A BLOCK TO EDIT                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. User clicks on block "BLK-A" in the block list or on canvas             │
│     │                                                                       │
│     └── editor.showBlockEditor("BLK-A") called                              │
│              │                                                              │
│              ▼                                                              │
│  2. BlockEditorHandler.showEditor("BLK-A")                                  │
│     │                                                                       │
│     ├── this._cleanup()  // Unsubscribe from any previous block             │
│     │                                                                       │
│     ├── const block = stateManager.get('blocks', "BLK-A")                   │
│     │              │                                                        │
│     │              └── Returns cached block data (already loaded)           │
│     │                                                                       │
│     ├── this._localState = deepCopy(block)                                  │
│     │                                                                       │
│     ├── this._renderPanel("BLK-A", block)                                   │
│     │              │                                                        │
│     │              └── Creates panel HTML, shows in secondary sidebar       │
│     │                                                                       │
│     └── this._unsubscribe = stateManager.subscribe(                         │
│              'blocks',                                                      │
│              "BLK-A",                                                       │
│              this._onDataUpdate.bind(this)                                  │
│         )                                                                   │
│              │                                                              │
│              ├── Adds callback to subscribers.blocks.get("BLK-A")           │
│              │                                                              │
│              └── Immediately calls callback with current data               │
│                  (which updates panel values)                               │
│                                                                             │
│  3. Panel is now showing and subscribed to BLK-A updates                    │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Pattern 3: User Edits and Saves (Edit Panels with Save Button)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ USER EDITS BLOCK NAME AND CLICKS SAVE                                       │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. User types "Main Platform Block" in name field                          │
│     │                                                                       │
│     └── Input 'change' event fires                                          │
│              │                                                              │
│              └── this._onFieldChange('name', "Main Platform Block")         │
│                       │                                                     │
│                       ├── this._localState.name = "Main Platform Block"     │
│                       ├── this._isDirty = true                              │
│                       └── this._updateSaveButtonState()  // Enable Save btn │
│                                                                             │
│  2. User clicks Save button                                                 │
│     │                                                                       │
│     └── this.saveChanges()                                                  │
│              │                                                              │
│              ▼                                                              │
│  3. API Call                                                                │
│     │                                                                       │
│     ├── const response = await api.updateBlock("BLK-A", this._localState)   │
│     │              │                                                        │
│     │              ├── PUT /api/blocks/BLK-A  { name: "Main Platform..." }  │
│     │              │              │                                         │
│     │              │              └── Backend validates and updates         │
│     │              │                                                        │
│     │              └── Response: { block: { ...updated block data... } }    │
│     │                                                                       │
│     ├── stateManager.updateEntity('blocks', "BLK-A", response.block)        │
│     │              │                                                        │
│     │              ├── Compares with cached data (different!)               │
│     │              ├── Updates cache: state.blocks.set("BLK-A", newData)    │
│     │              └── Notifies subscribers:                                │
│     │                       │                                               │
│     │                       └── this._onDataUpdate(newData, oldData)        │
│     │                                │                                      │
│     │                                └── this._isDirty is now false,        │
│     │                                    so panel values update normally    │
│     │                                                                       │
│     ├── this._isDirty = false                                               │
│     └── showToast('Block saved', 'success')                                 │
│                                                                             │
│  4. Panel now shows saved data, Save button disabled                        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Pattern 4: User Clicks Action Button (Control Panels)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ USER CLICKS "POWER ON" BUTTON IN TRAIN CONTROL PANEL                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. User clicks Power On button                                             │
│     │                                                                       │
│     └── Button onclick handler fires                                        │
│              │                                                              │
│              └── this.handlePowerOn()                                       │
│                       │                                                     │
│                       ▼                                                     │
│  2. Action Handler                                                          │
│     │                                                                       │
│     ├── if (this._isActionInProgress) return;  // Prevent double-click      │
│     ├── this._isActionInProgress = true;                                    │
│     │                                                                       │
│     ├── const response = await api.updateTrain("train-001", {power: 'on'})  │
│     │              │                                                        │
│     │              ├── PUT /api/trains/train-001  { power: 'on' }           │
│     │              │              │                                         │
│     │              │              └── Backend updates train state           │
│     │              │                                                        │
│     │              └── Response: { train: { state: 'idle', ... } }          │
│     │                                                                       │
│     └── stateManager.updateEntity('trains', "train-001", response.train)    │
│                      │                                                      │
│                      ├── Compares with cached (state changed!)              │
│                      ├── Updates cache                                      │
│                      └── Notifies subscribers:                              │
│                               │                                             │
│                               └── this._onTrainUpdate(newTrain, oldTrain)   │
│                                        │                                    │
│                                        ├── this._updateLiveValues(train)    │
│                                        │        └── Updates state display   │
│                                        │            to "IDLE"               │
│                                        │                                    │
│                                        └── this._updateButtonStates(train)  │
│                                                 └── Power On now disabled   │
│                                                     Direction btns enabled  │
│                                                                             │
│  3. Panel updated automatically via subscription                            │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Pattern 5: Simulation Polling

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ SIMULATION IS RUNNING - POLLING LOOP                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. User clicks Play button                                                 │
│     │                                                                       │
│     └── app.startSimulationPolling()                                        │
│              │                                                              │
│              └── this.simulationInterval = setInterval(async () => {        │
│                       // This runs every 200ms                              │
│                  }, 200)                                                    │
│                                                                             │
│  2. Every 200ms:                                                            │
│     │                                                                       │
│     ├── const network = await api.getNetwork()                              │
│     │              │                                                        │
│     │              └── Full network from backend                            │
│     │                  (trains have moved, blocks changed, etc.)            │
│     │                                                                       │
│     ├── stateManager.updateFromNetwork(network)                             │
│     │              │                                                        │
│     │              ├── For each train:                                      │
│     │              │     if (train data changed) {                          │
│     │              │         updateEntity() → notify subscribers            │
│     │              │     }                                                  │
│     │              │                                                        │
│     │              ├── For each block:                                      │
│     │              │     if (block state changed) {                         │
│     │              │         updateEntity() → notify subscribers            │
│     │              │     }                                                  │
│     │              │                                                        │
│     │              └── ... same for signals, switches, etc.                 │
│     │                                                                       │
│     │   ONLY entities that CHANGED trigger subscriber callbacks!            │
│     │   If train-002 didn't move, its subscribers are NOT called.           │
│     │                                                                       │
│     └── this.renderer.drawNetwork(...)                                      │
│                      │                                                      │
│                      └── Redraws canvas with latest positions               │
│                                                                             │
│  3. Panels update via their subscriptions:                                  │
│                                                                             │
│     ┌─────────────────────────────────────────────────────────────────┐     │
│     │ Train Control Panel (subscribed to "train-001")                 │     │
│     │                                                                 │     │
│     │ If train-001 changed:                                           │     │
│     │   _onTrainUpdate(newTrain, oldTrain) called                     │     │
│     │   → Updates speed display, position, state                      │     │
│     │                                                                 │     │
│     │ If train-001 did NOT change:                                    │     │
│     │   Nothing happens (no callback)                                 │     │
│     └─────────────────────────────────────────────────────────────────┘     │
│                                                                             │
│     ┌─────────────────────────────────────────────────────────────────┐     │
│     │ Block Editor Panel (subscribed to "BLK-A")                      │     │
│     │                                                                 │     │
│     │ If BLK-A state changed (e.g., became occupied):                 │     │
│     │   _onDataUpdate(newBlock, oldBlock) called                      │     │
│     │   → If user is editing (_isDirty): show indicator only          │     │
│     │   → If not editing: update panel values                         │     │
│     │                                                                 │     │
│     │ If BLK-A did NOT change:                                        │     │
│     │   Nothing happens                                               │     │
│     └─────────────────────────────────────────────────────────────────┘     │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Pattern 6: User Switches Selection

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ USER SELECTS A DIFFERENT TRAIN                                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. User clicks on train-002 in train list                                  │
│     │                                                                       │
│     └── app.selectTrain("train-002")                                        │
│              │                                                              │
│              ▼                                                              │
│  2. Close old panel                                                         │
│     │                                                                       │
│     └── trainHandler.hidePanel()                                            │
│              │                                                              │
│              └── this._cleanup()                                            │
│                       │                                                     │
│                       └── this._unsubscribe()  // Stop listening to 001     │
│                                                                             │
│  3. Open new panel                                                          │
│     │                                                                       │
│     └── trainHandler.showTrainControlPanel("train-002")                     │
│              │                                                              │
│              ├── const train = stateManager.get('trains', "train-002")      │
│              │                                                              │
│              ├── this._renderControlPanel("train-002", train)               │
│              │                                                              │
│              └── this._unsubscribe = stateManager.subscribe(                │
│                       'trains',                                             │
│                       "train-002",                                          │
│                       this._onTrainUpdate.bind(this)                        │
│                  )                                                          │
│                                                                             │
│  4. Now subscribed to train-002, not train-001                              │
│     │                                                                       │
│     ├── Polling continues at 200ms                                          │
│     ├── If train-001 changes: no callback (not subscribed)                  │
│     └── If train-002 changes: callback fires, panel updates                 │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Pattern 7: External Change While User is Editing

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ USER IS EDITING BLOCK, ANOTHER USER/PROCESS CHANGES IT                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Initial state:                                                             │
│  - Block Editor open for "BLK-A"                                            │
│  - User has changed name field (dirty = true)                               │
│  - User has NOT saved yet                                                   │
│                                                                             │
│  1. Polling brings in new network data                                      │
│     │                                                                       │
│     └── stateManager.updateFromNetwork(network)                             │
│              │                                                              │
│              └── Block "BLK-A" has different data from backend              │
│                  (e.g., another process changed its state)                  │
│                       │                                                     │
│                       └── updateEntity('blocks', "BLK-A", newData)          │
│                                │                                            │
│                                └── _onDataUpdate(newData, oldData) called   │
│                                         │                                   │
│                                         ▼                                   │
│  2. Panel handles the update                                                │
│     │                                                                       │
│     ├── if (this._isDirty) {                                                │
│     │       // User has unsaved changes!                                    │
│     │       // Don't overwrite their work                                   │
│     │       this._showExternalChangeIndicator();                            │
│     │       return;                                                         │
│     │   }                                                                   │
│     │                                                                       │
│     └── // Only reached if not dirty                                        │
│         this._localState = deepCopy(newData);                               │
│         this._updatePanelValues(newData);                                   │
│                                                                             │
│  3. Visual indicator shown                                                  │
│     │                                                                       │
│     └── Panel shows: "⚠️ External changes detected. Save or Discard?"      │
│                                                                             │
│  4. User can:                                                               │
│     │                                                                       │
│     ├── Click "Save" → Their changes overwrite external changes             │
│     │                                                                       │
│     └── Click "Discard" → External changes applied, their changes lost      │
│              │                                                              │
│              └── this.discardChanges()                                      │
│                       │                                                     │
│                       ├── const current = stateManager.get('blocks', id)    │
│                       ├── this._localState = deepCopy(current)              │
│                       ├── this._isDirty = false                             │
│                       └── this._updatePanelValues(current)                  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Canvas and Renderer Integration

The canvas needs ALL entity data to render the full layout. Here's how it integrates with StateManager.

### Option A: Read on Each Draw (Recommended)

The renderer reads from StateManager on each draw call. This is simple and always gets latest data.

```javascript
class Renderer {
    constructor(canvasId) {
        this.canvas = document.getElementById(canvasId);
        this.ctx = this.canvas.getContext('2d');
        // ... other setup ...
    }

    /**
     * Draw the full network.
     * Reads current state from StateManager.
     */
    draw() {
        this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
        
        // Get all data from StateManager
        const tracks = window.stateManager.getAll('tracks');
        const trains = window.stateManager.getAll('trains');
        const signals = window.stateManager.getAll('signals');
        const blocks = window.stateManager.getAll('blocks');
        const stations = window.stateManager.getAll('stations');
        const switches = window.stateManager.getAll('switches');
        
        // Draw layers in order
        this.drawGrid();
        this.drawTracks(tracks);
        this.drawBlocks(blocks, tracks);
        this.drawStations(stations);
        this.drawSignals(signals);
        this.drawSwitches(switches);
        this.drawTrains(trains);
        this.drawSelection();
    }

    // Called by app.js after polling
    refresh() {
        this.draw();
    }
}
```

### Option B: Subscribe to All Changes (Alternative)

The renderer subscribes to all entity types and redraws when any change occurs.

```javascript
class Renderer {
    constructor(canvasId) {
        // ... setup ...
        
        // Subscribe to all entity types
        this._subscriptions = [];
        
        const entityTypes = ['tracks', 'trains', 'signals', 'blocks', 'stations', 'switches'];
        for (const type of entityTypes) {
            const unsub = window.stateManager.subscribe(type, '*', () => {
                this._scheduleRedraw();
            });
            this._subscriptions.push(unsub);
        }
    }

    _scheduleRedraw() {
        // Debounce to avoid multiple redraws in same frame
        if (this._redrawScheduled) return;
        this._redrawScheduled = true;
        
        requestAnimationFrame(() => {
            this._redrawScheduled = false;
            this.draw();
        });
    }

    destroy() {
        // Cleanup subscriptions
        for (const unsub of this._subscriptions) {
            unsub();
        }
    }
}
```

### Recommendation

**Use Option A (Read on Each Draw)** because:

1. Simpler - no subscription management
2. Renderer already has a `draw()` method called explicitly
3. No risk of subscriber leaks
4. Canvas doesn't need to know "what changed" - it redraws everything anyway

The polling loop in `app.js` will call `renderer.draw()` after updating StateManager:

```javascript
// In App.startSimulationPolling()
this.simulationInterval = setInterval(async () => {
    const network = await api.getNetwork();
    stateManager.updateFromNetwork(network);  // Update cache, notify subscribers
    this.renderer.draw();                      // Redraw canvas from cache
}, 200);
```

---

## Handling User Editing State

This section details how to prevent confusing updates while the user is actively editing.

### Problem

During simulation polling (every 200ms), new data arrives. If the user is typing in a field, we don't want to:
- Reset their cursor position
- Clear their typed characters
- Replace their pending changes

### Solution: Three-Level Protection

#### Level 1: Dirty Flag

Track whether user has unsaved changes:

```javascript
class EditorHandler {
    _isDirty = false;
    _localState = null;

    _onFieldChange(field, value) {
        this._localState[field] = value;
        this._isDirty = true;
    }

    _onDataUpdate(newData, oldData) {
        if (this._isDirty) {
            // Don't overwrite user's changes
            this._showExternalChangeIndicator();
            return;
        }
        // Safe to update
        this._localState = deepCopy(newData);
        this._updatePanelValues(newData);
    }
}
```

#### Level 2: Focus Detection

Don't update panel while user has focus in an input:

```javascript
class EditorHandler {
    _isUserFocused = false;

    _initInputHandlers() {
        const inputs = this._panel.querySelectorAll('input, textarea, select');
        
        inputs.forEach(input => {
            input.addEventListener('focus', () => {
                this._isUserFocused = true;
            });
            
            input.addEventListener('blur', () => {
                this._isUserFocused = false;
                // Optionally apply pending external updates now
                if (this._pendingExternalData && !this._isDirty) {
                    this._updatePanelValues(this._pendingExternalData);
                    this._pendingExternalData = null;
                }
            });
        });
    }

    _onDataUpdate(newData, oldData) {
        if (this._isDirty) {
            this._showExternalChangeIndicator();
            return;
        }
        
        if (this._isUserFocused) {
            // Store for later, don't interrupt user
            this._pendingExternalData = newData;
            return;
        }
        
        this._updatePanelValues(newData);
    }
}
```

#### Level 3: Interaction Lock (for Tile Pickers)

During drag operations, lock all updates:

```javascript
class TileEditorHandler {
    _isUserInteracting = false;

    _initTilePicker() {
        this._tilePicker.addEventListener('dragstart', () => {
            this._isUserInteracting = true;
        });
        
        this._tilePicker.addEventListener('dragend', () => {
            this._isUserInteracting = false;
        });
    }

    _onDataUpdate(newData, oldData) {
        if (this._isUserInteracting) {
            return;  // Completely ignore during drag
        }
        // ... normal handling ...
    }
}
```

### Visual Indicators

When external changes are detected but not applied:

```javascript
_showExternalChangeIndicator() {
    const indicator = this._panel.querySelector('.external-change-indicator');
    if (!indicator) {
        // Create indicator element
        const div = document.createElement('div');
        div.className = 'external-change-indicator';
        div.innerHTML = `
            <span class="icon">⚠️</span>
            <span>External changes detected</span>
            <button class="btn-discard">Discard my changes</button>
        `;
        div.querySelector('.btn-discard').addEventListener('click', () => {
            this.discardChanges();
        });
        this._panel.querySelector('.panel-header').appendChild(div);
    }
    indicator?.classList.remove('hidden');
}

_hideExternalChangeIndicator() {
    const indicator = this._panel.querySelector('.external-change-indicator');
    indicator?.classList.add('hidden');
}
```

### CSS for External Change Indicator

```css
.external-change-indicator {
    background: var(--warning-bg);
    color: var(--warning-text);
    padding: 8px 12px;
    border-radius: 4px;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    margin-top: 8px;
}

.external-change-indicator.hidden {
    display: none;
}

.external-change-indicator .btn-discard {
    margin-left: auto;
    padding: 4px 8px;
    font-size: 11px;
}
```

---

## Backend API Endpoints

### New Entity-Specific Endpoints

These endpoints allow fetching individual entities without loading the entire network.

| Endpoint | Method | Description | Response |
|----------|--------|-------------|----------|
| `GET /api/trains` | GET | List all trains | `{ trains: [...] }` |
| `GET /api/trains/<id>` | GET | Single train (full data) | `{ train: {...} }` |
| `GET /api/blocks` | GET | List all blocks | `{ blocks: {...} }` |
| `GET /api/blocks/<id>` | GET | Single block | `{ block: {...} }` |
| `GET /api/signals/<id>` | GET | Single signal | `{ signal: {...} }` |
| `GET /api/switches/<id>` | GET | Single switch | `{ switch: {...} }` |
| `GET /api/stations/<id>` | GET | Single station | `{ station: {...} }` |

### Example Implementations

```python
# server.py additions

@app.route('/api/trains', methods=['GET'])
def get_all_trains():
    """Get all trains"""
    return jsonify({
        'trains': [t.to_dict(network) for t in network.trains.values()]
    })

@app.route('/api/trains/<train_id>', methods=['GET'])
def get_train(train_id):
    """Get a single train by ID"""
    train = network.trains.get(train_id)
    if not train:
        return jsonify({'error': 'Train not found'}), 404
    return jsonify({
        'train': train.to_dict(network)
    })

@app.route('/api/blocks/<block_id>', methods=['GET'])
def get_block(block_id):
    """Get a single block by ID"""
    block = network.blocks.get(block_id)
    if not block:
        return jsonify({'error': 'Block not found'}), 404
    return jsonify({
        'block': block.to_dict()
    })

@app.route('/api/signals/<signal_id>', methods=['GET'])
def get_signal(signal_id):
    """Get a single signal by ID"""
    signal = network.signals.get(signal_id)
    if not signal:
        return jsonify({'error': 'Signal not found'}), 404
    return jsonify({
        'signal': signal.to_dict()
    })

@app.route('/api/switches/<switch_id>', methods=['GET'])
def get_switch(switch_id):
    """Get a single switch by ID"""
    switch = network.switches.get(switch_id)
    if not switch:
        return jsonify({'error': 'Switch not found'}), 404
    return jsonify({
        'switch': switch.to_dict()
    })

@app.route('/api/stations/<station_id>', methods=['GET'])
def get_station(station_id):
    """Get a single station by ID"""
    station = network.stations.get(station_id)
    if not station:
        return jsonify({'error': 'Station not found'}), 404
    return jsonify({
        'station': station.to_dict_with_orientation(network)
    })
```

### API.js Additions

```javascript
// api.js additions

class API {
    // ... existing methods ...

    async getTrains() {
        const response = await fetch('/api/trains');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        return data.trains;
    }

    async getTrain(trainId) {
        const response = await fetch(`/api/trains/${trainId}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        return data.train;
    }

    async getBlock(blockId) {
        const response = await fetch(`/api/blocks/${blockId}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        return data.block;
    }

    async getSignal(signalId) {
        const response = await fetch(`/api/signals/${signalId}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        return data.signal;
    }

    async getSwitch(switchId) {
        const response = await fetch(`/api/switches/${switchId}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        return data.switch;
    }

    async getStation(stationId) {
        const response = await fetch(`/api/stations/${stationId}`);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        return data.station;
    }
}
```

---

## Migration Plan

### Overview

The migration is designed to be incremental. Each phase adds value without breaking existing functionality.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          MIGRATION PHASES                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Phase 1: Foundation                                                        │
│  ─────────────────────                                                      │
│  - Add StateManager class                                                   │
│  - Wire into existing refreshNetwork()                                      │
│  - No changes to panels yet                                                 │
│  - Everything still works as before                                         │
│                                                                             │
│  Phase 2: Backend Endpoints                                                 │
│  ──────────────────────────                                                 │
│  - Add individual entity GET endpoints                                      │
│  - Add API.js methods                                                       │
│  - Still not used by panels yet                                             │
│                                                                             │
│  Phase 3: Train Control Panel Migration (Proof of Concept)                  │
│  ─────────────────────────────────────────────────────────────              │
│  - Convert Train Control to subscription model                              │
│  - Remove special handling from app.js polling                              │
│  - Verify no regressions                                                    │
│                                                                             │
│  Phase 4: Other Panel Migrations                                            │
│  ───────────────────────────────                                            │
│  - Block Editor                                                             │
│  - Signal Editor                                                            │
│  - Station Editor                                                           │
│  - Switch Editor                                                            │
│  - Properties Panel                                                         │
│                                                                             │
│  Phase 5: Cleanup & Optimization                                            │
│  ───────────────────────────────                                            │
│  - Remove redundant refreshCurrentEditor() calls                            │
│  - Remove editor.cachedNetwork (use StateManager)                           │
│  - Add visual change indicators (optional)                                  │
│  - Performance tuning                                                       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Phase 1: Foundation (No Breaking Changes)

**Goal:** Add StateManager without changing any existing behavior.

**Files to create:**
- `static/js/state-manager.js`

**Files to modify:**
- `templates/index.html` - Add script tag for state-manager.js
- `static/js/app.js` - Call `stateManager.updateFromNetwork()` in `refreshNetwork()`

**Changes:**

```html
<!-- index.html - add before app.js -->
<script src="/static/js/state-manager.js"></script>
```

```javascript
// app.js - modify refreshNetwork()
async refreshNetwork() {
    try {
        const network = await window.api.getNetwork();
        this.currentNetwork = network;  // Keep for backward compatibility
        
        // NEW: Update StateManager
        window.stateManager.updateFromNetwork(network);
        
        this.editor.setCachedNetwork(network);
        this.renderer.drawNetwork(network);
        this.editor.validateCurrentEditor();
    } catch (e) {
        console.error('[App] Failed to refresh network:', e);
    }
}
```

**Verification:**
- App loads normally
- All panels work as before
- Console: `stateManager.debug()` shows correct entity counts

### Phase 2: Backend Endpoints

**Goal:** Add entity-specific GET endpoints for future use.

**Files to modify:**
- `server.py` - Add new routes
- `static/js/api.js` - Add new methods

**Verification:**
- Test each endpoint manually: `/api/trains/train-001`, etc.
- API.js methods return correct data

### Phase 3: Train Control Panel Migration

**Goal:** Convert the most complex panel as proof of concept.

**Files to modify:**
- `static/js/editor-trains.js` - Use subscriptions
- `static/js/app.js` - Remove special train handling in polling loop

**Key changes to editor-trains.js:**

```javascript
// Before
showTrainControlPanel(trainId) {
    // ... render panel ...
    this.editor._currentEditorType = 'train-control';
    this.editor._currentEditorId = trainId;
}

// After
showTrainControlPanel(trainId) {
    this._cleanup();
    this._trainId = trainId;
    
    const train = window.stateManager.get('trains', trainId);
    this._renderControlPanel(trainId, train);
    
    this._unsubscribe = window.stateManager.subscribe(
        'trains',
        trainId,
        (train) => this._onTrainUpdate(train)
    );
    
    this.editor._currentEditorType = 'train-control';
    this.editor._currentEditorId = trainId;
}
```

**Key changes to app.js:**

```javascript
// Before (in startSimulationPolling)
if (this.selectedTrainId) {
    const train = this.currentNetwork?.trains?.find(t => t.id === this.selectedTrainId);
    if (train) {
        this.editor.trainHandler.updateControlPanelValuesFromData(train);
        this.updateTrainDebugPanel(train);
    }
}

// After
// Remove the above - subscription handles it automatically
// Only keep debug panel update if it's not using subscriptions yet
if (this.selectedTrainId) {
    const train = window.stateManager.get('trains', this.selectedTrainId);
    if (train) {
        this.updateTrainDebugPanel(train);
    }
}
```

**Verification:**
- Train Control panel updates correctly during simulation
- Power/direction/depart buttons work
- Speed/position values update in real-time
- No console errors

### Phase 4: Other Panel Migrations

Migrate one panel at a time in this order:

1. **Signal Editor** - Simplest, good template
2. **Switch Editor** - Similar to signal
3. **Block Editor** - More complex, has tile picker
4. **Station Editor** - Similar to block
5. **Properties Panel** - Track selection panel

For each:
1. Add subscription in `showEditor()`
2. Add `_cleanup()` with unsubscribe
3. Add `_onDataUpdate()` handler with dirty checking
4. Update `saveChanges()` to call `stateManager.updateEntity()`
5. Test thoroughly

### Phase 5: Cleanup & Optimization

**Remove redundant code:**
- `editor.cachedNetwork` → Use `stateManager`
- `refreshCurrentEditor()` → Subscriptions handle updates
- Special case handling in `app.js` → Unified through StateManager

**Add enhancements (optional):**
- External change indicators
- "Last updated" timestamps
- Undo support (using oldData from callbacks)

---

## File Changes Summary

| File | Change Type | Phase | Description |
|------|-------------|-------|-------------|
| `static/js/state-manager.js` | **NEW** | 1 | StateManager class |
| `templates/index.html` | MODIFY | 1 | Add script tag |
| `static/js/app.js` | MODIFY | 1, 3 | Wire StateManager, remove special train handling |
| `server.py` | MODIFY | 2 | Add entity GET endpoints |
| `static/js/api.js` | MODIFY | 2 | Add entity fetch methods |
| `static/js/editor-trains.js` | MODIFY | 3 | Use subscriptions |
| `static/js/editor-signals.js` | MODIFY | 4 | Use subscriptions |
| `static/js/editor-switches.js` | MODIFY | 4 | Use subscriptions |
| `static/js/editor-blocks.js` | MODIFY | 4 | Use subscriptions |
| `static/js/editor-stations.js` | MODIFY | 4 | Use subscriptions |
| `static/js/editor.js` | MODIFY | 4, 5 | Properties panel, remove cachedNetwork |

---

## Implementation Details

### StateManager File Location

```
static/
  js/
    state-manager.js    ← NEW
    app.js
    api.js
    editor.js
    editor-trains.js
    editor-blocks.js
    editor-signals.js
    editor-stations.js
    editor-switches.js
    ...
```

### Script Load Order

StateManager must load before app.js:

```html
<!-- In index.html -->
<script src="/static/js/constants.js"></script>
<script src="/static/js/helpers.js"></script>
<script src="/static/js/api.js"></script>
<script src="/static/js/state-manager.js"></script>   <!-- NEW -->
<script src="/static/js/renderer.js"></script>
<script src="/static/js/pathfinders.js"></script>
<script src="/static/js/editor-ui.js"></script>
<script src="/static/js/editor-blocks.js"></script>
<script src="/static/js/editor-signals.js"></script>
<script src="/static/js/editor-stations.js"></script>
<script src="/static/js/editor-trains.js"></script>
<script src="/static/js/editor-switches.js"></script>
<script src="/static/js/editor-routes.js"></script>
<script src="/static/js/editor.js"></script>
<script src="/static/js/train-monitor.js"></script>
<script src="/static/js/app.js"></script>
```

### Debugging Tips

```javascript
// In browser console:

// Check state counts
stateManager.debug()
// → { version: 42, counts: { trains: 3, blocks: 5, ... }, subscriberCounts: {...} }

// List all subscribers
stateManager.debugSubscribers()
// → [StateManager] Active Subscribers
//     trains
//       train-001: 2 subscriber(s)
//       *: 1 subscriber(s)

// Get specific entity
stateManager.get('trains', 'train-001')
// → { id: 'train-001', state: 'running_moving', ... }

// Check if entity exists
stateManager.has('blocks', 'BLK-A')
// → true

// Get all trains
stateManager.getAll('trains')
// → [{ id: 'train-001', ... }, { id: 'train-002', ... }]

// Check last update time
stateManager.getLastUpdate('trains', 'train-001')
// → 1705312800000 (timestamp)
```

---

## Open Questions

### 1. Properties Panel Integration

The properties panel (left sidebar) shows info about the currently selected track. Should it:

- **Option A:** Subscribe to the selected track position
- **Option B:** Just read from StateManager on each selection
- **Option C:** Keep current behavior (read from cachedNetwork)

**Recommendation:** Option B - It only updates on selection, not continuously.

### 2. Debug Panel

The debug panel shows detailed train info. Should it:

- **Option A:** Subscribe to selected train (same as Train Control)
- **Option B:** Be updated by app.js polling (current behavior)
- **Option C:** Be merged into Train Control panel

**Recommendation:** Option A - Subscribe alongside Train Control.

### 3. Train Monitor (Floating Panel)

The Train Monitor is a separate floating component. Should it:

- **Option A:** Subscribe independently to selected train
- **Option B:** Receive data from app.js (current)
- **Option C:** Share subscription with Train Control

**Recommendation:** Option A or C - Independent subscription is cleanest.

### 4. Multiple Panels for Same Entity

What if two panels are open for the same entity? (e.g., Properties + Block Editor for same track)

**Answer:** Both subscribe. Both get callbacks. Both update independently. StateManager handles this automatically.

### 5. Offline/Disconnected Handling

What happens if backend becomes unreachable?

**For now:** Show error toast, keep cached data. This can be expanded in a separate error handling doc.

---

## Appendix

### A. Quick Reference: Adding a New Entity Type

If you need to add a new entity type (e.g., `platforms`):

1. **StateManager:** Add to `state` and `subscribers` objects

```javascript
this.state = {
    // ...existing...
    platforms: new Map(),
};

this.subscribers = {
    // ...existing...
    platforms: new Map(),
};
```

2. **updateFromNetwork:** Add parsing logic

```javascript
// In updateFromNetwork()
const currentPlatformIds = new Set(this.state.platforms.keys());
for (const platform of network.platforms || []) {
    this.updateEntity('platforms', platform.platform_id, platform);
    currentPlatformIds.delete(platform.platform_id);
}
for (const deletedId of currentPlatformIds) {
    this.removeEntity('platforms', deletedId);
}
```

3. **Backend:** Add `to_dict()` to entity class

4. **Backend:** Add to `RailwayNetwork.to_dict()`

5. **Backend:** Add individual GET endpoint

6. **API.js:** Add fetch method

7. **Panel:** Create handler following appropriate pattern

### B. Quick Reference: Adding a New Field to Existing Entity

If you need to add a new field to an existing entity (e.g., add `priority` to trains):

1. **Backend:** Add field to entity class

```python
@dataclass
class Train:
    # ...existing...
    priority: int = 5  # NEW
```

2. **Backend:** Add to `to_dict()`

```python
def to_dict(self, network=None) -> dict:
    return {
        # ...existing...
        'priority': self.priority,  # NEW
    }
```

3. **Backend:** Add to `from_dict()` if needed for persistence

4. **Frontend Panel:** Add to render and update methods

```javascript
// In _renderPanel()
html += `<div class="property-item">
    <label>Priority</label>
    <input type="number" id="train-priority" value="${train.priority}">
</div>`;

// In _updateLiveValues()
const priorityEl = document.getElementById('train-priority');
if (priorityEl) priorityEl.value = train.priority;
```

**Note:** StateManager requires NO changes - it's schema-agnostic.

### C. Testing Checklist

Before marking a panel migration complete:

- [ ] Panel opens correctly with data
- [ ] Panel updates when entity changes (during simulation)
- [ ] Panel does NOT update when other entities change
- [ ] User edits are preserved (dirty flag works)
- [ ] Save button commits to backend
- [ ] Backend response updates StateManager
- [ ] Panel shows saved data after save
- [ ] Panel handles entity deletion gracefully
- [ ] Switching to different entity unsubscribes properly
- [ ] No memory leaks (subscribers cleaned up)
- [ ] Console shows no errors
- [ ] Works with simulation running
- [ ] Works with simulation stopped

### D. Common Mistakes to Avoid

1. **Forgetting to unsubscribe**
   - Always call `_cleanup()` when hiding panel or switching entity
   - Store the unsubscribe function returned by `subscribe()`

2. **Modifying StateManager state directly**
   - Never do: `stateManager.state.trains.set(id, data)`
   - Always use: `stateManager.updateEntity('trains', id, data)`

3. **Not handling null in callbacks**
   - Callback receives `null` when entity is deleted
   - Always check: `if (!newData) { /* handle deletion */ }`

4. **Updating local state from callback when dirty**
   - Check `_isDirty` before overwriting user's changes
   - Show indicator instead of silently replacing

5. **Subscribing with wrong entity type**
   - Type is plural: `'trains'` not `'train'`
   - ID format must match: blocks use `block_id`, trains use `id`
