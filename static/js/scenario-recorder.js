/**
 * ScenarioRecorder - Records user API interactions for test scenario generation.
 *
 * Intercepts mutation API calls (POST/PUT/DELETE/PATCH) made through the API class,
 * records them with relative timestamps, and allows exporting as scenario JSON files
 * that can be replayed by the backend ScenarioRunner.
 *
 * Usage:
 *   const recorder = new ScenarioRecorder(window.api);
 *   await recorder.startRecording();
 *   // ... user interacts with the app ...
 *   await recorder.checkpoint();  // capture state snapshot
 *   recorder.stopRecording();
 *   recorder.saveScenario('my-test', 'Description of what this tests');
 */
class ScenarioRecorder {
    constructor(api) {
        this.api = api;
        this.recording = false;
        this.actions = [];
        this.layout = null;
        this.startTime = null;
        this.checkpointCount = 0;
        this._originalRequest = null;
    }

    /**
     * Start recording. Captures the current layout and begins intercepting API calls.
     */
    async startRecording() {
        try {
            this.layout = await this.api.getNetworkSteps();
        } catch (e) {
            console.error('[ScenarioRecorder] Failed to capture layout steps:', e);
            this.layout = [];
        }

        this.startTime = Date.now();
        this.actions = [];
        this.checkpointCount = 0;
        this.recording = true;
        this._wrapApi();
        console.log('[ScenarioRecorder] Recording started');
    }

    /**
     * Stop recording. Restores the original API request method.
     */
    stopRecording() {
        this.recording = false;
        this._unwrapApi();
        console.log(`[ScenarioRecorder] Recording stopped. ${this.actions.length} actions captured.`);
    }

    /**
     * Capture a checkpoint - fetches current network state as a snapshot.
     * User adds assertions to the checkpoint later by editing the JSON.
     */
    async checkpoint() {
        if (!this.recording) {
            console.warn('[ScenarioRecorder] Not recording, cannot create checkpoint');
            return;
        }
        const t = (Date.now() - this.startTime) / 1000;
        this.checkpointCount++;
        try {
            const snapshot = await this._fetchNetworkDirect();
            this.actions.push({
                t,
                action: 'checkpoint',
                label: `Checkpoint ${this.checkpointCount}`,
                snapshot,
                assertions: []
            });
            console.log(`[ScenarioRecorder] Checkpoint ${this.checkpointCount} at t=${t.toFixed(1)}s`);
        } catch (e) {
            console.error('[ScenarioRecorder] Checkpoint failed:', e);
        }
    }

    /**
     * Fetch network state directly (bypasses interception).
     */
    async _fetchNetworkDirect() {
        const resp = await fetch('/api/network');
        return await resp.json();
    }

    /**
     * Export the recorded scenario as a JSON object.
     * @param {string} name - Scenario name
     * @param {string} description - Scenario description
     * @returns {object} Scenario JSON object
     */
    exportScenario(name, description) {
        return {
            version: '1.0',
            format: 'scenario',
            name: name || 'Untitled Scenario',
            description: description || '',
            created: new Date().toISOString(),
            layout: this.layout || [],
            actions: this.actions
        };
    }

    /**
     * Save the scenario as a downloadable JSON file.
     * @param {string} name - Scenario name
     * @param {string} description - Scenario description
     */
    saveScenario(name, description) {
        const scenario = this.exportScenario(name, description);
        const json = JSON.stringify(scenario, null, 2);
        const blob = new Blob([json], { type: 'application/json' });
        const url = URL.createObjectURL(blob);

        const a = document.createElement('a');
        const filename = (name || 'scenario')
            .toLowerCase()
            .replace(/\s+/g, '-')
            .replace(/[^a-z0-9-]/g, '');
        a.href = url;
        a.download = `${filename}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);

        console.log(`[ScenarioRecorder] Scenario saved as ${filename}.json`);
    }

    /**
     * Wrap the API._request method to intercept mutation calls.
     * Only records POST, PUT, DELETE, PATCH - ignores GET (polling).
     */
    _wrapApi() {
        if (this._originalRequest) return;
        this._originalRequest = this.api._request.bind(this.api);

        const recorder = this;
        this.api._request = async function(endpoint, options = {}, caller = 'request') {
            const method = (options.method || 'GET').toUpperCase();

            // Call the original request
            const result = await recorder._originalRequest(endpoint, options, caller);

            // Record mutations only (not GET polling)
            if (recorder.recording && method !== 'GET') {
                const t = (Date.now() - recorder.startTime) / 1000;
                let body = options.body;
                // Parse body back from string if it was serialized
                if (typeof body === 'string') {
                    try { body = JSON.parse(body); } catch (e) { /* keep as string */ }
                }
                recorder.actions.push({
                    t,
                    method,
                    url: `/api${endpoint}`,
                    body: body ?? null,
                    caller
                });
            }

            return result;
        };
    }

    /**
     * Restore the original API._request method.
     */
    _unwrapApi() {
        if (this._originalRequest) {
            this.api._request = this._originalRequest;
            this._originalRequest = null;
        }
    }

    /**
     * Get recording status info for UI display.
     * @returns {object} Status with recording, duration, actionCount, checkpointCount
     */
    getStatus() {
        if (!this.recording) {
            return { recording: false, duration: 0, actionCount: 0, checkpointCount: 0 };
        }
        return {
            recording: true,
            duration: (Date.now() - this.startTime) / 1000,
            actionCount: this.actions.filter(a => !a.action).length,
            checkpointCount: this.checkpointCount
        };
    }

    /**
     * Whether there is a completed (stopped) recording available to save.
     * @returns {boolean}
     */
    get hasPendingRecording() {
        return !this.recording && this.actions.length > 0;
    }
}
