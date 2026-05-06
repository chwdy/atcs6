class API {
    constructor(baseUrl = '/api') {
        this.baseUrl = baseUrl;
    }

    /**
     * Centralized request handler to reduce boilerplate.
     * @param {string} endpoint - API endpoint (e.g., '/network')
     * @param {object} options - Fetch options (method, headers, body, etc.)
     * @param {string} caller - Name of the calling method for logging
     * @returns {Promise<any>} JSON response or null
     */
    async _request(endpoint, options = {}, caller = 'request') {
        const url = `${this.baseUrl}${endpoint}`;
        const config = {
            method: 'GET',
            headers: {},
            ...options
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
            const response = await fetch(url, config);

            if (response.status === 404 && returnNullOn404) {
                return null;
            }

            if (!response.ok) {
                let errorMessage = `HTTP ${response.status}: ${response.statusText}`;
                let errorData = null;
                try {
                    errorData = await response.json();
                    if (errorData.error) errorMessage = errorData.error;
                } catch (e) {
                    try {
                        const text = await response.text();
                        if (text) errorMessage = `HTTP ${response.status}: ${text}`;
                    } catch (e2) {}
                }
                const error = new Error(errorMessage);
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

    async getNetwork() {
        return this._request('/network', {}, 'getNetwork');
    }

    /**
     * Get the inspector snapshot for a single train.
     * Returns null when the train was deleted between polls.
     * @param {string} trainId
     * @returns {Promise<object|null>}
     */
    async getInspectorEntityTrain(trainId) {
        return this._request(
            `/inspector/entity/train/${encodeURIComponent(trainId)}`,
            { returnNullOn404: true },
            'getInspectorEntityTrain'
        );
    }

    /**
     * Get current network layout as StepRecorder steps.
     * Used by the scenario recorder to embed layout in scenario files.
     * @returns {Promise<Array>} List of step objects
     */
    async getNetworkSteps() {
        const data = await this._request('/network/steps', {}, 'getNetworkSteps');
        return data?.steps || [];
    }

    // Individual track operations

    async getTrack(x, y) {
        return this._request(`/track/${x}/${y}`, {}, 'getTrack');
    }

    async updateTrack(x, y, updates) {
        return this._request(`/track/${x}/${y}`, {
            method: 'PATCH',
            body: updates
        }, 'updateTrack');
    }

    async deleteTrack(x, y) {
        return this._request(`/track/${x}/${y}`, {
            method: 'DELETE'
        }, 'deleteTrack');
    }

    async addTracksBulk(tracks) {
        return this._request('/tracks/bulk', {
            method: 'POST',
            body: { tracks }
        }, 'addTracksBulk');
    }

    async deleteTracksBulk(positions) {
        return this._request('/tracks/bulk-delete', {
            method: 'POST',
            body: { positions }
        }, 'deleteTracksBulk');
    }

    async createTrain(trainData) {
        return this._request('/trains', {
            method: 'POST',
            body: trainData
        }, 'createTrain');
    }

    /**
     * Get all trains in the network.
     * @returns {Promise<Array>} List of train objects
     */
    async getTrains() {
        const result = await this._request('/trains', {}, 'getTrains');
        return result.trains || [];
    }

    /**
     * Delete a train.
     * @param {string} trainId - Train ID
     * @returns {Promise<object>} Result
     */
    async deleteTrain(trainId) {
        return this._request(`/trains/${encodeURIComponent(trainId)}`, {
            method: 'DELETE'
        }, 'deleteTrain');
    }

    /**
     * Update train properties.
     * @param {string} trainId - Train ID
     * @param {object} updates - Properties to update
     * @returns {Promise<object>} Updated train
     */
    async updateTrain(trainId, updates) {
        return this._request(`/trains/${encodeURIComponent(trainId)}`, {
            method: 'PATCH',
            body: updates
        }, 'updateTrain');
    }

    /**
     * Control simulation state.
     * @param {string} action - 'start', 'stop', 'reset', 'set_speed'
     * @param {number|null} speed - Simulation speed multiplier
     * @returns {Promise<object>} Simulation status
     */
    async controlSimulation(action, speed = null) {
        const data = { action };
        if (speed !== null) {
            data.speed = speed;
        }
        return this._request('/simulation/control', {
            method: 'POST',
            body: data
        }, 'controlSimulation');
    }

    // === Block Management ===

    /**
     * Get all blocks.
     * @returns {Promise<Array>} List of block objects
     */
    async getBlocks() {
        return this._request('/blocks', {}, 'getBlocks');
    }

    /**
     * Create a new block.
     * @param {string|null} blockId - Optional Block ID
     * @param {string} name - Block name
     * @param {object} options - Additional options (length, speed, etc.)
     * @returns {Promise<object>} Created block
     */
    async createBlock(blockId = null, name = '', options = {}) {
        return this._request('/blocks', {
            method: 'POST',
            body: {
                block_id: blockId,
                name,
                ...options
            }
        }, 'createBlock');
    }

    /**
     * Get a single block by ID.
     * @param {string} blockId - Block ID
     * @returns {Promise<object>} Block object
     */
    async getBlock(blockId) {
        return this._request(`/blocks/${encodeURIComponent(blockId)}`, {}, 'getBlock');
    }

    /**
     * Update block properties.
     * @param {string} blockId - Block ID
     * @param {object} data - Properties to update
     * @returns {Promise<object>} Updated block
     */
    async updateBlock(blockId, data) {
        return this._request(`/blocks/${encodeURIComponent(blockId)}`, {
            method: 'PATCH',
            body: data
        }, 'updateBlock');
    }

    /**
     * Delete a block.
     * @param {string} blockId - Block ID
     * @returns {Promise<object>} Result
     */
    async deleteBlock(blockId) {
        return this._request(`/blocks/${encodeURIComponent(blockId)}`, {
            method: 'DELETE'
        }, 'deleteBlock');
    }

    /**
     * Cycle block state (debug/testing).
     * @param {string} blockId - Block ID
     * @returns {Promise<object>} Updated block
     */
    async cycleBlockState(blockId) {
        return this._request(`/blocks/${encodeURIComponent(blockId)}/cycle-state`, {
            method: 'POST'
        }, 'cycleBlockState');
    }

    /**
     * Add tracks to a block.
     * @param {string} blockId - Block ID
     * @param {Array} positions - Array of {x, y} or [x, y]
     * @param {boolean} force - Force reassignment from other blocks
     * @returns {Promise<object>} Result
     */
    async addTracksToBlock(blockId, positions, force = false) {
        return this._request(`/blocks/${encodeURIComponent(blockId)}/tracks`, {
            method: 'POST',
            body: { positions, force }
        }, 'addTracksToBlock');
    }

    /**
     * Extend a block with new track positions.
     * Uses the /extend endpoint which supports signal removal with force=true.
     *
     * @param {string} blockId - Block ID to extend
     * @param {Array} positions - Array of [x, y] positions to add
     * @param {boolean} force - If true, invalidates signals in the path
     * @param {boolean} forceOverwrite - If true, allows overwriting tracks from other blocks
     * @returns {Promise<Object>} Block data with optional invalidated_signals array
     */
    async extendBlock(blockId, positions, force = false, forceOverwrite = false) {
        return this._request(`/blocks/${encodeURIComponent(blockId)}/extend`, {
            method: 'POST',
            body: { positions, force, force_overwrite: forceOverwrite }
        }, 'extendBlock');
    }

    /**
     * Check if a block can be extended to a specific position.
     * @param {string} blockId - Block ID
     * @param {number} x - X coordinate
     * @param {number} y - Y coordinate
     * @returns {Promise<object>} Validation result
     */
    async canExtendBlock(blockId, x, y) {
        return this._request(`/blocks/${encodeURIComponent(blockId)}/can_extend`, {
            method: 'POST',
            body: { x, y }
        }, 'canExtendBlock');
    }


    // === Signal Management ===

    /**
     * Create a new signal.
     * @param {number} x - X coordinate
     * @param {number} y - Y coordinate
     * @param {string} detectionDirection - Direction to detect trains from
     * @param {string} signalType - 'block', 'chain', etc.
     * @param {string|null} name - Signal name
     * @param {boolean} autoConnect - Auto-connect to block logic
     * @returns {Promise<object>} Created signal
     */
    async createSignal(x, y, detectionDirection, signalType = 'block', name = null, autoConnect = true) {
        return this._request('/signal', {
            method: 'POST',
            body: {
                x,
                y,
                detection_direction: detectionDirection,
                signal_type: signalType,
                name,
                auto_connect: autoConnect
            }
        }, 'createSignal');
    }

    /**
     * Get a signal by ID.
     * @param {string} signalId - Signal ID
     * @returns {Promise<object>} Signal object
     */
    async getSignal(signalId) {
        return this._request(`/signal/${encodeURIComponent(signalId)}`, {}, 'getSignal');
    }

    /**
     * Get a signal at specific coordinates.
     * @param {number} x - X coordinate
     * @param {number} y - Y coordinate
     * @returns {Promise<object|null>} Signal object or null
     */
    async getSignalAt(x, y) {
        return this._request(`/signal/${x}/${y}`, {
            returnNullOn404: true
        }, 'getSignalAt');
    }

    /**
     * Update signal properties.
     * @param {string} signalId - Signal ID
     * @param {object} updates - Properties to update
     * @returns {Promise<object>} Updated signal
     */
    async updateSignal(signalId, updates) {
        return this._request(`/signal/${encodeURIComponent(signalId)}`, {
            method: 'PATCH',
            body: updates
        }, 'updateSignal');
    }

    /**
     * Delete a signal.
     * @param {string} signalId - Signal ID
     * @returns {Promise<object>} Result
     */
    async deleteSignal(signalId) {
        return this._request(`/signal/${encodeURIComponent(signalId)}`, {
            method: 'DELETE'
        }, 'deleteSignal');
    }

    /**
     * Delete signal at specific coordinates.
     * @param {number} x - X coordinate
     * @param {number} y - Y coordinate
     * @returns {Promise<object>} Result
     */
    async deleteSignalAt(x, y) {
        return this._request(`/signal/${x}/${y}`, {
            method: 'DELETE'
        }, 'deleteSignalAt');
    }

    /**
     * Validate signal placement.
     * @param {number} x - X coordinate
     * @param {number} y - Y coordinate
     * @returns {Promise<object>} Validation result
     */
    async validateSignalPlacement(x, y) {
        return this._request(`/signal/validate/${x}/${y}`, {}, 'validateSignalPlacement');
    }

    /**
     * Get blocks reachable from a signal.
     * @param {string} signalId - Signal ID
     * @returns {Promise<object>} Reachable blocks
     */
    async getReachableBlocks(signalId) {
        return this._request(`/signal/${encodeURIComponent(signalId)}/reachable-blocks`, {}, 'getReachableBlocks');
    }

    // === Station API Methods ===

    // === Platform API Methods (new naming, same physical entity) ===

    async createPlatform(data) {
        return this._request('/platform', {
            method: 'POST',
            body: data
        }, 'createPlatform');
    }

    async getPlatform(platformId) {
        return this._request(`/platform/${encodeURIComponent(platformId)}`, {
            returnNullOn404: true
        }, 'getPlatform');
    }

    async updatePlatform(platformId, updates) {
        return this._request(`/platform/${encodeURIComponent(platformId)}`, {
            method: 'PATCH',
            body: updates
        }, 'updatePlatform');
    }

    async deletePlatform(platformId) {
        return this._request(`/platform/${encodeURIComponent(platformId)}`, {
            method: 'DELETE'
        }, 'deletePlatform');
    }

    async validatePlatformPlacement(x, y) {
        return this._request(`/platform/validate/${x}/${y}`, {}, 'validatePlatformPlacement');
    }

    async extendPlatform(platformId, positions) {
        return this._request(`/platform/${encodeURIComponent(platformId)}/extend`, {
            method: 'POST',
            body: { positions }
        }, 'extendPlatform');
    }

    async removePlatformTile(platformId, x, y) {
        return this._request(`/platform/${encodeURIComponent(platformId)}/remove_tile`, {
            method: 'POST',
            body: { x, y }
        }, 'removePlatformTile');
    }

    async assignPlatformTileToBlock(platformId, x, y, blockId) {
        return this._request(`/platform/${encodeURIComponent(platformId)}/assign_tile`, {
            method: 'POST',
            body: { x, y, block_id: blockId }
        }, 'assignPlatformTileToBlock');
    }

    /** Legacy compatibility wrapper: get physical platform by ID. */
    async getStation(stationId) {
        return this._request(`/platform/${encodeURIComponent(stationId)}`, {
            returnNullOn404: true
        }, 'getStation');
    }

    // === Logical Station Group API Methods ===

    async createStationGroup(data) {
        return this._request('/station', {
            method: 'POST',
            body: data
        }, 'createStationGroup');
    }

    async listStationGroups() {
        return this._request('/station', {}, 'listStationGroups');
    }

    async deleteStationGroup(stationId) {
        return this._request(`/station/${encodeURIComponent(stationId)}`, {
            method: 'DELETE'
        }, 'deleteStationGroup');
    }

    async addPlatformToStation(stationId, platformId) {
        return this._request(`/station/${encodeURIComponent(stationId)}/add-platform`, {
            method: 'POST',
            body: { platform_id: platformId }
        }, 'addPlatformToStation');
    }

    async removePlatformFromStation(stationId, platformId) {
        return this._request(`/station/${encodeURIComponent(stationId)}/remove-platform`, {
            method: 'POST',
            body: { platform_id: platformId }
        }, 'removePlatformFromStation');
    }

    /**
     * Remove tiles from a block.
     * @param {string} blockId - Block ID
     * @param {Array} positions - Positions to remove
     * @returns {Promise<object>} Result
     */
    async removeBlockTiles(blockId, positions) {
        return this._request(`/blocks/${encodeURIComponent(blockId)}/tracks`, {
            method: 'DELETE',
            body: { positions }
        }, 'removeBlockTiles');
    }

    /**
     * Get a specific train.
     * @param {string} trainId - Train ID
     * @returns {Promise<object>} Train object
     */
    async getTrain(trainId) {
        return this._request(`/trains/${encodeURIComponent(trainId)}`, {}, 'getTrain');
    }

    /**
     * Get train lookahead data for the train monitor.
     * @param {string} trainId - Train ID
     * @returns {Promise<object>} Lookahead data with blocks, signals, switches, stations
     */
    async getTrainLookahead(trainId) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/lookahead`, {}, 'getTrainLookahead');
    }

    // === Train Control API ===

    /**
     * Turn train power on.
     * @param {string} trainId - Train ID
     * @returns {Promise<object>} Result
     */
    async trainPowerOn(trainId) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/power-on`, {
            method: 'POST'
        }, 'trainPowerOn');
    }

    /**
     * Turn train power off.
     *
     * In v1 power-off is latched: ``power_off_cmd()`` writes
     * ``pending_power_off`` (and a ``pending_path_cancel_request`` when there
     * is a live path), and Phase 3 commits the OFF transition on a later
     * tick. The response indicates the request was accepted; callers must
     * observe ``train.onboard.power_on === false`` on a subsequent fetch
     * before treating the train as actually OFF.
     *
     * @param {string} trainId - Train ID
     * @returns {Promise<object>} Result
     */
    async trainPowerOff(trainId) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/power-off`, {
            method: 'POST'
        }, 'trainPowerOff');
    }

    /**
     * Set active cab.
     * @param {string} trainId - Train ID
     * @param {string} cab - 'A' or 'B'
     * @returns {Promise<object>} Result
     */
    async trainSetCab(trainId, cab) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/set-cab`, {
            method: 'POST',
            body: { cab }
        }, 'trainSetCab');
    }


    /**
     * Set manual speed.
     * @param {string} trainId - Train ID
     * @param {number} speed - Speed
     * @returns {Promise<object>} Result
     */
    async trainSetManualSpeed(trainId, speed) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/manual-speed`, {
            method: 'POST',
            body: { speed }
        }, 'trainSetManualSpeed');
    }

    /**
     * Set travel direction.
     * @param {string} trainId - Train ID
     * @param {string} direction - 'forward' or 'reverse'
     * @returns {Promise<object>} Result
     */
    async trainSetTravelDirection(trainId, direction) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/travel-direction`, {
            method: 'POST',
            body: { direction }
        }, 'trainSetTravelDirection');
    }


    // === OnboardComputer API (New Train Control Architecture) ===
    
    /**
     * Get OnboardComputer state for a train.
     * This is the single source of truth for train control state.
     * @param {string} trainId - Train ID
     * @returns {Promise<object>} OnboardComputer state
     */
    async getOnboardState(trainId) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/onboard`, {}, 'getOnboardState');
    }

    /**
     * Set throttle/brake handle position.
     * @param {string} trainId - Train ID
     * @param {string} position - Handle position: P1-P5, N, B1-B8, EB
     * @returns {Promise<object>} { status, onboard }
     */
    async setHandle(trainId, position) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/handle`, {
            method: 'POST',
            body: { position }
        }, 'setHandle');
    }

    /**
     * Set control mode (ATO or Manual) via OnboardComputer.
     * @param {string} trainId - Train ID
     * @param {string} mode - 'ato' or 'manual'
     * @returns {Promise<object>} { status, onboard }
     */
    async setControlMode(trainId, mode) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/mode`, {
            method: 'POST',
            body: { mode }
        }, 'setControlMode');
    }

    /**
     * Start ATO movement via OnboardComputer.
     * @param {string} trainId - Train ID
     * @returns {Promise<object>} { status, onboard }
     */
    async atoGo(trainId) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/ato-go`, {
            method: 'POST'
        }, 'atoGo');
    }

    /**
     * Stop ATO movement (service stop) via OnboardComputer.
     * @param {string} trainId - Train ID
     * @returns {Promise<object>} { status, onboard }
     */
    async atoStop(trainId) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/ato-stop`, {
            method: 'POST'
        }, 'atoStop');
    }

    // === Schedule API ===

    /**
     * Get all schedules.
     * @returns {Promise<Array>} List of schedules
     */
    async getSchedules() {
        const result = await this._request('/schedules', {}, 'getSchedules');
        return result.schedules || [];
    }


    /**
     * Delete a schedule.
     * @param {string} scheduleId - Schedule ID
     * @returns {Promise<object>} Result
     */
    async deleteSchedule(scheduleId) {
        return this._request(`/schedules/${encodeURIComponent(scheduleId)}`, {
            method: 'DELETE'
        }, 'deleteSchedule');
    }

    // === Layout Persistence API ===

    /**
     * Get list of saved layouts.
     * @returns {Promise<Array>} List of layout objects
     */
    async getLayouts() {
        const result = await this._request('/layouts', {}, 'getLayouts');
        return result.layouts || [];
    }

    /**
     * Save current layout.
     * @param {string} name - Layout name
     * @param {string} description - Description
     * @returns {Promise<object>} Save result
     */
    async saveLayout(name, description = '') {
        return this._request('/layouts', {
            method: 'POST',
            body: { name, description }
        }, 'saveLayout');
    }

    /**
     * Load a saved layout.
     * @param {string} name - Layout name
     * @returns {Promise<object>} Load result
     */
    async loadLayout(name) {
        return this._request(`/layouts/${encodeURIComponent(name)}/load`, {
            method: 'POST'
        }, 'loadLayout');
    }

    /**
     * Delete a saved layout.
     * @param {string} name - Layout name
     * @returns {Promise<object>} Result
     */
    async deleteLayout(name) {
        return this._request(`/layouts/${encodeURIComponent(name)}`, {
            method: 'DELETE'
        }, 'deleteLayout');
    }

    /**
     * Clear the entire network.
     * @returns {Promise<object>} Cleared stats
     */
    async clearNetwork() {
        return this._request('/network/clear', {
            method: 'POST'
        }, 'clearNetwork');
    }

    /**
     * Get network statistics.
     * @returns {Promise<object>} Stats object
     */
    async getNetworkStats() {
        return this._request('/network/stats', {}, 'getNetworkStats');
    }

    // === Scale Configuration API ===

    /**
     * Get current scale configuration.
     * @returns {Promise<object>} Scale settings
     */
    async getScale() {
        return this._request('/scale', {}, 'getScale');
    }

    /**
     * Set custom scale.
     * @param {number} scale - Scale ratio (e.g. 160)
     * @returns {Promise<object>} Updated scale
     */
    async setScale(scale) {
        return this._request('/scale', {
            method: 'PUT',
            body: { scale }
        }, 'setScale');
    }


    // === Path Dispatcher API ===

    /**
     * Create a path for a train.
     *
     * The starting block is implicit (the train's current head block); the
     * server only needs the destination and direction.
     *
     * @param {object} args
     * @param {string} args.train_id - Train ID requesting the path.
     * @param {string} args.requested_destination_block_id - Destination block ID.
     * @param {string} [args.requested_destination_direction] - "forward" or "reverse".
     * @param {string} [args.acquisition_mode] - "all_or_nothing" or "best_effort".
     * @returns {Promise<object>} PathAcquisitionResult.
     */
    async createPath({
        train_id,
        requested_destination_block_id,
        requested_destination_direction = 'forward',
        acquisition_mode = 'all_or_nothing'
    } = {}) {
        return this._request('/paths', {
            method: 'POST',
            body: {
                train_id,
                requested_destination_block_id,
                requested_destination_direction,
                acquisition_mode
            }
        }, 'createPath');
    }

    /**
     * Get list of active paths.
     * @param {object} filters - Optional filters: { train_id, block_id, status }
     * @returns {Promise<Array>} List of path objects
     */
    async getPaths(filters = {}) {
        const params = new URLSearchParams();
        if (filters.train_id) params.append('train_id', filters.train_id);
        if (filters.block_id) params.append('block_id', filters.block_id);
        if (filters.status) params.append('status', filters.status);
        
        const queryString = params.toString() ? `?${params}` : '';
        const result = await this._request(`/paths${queryString}`, {}, 'getPaths');
        return result.paths || [];
    }

    /**
     * Get details of a specific path.
     * @param {string} pathId - Path ID
     * @returns {Promise<object>} Path object or error
     */
    async getPath(pathId) {
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
     *
     * @param {string} pathId - Path ID to cancel
     * @returns {Promise<object>} `{ success, status, path_id, train_id?, note? }`
     *     when latched, or a CancellationResult when cancelled synchronously.
     */
    async cancelPath(pathId) {
        return this._request(`/paths/${encodeURIComponent(pathId)}`, {
            method: 'DELETE'
        }, 'cancelPath');
    }

    /**
     * Extend an existing path to a new destination.
     *
     * @param {string} pathId - Path ID to extend.
     * @param {object} args
     * @param {string} args.requested_destination_block_id - New destination block ID.
     * @param {string} [args.requested_destination_direction] - "forward" or "reverse".
     * @param {string} [args.acquisition_mode] - "all_or_nothing" or "best_effort".
     * @returns {Promise<object>} PathAcquisitionResult (result_kind=EXTENDED on success).
     */
    async extendPath(pathId, {
        requested_destination_block_id,
        requested_destination_direction = 'forward',
        acquisition_mode = 'all_or_nothing'
    } = {}) {
        return this._request(`/paths/${encodeURIComponent(pathId)}/extend`, {
            method: 'PUT',
            body: {
                requested_destination_block_id,
                requested_destination_direction,
                acquisition_mode
            }
        }, 'extendPath');
    }


    /**
     * Get all blocks reachable from a starting block.
     * @param {string} blockId - Starting block ID
     * @param {boolean} includeStations - Include station IDs (default: true)
     * @returns {Promise<object>} { reachable_endpoints: [], count }
     */
    async getReachableEndpoints(blockId, includeStations = true) {
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
     * @param {string} pathId - Path ID to get extension endpoints for
     * @param {boolean} includeStations - Include station IDs (default: true)
     * @returns {Promise<object>} { path_id, reachable_endpoints: [], count, exit_direction, error }
     */
    async getPathReachableExtensions(pathId, includeStations = true) {
        return this._request(
            `/paths/${encodeURIComponent(pathId)}/reachable-extensions?include_stations=${includeStations}`,
            {},
            'getPathReachableExtensions'
        );
    }


    // === Switch Management API ===


    /**
     * Get a specific switch.
     * @param {string} switchId - Switch ID
     * @returns {Promise<object>} Switch object
     */
    async getSwitch(switchId) {
        return this._request(`/switches/${encodeURIComponent(switchId)}`, {}, 'getSwitch');
    }

    /**
     * Update a switch.
     * @param {string} switchId - Switch ID
     * @param {object} updates - Updates
     * @returns {Promise<object>} Updated switch
     */
    async updateSwitch(switchId, updates) {
        return this._request(`/switches/${encodeURIComponent(switchId)}`, {
            method: 'PUT',
            body: updates
        }, 'updateSwitch');
    }

    /**
     * Delete a switch.
     * @param {string} switchId - Switch ID
     * @returns {Promise<object>} Result
     */
    async deleteSwitch(switchId) {
        return this._request(`/switches/${encodeURIComponent(switchId)}`, {
            method: 'DELETE'
        }, 'deleteSwitch');
    }

    /**
     * Change switch state (MAIN=0, DIVERGE=1).
     * Fails with 409 Conflict if switch is locked by a path.
     * @param {string} switchId - Switch ID
     * @param {number} state - 0 for MAIN, 1 for DIVERGE
     * @returns {Promise<object>} { success, message, switch }
     */
    async setSwitchState(switchId, state) {
        return this._request(`/switches/${encodeURIComponent(switchId)}/state`, {
            method: 'PUT',
            body: { state }
        }, 'setSwitchState');
    }

    // === Train Routes (Route Editor) ===

    /**
     * Get all train routes.
     * @returns {Promise<Array>} List of routes
     */
    async getRoutes() {
        return this._request('/train-routes', {}, 'getRoutes');
    }

    /**
     * Get a specific route.
     * @param {string} routeId - Route ID
     * @returns {Promise<object>} Route object
     */
    async getRoute(routeId) {
        return this._request(`/train-routes/${encodeURIComponent(routeId)}`, {}, 'getRoute');
    }

    /**
     * Create a new route.
     * @param {object} routeData - Route data
     * @returns {Promise<object>} Created route
     */
    async createRoute(routeData) {
        return this._request('/train-routes', {
            method: 'POST',
            body: routeData
        }, 'createRoute');
    }

    /**
     * Update a route.
     * @param {string} routeId - Route ID
     * @param {object} updates - Properties to update
     * @returns {Promise<object>} Updated route
     */
    async updateRoute(routeId, updates) {
        return this._request(`/train-routes/${encodeURIComponent(routeId)}`, {
            method: 'PUT',
            body: updates
        }, 'updateRoute');
    }

    /**
     * Delete a route.
     * @param {string} routeId - Route ID
     * @returns {Promise<object>} Result
     */
    async deleteRoute(routeId) {
        return this._request(`/train-routes/${encodeURIComponent(routeId)}`, {
            method: 'DELETE'
        }, 'deleteRoute');
    }

    /**
     * Activate a route.
     * @param {string} routeId - Route ID
     * @returns {Promise<object>} Updated route
     */
    async activateRoute(routeId) {
        return this._request(`/train-routes/${encodeURIComponent(routeId)}/activate`, {
            method: 'POST'
        }, 'activateRoute');
    }

    /**
     * Deactivate a route.
     * @param {string} routeId - Route ID
     * @returns {Promise<object>} Updated route
     */
    async deactivateRoute(routeId) {
        return this._request(`/train-routes/${encodeURIComponent(routeId)}/deactivate`, {
            method: 'POST'
        }, 'deactivateRoute');
    }

    /**
     * Get route execution status.
     * @param {string} routeId - Route ID
     * @returns {Promise<object>} Route status with train execution states
     */
    async getRouteStatus(routeId) {
        return this._request(`/train-routes/${encodeURIComponent(routeId)}/status`, {}, 'getRouteStatus');
    }

    /**
     * Check viability between platform pairs.
     * @param {Array} pairs - Array of [from_platform_id, to_platform_id] pairs
     * @returns {Promise<object>} Viability results
     */
    async checkViability(pairs) {
        return this._request('/train-routes/check-viability', {
            method: 'POST',
            body: { pairs }
        }, 'checkViability');
    }

    /**
     * Assign/unassign a train to/from a route.
     * @param {string} trainId - Train ID
     * @param {string|null} routeId - Route ID or null to unassign
     * @param {object} options - Assignment options
     * @returns {Promise<object>} Result
     */
    async assignRoute(trainId, routeId, options = {}) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/assign-route`, {
            method: 'PUT',
            body: {
                route_id: routeId,
                force: options.force || false,
                auto_park: options.auto_park !== false,
                next_waypoint_index: options.nextWaypointIndex
            }
        }, 'assignRoute');
    }

    /**
     * Override a train's current waypoint index.
     * @param {string} trainId - Train ID
     * @param {number} waypointIndex - New waypoint index
     * @returns {Promise<object>} Result
     */
    async setTrainWaypoint(trainId, waypointIndex) {
        return this._request(`/trains/${encodeURIComponent(trainId)}/set-waypoint`, {
            method: 'POST',
            body: { waypoint_index: waypointIndex }
        }, 'setTrainWaypoint');
    }
}

// Export API instance
window.api = new API();
