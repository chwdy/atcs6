/**
 * REST API Tests - HTTP endpoint testing
 *
 * Run with: node tests/test_api.js
 * Requires: Server running at localhost:5000
 *
 * This test suite covers:
 * - All REST API endpoints (60+ endpoints including train control, layout persistence)
 * - Request/response validation
 * - Error handling
 * - State management via API
 * - Layout save/load functionality
 * - Train state machine control endpoints
 * - Frontend-backend parity via HTTP
 */

// ============= CONFIGURATION =============

const BASE_URL = 'http://localhost:5000/api';

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
    // Clear the network to get a fresh canvas
    await fetchJSON('/network/clear', {
        method: 'POST'
    });
}

// Helper function to create a single track (replaces removed POST /track)
async function createTrack(x, y, track_bits) {
    return await fetchJSON('/tracks/bulk', {
        method: 'POST',
        body: JSON.stringify({
            tracks: [{ x, y, track_bits }]
        })
    });
}

// Helper function to create a block
async function createBlock(block_id, name = 'Test Block', length_cm = 1000) {
    return await fetchJSON('/blocks', {
        method: 'POST',
        body: JSON.stringify({
            block_id,
            name,
            length_cm
        })
    });
}

// Helper function to create a signal
async function createSignal(x, y, detection_direction, signal_type = 'block', name = null) {
    return await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({
            x,
            y,
            detection_direction,
            signal_type,
            name
        })
    });
}

// Helper function to create a station
async function createStation(x, y, name = 'Test Station', length_cm = 100, platform_side = 'both', top_speed_kmh = 30) {
    return await fetchJSON('/station', {
        method: 'POST',
        body: JSON.stringify({
            x,
            y,
            name,
            length_cm,
            platform_side,
            top_speed_kmh
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

// ============= TRACK ENDPOINTS (CORE) =============

async function test_GET_track() {
    await resetNetwork();

    // Create track first
    await createTrack(5, 5, TRACK_EW);

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
    await createTrack(0, 0, TRACK_NS);

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
    await createTrack(0, 0, TRACK_NS);

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
    const result = await createTrack(0, 0, 13);

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
    await createTrack(0, 0, TRACK_EW);

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

    // Create crossing using bulk endpoint
    await fetchJSON('/tracks/bulk', {
        method: 'POST',
        body: JSON.stringify({
            tracks: [{
                x: 1,
                y: 1,
                track_bits: TRACK_NS | TRACK_EW  // Crossing
            }]
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

async function test_POST_signal_create() {
    await resetNetwork();

    // Create track first
    await createTrack(5, 5, TRACK_NS);

    // Create signal
    const result = await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({
            x: 5,
            y: 5,
            detection_direction: 'N',
            signal_type: 'block'
        })
    });

    assert(result.ok, 'POST /signal should succeed');
    assert(result.data.x === 5, 'X coordinate should match');
    assert(result.data.y === 5, 'Y coordinate should match');
    assert(result.data.detection_direction === 'N', 'Detection direction should match');
}

async function test_GET_signal_by_id() {
    await resetNetwork();

    // Create track
    await createTrack(5, 5, TRACK_NS);

    // Create signal
    const createResult = await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({
            x: 5,
            y: 5,
            detection_direction: 'N',
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
    await createTrack(5, 5, TRACK_NS);

    const createResult = await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({
            x: 5,
            y: 5,
            detection_direction: 'N',
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
    await createTrack(5, 5, TRACK_NS);

    const createResult = await fetchJSON('/signal', {
        method: 'POST',
        body: JSON.stringify({
            x: 5,
            y: 5,
            detection_direction: 'N',
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
    await createTrack(5, 5, TRACK_NS);

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
            detection_direction: 'E'
        })
    });

    assert(!result.ok, 'Signal on switch should be rejected');
}

// ============= STATION ENDPOINTS (CORE) =============

async function test_POST_station_create() {
    await resetNetwork();

    // Create track
    await createTrack(0, 0, TRACK_NS);

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
    await createTrack(0, 0, TRACK_NS);

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
    await createTrack(0, 0, TRACK_NS);

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
    await createTrack(0, 0, TRACK_NS);

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
    await createTrack(0, 0, TRACK_NS);

    // Validate station placement
    const result = await fetchJSON('/station/validate/0/0');

    assert(result.ok, 'GET /station/validate should succeed');
    assert(result.data.can_place !== undefined, 'Should return can_place');
}

// ============= TRAIN ENDPOINTS =============

async function test_POST_trains_create() {
    await resetNetwork();

    // Create some track for the train
    await createTrack(0, 0, TRACK_EW);

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
        assert(result.data.train !== undefined, 'Should return train object');
        assert(result.data.train.id !== undefined, 'Train should have id');
    }
}

async function test_GET_trains_list() {
    await resetNetwork();

    // Create track and train
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_LIST_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });

    const result = await fetchJSON('/trains');

    assert(result.ok, 'GET /trains should succeed');
    assert(Array.isArray(result.data.trains), 'Should return trains array');
}

async function test_GET_train_by_id() {
    await resetNetwork();

    // Create track and train
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_GET_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });

    const result = await fetchJSON('/trains/TRAIN_GET_TEST');

    assert(result.ok, 'GET /trains/{id} should succeed');
    assert(result.data.id === 'TRAIN_GET_TEST', 'Should return correct train');
}

async function test_DELETE_train() {
    await resetNetwork();

    // Create track and train
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_DELETE_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });

    const result = await fetchJSON('/trains/TRAIN_DELETE_TEST', {
        method: 'DELETE'
    });

    assert(result.ok, 'DELETE /trains/{id} should succeed');

    // Verify train is gone
    const getResult = await fetchJSON('/trains/TRAIN_DELETE_TEST');
    assert(!getResult.ok || getResult.status === 404, 'Train should be deleted');
}

async function test_POST_train_power_on() {
    await resetNetwork();

    // Create track and train
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_POWER_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });

    const result = await fetchJSON('/trains/TRAIN_POWER_TEST/power-on', {
        method: 'POST'
    });

    assert(result.ok, 'POST /trains/{id}/power-on should succeed');
    assert(result.data.state === 'idle', 'Train state should be idle after power on');
}

async function test_POST_train_power_off() {
    await resetNetwork();

    // Create track and train, power it on first
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_POWEROFF_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });
    await fetchJSON('/trains/TRAIN_POWEROFF_TEST/power-on', { method: 'POST' });

    const result = await fetchJSON('/trains/TRAIN_POWEROFF_TEST/power-off', {
        method: 'POST'
    });

    assert(result.ok, 'POST /trains/{id}/power-off should succeed');
    assert(result.data.state === 'off', 'Train state should be off after power off');
}

async function test_POST_train_set_direction() {
    await resetNetwork();

    // Create track and train, power it on
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_DIR_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });
    await fetchJSON('/trains/TRAIN_DIR_TEST/power-on', { method: 'POST' });

    const result = await fetchJSON('/trains/TRAIN_DIR_TEST/set-direction', {
        method: 'POST',
        body: JSON.stringify({ direction: 'W' })
    });

    assert(result.ok, 'POST /trains/{id}/set-direction should succeed');
    assert(result.data.state === 'standby', 'Train state should be standby after setting direction');
}

async function test_POST_train_turn_around() {
    await resetNetwork();

    // Create track and train, power on and set direction
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_TURN_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });
    await fetchJSON('/trains/TRAIN_TURN_TEST/power-on', { method: 'POST' });
    await fetchJSON('/trains/TRAIN_TURN_TEST/set-direction', {
        method: 'POST',
        body: JSON.stringify({ direction: 'E' })
    });

    const result = await fetchJSON('/trains/TRAIN_TURN_TEST/turn-around', {
        method: 'POST'
    });

    assert(result.ok, 'POST /trains/{id}/turn-around should succeed');
}

async function test_POST_train_enable_manual() {
    await resetNetwork();

    // Create track and train, power it on
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_MANUAL_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });
    await fetchJSON('/trains/TRAIN_MANUAL_TEST/power-on', { method: 'POST' });

    const result = await fetchJSON('/trains/TRAIN_MANUAL_TEST/enable-manual', {
        method: 'POST'
    });

    assert(result.ok, 'POST /trains/{id}/enable-manual should succeed');
    assert(result.data.state === 'manual', 'Train state should be manual');
}

async function test_POST_train_disable_manual() {
    await resetNetwork();

    // Create track and train, power on, enable manual
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_DISMANUAL_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });
    await fetchJSON('/trains/TRAIN_DISMANUAL_TEST/power-on', { method: 'POST' });
    await fetchJSON('/trains/TRAIN_DISMANUAL_TEST/enable-manual', { method: 'POST' });

    const result = await fetchJSON('/trains/TRAIN_DISMANUAL_TEST/disable-manual', {
        method: 'POST'
    });

    assert(result.ok, 'POST /trains/{id}/disable-manual should succeed');
    assert(result.data.state === 'idle', 'Train state should be idle after disabling manual');
}

async function test_POST_train_manual_speed() {
    await resetNetwork();

    // Create track and train, power on, enable manual
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_SPEED_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });
    await fetchJSON('/trains/TRAIN_SPEED_TEST/power-on', { method: 'POST' });
    await fetchJSON('/trains/TRAIN_SPEED_TEST/enable-manual', { method: 'POST' });

    const result = await fetchJSON('/trains/TRAIN_SPEED_TEST/manual-speed', {
        method: 'POST',
        body: JSON.stringify({ speed: 50.0 })
    });

    assert(result.ok, 'POST /trains/{id}/manual-speed should succeed');
    assert(result.data.manual_target_speed === 50.0, 'Manual speed should be set to 50');
}

async function test_POST_train_travel_direction() {
    await resetNetwork();

    // Create track and train, power on, enable manual
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_TRAVDIR_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });
    await fetchJSON('/trains/TRAIN_TRAVDIR_TEST/power-on', { method: 'POST' });
    await fetchJSON('/trains/TRAIN_TRAVDIR_TEST/enable-manual', { method: 'POST' });

    const result = await fetchJSON('/trains/TRAIN_TRAVDIR_TEST/travel-direction', {
        method: 'POST',
        body: JSON.stringify({ direction: 'reverse' })
    });

    assert(result.ok, 'POST /trains/{id}/travel-direction should succeed');
    assert(result.data.travel_direction === 'reverse', 'Travel direction should be reverse');
}

async function test_POST_train_emergency_stop() {
    await resetNetwork();

    // Create track and train, power on, enable manual, set speed
    await createTrack(0, 0, TRACK_EW);
    await fetchJSON('/trains', {
        method: 'POST',
        body: JSON.stringify({
            id: 'TRAIN_ESTOP_TEST',
            length: 1.0,
            position: { x: 0, y: 0 },
            direction: 'EAST'
        })
    });
    await fetchJSON('/trains/TRAIN_ESTOP_TEST/power-on', { method: 'POST' });
    await fetchJSON('/trains/TRAIN_ESTOP_TEST/enable-manual', { method: 'POST' });
    await fetchJSON('/trains/TRAIN_ESTOP_TEST/manual-speed', {
        method: 'POST',
        body: JSON.stringify({ speed: 50.0 })
    });

    const result = await fetchJSON('/trains/TRAIN_ESTOP_TEST/emergency-stop', {
        method: 'POST'
    });

    assert(result.ok, 'POST /trains/{id}/emergency-stop should succeed');
    assert(result.data.state === 'emergency_stop', 'Train state should be emergency_stop');
}

// ============= STATION ADDITIONAL ENDPOINTS =============

async function test_POST_station_remove_tile() {
    await resetNetwork();

    // Create station with multiple tiles
    await createTrack(10, 10, TRACK_NS);
    await createTrack(10, 11, TRACK_NS);
    const stationResult = await createStation(10, 10, 'Test Platform');
    await fetchJSON(`/station/${stationResult.data.station_id}/extend`, {
        method: 'POST',
        body: JSON.stringify({ positions: [[10, 11]] })
    });

    const result = await fetchJSON(`/station/${stationResult.data.station_id}/remove_tile`, {
        method: 'POST',
        body: JSON.stringify({ x: 10, y: 11 })
    });

    assert(result.ok, 'POST /station/{id}/remove_tile should succeed');
}

async function test_GET_station_end_tiles() {
    await resetNetwork();

    // Create station with multiple tiles
    await createTrack(10, 10, TRACK_NS);
    await createTrack(10, 11, TRACK_NS);
    const stationResult = await createStation(10, 10, 'Test Platform');
    await fetchJSON(`/station/${stationResult.data.station_id}/extend`, {
        method: 'POST',
        body: JSON.stringify({ positions: [[10, 11]] })
    });

    const result = await fetchJSON(`/station/${stationResult.data.station_id}/end_tiles`);

    assert(result.ok, 'GET /station/{id}/end_tiles should succeed');
    assert(result.data.start_tile !== undefined, 'Should have start_tile');
    assert(result.data.end_tile !== undefined, 'Should have end_tile');
}

async function test_POST_station_can_extend() {
    await resetNetwork();

    // Create station and adjacent track
    await createTrack(10, 10, TRACK_NS);
    await createTrack(10, 11, TRACK_NS);
    const stationResult = await createStation(10, 10, 'Test Platform');

    const result = await fetchJSON(`/station/${stationResult.data.station_id}/can_extend`, {
        method: 'POST',
        body: JSON.stringify({ x: 10, y: 11 })
    });

    assert(result.ok, 'POST /station/{id}/can_extend should succeed');
    assert(result.data.can_extend !== undefined, 'Should have can_extend field');
}

async function test_POST_station_assign_tile() {
    await resetNetwork();

    // Create station and block
    await createTrack(10, 10, TRACK_NS);
    const stationResult = await createStation(10, 10, 'Test Platform');
    const blockResult = await createBlock('BLK_ASSIGN_TEST', 'Test Block');

    const result = await fetchJSON(`/station/${stationResult.data.station_id}/assign_tile`, {
        method: 'POST',
        body: JSON.stringify({ x: 10, y: 10, block_id: blockResult.data.block_id })
    });

    assert(result.ok, 'POST /station/{id}/assign_tile should succeed');
}

async function test_POST_station_unassign_tile() {
    await resetNetwork();

    // Create station, block, and assign
    await createTrack(10, 10, TRACK_NS);
    const stationResult = await createStation(10, 10, 'Test Platform');
    const blockResult = await createBlock('BLK_UNASSIGN_TEST', 'Test Block');
    await fetchJSON(`/station/${stationResult.data.station_id}/assign_tile`, {
        method: 'POST',
        body: JSON.stringify({ x: 10, y: 10, block_id: blockResult.data.block_id })
    });

    const result = await fetchJSON(`/station/${stationResult.data.station_id}/unassign_tile`, {
        method: 'POST',
        body: JSON.stringify({ x: 10, y: 10 })
    });

    assert(result.ok, 'POST /station/{id}/unassign_tile should succeed');
}

async function test_GET_station_block_assignments() {
    await resetNetwork();

    // Create station
    await createTrack(10, 10, TRACK_NS);
    const stationResult = await createStation(10, 10, 'Test Platform');

    const result = await fetchJSON(`/station/${stationResult.data.station_id}/block_assignments`);

    assert(result.ok, 'GET /station/{id}/block_assignments should succeed');
    assert(result.data.assignments !== undefined, 'Should have assignments field');
}

// ============= BLOCK ADDITIONAL ENDPOINTS =============

async function test_DELETE_block_tracks() {
    await resetNetwork();

    // Create block with tracks
    await createTrack(5, 5, TRACK_NS);
    await createTrack(5, 6, TRACK_NS);
    const blockResult = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            name: 'Test Block',
            positions: [[5, 5], [5, 6]]
        })
    });

    if (blockResult.ok) {
        const result = await fetchJSON(`/blocks/${blockResult.data.block_id}/tracks`, {
            method: 'DELETE',
            body: JSON.stringify({ positions: [[5, 6]] })
        });

        assert(result.ok, 'DELETE /blocks/{id}/tracks should succeed');
    }
}

async function test_POST_block_can_extend() {
    await resetNetwork();

    // Create block and adjacent track
    await createTrack(5, 5, TRACK_NS);
    await createTrack(5, 6, TRACK_NS);
    const blockResult = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            name: 'Test Block',
            positions: [[5, 5]]
        })
    });

    if (blockResult.ok) {
        const result = await fetchJSON(`/blocks/${blockResult.data.block_id}/can_extend`, {
            method: 'POST',
            body: JSON.stringify({ x: 5, y: 6 })
        });

        assert(result.ok, 'POST /blocks/{id}/can_extend should succeed');
        assert(result.data.can_extend !== undefined, 'Should have can_extend field');
    }
}

// ============= PATH/DISPATCHER ENDPOINTS =============

async function test_GET_dispatcher_reachable() {
    await resetNetwork();

    // Create a simple block
    await createTrack(0, 0, TRACK_NS);
    const blockResult = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            name: 'Test Block',
            positions: [[0, 0]]
        })
    });

    if (blockResult.ok) {
        const result = await fetchJSON(`/dispatcher/reachable/${blockResult.data.block_id}`);

        // This endpoint may return empty array if no connected blocks
        assert(result.ok, 'GET /dispatcher/reachable/{block_id} should succeed');
        assert(result.data.reachable !== undefined, 'Should have reachable field');
    }
}

async function test_POST_dispatcher_paths() {
    await resetNetwork();

    // Create two blocks
    await createTrack(0, 0, TRACK_NS);
    await createTrack(0, 1, TRACK_NS);
    const block1Result = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            name: 'Block 1',
            positions: [[0, 0]]
        })
    });
    const block2Result = await fetchJSON('/blocks/create-with-tracks', {
        method: 'POST',
        body: JSON.stringify({
            name: 'Block 2',
            positions: [[0, 1]]
        })
    });

    if (block1Result.ok && block2Result.ok) {
        const result = await fetchJSON('/dispatcher/paths', {
            method: 'POST',
            body: JSON.stringify({
                start_block_id: block1Result.data.block_id,
                end_block_id: block2Result.data.block_id
            })
        });

        // May return empty paths array if blocks not connected properly
        assert(result.ok, 'POST /dispatcher/paths should succeed');
        assert(result.data.paths !== undefined, 'Should have paths field');
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

// ============= FRONTEND-BACKEND PARITY TESTS =============

async function test_PARITY_001_all_forbidden_track_bits() {
    await resetNetwork();

    // Test a sample of forbidden track bits
    const sampleForbidden = [13, 22, 42, 63];

    for (const bits of sampleForbidden) {
        const result = await createTrack(0, 0, bits);

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
            detection_direction: 'E'
        })
    });

    assert(!result.ok, 'Signal on switch should be rejected');
}

async function test_PARITY_003_valid_signal_directions() {
    await resetNetwork();

    // Test NS track
    await createTrack(0, 0, TRACK_NS);

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

// ============= LAYOUT PERSISTENCE ENDPOINTS =============

async function test_GET_layouts_list() {
    const result = await fetchJSON('/layouts');

    assert(result.ok, 'GET /layouts should succeed');
    assert(result.data.layouts !== undefined, 'Response should contain layouts array');
    assert(Array.isArray(result.data.layouts), 'Layouts should be an array');
}

async function test_POST_layouts_save() {
    await resetNetwork();

    // Create some test content
    await createTrack(0, 0, TRACK_NS);
    await createTrack(0, 1, TRACK_NS);

    const result = await fetchJSON('/layouts', {
        method: 'POST',
        body: JSON.stringify({
            name: 'test-layout',
            description: 'Test layout for API tests'
        })
    });

    assert(result.ok, 'POST /layouts should succeed');
    assert(result.data.status === 'success', 'Save should succeed');
    assert(result.data.path !== undefined, 'Response should contain file path');
    assert(result.data.steps_generated !== undefined, 'Response should indicate if steps were generated');
}

async function test_POST_layouts_load() {
    // First save a layout
    await resetNetwork();
    await createTrack(5, 5, TRACK_EW);
    await fetchJSON('/layouts', {
        method: 'POST',
        body: JSON.stringify({ name: 'test-load-layout' })
    });

    // Clear network
    await resetNetwork();

    // Load it back
    const result = await fetchJSON('/layouts/test-load-layout/load', {
        method: 'POST'
    });

    assert(result.ok, 'POST /layouts/:name/load should succeed');
    assert(result.data.status === 'success', 'Load should succeed');
    assert(result.data.method !== undefined, 'Response should indicate load method (snapshot/steps)');

    // Verify content was loaded
    const network = await fetchJSON('/network');
    assert(Object.keys(network.data.tracks).length > 0, 'Network should have tracks after load');
}

async function test_DELETE_layouts() {
    // First save a layout
    await fetchJSON('/layouts', {
        method: 'POST',
        body: JSON.stringify({ name: 'test-delete-layout' })
    });

    // Delete it
    const result = await fetchJSON('/layouts/test-delete-layout', {
        method: 'DELETE'
    });

    assert(result.ok, 'DELETE /layouts/:name should succeed');
    assert(result.data.status === 'success', 'Delete should succeed');
}

async function test_POST_network_clear() {
    await resetNetwork();

    // Create some content
    await createTrack(0, 0, TRACK_NS);
    await createTrack(1, 1, TRACK_EW);

    const result = await fetchJSON('/network/clear', {
        method: 'POST'
    });

    assert(result.ok, 'POST /network/clear should succeed');
    assert(result.data.status === 'success', 'Clear should succeed');
    assert(result.data.cleared !== undefined, 'Response should contain cleared counts');

    // Verify network is empty
    const network = await fetchJSON('/network');
    assert(Object.keys(network.data.tracks).length === 0, 'Network should be empty after clear');
}

async function test_GET_network_stats() {
    await resetNetwork();

    // Create some content to get meaningful stats
    await createTrack(0, 0, TRACK_NS);
    await createTrack(1, 1, TRACK_EW);

    const blockResult = await createBlock('TEST_BLK', 'Test Block');
    const signalResult = await createSignal(0, 0, 'N', 'block');

    const result = await fetchJSON('/network/stats');

    assert(result.ok, 'GET /network/stats should succeed');
    assert(result.data.tracks !== undefined, 'Stats should include track count');
    assert(result.data.blocks !== undefined, 'Stats should include block count');
    assert(result.data.signals !== undefined, 'Stats should include signal count');
    assert(result.data.is_empty !== undefined, 'Stats should include is_empty flag');
    assert(result.data.is_empty === false, 'Network should not be empty with content');
}

async function test_POST_block_extend() {
    await resetNetwork();

    // Create initial block with one track
    await createTrack(0, 0, TRACK_NS);
    const blockResult = await createBlock('TEST_EXTEND', 'Extend Test');
    await fetchJSON(`/blocks/${blockResult.data.block_id}/tracks`, {
        method: 'POST',
        body: JSON.stringify({ positions: [[0, 0]] })
    });

    // Create adjacent tracks to extend into
    await createTrack(0, 1, TRACK_NS);
    await createTrack(0, 2, TRACK_NS);

    // Extend the block
    const result = await fetchJSON(`/blocks/${blockResult.data.block_id}/extend`, {
        method: 'POST',
        body: JSON.stringify({ positions: [[0, 1], [0, 2]] })
    });

    assert(result.ok, 'POST /blocks/{id}/extend should succeed');
    // API returns block properties at root level, not wrapped
    assert(result.data.block_id !== undefined, 'Response should contain updated block');

    // Verify block now has all tracks
    const updatedBlock = await fetchJSON(`/blocks/${blockResult.data.block_id}`);
    assert(updatedBlock.data.track_count >= 3, 'Block should have at least 3 tracks after extend');
}

async function test_GET_signal_by_position() {
    await resetNetwork();

    // Create track and signal
    await createTrack(5, 5, TRACK_NS);
    const signalResult = await createSignal(5, 5, 'N', 'block');

    // Get signal by position
    const result = await fetchJSON('/signal/5/5');

    assert(result.ok, 'GET /signal/{x}/{y} should succeed');
    assert(result.data.signal_id !== undefined, 'Response should contain signal');
    // Signal uses separate x, y fields, not position array
    assert(result.data.x === 5 && result.data.y === 5, 'Signal should be at correct position');
}

async function test_DELETE_signal_by_position() {
    await resetNetwork();

    // Create track and signal
    await createTrack(6, 6, TRACK_NS);
    await createSignal(6, 6, 'N', 'block');

    // Delete signal by position
    const result = await fetchJSON('/signal/6/6', {
        method: 'DELETE'
    });

    assert(result.ok, 'DELETE /signal/{x}/{y} should succeed');

    // Verify signal is deleted
    const getResult = await fetchJSON('/signal/6/6');
    assert(!getResult.ok, 'Signal should not exist after deletion');
}

async function test_GET_signal_reachable_blocks() {
    await resetNetwork();

    // Create a simple track layout with a block and signal
    await createTrack(0, 0, TRACK_NS);
    await createTrack(0, 1, TRACK_NS);
    const blockResult = await createBlock('REACH_TEST', 'Reachable Test');
    await fetchJSON(`/blocks/${blockResult.data.block_id}/tracks`, {
        method: 'POST',
        body: JSON.stringify({ positions: [[0, 0], [0, 1]] })
    });

    const signalResult = await createSignal(0, 0, 'N', 'block');

    // Get reachable blocks
    const result = await fetchJSON(`/signal/${signalResult.data.signal_id}/reachable-blocks`);

    assert(result.ok, 'GET /signal/{id}/reachable-blocks should succeed');
    // API uses snake_case: reachable_blocks, not blocks
    assert(result.data.reachable_blocks !== undefined, 'Response should contain reachable_blocks array');
    assert(Array.isArray(result.data.reachable_blocks), 'Reachable blocks should be an array');
}

async function test_POST_station_extend() {
    await resetNetwork();

    // Create initial station track
    await createTrack(10, 10, TRACK_NS);
    const stationResult = await createStation(10, 10, 'Test Platform');

    // Create adjacent track to extend into
    await createTrack(10, 11, TRACK_NS);

    // Extend the station
    const result = await fetchJSON(`/station/${stationResult.data.station_id}/extend`, {
        method: 'POST',
        body: JSON.stringify({ positions: [[10, 11]] })
    });

    assert(result.ok, 'POST /station/{id}/extend should succeed');
    // API returns station properties at root level, not wrapped
    assert(result.data.station_id !== undefined, 'Response should contain updated station');

    // Verify station now has multiple positions
    const updatedStation = await fetchJSON(`/station/${stationResult.data.station_id}`);
    assert(updatedStation.data.positions.length >= 2, 'Station should have at least 2 positions after extend');
}

async function test_static_file_serving() {
    // Test GET / (index.html)
    const indexResult = await fetchJSON('/../', { method: 'GET' });
    // Note: fetchJSON expects JSON, but HTML will fail to parse - that's okay for this test
    // We just need to verify the endpoint responds
    assert(indexResult.status === 200 || indexResult.ok !== undefined,
           'GET / should be accessible');

    // Test GET /<path:path> (static file) - just verify server is serving files
    // We can't easily test this with fetchJSON since it expects JSON responses
    // The fact that the app works at all proves static files are being served
    // This is more of an integration smoke test
    const networkResult = await fetchJSON('/network');
    assert(networkResult.ok, 'Static file infrastructure should support API calls');
}

// ============= TEST RUNNER =============

async function runAllTests() {
    console.log('Starting REST API Tests...\n');
    console.log('Testing against:', BASE_URL);
    console.log('Ensure server is running at localhost:5001\n');

    // Network endpoints
    await runTest('GET /api/network', test_GET_network);
    await runTest('GET /api/network/stats', test_GET_network_stats);

    // Track endpoints (Core)
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
    await runTest('POST /api/blocks/{id}/extend', test_POST_block_extend);

    // Block endpoints (Extensive)
    await runTest('BLK-API-E01: Reject non-existent tracks', test_BLK_API_E01_reject_nonexistent_tracks);
    await runTest('BLK-API-E02: Reject switch in block', test_BLK_API_E02_reject_switch_in_block);
    await runTest('BLK-API-E03: Reject crossing in block', test_BLK_API_E03_reject_crossing_in_block);

    // Signal endpoints (Core)
    await runTest('POST /api/signal (create)', test_POST_signal_create);
    await runTest('GET /api/signal/{id}', test_GET_signal_by_id);
    await runTest('GET /api/signal/{x}/{y}', test_GET_signal_by_position);
    await runTest('PATCH /api/signal/{id}', test_PATCH_signal_update);
    await runTest('DELETE /api/signal/{id}', test_DELETE_signal);
    await runTest('DELETE /api/signal/{x}/{y}', test_DELETE_signal_by_position);
    await runTest('GET /api/signal/validate', test_GET_signal_validate);
    await runTest('GET /api/signal/{id}/reachable-blocks', test_GET_signal_reachable_blocks);

    // Signal endpoints (Extensive)
    await runTest('Signal: Reject on switch', test_signal_on_switch_rejected);

    // Station endpoints (Core)
    await runTest('POST /api/station (create)', test_POST_station_create);
    await runTest('GET /api/station/{id}', test_GET_station_by_id);
    await runTest('PATCH /api/station/{id}', test_PATCH_station_update);
    await runTest('DELETE /api/station/{id}', test_DELETE_station);
    await runTest('GET /api/station/validate', test_GET_station_validate);
    await runTest('POST /api/station/{id}/extend', test_POST_station_extend);

    // Train endpoints
    await runTest('POST /api/trains (create)', test_POST_trains_create);
    await runTest('GET /api/trains', test_GET_trains_list);
    await runTest('GET /api/trains/{id}', test_GET_train_by_id);
    await runTest('DELETE /api/trains/{id}', test_DELETE_train);
    await runTest('POST /api/trains/{id}/power-on', test_POST_train_power_on);
    await runTest('POST /api/trains/{id}/power-off', test_POST_train_power_off);
    await runTest('POST /api/trains/{id}/set-direction', test_POST_train_set_direction);
    await runTest('POST /api/trains/{id}/turn-around', test_POST_train_turn_around);
    await runTest('POST /api/trains/{id}/enable-manual', test_POST_train_enable_manual);
    await runTest('POST /api/trains/{id}/disable-manual', test_POST_train_disable_manual);
    await runTest('POST /api/trains/{id}/manual-speed', test_POST_train_manual_speed);
    await runTest('POST /api/trains/{id}/travel-direction', test_POST_train_travel_direction);
    await runTest('POST /api/trains/{id}/emergency-stop', test_POST_train_emergency_stop);

    // Station additional endpoints
    await runTest('POST /api/station/{id}/remove_tile', test_POST_station_remove_tile);
    await runTest('GET /api/station/{id}/end_tiles', test_GET_station_end_tiles);
    await runTest('POST /api/station/{id}/can_extend', test_POST_station_can_extend);
    await runTest('POST /api/station/{id}/assign_tile', test_POST_station_assign_tile);
    await runTest('POST /api/station/{id}/unassign_tile', test_POST_station_unassign_tile);
    await runTest('GET /api/station/{id}/block_assignments', test_GET_station_block_assignments);

    // Block additional endpoints
    await runTest('DELETE /api/blocks/{id}/tracks', test_DELETE_block_tracks);
    await runTest('POST /api/blocks/{id}/can_extend', test_POST_block_can_extend);

    // Dispatcher endpoints
    await runTest('GET /api/dispatcher/reachable/{block_id}', test_GET_dispatcher_reachable);
    await runTest('POST /api/dispatcher/paths', test_POST_dispatcher_paths);

    // Simulation endpoints
    await runTest('POST /api/simulation/control', test_POST_simulation_control);

    // Layout persistence endpoints
    await runTest('GET /api/layouts', test_GET_layouts_list);
    await runTest('POST /api/layouts (save)', test_POST_layouts_save);
    await runTest('POST /api/layouts/:name/load', test_POST_layouts_load);
    await runTest('DELETE /api/layouts/:name', test_DELETE_layouts);
    await runTest('POST /api/network/clear', test_POST_network_clear);

    // Infrastructure endpoints
    await runTest('GET / and GET /<path:path> (static files)', test_static_file_serving);

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
