# Reference: Track Display and Rendering

## Context
This document explains how our system renders tracks using the 6-bit `track_bits` system (inspired by OpenTTD's tile geometry). The system uses canvas-based rendering where each track segment is drawn as a line between edge midpoints.

## 1. The 6-Bit Track System

### Track Bits Overview
Our system uses a **6-bit integer** called `track_bits` (0-63) where each bit represents one of 6 possible rail segments on a tile:

| Bit | Value | Segment | Description |
|:---:|:-----:|:-------:|:------------|
| 0 | 1 | **NS** | North to South (vertical) |
| 1 | 2 | **EW** | East to West (horizontal) |
| 2 | 4 | **NE** | North to East (diagonal) |
| 3 | 8 | **NW** | North to West (diagonal) |
| 4 | 16 | **SE** | South to East (diagonal) |
| 5 | 32 | **SW** | South to West (diagonal) |

**Examples:**
*   Vertical straight track: `track_bits = 1` (NS only)
*   Horizontal straight track: `track_bits = 2` (EW only)
*   Right curve (NE): `track_bits = 4`
*   Simple crossing (NS + EW): `track_bits = 3`
*   Switch (NS + NE): `track_bits = 5`
*   Diamond crossing (NE + SW): `track_bits = 36`

### Edge Masks
To determine which edges are connected, use these bitmasks:
*   **North** = 13 (0b001101) = NS + NE + NW
*   **East** = 22 (0b010110) = EW + NE + SE
*   **South** = 49 (0b110001) = NS + SE + SW
*   **West** = 42 (0b101010) = EW + NW + SW

**Usage:** `if (track_bits & 13) // North edge is connected`

### Forbidden Configurations
28 `track_bits` values are **forbidden** (cannot be built):
*   **3-way switches**: 13, 22, 42, 49
*   **Multi-diagonal (no through route)**: 28, 44, 52, 56, 60
*   **Complex 4+ path configs**: 15, 23, 29, 30, 43, 45, 46, 51, 53, 54, 57, 58
*   **5+ path configs**: 31, 47, 55, 59, 61, 62, 63

---

## 2. Rendering Implementation

### `drawTrackFromBits(trackBits, half, color)`
The primary rendering function iterates through all 6 bits and draws active segments:

```javascript
const BIT_TO_EDGES = {
    0: ['N', 'S'], 1: ['E', 'W'], 2: ['N', 'E'],
    3: ['N', 'W'], 4: ['S', 'E'], 5: ['S', 'W']
};

for (let bit = 0; bit < 6; bit++) {
    if (trackBits & (1 << bit)) {
        const [e1, e2] = BIT_TO_EDGES[bit];
        // Draw line from edge e1 to edge e2
    }
}
```

### Edge Points (Isometric Diamond)
In our **45° rotated isometric view** with 1:1 square diamond tiles:

**Coordinate System:**
*   **X+ points East** (toward screen bottom-right)
*   **Y+ points North** (toward screen bottom-left)

**Grid-to-Screen Transformation:**
```javascript
// Updated convention: X+ goes down-right, Y+ goes up-right
// Tile center at (x+0.5, y+0.5), so we add 1 to (gridX + gridY)
screenX = centerX + panX + (gridX + gridY + 1) * tileSize / 2
screenY = centerY + panY + (gridX - gridY) * tileSize / 2
```

**Diamond Tile Geometry:**
The tile is a square rotated 45° with corners at:
*   **Top corner**: (0, -tileSize/2)
*   **Right corner**: (+tileSize/2, 0)
*   **Bottom corner**: (0, +tileSize/2)
*   **Left corner**: (-tileSize/2, 0)

**Edge Midpoints (where tracks connect to neighbors):**
Each edge connects to a neighbor tile in that cardinal direction:
*   **N edge** (connects to y+1 neighbor, up-right on screen): `(+h/2, -h/2)` top-right
*   **E edge** (connects to x+1 neighbor, down-right on screen): `(+h/2, +h/2)` bottom-right
*   **S edge** (connects to y-1 neighbor, down-left on screen): `(-h/2, +h/2)` bottom-left
*   **W edge** (connects to x-1 neighbor, up-left on screen): `(-h/2, -h/2)` top-left

**Screen appearance of segments:**
| Segment | Screen Appearance | Description |
|---------|-------------------|-------------|
| NS | `╱` diagonal | Top-right to bottom-left |
| EW | `╲` diagonal | Bottom-right to top-left |
| NE | Right edge | Top-right to bottom-right (vertical on screen) |
| NW | Top edge | Top-right to top-left (horizontal on screen) |
| SE | Bottom edge | Bottom-left to bottom-right |
| SW | Left edge | Bottom-left to top-left |

### Switch Rendering
When a track has switch points (edge with 2+ exits):
1. Draw **all segments** as thin grey lines (inactive paths)
2. Draw **active path** as thick colored line based on `switch_states`
3. Draw **switch indicator** (M/D) near the diverging exit

---

## 3. Track Bits Value Reference (0-63)

### Single Segments (6 values)
| track_bits | Binary | Segments | Description |
|:----------:|:------:|:--------:|:------------|
| 1 | 000001 | NS | Vertical straight |
| 2 | 000010 | EW | Horizontal straight |
| 4 | 000100 | NE | NE diagonal |
| 8 | 001000 | NW | NW diagonal |
| 16 | 010000 | SE | SE diagonal |
| 32 | 100000 | SW | SW diagonal |

### Common Combinations
| track_bits | Binary | Segments | Description |
|:----------:|:------:|:--------:|:------------|
| 3 | 000011 | NS+EW | Simple crossing |
| 5 | 000101 | NS+NE | Switch (N or S → E) |
| 9 | 001001 | NS+NW | Switch (N or S → W) |
| 24 | 011000 | NW+SE | Diamond (NW-SE diagonal) |
| 36 | 100100 | NE+SW | Diamond (NE-SW diagonal) |

---

## 4. Migration from RIBI (DEPRECATED)

> **Note:** RIBI is now deprecated and has been removed from the codebase. Use `track_bits` with `EDGE_MASKS` instead.

The old 4-bit RIBI system (Simutrans-style) only tracked which **edges** were connected:
*   N=1, E=2, S=4, W=8 (total: 16 values 0-15)

The new 6-bit system tracks which **segments** exist:
*   NS=1, EW=2, NE=4, NW=8, SE=16, SW=32 (total: 64 values 0-63)

**Key differences:**
*   RIBI can't distinguish NE curve from NW curve (both have N connected)
*   RIBI can't distinguish simple crossing from diamond crossing
*   `track_bits` provides full geometric information

**Conversion (for edge connectivity only):**
```javascript
function trackBitsToEdges(trackBits) {
    const edges = new Set();
    if (trackBits & 13) edges.add('N');  // EDGE_MASK_N
    if (trackBits & 22) edges.add('E');  // EDGE_MASK_E
    if (trackBits & 49) edges.add('S');  // EDGE_MASK_S
    if (trackBits & 42) edges.add('W');  // EDGE_MASK_W
    return edges;
}
```

---

## German-to-English Keyword Translation

| German Term | English Meaning | Description |
|:------------|:----------------|:------------|
| `weg` | Way | Generic term for road/rail/path. |
| `schiene` | Rail | Railway specific implementation. |
| `ribi` | Direction Bits | **DEPRECATED**: Old 4-bit mask. Use `track_bits`. |
| `weiche` | Switch | Railway switch/turnout. |
| `kreuzung` | Crossing | Intersection of two ways. |
| `bild` | Image | Sprite/Graphic. |
| `vordergrund` | Foreground | Top visual layer (wires/bridges). |
| `oberleitung` | Overhead Line | Catenary/Electrification. |
| `schnee` | Snow | Winter textures. |
