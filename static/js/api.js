class API {
    constructor(baseUrl = '/api') {
        this.baseUrl = baseUrl;
    }

    async getNetwork() {
        try {
            const response = await fetch(`${this.baseUrl}/network`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            const data = await response.json();
            return data;
        } catch (e) {
            console.error('[API] getNetwork failed:', e);
            throw e;
        }
    }

    // Individual track operations

    async getTrack(x, y) {
        try {
            const response = await fetch(`${this.baseUrl}/track/${x}/${y}`);
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getTrack failed:', e);
            throw e;
        }
    }

    async updateTrack(x, y, updates) {
        try {
            const response = await fetch(`${this.baseUrl}/track/${x}/${y}`, {
                method: 'PATCH',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(updates)
            });
            if (!response.ok) {
                const text = await response.text();
                throw new Error(`HTTP ${response.status}: ${text}`);
            }
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] updateTrack failed:', e);
            throw e;
        }
    }

    async deleteTrack(x, y) {
        try {
            const response = await fetch(`${this.baseUrl}/track/${x}/${y}`, {
                method: 'DELETE'
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] deleteTrack failed:', e);
            throw e;
        }
    }

    async addTracksBulk(tracks) {
        try {
            const response = await fetch(`${this.baseUrl}/tracks/bulk`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ tracks })
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] addTracksBulk failed:', e);
            throw e;
        }
    }

    async deleteTracksBulk(positions) {
        try {
            const response = await fetch(`${this.baseUrl}/tracks/bulk-delete`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ positions })
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] deleteTracksBulk failed:', e);
            throw e;
        }
    }

    async createTrain(trainData) {
        try {
            const response = await fetch(`${this.baseUrl}/trains`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(trainData)
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] createTrain failed:', e);
            throw e;
        }
    }

    async getTrains() {
        try {
            const response = await fetch(`${this.baseUrl}/trains`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getTrains failed:', e);
            throw e;
        }
    }

    async deleteTrain(trainId) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}`, {
                method: 'DELETE'
            });
            if (!response.ok) {
                const result = await response.json();
                throw new Error(result.error || `HTTP ${response.status}: ${response.statusText}`);
            }
            return await response.json();
        } catch (e) {
            console.error('[API] deleteTrain failed:', e);
            throw e;
        }
    }

    async updateTrain(trainId, updates) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(updates)
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to update train');
            }
            return result;
        } catch (e) {
            console.error('[API] updateTrain failed:', e);
            throw e;
        }
    }

    async controlSimulation(action, speed = null) {
        const data = { action };
        if (speed !== null) {
            data.speed = speed;
        }

        try {
            const response = await fetch(`${this.baseUrl}/simulation/control`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(data)
            });
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] controlSimulation failed:', e);
            throw e;
        }
    }

    // === Block Management ===

    async getBlocks() {
        try {
            const response = await fetch(`${this.baseUrl}/blocks`);
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getBlocks failed:', e);
            throw e;
        }
    }

    async createBlock(blockId = null, name = '', options = {}) {
        try {
            const response = await fetch(`${this.baseUrl}/blocks`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    block_id: blockId,
                    name,
                    ...options
                })
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] createBlock failed:', e);
            throw e;
        }
    }

    async getBlock(blockId) {
        try {
            const response = await fetch(`${this.baseUrl}/blocks/${blockId}`);
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getBlock failed:', e);
            throw e;
        }
    }

    async updateBlock(blockId, data) {
        try {
            const response = await fetch(`${this.baseUrl}/blocks/${blockId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] updateBlock failed:', e);
            throw e;
        }
    }

    async deleteBlock(blockId) {
        try {
            const response = await fetch(`${this.baseUrl}/blocks/${blockId}`, {
                method: 'DELETE'
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] deleteBlock failed:', e);
            throw e;
        }
    }

    async cycleBlockState(blockId) {
        try {
            const response = await fetch(`${this.baseUrl}/blocks/${blockId}/cycle-state`, {
                method: 'POST'
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] cycleBlockState failed:', e);
            throw e;
        }
    }

    async addTracksToBlock(blockId, positions, force = false) {
        try {
            const response = await fetch(`${this.baseUrl}/blocks/${blockId}/tracks`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ positions, force })
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] addTracksToBlock failed:', e);
            throw e;
        }
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
        try {
            const response = await fetch(`${this.baseUrl}/blocks/${blockId}/extend`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ positions, force, force_overwrite: forceOverwrite })
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to extend block');
            }
            return result;
        } catch (e) {
            console.error('[API] extendBlock failed:', e);
            throw e;
        }
    }

    async canExtendBlock(blockId, x, y) {
        try {
            const response = await fetch(`${this.baseUrl}/blocks/${blockId}/can_extend`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ x, y })
            });
            const result = await response.json();
            if (!response.ok && response.status !== 200) {
                console.error('[API] canExtendBlock error:', result);
                throw new Error(result.error || 'Failed to check block extension');
            }
            return result;
        } catch (e) {
            console.error('[API] canExtendBlock failed:', e);
            throw e;
        }
    }

    async removeTracksFromBlock(blockId, positions) {
        try {
            const response = await fetch(`${this.baseUrl}/blocks/${blockId}/tracks`, {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ positions })
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] removeTracksFromBlock failed:', e);
            throw e;
        }
    }

    // === Signal Management ===

    async createSignal(x, y, detectionDirection, signalType = 'block', name = null, autoConnect = true) {
        try {
            const response = await fetch(`${this.baseUrl}/signal`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    x,
                    y,
                    detection_direction: detectionDirection,
                    signal_type: signalType,
                    name,
                    auto_connect: autoConnect
                })
            });
            const result = await response.json();
            if (!response.ok) {
                console.error('[API] createSignal error:', result);
                throw new Error(result.error || 'Failed to create signal');
            }
            return result;
        } catch (e) {
            console.error('[API] createSignal failed:', e);
            throw e;
        }
    }

    async getSignal(signalId) {
        try {
            const response = await fetch(`${this.baseUrl}/signal/${signalId}`);
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getSignal failed:', e);
            throw e;
        }
    }

    async getSignalAt(x, y) {
        try {
            const response = await fetch(`${this.baseUrl}/signal/${x}/${y}`);
            if (response.status === 404) {
                return null;
            }
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getSignalAt failed:', e);
            throw e;
        }
    }

    async updateSignal(signalId, updates) {
        try {
            const response = await fetch(`${this.baseUrl}/signal/${signalId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(updates)
            });
            const result = await response.json();
            if (!response.ok) {
                console.error('[API] updateSignal error:', result);
                throw new Error(result.error || 'Failed to update signal');
            }
            return result;
        } catch (e) {
            console.error('[API] updateSignal failed:', e);
            throw e;
        }
    }

    async deleteSignal(signalId) {
        try {
            const response = await fetch(`${this.baseUrl}/signal/${signalId}`, {
                method: 'DELETE'
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] deleteSignal failed:', e);
            throw e;
        }
    }

    async deleteSignalAt(x, y) {
        try {
            const response = await fetch(`${this.baseUrl}/signal/${x}/${y}`, {
                method: 'DELETE'
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] deleteSignalAt failed:', e);
            throw e;
        }
    }

    async validateSignalPlacement(x, y) {
        try {
            const response = await fetch(`${this.baseUrl}/signal/validate/${x}/${y}`);
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] validateSignalPlacement failed:', e);
            throw e;
        }
    }

    async getReachableBlocks(signalId) {
        try {
            const response = await fetch(`${this.baseUrl}/signal/${signalId}/reachable-blocks`);
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getReachableBlocks failed:', e);
            throw e;
        }
    }

    // === Station API Methods ===

    async createStation(x, y, name, lengthCm, platformSide, topSpeedKmh, stationId = null) {
        try {
            const response = await fetch(`${this.baseUrl}/station`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    x, y, name,
                    length_cm: lengthCm,
                    platform_side: platformSide,
                    top_speed_kmh: topSpeedKmh,
                    station_id: stationId
                })
            });
            const result = await response.json();
            if (!response.ok) {
                console.error('[API] createStation error:', result);
                throw new Error(result.error || 'Failed to create station');
            }
            return result;
        } catch (e) {
            console.error('[API] createStation failed:', e);
            throw e;
        }
    }

    async getStation(stationId) {
        try {
            const response = await fetch(`${this.baseUrl}/station/${stationId}`);
            if (response.status === 404) {
                return null;
            }
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getStation failed:', e);
            throw e;
        }
    }

    async updateStation(stationId, updates) {
        try {
            const response = await fetch(`${this.baseUrl}/station/${stationId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(updates)
            });
            const result = await response.json();
            if (!response.ok) {
                console.error('[API] updateStation error:', result);
                throw new Error(result.error || 'Failed to update station');
            }
            return result;
        } catch (e) {
            console.error('[API] updateStation failed:', e);
            throw e;
        }
    }

    async deleteStation(stationId) {
        try {
            const response = await fetch(`${this.baseUrl}/station/${stationId}`, {
                method: 'DELETE'
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] deleteStation failed:', e);
            throw e;
        }
    }

    async validateStationPlacement(x, y) {
        try {
            const response = await fetch(`${this.baseUrl}/station/validate/${x}/${y}`);
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] validateStationPlacement failed:', e);
            throw e;
        }
    }

    async extendStation(stationId, positions) {
        try {
            const response = await fetch(`${this.baseUrl}/station/${stationId}/extend`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ positions })
            });
            const result = await response.json();
            if (!response.ok) {
                console.error('[API] extendStation error:', result);
                throw new Error(result.error || 'Failed to extend station');
            }
            return result;
        } catch (e) {
            console.error('[API] extendStation failed:', e);
            throw e;
        }
    }

    async removeStationTile(stationId, x, y) {
        try {
            const response = await fetch(`${this.baseUrl}/station/${stationId}/remove_tile`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ x, y })
            });
            const result = await response.json();
            if (!response.ok && response.status !== 200) {
                console.error('[API] removeStationTile error:', result);
                throw new Error(result.error || 'Failed to remove station tile');
            }
            return result;
        } catch (e) {
            console.error('[API] removeStationTile failed:', e);
            throw e;
        }
    }

    async canExtendStation(stationId, x, y) {
        try {
            const response = await fetch(`${this.baseUrl}/station/${stationId}/can_extend`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ x, y })
            });
            const result = await response.json();
            if (!response.ok && response.status !== 200) {
                console.error('[API] canExtendStation error:', result);
                throw new Error(result.error || 'Failed to check station extension');
            }
            return result;
        } catch (e) {
            console.error('[API] canExtendStation failed:', e);
            throw e;
        }
    }

    async assignTileToBlock(stationId, x, y, blockId) {
        try {
            const response = await fetch(`${this.baseUrl}/station/${stationId}/assign_tile`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ x, y, block_id: blockId })
            });
            const result = await response.json();
            if (!response.ok && response.status !== 200) {
                console.error('[API] assignTileToBlock error:', result);
                throw new Error(result.error || 'Failed to assign tile to block');
            }
            return result;
        } catch (e) {
            console.error('[API] assignTileToBlock failed:', e);
            throw e;
        }
    }

    async unassignTileFromBlock(stationId, x, y) {
        try {
            const response = await fetch(`${this.baseUrl}/station/${stationId}/unassign_tile`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ x, y })
            });
            const result = await response.json();
            if (!response.ok && response.status !== 200) {
                console.error('[API] unassignTileFromBlock error:', result);
                throw new Error(result.error || 'Failed to unassign tile from block');
            }
            return result;
        } catch (e) {
            console.error('[API] unassignTileFromBlock failed:', e);
            throw e;
        }
    }

    async getBlockAssignments(stationId) {
        try {
            const response = await fetch(`${this.baseUrl}/station/${stationId}/block_assignments`);
            const result = await response.json();
            if (!response.ok) {
                console.error('[API] getBlockAssignments error:', result);
                throw new Error(result.error || 'Failed to get block assignments');
            }
            return result;
        } catch (e) {
            console.error('[API] getBlockAssignments failed:', e);
            throw e;
        }
    }

    async removeBlockTiles(blockId, positions) {
        try {
            const response = await fetch(`${this.baseUrl}/blocks/${blockId}/tracks`, {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ positions })
            });
            const result = await response.json();
            if (!response.ok && response.status !== 200) {
                console.error('[API] removeBlockTiles error:', result);
                throw new Error(result.error || 'Failed to remove block tiles');
            }
            return result;
        } catch (e) {
            console.error('[API] removeBlockTiles failed:', e);
            throw e;
        }
    }

    async getTrain(trainId) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}`);
            const result = await response.json();
            if (!response.ok) {
                console.error('[API] getTrain error:', result);
                throw new Error(result.error || 'Failed to get train');
            }
            return result;
        } catch (e) {
            console.error('[API] getTrain failed:', e);
            throw e;
        }
    }

    // === Train Control API ===

    async trainPowerOn(trainId) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/power-on`, {
                method: 'POST'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to power on train');
            }
            return result;
        } catch (e) {
            console.error('[API] trainPowerOn failed:', e);
            throw e;
        }
    }

    async trainPowerOff(trainId) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/power-off`, {
                method: 'POST'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to power off train');
            }
            return result;
        } catch (e) {
            console.error('[API] trainPowerOff failed:', e);
            throw e;
        }
    }

    async trainSetDirection(trainId, direction) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/set-direction`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ direction })
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to set train direction');
            }
            return result;
        } catch (e) {
            console.error('[API] trainSetDirection failed:', e);
            throw e;
        }
    }

    async trainClearDirection(trainId) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/clear-direction`, {
                method: 'POST'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to clear train direction');
            }
            return result;
        } catch (e) {
            console.error('[API] trainClearDirection failed:', e);
            throw e;
        }
    }

    async trainDepart(trainId) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/depart`, {
                method: 'POST'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to depart train');
            }
            return result;
        } catch (e) {
            console.error('[API] trainDepart failed:', e);
            throw e;
        }
    }

    async trainArrive(trainId) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/arrive`, {
                method: 'POST'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to arrive train');
            }
            return result;
        } catch (e) {
            console.error('[API] trainArrive failed:', e);
            throw e;
        }
    }

    async trainTurnAround(trainId) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/turn-around`, {
                method: 'POST'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to turn around train');
            }
            return result;
        } catch (e) {
            console.error('[API] trainTurnAround failed:', e);
            throw e;
        }
    }

    async trainEnableManual(trainId) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/enable-manual`, {
                method: 'POST'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to enable manual mode');
            }
            return result;
        } catch (e) {
            console.error('[API] trainEnableManual failed:', e);
            throw e;
        }
    }

    async trainDisableManual(trainId) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/disable-manual`, {
                method: 'POST'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to disable manual mode');
            }
            return result;
        } catch (e) {
            console.error('[API] trainDisableManual failed:', e);
            throw e;
        }
    }

    async trainSetManualSpeed(trainId, speed) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/manual-speed`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ speed })
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to set manual speed');
            }
            return result;
        } catch (e) {
            console.error('[API] trainSetManualSpeed failed:', e);
            throw e;
        }
    }

    async trainSetTravelDirection(trainId, direction) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/travel-direction`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ direction })
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to set travel direction');
            }
            return result;
        } catch (e) {
            console.error('[API] trainSetTravelDirection failed:', e);
            throw e;
        }
    }

    async trainEmergencyStop(trainId) {
        try {
            const response = await fetch(`${this.baseUrl}/trains/${trainId}/emergency-stop`, {
                method: 'POST'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to emergency stop train');
            }
            return result;
        } catch (e) {
            console.error('[API] trainEmergencyStop failed:', e);
            throw e;
        }
    }

    /**
     * Get lookahead data for train monitor display
     * @param {string} trainId - Train ID
     * @param {number} maxDistanceCm - Maximum lookahead distance in cm (default: 100000)
     * @returns {Object} Lookahead data with signals, blocks, stations ahead
     */
    async getTrainLookahead(trainId, maxDistanceCm = 100000) {
        try {
            const url = new URL(`${this.baseUrl}/trains/${trainId}/lookahead`, window.location.origin);
            if (maxDistanceCm !== 100000) {
                url.searchParams.set('max_distance_cm', maxDistanceCm);
            }
            const response = await fetch(url);
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to get train lookahead');
            }
            return result;
        } catch (e) {
            console.error('[API] getTrainLookahead failed:', e);
            throw e;
        }
    }

    // === Schedule API ===

    async getSchedules() {
        try {
            const response = await fetch(`${this.baseUrl}/schedules`);
            const result = await response.json();
            return result.schedules || [];
        } catch (e) {
            console.error('[API] getSchedules failed:', e);
            throw e;
        }
    }

    async getSchedule(scheduleId) {
        try {
            const response = await fetch(`${this.baseUrl}/schedules/${encodeURIComponent(scheduleId)}`);
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to get schedule');
            }
            return result;
        } catch (e) {
            console.error('[API] getSchedule failed:', e);
            throw e;
        }
    }

    async createSchedule(trainId, stops, priority = 0) {
        try {
            const response = await fetch(`${this.baseUrl}/schedules`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ train_id: trainId, stops, priority })
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to create schedule');
            }
            return result;
        } catch (e) {
            console.error('[API] createSchedule failed:', e);
            throw e;
        }
    }

    async deleteSchedule(scheduleId) {
        try {
            const response = await fetch(`${this.baseUrl}/schedules/${encodeURIComponent(scheduleId)}`, {
                method: 'DELETE'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to delete schedule');
            }
            return result;
        } catch (e) {
            console.error('[API] deleteSchedule failed:', e);
            throw e;
        }
    }

    // === Layout Persistence API ===

    async getLayouts() {
        try {
            const response = await fetch(`${this.baseUrl}/layouts`);
            const result = await response.json();
            return result.layouts || [];
        } catch (e) {
            console.error('[API] getLayouts failed:', e);
            throw e;
        }
    }

    async saveLayout(name, description = '') {
        try {
            const response = await fetch(`${this.baseUrl}/layouts`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, description })
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to save layout');
            }
            return result;
        } catch (e) {
            console.error('[API] saveLayout failed:', e);
            throw e;
        }
    }

    async loadLayout(name) {
        try {
            const response = await fetch(`${this.baseUrl}/layouts/${encodeURIComponent(name)}/load`, {
                method: 'POST'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to load layout');
            }
            return result;
        } catch (e) {
            console.error('[API] loadLayout failed:', e);
            throw e;
        }
    }

    async deleteLayout(name) {
        try {
            const response = await fetch(`${this.baseUrl}/layouts/${encodeURIComponent(name)}`, {
                method: 'DELETE'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to delete layout');
            }
            return result;
        } catch (e) {
            console.error('[API] deleteLayout failed:', e);
            throw e;
        }
    }

    async clearNetwork() {
        try {
            const response = await fetch(`${this.baseUrl}/network/clear`, {
                method: 'POST'
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to clear network');
            }
            return result;
        } catch (e) {
            console.error('[API] clearNetwork failed:', e);
            throw e;
        }
    }

    async getNetworkStats() {
        try {
            const response = await fetch(`${this.baseUrl}/network/stats`);
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getNetworkStats failed:', e);
            throw e;
        }
    }

    // === Scale Configuration API ===

    async getScale() {
        try {
            const response = await fetch(`${this.baseUrl}/scale`);
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getScale failed:', e);
            throw e;
        }
    }

    async setScale(scale) {
        try {
            const response = await fetch(`${this.baseUrl}/scale`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ scale })
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to set scale');
            }
            return result;
        } catch (e) {
            console.error('[API] setScale failed:', e);
            throw e;
        }
    }

    async setScalePreset(preset) {
        try {
            const response = await fetch(`${this.baseUrl}/scale`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ preset })
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to set scale preset');
            }
            return result;
        } catch (e) {
            console.error('[API] setScalePreset failed:', e);
            throw e;
        }
    }

    // === Path Dispatcher API ===

    /**
     * Create a path between two blocks.
     * @param {string} startBlockId - Starting block ID
     * @param {string} endBlockId - Destination block ID
     * @param {object} options - Optional: { train_id, priority, dispatcher_id }
     * @returns {Promise<object>} PathResult with success flag and path or error
     */
    async createPath(startBlockId, endBlockId, options = {}) {
        try {
            const response = await fetch(`${this.baseUrl}/paths`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    start_block_id: startBlockId,
                    end_block_id: endBlockId,
                    ...options
                })
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] createPath failed:', e);
            throw e;
        }
    }

    /**
     * Get list of active paths.
     * @param {object} filters - Optional filters: { train_id, block_id, status }
     * @returns {Promise<object>} { paths: [], count }
     */
    async getPaths(filters = {}) {
        try {
            const params = new URLSearchParams();
            if (filters.train_id) params.append('train_id', filters.train_id);
            if (filters.block_id) params.append('block_id', filters.block_id);
            if (filters.status) params.append('status', filters.status);
            
            const url = params.toString() 
                ? `${this.baseUrl}/paths?${params}` 
                : `${this.baseUrl}/paths`;
            const response = await fetch(url);
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getPaths failed:', e);
            throw e;
        }
    }

    /**
     * Get details of a specific path.
     * @param {string} pathId - Path ID
     * @returns {Promise<object>} Path object or error
     */
    async getPath(pathId) {
        try {
            const response = await fetch(`${this.baseUrl}/paths/${encodeURIComponent(pathId)}`);
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] getPath failed:', e);
            throw e;
        }
    }

    /**
     * Cancel a path and release its resources.
     * @param {string} pathId - Path ID to cancel
     * @param {boolean} force - If true, force cancellation even with approach locking
     * @returns {Promise<object>} CancellationResult
     */
    async cancelPath(pathId, force = false) {
        try {
            const url = force 
                ? `${this.baseUrl}/paths/${encodeURIComponent(pathId)}?force=true`
                : `${this.baseUrl}/paths/${encodeURIComponent(pathId)}`;
            const response = await fetch(url, { method: 'DELETE' });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] cancelPath failed:', e);
            throw e;
        }
    }

    /**
     * Extend an existing path to a new destination.
     * @param {string} pathId - Path ID to extend
     * @param {string} newEndBlockId - New destination block ID
     * @returns {Promise<object>} ExtensionResult
     */
    async extendPath(pathId, newEndBlockId) {
        try {
            const response = await fetch(`${this.baseUrl}/paths/${encodeURIComponent(pathId)}/extend`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ new_end_block_id: newEndBlockId })
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] extendPath failed:', e);
            throw e;
        }
    }

    /**
     * Mark a path as completed and release all resources.
     * @param {string} pathId - Path ID to complete
     * @returns {Promise<object>} Success message or error
     */
    async completePath(pathId) {
        try {
            const response = await fetch(`${this.baseUrl}/paths/${encodeURIComponent(pathId)}/complete`, {
                method: 'POST'
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] completePath failed:', e);
            throw e;
        }
    }

    /**
     * Get all blocks reachable from a starting block.
     * @param {string} blockId - Starting block ID
     * @param {boolean} includeStations - Include station IDs (default: true)
     * @returns {Promise<object>} { start_block_id, reachable_endpoints: [], count }
     */
    async getReachableEndpoints(blockId, includeStations = true) {
        try {
            const url = `${this.baseUrl}/dispatcher/reachable/${encodeURIComponent(blockId)}?include_stations=${includeStations}`;
            const response = await fetch(url);
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to get reachable endpoints');
            }
            return result;
        } catch (e) {
            console.error('[API] getReachableEndpoints failed:', e);
            throw e;
        }
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
        try {
            const url = `${this.baseUrl}/paths/${encodeURIComponent(pathId)}/reachable-extensions?include_stations=${includeStations}`;
            const response = await fetch(url);
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || 'Failed to get reachable extensions');
            }
            return result;
        } catch (e) {
            console.error('[API] getPathReachableExtensions failed:', e);
            throw e;
        }
    }

    /**
     * Find all possible paths between two blocks.
     * @param {string} startBlockId - Starting block ID
     * @param {string} endBlockId - Destination block ID
     * @param {string|null} trainId - Optional train ID for validation
     * @returns {Promise<object>} { paths: [], count }
     */
    async findPaths(startBlockId, endBlockId, trainId = null) {
        try {
            const body = {
                start_block_id: startBlockId,
                end_block_id: endBlockId
            };
            if (trainId) body.train_id = trainId;
            
            const response = await fetch(`${this.baseUrl}/dispatcher/paths`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            const result = await response.json();
            return result;
        } catch (e) {
            console.error('[API] findPaths failed:', e);
            throw e;
        }
    }
}

// Export as global
window.api = new API();
