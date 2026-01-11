// ============================================================================
// constants.js - Shared Track Bit Constants and Utilities
// ============================================================================
// This file must be loaded FIRST before all other JS files.
// Contains the 6-bit track system constants and utility functions used by
// both renderer.js and editor.js.
// ============================================================================

// ============================================================================
// 6-bit Track Bits System (OpenTTD-style)
// Each bit represents one of 6 possible rail segments on a tile.
// ============================================================================

// Individual track bit constants (for backward compatibility with editor.js)
const TRACK_NS = 1;   // 0b000001 - North to South (vertical)
const TRACK_EW = 2;   // 0b000010 - East to West (horizontal)
const TRACK_NE = 4;   // 0b000100 - North to East (diagonal)
const TRACK_NW = 8;   // 0b001000 - North to West (diagonal)
const TRACK_SE = 16;  // 0b010000 - South to East (diagonal)
const TRACK_SW = 32;  // 0b100000 - South to West (diagonal)

// Track bits object (alternative access pattern)
const TRACK_BITS = {
    NONE: 0,
    NS: 1,    // 0b000001 - North to South (vertical)
    EW: 2,    // 0b000010 - East to West (horizontal)
    NE: 4,    // 0b000100 - North to East (diagonal)
    NW: 8,    // 0b001000 - North to West (diagonal)
    SE: 16,   // 0b010000 - South to East (diagonal)
    SW: 32,   // 0b100000 - South to West (diagonal)
};

// ============================================================================
// Edge System Constants
// ============================================================================

// Bit index (0-5) -> which two edges the segment connects
const BIT_TO_EDGES = {
    0: ['N', 'S'],  // TRACK_NS
    1: ['E', 'W'],  // TRACK_EW
    2: ['N', 'E'],  // TRACK_NE
    3: ['N', 'W'],  // TRACK_NW
    4: ['S', 'E'],  // TRACK_SE
    5: ['S', 'W'],  // TRACK_SW
};

// Edge masks - which bits touch each edge (for connectivity checks)
const EDGE_MASKS = { N: 13, E: 22, S: 49, W: 42 };

// Opposite edge lookup
const OPPOSITE_EDGE = { N: 'S', S: 'N', E: 'W', W: 'E' };

// ============================================================================
// Rendering Constants
// ============================================================================

// Track rendering constants
const TRACK_WIDTH = 4;  // Base track width in pixels (at scale=1)
const DEBUG_BLOCK_WIDTH = 12;  // Debug block overlay width in pixels (at scale=1)

// ============================================================================
// Forbidden Track Configurations
// ============================================================================

// Forbidden track_bits configurations (3-way switches and complex crossings)
const FORBIDDEN_TRACK_BITS = new Set([
    13, 22, 42, 49,        // 3-way switches
    28, 44, 52, 56, 60,    // Multi-diagonal without through route
    15, 23, 43, 51, 29, 30, 45, 46, 53, 54, 57, 58,  // 4-path configs
    31, 47, 55, 59, 61, 62, 63  // 5+ path configs
]);

// ============================================================================
// Quadrant System (for signal/label placement)
// ============================================================================

const SEGMENT_QUADRANTS = {
    0: ['NE', 'SW'],  // NS segment occupies NE and SW quadrants
    1: ['SE', 'NW'],  // EW segment occupies SE and NW quadrants
    2: ['NE', 'SE'],  // NE segment (vertical on screen)
    3: ['NE', 'NW'],  // NW segment (horizontal on screen)
    4: ['SE', 'SW'],  // SE segment (horizontal on screen)
    5: ['NW', 'SW'],  // SW segment (vertical on screen)
};

const QUADRANT_OFFSETS = {
    'NE': { dx: 0.35, dy: -0.35 },
    'NW': { dx: -0.35, dy: -0.35 },
    'SE': { dx: 0.35, dy: 0.35 },
    'SW': { dx: -0.35, dy: 0.35 },
};

const DIRECTION_PREFERRED_QUADRANTS = {
    'NORTH': ['SW', 'SE', 'NW', 'NE'],
    'SOUTH': ['NE', 'NW', 'SE', 'SW'],
    'EAST': ['NW', 'SW', 'NE', 'SE'],
    'WEST': ['SE', 'NE', 'SW', 'NW'],
};

// ============================================================================
// Track Bit Utility Functions
// ============================================================================

/**
 * Get which quadrants are occupied by track segments
 * @param {number} trackBits - 6-bit track configuration
 * @returns {Set<string>} Set of occupied quadrant names ('NE', 'NW', 'SE', 'SW')
 */
function getOccupiedQuadrants(trackBits) {
    const occupied = new Set();
    for (let bit = 0; bit < 6; bit++) {
        if (trackBits & (1 << bit)) {
            const quads = SEGMENT_QUADRANTS[bit];
            if (quads) {
                quads.forEach(q => occupied.add(q));
            }
        }
    }
    return occupied;
}

/**
 * Get the best quadrant for placing a label based on track configuration and direction
 * @param {number} trackBits - 6-bit track configuration
 * @param {string} direction - Direction name ('NORTH', 'SOUTH', 'EAST', 'WEST')
 * @returns {string} Best quadrant name for label placement
 */
function getBestLabelQuadrant(trackBits, direction) {
    const occupied = getOccupiedQuadrants(trackBits);
    const preferred = DIRECTION_PREFERRED_QUADRANTS[direction] || ['NE', 'NW', 'SE', 'SW'];
    
    for (const quad of preferred) {
        if (!occupied.has(quad)) {
            return quad;
        }
    }
    return preferred[0];
}

/**
 * Convert track_bits to list of connected edges
 * @param {number} trackBits - 6-bit track configuration
 * @returns {Set<string>} Set of connected edge names ('N', 'E', 'S', 'W')
 */
function trackBitsToEdges(trackBits) {
    const edges = new Set();
    for (let bit = 0; bit < 6; bit++) {
        if (trackBits & (1 << bit)) {
            const [e1, e2] = BIT_TO_EDGES[bit];
            edges.add(e1);
            edges.add(e2);
        }
    }
    return edges;
}

/**
 * Get active segments as array of [edge1, edge2] pairs
 * @param {number} trackBits - 6-bit track configuration
 * @returns {Array<Array<string>>} Array of [edge1, edge2] pairs
 */
function getActiveSegments(trackBits) {
    const segments = [];
    for (let bit = 0; bit < 6; bit++) {
        if (trackBits & (1 << bit)) {
            segments.push(BIT_TO_EDGES[bit]);
        }
    }
    return segments;
}

/**
 * Check if track_bits has a specific segment
 * @param {number} trackBits - 6-bit track configuration
 * @param {string} edge1 - First edge ('N', 'E', 'S', 'W' or full name)
 * @param {string} edge2 - Second edge ('N', 'E', 'S', 'W' or full name)
 * @returns {boolean} True if the segment exists
 */
function hasSegment(trackBits, edge1, edge2) {
    // Normalize to single-letter format
    const e1 = edge1.charAt(0).toUpperCase();
    const e2 = edge2.charAt(0).toUpperCase();

    for (let bit = 0; bit < 6; bit++) {
        const [a, b] = BIT_TO_EDGES[bit];
        if ((a === e1 && b === e2) || (a === e2 && b === e1)) {
            return !!(trackBits & (1 << bit));
        }
    }
    return false;
}

/**
 * Check if an edge is connected (has any segment touching it)
 * @param {number} trackBits - 6-bit track configuration
 * @param {string} edge - Edge name ('N', 'E', 'S', 'W' or full name)
 * @returns {boolean} True if the edge has any connected segment
 */
function edgeIsConnected(trackBits, edge) {
    const e = edge.charAt(0).toUpperCase();
    const mask = EDGE_MASKS[e];
    return mask ? !!(trackBits & mask) : false;
}

/**
 * Count number of active segments (popcount)
 * @param {number} trackBits - 6-bit track configuration
 * @returns {number} Number of active segments (0-6)
 */
function countSegments(trackBits) {
    let count = 0;
    for (let bit = 0; bit < 6; bit++) {
        if (trackBits & (1 << bit)) count++;
    }
    return count;
}

/**
 * Check if track is a simple crossing (NS + EW)
 * @param {number} trackBits - 6-bit track configuration
 * @returns {boolean} True if this is a simple crossing
 */
function isCrossing(trackBits) {
    return trackBits === (TRACK_BITS.NS | TRACK_BITS.EW);  // 3
}

/**
 * Check if track is a diamond crossing (NE + SW or NW + SE)
 * @param {number} trackBits - 6-bit track configuration
 * @returns {boolean} True if this is a diamond crossing
 */
function isDiamondCrossing(trackBits) {
    return trackBits === (TRACK_BITS.NE | TRACK_BITS.SW) ||  // 36
        trackBits === (TRACK_BITS.NW | TRACK_BITS.SE);    // 24
}

/**
 * Get opposite edge
 * @param {string} edge - Edge name ('N', 'E', 'S', 'W' or full name)
 * @returns {string|null} Opposite edge name or null if invalid
 */
function getOppositeEdge(edge) {
    const e = edge.charAt(0).toUpperCase();
    return OPPOSITE_EDGE[e] || null;
}

/**
 * Get segment group for smart line cap decisions
 * Track segments are grouped by their orientation:
 * - axial-vertical: NS (long vertical path)
 * - axial-horizontal: EW (long horizontal path)
 * - diagonal-nesw: NE/SW diagonal pair
 * - diagonal-nwse: NW/SE diagonal pair
 * @param {string} e1 - First edge
 * @param {string} e2 - Second edge
 * @returns {string} Segment group name
 */
function getSegmentGroup(e1, e2) {
    const normalized = [e1, e2].sort().join('');
    if (normalized === 'NS') return 'axial-vertical';
    if (normalized === 'EW') return 'axial-horizontal';
    if (normalized === 'EN' || normalized === 'SW') return 'diagonal-nesw';
    if (normalized === 'ES' || normalized === 'NW') return 'diagonal-nwse';
    return 'unknown';
}
