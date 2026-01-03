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
}

// Export as global
window.api = new API();
