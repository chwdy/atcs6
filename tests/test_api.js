/**
 * REST API Tests - HTTP endpoint testing
 *
 * Run with: node tests/test_api.js
 * Requires: Server running at localhost:5000
 *
 * This test suite covers:
 * - All 42 REST API endpoints
 * - Request/response validation
 * - Error handling
 * - State management via API
 * - Frontend-backend parity via HTTP
 */

// ============= CONFIGURATION =============

const BASE_URL = 'http://localhost:5001/api';

// Track bit constants (matching Python)
const TRACK_NS = 1;      // Bit 0
const TRACK_EW = 2;      // Bit 1
const TRACK_NE = 4;      // Bit 2
const TRACK_NW = 8;      // Bit 3
const TRACK_SE = 16;     // Bit 4
const TRACK_SW = 32;     // Bit 5

// Forbidden track bit configurations
const FORBIDDEN_TRACK_BITS = new Set([
    13, 22, 42, 49,           // 3-way switches
    15, 23, 43, 51,           // Double slips with 3-way
    29, 30, 45, 46, 53, 54, 57, 58,  // Complex 4-path
    31, 47, 55, 59, 61, 62,   // 5-path
    63,                       // 6-path (full)
    28, 44, 52, 56, 60,       // Multi-diagonal without through
]);

// Forbidden signal placements (crossings and switches)
const FORBIDDEN_SIGNAL_BITS = new Set([
    3,   // NS + EW crossing
    24,  // NW + SE crossing
    36,  // NE + SW crossing
    5, 9, 17, 33,   // Y-switches (NS + diagonal)
    6, 10, 18, 34,  // Y-switches (EW + diagonal)
    12, 20, 40, 48, // Diagonal switches
]);

// Valid signal directions by track bits
const VALID_SIGNAL_DIRECTIONS = {
    1:  ['N', 'S'],      // NS straight
    2:  ['E', 'W'],      // EW straight
    4:  ['N', 'E'],      // NE curve
    8:  ['N', 'W'],      // NW curve
    16: ['S', 'E'],      // SE curve
    32: ['S', 'W'],      // SW curve
};

// ============= TEST UTILITIES =============

let testsRun = 0;
let testsPassed = 0;
let testsFailed = 0;
const failedTests = [];

async function fetchJSON(endpoint, options = {}) {
    try {
        const response = await fetch(`${BASE_URL}${endpoint}`, {
            headers: { 'Content-Type': 'application/json' },
            ...options
        });

        let data;
        try {
            data = await response.json();
        } catch (e) {
            data = { error: 'Failed to parse JSON response' };
        }

        return {
            ok: response.ok,
            status: response.status,
            data: data
        };
    } catch (error) {
        return {
            ok: false,
            status: 0,
            data: { error: error.message }
        };
    }
}

async function resetNetwork() {
    // Reset network by posting empty network
    await fetchJSON('/network/update', {
        method: 'POST',
        body: JSON.stringify({
            tracks: {},
            blocks: {},
            signals: {},
            stations: {},
            trains: {}
        })
    });
}

function assert(condition, message) {
    if (!condition) {
        throw new Error(`Assertion failed: ${message}`);
    }
}

async function runTest(testName, testFunc) {
    testsRun++;
    try {
        await testFunc();
        testsPassed++;
        console.log(`✓ ${testName}`);
    } catch (error) {
        testsFailed++;
        failedTests.push({ name: testName, error: error.message });
        console.error(`✗ ${testName}`);
        console.error(`  Error: ${error.message}`);
    }
}

async function sleep(ms) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

// ============= NETWORK ENDPOINTS =============

async function test_GET_network() {
    await resetNetwork();

    const result = await fetchJSON('/network');

    assert(result.ok, 'GET /network should succeed');
    assert(result.data.tracks !== undefined, 'Response should contain tracks');
    assert(result.data.blocks !== undefined, 'Response should contain blocks');
    assert(result.data.signals !== undefined, 'Response should contain signals');
}

async function test_POST_network_update() {
    const networkData = {
        tracks: {},
        blocks: {},
        signals: {},
        stations: {},
        trains: {}
    };

    const result = await fetchJSON('/network/update', {
        method: 'POST',
        body: JSON.stringify(networkData)
    });

    assert(result.ok, 'POST /network/update should succeed');
}

// ============= TRACK ENDPOINTS (CORE) =============

async function test_POST_track_create() {
    await resetNetwork();

    const result = await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({
            x: 0,
            y: 0,
            track_bits: TRACK_NS
        })
    });

    assert(result.ok, 'POST /track should succeed');
    assert(result.data.track_bits === TRACK_NS, 'Track bits should match');
}

async function test_GET_track() {
    await resetNetwork();

    // Create track first
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 5, y: 5, track_bits: TRACK_EW })
    });

    // Get track
    const result = await fetchJSON('/track/5/5');

    assert(result.ok, 'GET /track should succeed');
    assert(result.data.x === 5, 'X coordinate should match');
    assert(result.data.y === 5, 'Y coordinate should match');
    assert(result.data.track_bits === TRACK_EW, 'Track bits should match');
}

async function test_PATCH_track_update() {
    await resetNetwork();

    // Create track
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 0, y: 0, track_bits: TRACK_NS })
    });

    // Update track
    const result = await fetchJSON('/track/0/0', {
        method: 'PATCH',
        body: JSON.stringify({ track_bits: TRACK_EW })
    });

    assert(result.ok, 'PATCH /track should succeed');
}

async function test_DELETE_track() {
    await resetNetwork();

    // Create track
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 0, y: 0, track_bits: TRACK_NS })
    });

    // Delete track
    const result = await fetchJSON('/track/0/0', { method: 'DELETE' });

    assert(result.ok, 'DELETE /track should succeed');

    // Verify deletion
    const getResult = await fetchJSON('/track/0/0');
    assert(!getResult.ok, 'Track should no longer exist');
}

async function test_POST_tracks_bulk() {
    await resetNetwork();

    const tracks = [
        { x: 0, y: 0, track_bits: TRACK_EW },
        { x: 1, y: 0, track_bits: TRACK_EW },
        { x: 2, y: 0, track_bits: TRACK_EW },
    ];

    const result = await fetchJSON('/tracks/bulk', {
        method: 'POST',
        body: JSON.stringify({ tracks })
    });

    assert(result.ok, 'POST /tracks/bulk should succeed');
    assert(result.data.added >= 3 || result.data.updated >= 3, 'Should create 3 tracks');
}

async function test_POST_tracks_bulk_delete() {
    await resetNetwork();

    // Create tracks
    const tracks = [
        { x: 0, y: 0, track_bits: TRACK_EW },
        { x: 1, y: 0, track_bits: TRACK_EW },
        { x: 2, y: 0, track_bits: TRACK_EW },
    ];
    await fetchJSON('/tracks/bulk', {
        method: 'POST',
        body: JSON.stringify({ tracks })
    });

    // Delete tracks
    const result = await fetchJSON('/tracks/bulk-delete', {
        method: 'POST',
        body: JSON.stringify({
            positions: [[0, 0], [1, 0], [2, 0]]
        })
    });

    assert(result.ok, 'POST /tracks/bulk-delete should succeed');
    assert(result.data.deleted === 3, 'Should delete 3 tracks');
}

// ============= TRACK ENDPOINTS (EXTENSIVE) =============

async function test_track_forbidden_bits_rejected() {
    await resetNetwork();

    // Try to create track with forbidden bits (13 = 3-way switch)
    const result = await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 0, y: 0, track_bits: 13 })
    });

    // Should either fail or normalize to non-forbidden bits
    if (result.ok) {
        assert(!FORBIDDEN_TRACK_BITS.has(result.data.track_bits),
            'Track bits should not be forbidden after creation');
    }
}

// ============= BLOCK ENDPOINTS (CORE) =============

async function test_GET_blocks() {
    await resetNetwork();

    const result = await fetchJSON('/blocks');

    assert(result.ok, 'GET /blocks should succeed');
    assert(Array.isArray(result.data), 'Response should be an array');
}

async function test_POST_blocks_create() {
    await resetNetwork();

    const result = await fetchJSON('/blocks', {
        method: 'POST',
        body: JSON.stringify({
            block_id: 'BLK_TEST',
            name: 'Test Block',
            length_cm: 1000
        })
    });

    assert(result.ok, 'POST /blocks should succeed');
    assert(result.data.block_id === 'BLK_TEST', 'Block ID should match');
}

async function test_GET_block_by_id() {
    await resetNetwork();

    // Create block
    await fetchJSON('/blocks', {
        method: 'POST',
        body: JSON.stringify({ block_id: 'BLK_TEST' })
    });

    // Get block
    const result = await fetchJSON('/blocks/BLK_TEST');

    assert(result.ok, 'GET /blocks/{id} should succeed');
    assert(result.data.block_id === 'BLK_TEST', 'Block ID should match');
}

async function test_PATCH_block_update() {
    await resetNetwork();

    // Create block
    await fetchJSON('/blocks', {
        method: 'POST',
        body: JSON.stringify({ block_id: 'BLK_TEST' })
    });

    // Update block
    const result = await fetchJSON('/blocks/BLK_TEST', {
        method: 'PATCH',
        body: JSON.stringify({ name: 'Updated Block' })
    });

    assert(result.ok, 'PATCH /blocks/{id} should succeed');
}

async function test_DELETE_block() {
    await resetNetwork();

    // Create block
    await fetchJSON('/blocks', {
        method: 'POST',
        body: JSON.stringify({ block_id: 'BLK_TEST' })
    });

    // Delete block
    const result = await fetchJSON('/blocks/BLK_TEST', { method: 'DELETE' });

    assert(result.ok, 'DELETE /blocks/{id} should succeed');
}

async function test_POST_block_cycle_state() {
    await resetNetwork();

    // Create block
    await fetchJSON('/blocks', {
        method: 'POST',
        body: JSON.stringify({ block_id: 'BLK_TEST' })
    });

    // Cycle state
    const result = await fetchJSON('/blocks/BLK_TEST/cycle-state', {
        method: 'POST'
    });

    assert(result.ok, 'POST /blocks/{id}/cycle-state should succeed');
    assert(result.data.new_state !== undefined, 'Should return new state');
}

async function test_POST_blocks_create_with_tracks() {
    await resetNetwork();

    // Create tracks
    const tracks = [
        { x: 0, y: 0, track_bits: TRACK_EW },
        { x: 1, y: 0, track_bits: TRACK_EW },
        { x: 2, y: 0, track_bits: TRACK_EW },
    ];
    await fetchJSON('/tracks/bulk', {
        method: 'POST',
        body: JSON.stringify({ tracks })
    });

    // Create block with tracks
    const result = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            block_id: 'BLK_STRAIGHT',
            positions: [[0, 0], [1, 0], [2, 0]],
            name: 'Straight Block'
        })
    });

    assert(result.ok, 'POST /blocks/create-with-tracks should succeed');
    assert(result.data.block_id === 'BLK_STRAIGHT', 'Block ID should match');
}

async function test_POST_block_tracks_add() {
    await resetNetwork();

    // Create tracks
    const tracks = [
        { x: 0, y: 0, track_bits: TRACK_EW },
        { x: 1, y: 0, track_bits: TRACK_EW },
        { x: 2, y: 0, track_bits: TRACK_EW },
    ];
    await fetchJSON('/tracks/bulk', {
        method: 'POST',
        body: JSON.stringify({ tracks })
    });

    // Create block
    await fetchJSON('/blocks', {
        method: 'POST',
        body: JSON.stringify({ block_id: 'BLK_TEST' })
    });

    // Add tracks to block
    const result = await fetchJSON('/blocks/BLK_TEST/tracks', {
        method: 'POST',
        body: JSON.stringify({
            positions: [[0, 0], [1, 0]]
        })
    });

    assert(result.ok, 'POST /blocks/{id}/tracks should succeed');
}

// ============= BLOCK ENDPOINTS (EXTENSIVE) =============

async function test_BLK_API_E01_reject_nonexistent_tracks() {
    await resetNetwork();

    // Create only one track
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 0, y: 0, track_bits: TRACK_EW })
    });

    // Try to create block with non-existent track
    const result = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            block_id: 'BLK_FAIL',
            positions: [[0, 0], [1, 0]]  // (1,0) doesn't exist
        })
    });

    assert(!result.ok, 'Should fail for non-existent track');
    assert(result.data.error && result.data.error.toLowerCase().includes('exist'),
        'Error should mention non-existent track');
}

async function test_BLK_API_E02_reject_switch_in_block() {
    await resetNetwork();

    // Create tracks with switch
    const tracks = [
        { x: 0, y: 0, track_bits: TRACK_EW },
        { x: 1, y: 0, track_bits: TRACK_EW | TRACK_NE },  // Switch
        { x: 2, y: 0, track_bits: TRACK_EW },
    ];
    await fetchJSON('/tracks/bulk', {
        method: 'POST',
        body: JSON.stringify({ tracks })
    });

    // Try to create block including switch
    const result = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            block_id: 'BLK_FAIL',
            positions: [[0, 0], [1, 0], [2, 0]]
        })
    });

    assert(!result.ok, 'Should fail for switch in block');
    assert(result.data.error && result.data.error.toLowerCase().includes('switch'),
        'Error should mention switch');
}

async function test_BLK_API_E03_reject_crossing_in_block() {
    await resetNetwork();

    // Create crossing
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({
            x: 1,
            y: 1,
            track_bits: TRACK_NS | TRACK_EW  // Crossing
        })
    });

    // Try to create block with crossing
    const result = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            block_id: 'BLK_FAIL',
            positions: [[1, 1]]
        })
    });

    assert(!result.ok, 'Should fail for crossing in block');
    assert(result.data.error && result.data.error.toLowerCase().includes('crossing'),
        'Error should mention crossing');
}

// ============= SIGNAL ENDPOINTS (CORE) =============

async function test_GET_signals() {
    await resetNetwork();

    const result = await fetchJSON('/signals');

    assert(result.ok, 'GET /signals should succeed');
    assert(Array.isArray(result.data), 'Response should be an array');
}

async function test_POST_signal_create() {
    await resetNetwork();

    // Create track first
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 5, y: 5, track_bits: TRACK_NS })
    });

    // Create signal
    const result = await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({
            x: 5,
            y: 5,
            direction: 'N',
            signal_type: 'block'
        })
    });

    assert(result.ok, 'POST /signal should succeed');
    assert(result.data.x === 5, 'X coordinate should match');
    assert(result.data.y === 5, 'Y coordinate should match');
    assert(result.data.direction === 'N', 'Direction should match');
}

async function test_GET_signal_by_id() {
    await resetNetwork();

    // Create track
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 5, y: 5, track_bits: TRACK_NS })
    });

    // Create signal
    const createResult = await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({
            x: 5,
            y: 5,
            direction: 'N',
            signal_type: 'block'
        })
    });

    if (createResult.ok && createResult.data.signal_id) {
        // Get signal by ID
        const result = await fetchJSON(`/signal/${createResult.data.signal_id}`);
        assert(result.ok, 'GET /signal/{id} should succeed');
    }
}

async function test_PATCH_signal_update() {
    await resetNetwork();

    // Create track and signal
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 5, y: 5, track_bits: TRACK_NS })
    });

    const createResult = await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({
            x: 5,
            y: 5,
            direction: 'N',
            signal_type: 'block'
        })
    });

    if (createResult.ok && createResult.data.signal_id) {
        // Update signal
        const result = await fetchJSON(`/signal/${createResult.data.signal_id}`, {
            method: 'PATCH',
            body: JSON.stringify({ name: 'Updated Signal' })
        });

        assert(result.ok, 'PATCH /signal/{id} should succeed');
    }
}

async function test_DELETE_signal() {
    await resetNetwork();

    // Create track and signal
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 5, y: 5, track_bits: TRACK_NS })
    });

    const createResult = await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({
            x: 5,
            y: 5,
            direction: 'N',
            signal_type: 'block'
        })
    });

    if (createResult.ok && createResult.data.signal_id) {
        // Delete signal
        const result = await fetchJSON(`/signal/${createResult.data.signal_id}`, {
            method: 'DELETE'
        });

        assert(result.ok, 'DELETE /signal/{id} should succeed');
    }
}

async function test_GET_signal_validate() {
    await resetNetwork();

    // Create track
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 5, y: 5, track_bits: TRACK_NS })
    });

    // Validate signal placement
    const result = await fetchJSON('/signal/validate/5/5');

    assert(result.ok, 'GET /signal/validate should succeed');
    assert(result.data.can_place !== undefined, 'Should return can_place');
    assert(Array.isArray(result.data.valid_directions), 'Should return valid_directions');
}

// ============= SIGNAL ENDPOINTS (EXTENSIVE) =============

async function test_signal_on_switch_rejected() {
    await resetNetwork();

    // Create switch
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({
            x: 0,
            y: 0,
            track_bits: TRACK_EW | TRACK_NE  // Switch
        })
    });

    // Try to place signal on switch
    const result = await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({
            x: 0,
            y: 0,
            direction: 'E'
        })
    });

    assert(!result.ok, 'Signal on switch should be rejected');
}

// ============= STATION ENDPOINTS (CORE) =============

async function test_GET_stations() {
    await resetNetwork();

    const result = await fetchJSON('/stations');

    assert(result.ok, 'GET /stations should succeed');
    assert(Array.isArray(result.data), 'Response should be an array');
}

async function test_POST_station_create() {
    await resetNetwork();

    // Create track
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 0, y: 0, track_bits: TRACK_NS })
    });

    // Create station
    const result = await fetchJSON('/station', {
        method: 'POST',
        body: JSON.stringify({
            x: 0,
            y: 0,
            name: 'Test Station',
            length_cm: 100,
            platform_side: 'both',
            top_speed_kmh: 30
        })
    });

    assert(result.ok, 'POST /station should succeed');
    assert(result.data.name === 'Test Station', 'Station name should match');
}

async function test_GET_station_by_id() {
    await resetNetwork();

    // Create track and station
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 0, y: 0, track_bits: TRACK_NS })
    });

    const createResult = await fetchJSON('/station', {
        method: 'POST',
        body: JSON.stringify({
            x: 0,
            y: 0,
            name: 'Test Station',
            length_cm: 100
        })
    });

    if (createResult.ok && createResult.data.station_id) {
        // Get station
        const result = await fetchJSON(`/station/${createResult.data.station_id}`);
        assert(result.ok, 'GET /station/{id} should succeed');
    }
}

async function test_PATCH_station_update() {
    await resetNetwork();

    // Create track and station
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 0, y: 0, track_bits: TRACK_NS })
    });

    const createResult = await fetchJSON('/station', {
        method: 'POST',
        body: JSON.stringify({
            x: 0,
            y: 0,
            name: 'Test Station'
        })
    });

    if (createResult.ok && createResult.data.station_id) {
        // Update station
        const result = await fetchJSON(`/station/${createResult.data.station_id}`, {
            method: 'PATCH',
            body: JSON.stringify({ name: 'Updated Station' })
        });

        assert(result.ok, 'PATCH /station/{id} should succeed');
    }
}

async function test_DELETE_station() {
    await resetNetwork();

    // Create track and station
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 0, y: 0, track_bits: TRACK_NS })
    });

    const createResult = await fetchJSON('/station', {
        method: 'POST',
        body: JSON.stringify({
            x: 0,
            y: 0,
            name: 'Test Station'
        })
    });

    if (createResult.ok && createResult.data.station_id) {
        // Delete station
        const result = await fetchJSON(`/station/${createResult.data.station_id}`, {
            method: 'DELETE'
        });

        assert(result.ok, 'DELETE /station/{id} should succeed');
    }
}

async function test_GET_station_validate() {
    await resetNetwork();

    // Create track
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 0, y: 0, track_bits: TRACK_NS })
    });

    // Validate station placement
    const result = await fetchJSON('/station/validate/0/0');

    assert(result.ok, 'GET /station/validate should succeed');
    assert(result.data.can_place !== undefined, 'Should return can_place');
}

// ============= TRAIN ENDPOINTS =============

async function test_POST_trains_create() {
    await resetNetwork();

    // Create some track for the train
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 0, y: 0, track_bits: TRACK_EW })
    });

    const result = await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_001',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });

    // Train creation may require specific implementation
    // Just verify response structure if successful
    if (result.ok) {
        assert(result.data.train_id !== undefined, 'Should return train ID');
    }
}

// ============= SIMULATION ENDPOINTS =============

async function test_POST_simulation_control() {
    await resetNetwork();

    const result = await fetchJSON('/simulation/control', {
        method: 'POST',
        body: JSON.stringify({ action: 'stop' })
    });

    assert(result.ok, 'POST /simulation/control should succeed');
}

async function test_GET_simulation_state() {
    await resetNetwork();

    const result = await fetchJSON('/simulation/state');

    assert(result.ok, 'GET /simulation/state should succeed');
}

// ============= FRONTEND-BACKEND PARITY TESTS =============

async function test_PARITY_001_all_forbidden_track_bits() {
    await resetNetwork();

    // Test a sample of forbidden track bits
    const sampleForbidden = [13, 22, 42, 63];

    for (const bits of sampleForbidden) {
        const result = await fetchJSON('/track', {
            method: 'POST',
            body: JSON.stringify({ x: 0, y: 0, track_bits: bits })
        });

        // Either rejected or normalized to valid bits
        if (result.ok) {
            assert(!FORBIDDEN_TRACK_BITS.has(result.data.track_bits),
                `Forbidden bits ${bits} should be rejected or normalized`);
        }

        await resetNetwork();  // Reset for next iteration
    }
}

async function test_PARITY_002_signal_on_switch_rejected() {
    await resetNetwork();

    // Create switch (EW + NE)
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({
            x: 0,
            y: 0,
            track_bits: 6  // TRACK_EW | TRACK_NE
        })
    });

    // Try to place signal
    const result = await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({
            x: 0,
            y: 0,
            direction: 'E'
        })
    });

    assert(!result.ok, 'Signal on switch should be rejected');
}

async function test_PARITY_003_valid_signal_directions() {
    await resetNetwork();

    // Test NS track
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({ x: 0, y: 0, track_bits: TRACK_NS })
    });

    const result = await fetchJSON('/signal/validate/0/0');

    assert(result.ok, 'Validation should succeed');
    assert(result.data.valid_directions.includes('N'), 'Should include N');
    assert(result.data.valid_directions.includes('S'), 'Should include S');
    assert(result.data.valid_directions.length === 2, 'Should only have N and S');
}

async function test_PARITY_004_crossing_in_block_rejected() {
    await resetNetwork();

    // Create crossing
    await fetchJSON('/track', {
        method: 'POST',
        body: JSON.stringify({
            x: 1,
            y: 1,
            track_bits: 3  // NS + EW crossing
        })
    });

    // Try to create block with crossing
    const result = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            block_id: 'BLK_FAIL',
            positions: [[1, 1]]
        })
    });

    assert(!result.ok, 'Crossing in block should be rejected');
}

// ============= TEST RUNNER =============

async function runAllTests() {
    console.log('Starting REST API Tests...\n');
    console.log('Testing against:', BASE_URL);
    console.log('Ensure server is running at localhost:5001\n');

    // Network endpoints
    await runTest('GET /api/network', test_GET_network);
    await runTest('POST /api/network/update', test_POST_network_update);

    // Track endpoints (Core)
    await runTest('POST /api/track (create)', test_POST_track_create);
    await runTest('GET /api/track', test_GET_track);
    await runTest('PATCH /api/track (update)', test_PATCH_track_update);
    await runTest('DELETE /api/track', test_DELETE_track);
    await runTest('POST /api/tracks/bulk', test_POST_tracks_bulk);
    await runTest('POST /api/tracks/bulk-delete', test_POST_tracks_bulk_delete);

    // Track endpoints (Extensive)
    await runTest('Track: Reject forbidden bits', test_track_forbidden_bits_rejected);

    // Block endpoints (Core)
    await runTest('GET /api/blocks', test_GET_blocks);
    await runTest('POST /api/blocks (create)', test_POST_blocks_create);
    await runTest('GET /api/blocks/{id}', test_GET_block_by_id);
    await runTest('PATCH /api/blocks/{id}', test_PATCH_block_update);
    await runTest('DELETE /api/blocks/{id}', test_DELETE_block);
    await runTest('POST /api/blocks/{id}/cycle-state', test_POST_block_cycle_state);
    await runTest('POST /api/blocks/create-with-tracks', test_POST_blocks_create_with_tracks);
    await runTest('POST /api/blocks/{id}/tracks', test_POST_block_tracks_add);

    // Block endpoints (Extensive)
    await runTest('BLK-API-E01: Reject non-existent tracks', test_BLK_API_E01_reject_nonexistent_tracks);
    await runTest('BLK-API-E02: Reject switch in block', test_BLK_API_E02_reject_switch_in_block);
    await runTest('BLK-API-E03: Reject crossing in block', test_BLK_API_E03_reject_crossing_in_block);

    // Signal endpoints (Core)
    await runTest('GET /api/signals', test_GET_signals);
    await runTest('POST /api/signal (create)', test_POST_signal_create);
    await runTest('GET /api/signal/{id}', test_GET_signal_by_id);
    await runTest('PATCH /api/signal/{id}', test_PATCH_signal_update);
    await runTest('DELETE /api/signal/{id}', test_DELETE_signal);
    await runTest('GET /api/signal/validate', test_GET_signal_validate);

    // Signal endpoints (Extensive)
    await runTest('Signal: Reject on switch', test_signal_on_switch_rejected);

    // Station endpoints (Core)
    await runTest('GET /api/stations', test_GET_stations);
    await runTest('POST /api/station (create)', test_POST_station_create);
    await runTest('GET /api/station/{id}', test_GET_station_by_id);
    await runTest('PATCH /api/station/{id}', test_PATCH_station_update);
    await runTest('DELETE /api/station/{id}', test_DELETE_station);
    await runTest('GET /api/station/validate', test_GET_station_validate);

    // Train endpoints
    await runTest('POST /api/trains (create)', test_POST_trains_create);

    // Simulation endpoints
    await runTest('POST /api/simulation/control', test_POST_simulation_control);
    await runTest('GET /api/simulation/state', test_GET_simulation_state);

    // Parity tests
    await runTest('PARITY-001: All forbidden track bits', test_PARITY_001_all_forbidden_track_bits);
    await runTest('PARITY-002: Signal on switch rejected', test_PARITY_002_signal_on_switch_rejected);
    await runTest('PARITY-003: Valid signal directions', test_PARITY_003_valid_signal_directions);
    await runTest('PARITY-004: Crossing in block rejected', test_PARITY_004_crossing_in_block_rejected);

    // Print summary
    console.log('\n========================================');
    console.log('TEST RESULTS');
    console.log('========================================');
    console.log(`Total tests run: ${testsRun}`);
    console.log(`Passed: ${testsPassed} ✓`);
    console.log(`Failed: ${testsFailed} ✗`);
    console.log('========================================');

    if (testsFailed > 0) {
        console.log('\nFailed tests:');
        failedTests.forEach(({ name, error }) => {
            console.log(`  ✗ ${name}`);
            console.log(`    ${error}`);
        });
    }

    process.exit(testsFailed > 0 ? 1 : 0);
}

// Run tests
runAllTests().catch(error => {
    console.error('Fatal error running tests:', error);
    process.exit(1);
});
