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

// A "square corner" switch joins a HORIZONTAL bit (NW=8 or SE=16, screen 0°)
// and a VERTICAL bit (NE=4 or SW=32, screen 90°) with NO 45° diagonal
// (NS=1 or EW=2) to ease it. On the rotated screen it renders as a 90° T-bone
// — a connecting segment meets a switch branch at a right angle — which is
// unintuitive, so these placements are rejected. A turnout carrying a diagonal
// NS/EW bit eases the branch at 45° (like the scissors crossovers) and is OK.
// Mirror of the server check in track.py (is_square_corner_switch).
function isSquareCornerSwitch(bits) {
    const hasHorizontal = (bits & (8 | 16)) !== 0;   // NW | SE
    const hasVertical   = (bits & (4 | 32)) !== 0;   // NE | SW
    const hasDiagonal   = (bits & (1 | 2)) !== 0;    // NS | EW
    return hasHorizontal && hasVertical && !hasDiagonal;
}

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

// ============================================================================
// Label Spacing System
// ============================================================================

/**
 * Label spacing presets - multiplier applied to tileSize for label offset
 */
const LABEL_SPACING_PRESETS = {
    'tight': 0.35,
    'mid': 0.5,
    'cozy': 0.7
};

// Current label spacing setting (can be changed at runtime)
let currentLabelSpacing = 'mid';

/**
 * Get the current label spacing multiplier
 * @returns {number} Spacing multiplier (0.35, 0.5, or 0.7)
 */
function getLabelSpacingMultiplier() {
    return LABEL_SPACING_PRESETS[currentLabelSpacing] || 0.5;
}

/**
 * Set the current label spacing
 * @param {string} spacing - 'tight', 'mid', or 'cozy'
 */
function setLabelSpacing(spacing) {
    if (LABEL_SPACING_PRESETS[spacing] !== undefined) {
        currentLabelSpacing = spacing;
    }
}

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
 * Check if track is a simple crossing (NS + EW)
 * @param {number} trackBits - 6-bit track configuration
 * @returns {boolean} True if this is a simple crossing
 */
function isCrossing(trackBits) {
    return trackBits === (TRACK_BITS.NS | TRACK_BITS.EW);  // 3
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
