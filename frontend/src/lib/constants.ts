// constants.ts — Shared track-bit constants and tile geometry utilities.
//
// Migrated from static/js/constants.js (longterm_todo.md item #7, Wave 1 —
// foundation cluster). The values and functions are unchanged; behavior must
// stay byte-for-byte identical to the classic file it replaces.
//
// `isSquareCornerSwitch` is pinned line-for-line against the Python backend by
// tests/test_square_corner_parity.py — keep the two in sync.
//
// Part of the Vite bundle. The editor, app, and renderer modules import the
// symbols they need directly from this module.

// ============================================================================
// 6-bit Track Bits System (OpenTTD-style)
// Each bit represents one of 6 possible rail segments on a tile.
// ============================================================================

// Individual track bit constants (for backward compatibility with editor.js)
export const TRACK_NS = 1; // 0b000001 - North to South (vertical)
export const TRACK_EW = 2; // 0b000010 - East to West (horizontal)
export const TRACK_NE = 4; // 0b000100 - North to East (diagonal)
export const TRACK_NW = 8; // 0b001000 - North to West (diagonal)
export const TRACK_SE = 16; // 0b010000 - South to East (diagonal)
export const TRACK_SW = 32; // 0b100000 - South to West (diagonal)

// Track bits object (alternative access pattern)
export const TRACK_BITS = {
  NONE: 0,
  NS: 1, // 0b000001 - North to South (vertical)
  EW: 2, // 0b000010 - East to West (horizontal)
  NE: 4, // 0b000100 - North to East (diagonal)
  NW: 8, // 0b001000 - North to West (diagonal)
  SE: 16, // 0b010000 - South to East (diagonal)
  SW: 32, // 0b100000 - South to West (diagonal)
};

// ============================================================================
// Edge System Constants
// ============================================================================

// Bit index (0-5) -> which two edges the segment connects
export const BIT_TO_EDGES: Record<number, [string, string]> = {
  0: ['N', 'S'], // TRACK_NS
  1: ['E', 'W'], // TRACK_EW
  2: ['N', 'E'], // TRACK_NE
  3: ['N', 'W'], // TRACK_NW
  4: ['S', 'E'], // TRACK_SE
  5: ['S', 'W'], // TRACK_SW
};

// Edge masks - which bits touch each edge (for connectivity checks)
export const EDGE_MASKS: Record<string, number> = { N: 13, E: 22, S: 49, W: 42 };

// Opposite edge lookup
export const OPPOSITE_EDGE: Record<string, string> = { N: 'S', S: 'N', E: 'W', W: 'E' };

// ============================================================================
// Rendering Constants
// ============================================================================

// Track rendering constants
export const TRACK_WIDTH = 4; // Base track width in pixels (at scale=1)
export const DEBUG_BLOCK_WIDTH = 12; // Debug block overlay width in pixels (at scale=1)

// ============================================================================
// Forbidden Track Configurations
// ============================================================================

// Forbidden track_bits configurations (3-way switches and complex crossings)
export const FORBIDDEN_TRACK_BITS = new Set<number>([
  13, 22, 42, 49, // 3-way switches
  28, 44, 52, 56, 60, // Multi-diagonal without through route
  15, 23, 43, 51, 29, 30, 45, 46, 53, 54, 57, 58, // 4-path configs
  31, 47, 55, 59, 61, 62, 63, // 5+ path configs
]);

// A "square corner" switch joins a HORIZONTAL bit (NW=8 or SE=16, screen 0°)
// and a VERTICAL bit (NE=4 or SW=32, screen 90°) with NO 45° diagonal
// (NS=1 or EW=2) to ease it. On the rotated screen it renders as a 90° T-bone
// — a connecting segment meets a switch branch at a right angle — which is
// unintuitive, so these placements are rejected. A turnout carrying a diagonal
// NS/EW bit eases the branch at 45° (like the scissors crossovers) and is OK.
// Mirror of the server check in track.py (is_square_corner_switch).
export function isSquareCornerSwitch(bits: number): boolean {
  const hasHorizontal = (bits & (8 | 16)) !== 0; // NW | SE
  const hasVertical = (bits & (4 | 32)) !== 0; // NE | SW
  const hasDiagonal = (bits & (1 | 2)) !== 0; // NS | EW
  return hasHorizontal && hasVertical && !hasDiagonal;
}

// ============================================================================
// Quadrant System (for signal/label placement)
// ============================================================================

export const SEGMENT_QUADRANTS: Record<number, string[]> = {
  0: ['NE', 'SW'], // NS segment occupies NE and SW quadrants
  1: ['SE', 'NW'], // EW segment occupies SE and NW quadrants
  2: ['NE', 'SE'], // NE segment (vertical on screen)
  3: ['NE', 'NW'], // NW segment (horizontal on screen)
  4: ['SE', 'SW'], // SE segment (horizontal on screen)
  5: ['NW', 'SW'], // SW segment (vertical on screen)
};

// ============================================================================
// Label Spacing System
// ============================================================================

/** Label spacing presets - multiplier applied to tileSize for label offset */
export const LABEL_SPACING_PRESETS: Record<string, number> = {
  tight: 0.35,
  mid: 0.5,
  cozy: 0.7,
};

// Current label spacing setting (can be changed at runtime)
let currentLabelSpacing = 'mid';

/** Get the current label spacing multiplier (0.35, 0.5, or 0.7). */
export function getLabelSpacingMultiplier(): number {
  return LABEL_SPACING_PRESETS[currentLabelSpacing] || 0.5;
}

/** Set the current label spacing ('tight', 'mid', or 'cozy'). */
export function setLabelSpacing(spacing: string): void {
  if (LABEL_SPACING_PRESETS[spacing] !== undefined) {
    currentLabelSpacing = spacing;
  }
}

export const DIRECTION_PREFERRED_QUADRANTS: Record<string, string[]> = {
  NORTH: ['SW', 'SE', 'NW', 'NE'],
  SOUTH: ['NE', 'NW', 'SE', 'SW'],
  EAST: ['NW', 'SW', 'NE', 'SE'],
  WEST: ['SE', 'NE', 'SW', 'NW'],
};

// ============================================================================
// Track Bit Utility Functions
// ============================================================================

/** Convert track_bits to the set of connected edge names ('N', 'E', 'S', 'W'). */
export function trackBitsToEdges(trackBits: number): Set<string> {
  const edges = new Set<string>();
  for (let bit = 0; bit < 6; bit++) {
    if (trackBits & (1 << bit)) {
      const [e1, e2] = BIT_TO_EDGES[bit];
      edges.add(e1);
      edges.add(e2);
    }
  }
  return edges;
}

/** Get active segments as an array of [edge1, edge2] pairs. */
export function getActiveSegments(trackBits: number): Array<[string, string]> {
  const segments: Array<[string, string]> = [];
  for (let bit = 0; bit < 6; bit++) {
    if (trackBits & (1 << bit)) {
      segments.push(BIT_TO_EDGES[bit]);
    }
  }
  return segments;
}

/** Check if track is a simple crossing (NS + EW). */
export function isCrossing(trackBits: number): boolean {
  return trackBits === (TRACK_BITS.NS | TRACK_BITS.EW); // 3
}

/** Get the opposite edge name, or null if invalid. */
export function getOppositeEdge(edge: string): string | null {
  const e = edge.charAt(0).toUpperCase();
  return OPPOSITE_EDGE[e] || null;
}
