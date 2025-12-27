class API {
    constructor(baseUrl = '/api') {
        this.baseUrl = baseUrl;
        console.log('[API] Initialized with baseUrl:', baseUrl);
    }

    async getNetwork() {
        console.log('[API] Fetching network...');
        try {
            const response = await fetch(`${this.baseUrl}/network`);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            const data = await response.json();
            console.log('[API] Network fetched successfully');
            return data;
        } catch (e) {
            console.error('[API] getNetwork failed:', e);
            throw e;
        }
    }

    async updateNetwork(networkData) {
        const response = await fetch(`${this.baseUrl}/network/update`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(networkData)
        });
        return await response.json();
    }

    // Individual track operations
    async addTrack(trackData) {
        console.log('[API] addTrack:', trackData);
        try {
            const response = await fetch(`${this.baseUrl}/track`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(trackData)
            });
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            const result = await response.json();
            console.log('[API] addTrack response:', result);
            return result;
        } catch (e) {
            console.error('[API] addTrack failed:', e);
            throw e;
        }
    }

    async getTrack(x, y) {
        console.log('[API] getTrack at', x, y);
        try {
            const response = await fetch(`${this.baseUrl}/track/${x}/${y}`);
            const result = await response.json();
            console.log('[API] getTrack response:', result);
            return result;
        } catch (e) {
            console.error('[API] getTrack failed:', e);
            throw e;
        }
    }

    async updateTrack(x, y, updates) {
        console.log('[API] updateTrack at', x, y, 'with', updates);
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
            console.log('[API] updateTrack response:', result);
            return result;
        } catch (e) {
            console.error('[API] updateTrack failed:', e);
            throw e;
        }
    }

    async deleteTrack(x, y) {
        console.log('[API] deleteTrack at', x, y);
        try {
            const response = await fetch(`${this.baseUrl}/track/${x}/${y}`, {
                method: 'DELETE'
            });
            const result = await response.json();
            console.log('[API] deleteTrack response:', result);
            return result;
        } catch (e) {
            console.error('[API] deleteTrack failed:', e);
            throw e;
        }
    }

    async addTracksBulk(tracks) {
        console.log('[API] addTracksBulk:', tracks.length, 'tracks');
        try {
            const response = await fetch(`${this.baseUrl}/tracks/bulk`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ tracks })
            });
            const result = await response.json();
            console.log('[API] addTracksBulk response:', result);
            return result;
        } catch (e) {
            console.error('[API] addTracksBulk failed:', e);
            throw e;
        }
    }

    async createTrain(trainData) {
        console.log('[API] createTrain:', trainData);
        try {
            const response = await fetch(`${this.baseUrl}/trains`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify(trainData)
            });
            const result = await response.json();
            console.log('[API] createTrain response:', result);
            return result;
        } catch (e) {
            console.error('[API] createTrain failed:', e);
            throw e;
        }
    }

    async controlSimulation(action, speed = null) {
        console.log('[API] controlSimulation:', action, speed);
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
            console.log('[API] controlSimulation response:', result);
            return result;
        } catch (e) {
            console.error('[API] controlSimulation failed:', e);
            throw e;
        }
    }

    async getSimulationState() {
        const response = await fetch(`${this.baseUrl}/simulation/state`);
        return await response.json();
    }
}

// Export as global
window.api = new API();
