# 6-Bit TrackBits System Reference

> **Last Verified:** 2026-01-01

## 1. Overview

### Why 6-Bit?

With **4 edges** (N, E, S, W) on a tile, there are exactly **C(4,2) = 6 possible paths** between any two edges:

```
Number of ways to choose 2 edges from 4:
C(4,2) = 4! / (2! × 2!) = 6
```

Each bit in a 6-bit integer represents one physical rail segment connecting two edges. This replaces the previous `directed_paths: List[DirectedPath]` system with a single integer (0-63) as the **single source of truth**.

### Benefits

| Aspect | Old System (directed_paths) | New System (track_bits) |
|--------|----------------------------|------------------------|
| Storage | List of objects | Single integer |
| Ambiguity | None (explicit) | None (each value = unique geometry) |
| Comparison | Loop through list | Bitwise AND/OR |
| Serialization | JSON array | Single number |
| Switch detection | Count paths per port | Count bits per edge mask |

### Tile Edge Reference

```
              NORTH (N)
                 │
                 │
    WEST (W) ────┼──── EAST (E)
                 │
                 │
              SOUTH (S)
```

---

## 2. Bit Definitions

Each bit represents a physical rail segment connecting two edges:

| Bit | Constant | Value | Binary | Path | Orientation Group |
|-----|----------|-------|--------|------|-------------------|
| 0 | TRACK_NS | 1 | 000001 | N ↔ S | NS |
| 1 | TRACK_EW | 2 | 000010 | E ↔ W | EW |
| 2 | TRACK_NE | 4 | 000100 | N ↔ E | NE_SW |
| 3 | TRACK_NW | 8 | 001000 | N ↔ W | NW_SE |
| 4 | TRACK_SE | 16 | 010000 | S ↔ E | NW_SE |
| 5 | TRACK_SW | 32 | 100000 | S ↔ W | NE_SW |

### ASCII Art for Each Track Bit

```
TRACK_NS (1):       TRACK_EW (2):       TRACK_NE (4):
      N                                       N
      │                                       │
      │              W ─────────── E          └───── E
      │                             
      S                             

TRACK_NW (8):       TRACK_SE (16):      TRACK_SW (32):
      N                                        
      │                                        
W ────┘                      ┌───── E      W ────┐
                             │                   │
                             S                   S
```

### Python Constants

```python
# Track bit constants
TRACK_NS = 1   # 0b000001 - North to South
TRACK_EW = 2   # 0b000010 - East to West
TRACK_NE = 4   # 0b000100 - North to East
TRACK_NW = 8   # 0b001000 - North to West
TRACK_SE = 16  # 0b010000 - South to East
TRACK_SW = 32  # 0b100000 - South to West
```

---

## 3. Edge Masks

For each edge, which bits represent paths connecting to that edge?

| Edge | Connected Tracks | Calculation | Mask (Dec) | Mask (Bin) |
|------|------------------|-------------|------------|------------|
| N | NS, NE, NW | 1 + 4 + 8 | 13 | 001101 |
| E | EW, NE, SE | 2 + 4 + 16 | 22 | 010110 |
| S | NS, SE, SW | 1 + 16 + 32 | 49 | 110001 |
| W | EW, NW, SW | 2 + 8 + 32 | 42 | 101010 |

### Python Constants

```python
# Edge masks - which track bits connect to each edge
EDGE_MASK_N = 13   # 0b001101 - NS, NE, NW
EDGE_MASK_E = 22   # 0b010110 - EW, NE, SE
EDGE_MASK_S = 49   # 0b110001 - NS, SE, SW
EDGE_MASK_W = 42   # 0b101010 - EW, NW, SW

EDGE_MASKS = {'N': 13, 'E': 22, 'S': 49, 'W': 42}
```

### Bit-to-Edges Mapping

For each bit, which two edges does it connect?

```python
BIT_TO_EDGES = {
    0: ('N', 'S'),  # TRACK_NS
    1: ('E', 'W'),  # TRACK_EW
    2: ('N', 'E'),  # TRACK_NE
    3: ('N', 'W'),  # TRACK_NW
    4: ('S', 'E'),  # TRACK_SE
    5: ('S', 'W'),  # TRACK_SW
}
```

---

## 4. All 64 Combinations - Complete Reference Table

### Legend

- **Dec**: Decimal value (0-63)
- **Bin**: Binary representation (6 bits)
- **#P**: Number of paths (popcount)
- **Tracks**: Which track segments are present
- **Edges**: Which edges are connected
- **Type**: Track classification
- **Switches**: Edges with 2+ exits (switch points)
- **Valid**: ✓ = allowed, ✗ = forbidden

---

### Group 0: Empty (1 combination)

| Dec | Bin | #P | Tracks | Edges | Type | Switches | Valid |
|-----|-----|----|----|-------|------|----------|-------|
| 0 | 000000 | 0 | (none) | ∅ | EMPTY | ∅ | ✓ |

---

### Group 1: Single Path (6 combinations)

All single-path configurations are valid. No switches possible.

**Note**: The "Type" column shows the path's orientation group, NOT whether traversal is a curve. Curve/straight is determined by comparing this path's group with the connected neighbor path's group (see Section 6).

| Dec | Bin | #P | Tracks | Edges | Orientation Group | Switches | Valid |
|-----|-----|----|----|-------|-------------------|----------|-------|
| 1 | 000001 | 1 | NS | N,S | NS | ∅ | ✓ |
| 2 | 000010 | 1 | EW | E,W | EW | ∅ | ✓ |
| 4 | 000100 | 1 | NE | N,E | NE_SW | ∅ | ✓ |
| 8 | 001000 | 1 | NW | N,W | NW_SE | ∅ | ✓ |
| 16 | 010000 | 1 | SE | S,E | NW_SE | ∅ | ✓ |
| 32 | 100000 | 1 | SW | S,W | NE_SW | ∅ | ✓ |

---

### Group 2: Two Paths (15 combinations)

Two paths can be:
- **Diamond Crossing**: No shared edge (paths cross but trains cannot switch)
- **Simple Switch (Y-Switch)**: One shared edge (that edge is a switch point)

#### Diamond Crossings (3 combinations) - No Switches

| Dec | Bin | #P | Tracks | Edges | Type | Switches | Valid |
|-----|-----|----|----|-------|------|----------|-------|
| 3 | 000011 | 2 | NS+EW | N,E,S,W | DIAMOND | ∅ | ✓ |
| 24 | 011000 | 2 | NW+SE | N,E,S,W | DIAMOND | ∅ | ✓ |
| 36 | 100100 | 2 | NE+SW | N,E,S,W | DIAMOND | ∅ | ✓ |

```
3 (NS+EW):           24 (NW+SE):          36 (NE+SW):
      N                   N                    N
      │                   │                    │
  W───┼───E           W───┘                    └───E
      │                       └───E        W───┐
      S                   S                    S

Standard cross       Diagonal X           Diagonal X
```

**Important:** Trains on the N-S path cannot switch to the E-W path and vice versa. The paths are physically independent - they cross in space but do not allow path changes. There are no switches at diamond crossings.

#### Simple Switches / Y-Switches (12 combinations) - One Switch Point

| Dec | Bin | #P | Tracks | Edges | Type | Switch Edge | Exits | Valid |
|-----|-----|----|----|-------|------|-------------|-------|-------|
| 5 | 000101 | 2 | NS+NE | N,E,S | SWITCH | N | E,S | ✓ |
| 6 | 000110 | 2 | EW+NE | N,E,W | SWITCH | E | N,W | ✓ |
| 9 | 001001 | 2 | NS+NW | N,S,W | SWITCH | N | S,W | ✓ |
| 10 | 001010 | 2 | EW+NW | N,E,W | SWITCH | W | E,N | ✓ |
| 12 | 001100 | 2 | NE+NW | N,E,W | SWITCH | N | E,W | ✓ |
| 17 | 010001 | 2 | NS+SE | N,E,S | SWITCH | S | E,N | ✓ |
| 18 | 010010 | 2 | EW+SE | E,S,W | SWITCH | E | S,W | ✓ |
| 20 | 010100 | 2 | NE+SE | N,E,S | SWITCH | E | N,S | ✓ |
| 33 | 100001 | 2 | NS+SW | N,S,W | SWITCH | S | N,W | ✓ |
| 34 | 100010 | 2 | EW+SW | E,S,W | SWITCH | W | E,S | ✓ |
| 40 | 101000 | 2 | NW+SW | N,S,W | SWITCH | W | N,S | ✓ |
| 48 | 110000 | 2 | SE+SW | E,S,W | SWITCH | S | E,W | ✓ |

```
Example: 5 (NS+NE) - Switch at N, exits to E or S

      N ← Switch point
      ├───E
      │
      S

Train entering from N: switch decides → E or S
Train entering from S: must exit → N
Train entering from E: must exit → N
```

---

### Group 3: Three Paths (20 combinations)

Three paths can form:
- **WYE Junction**: 3 edges fully interconnected (every edge is a switch) - **VALID**
- **3-Way Switch**: 1 edge connects to 3 others - **FORBIDDEN**
- **Complex**: 2 switches at different edges - **VALID**

#### WYE Junctions (4 combinations) - All Valid

A WYE has exactly 3 edges, each connected to the other two. Every edge is a switch point.

| Dec | Bin | #P | Tracks | Edges | Type | Switches | Valid |
|-----|-----|----|----|-------|------|----------|-------|
| 14 | 001110 | 3 | EW+NE+NW | N,E,W | WYE | N,E,W | ✓ |
| 21 | 010101 | 3 | NS+NE+SE | N,E,S | WYE | N,E,S | ✓ |
| 41 | 101001 | 3 | NS+NW+SW | N,S,W | WYE | N,S,W | ✓ |
| 50 | 110010 | 3 | EW+SE+SW | E,S,W | WYE | E,S,W | ✓ |

```
14 (WYE N-E-W):      21 (WYE N-E-S):      41 (WYE N-S-W):      50 (WYE E-S-W):
      N                    N                    N                         
      │                    │                    │                         
  W───┼───E                ├───E            W───┤                  W───┬───E
                           │                    │                      │
                           S                    S                      S
```

#### 3-Way Switches (4 combinations) - FORBIDDEN

One edge has 3 paths (connects to all other edges). **Blocked: No physical 3-way switch exists.**

| Dec | Bin | #P | Tracks | Edges | Type | 3-Way Edge | Valid | Blocked Reason |
|-----|-----|----|----|-------|------|------------|-------|----------------|
| 13 | 001101 | 3 | NS+NE+NW | N,E,S,W | 3-WAY | N→(E,S,W) | ✗ | No 3-way switch hardware |
| 22 | 010110 | 3 | EW+NE+SE | N,E,S,W | 3-WAY | E→(N,S,W) | ✗ | No 3-way switch hardware |
| 42 | 101010 | 3 | EW+NW+SW | N,E,S,W | 3-WAY | W→(N,E,S) | ✗ | No 3-way switch hardware |
| 49 | 110001 | 3 | NS+SE+SW | N,E,S,W | 3-WAY | S→(N,E,W) | ✗ | No 3-way switch hardware |

```
Example: 13 (3-way at N) - FORBIDDEN

      N ← 3-way switch (impossible!)
     /│\
    W │ E
      │
      S

Real-world solution: Use two adjacent tiles with 2-way switches
```

#### Complex 3-Path Junctions (8 valid, 4 forbidden)

These have 2 switch points at different edges. The 8 valid ones have at least one axial path (NS or EW).

| Dec | Bin | #P | Tracks | Edges | Type | Switches | Valid |
|-----|-----|----|----|-------|------|----------|-------|
| 7 | 000111 | 3 | NS+EW+NE | N,E,S,W | COMPLEX | N(E,S), E(N,W) | ✓ |
| 11 | 001011 | 3 | NS+EW+NW | N,E,S,W | COMPLEX | N(S,W), W(E,N) | ✓ |
| 19 | 010011 | 3 | NS+EW+SE | N,E,S,W | COMPLEX | S(N,E), E(S,W) | ✓ |
| 25 | 011001 | 3 | NS+NW+SE | N,E,S,W | COMPLEX | N(S,W), S(N,E) | ✓ |
| 35 | 100011 | 3 | NS+EW+SW | N,E,S,W | COMPLEX | S(N,W), W(E,S) | ✓ |
| 37 | 100101 | 3 | NS+NE+SW | N,E,S,W | COMPLEX | N(E,S), S(N,W) | ✓ |
| 38 | 100110 | 3 | EW+NE+SW | N,E,S,W | COMPLEX | E(N,W), W(E,S) | ✓ |
| 26 | 011010 | 3 | EW+NW+SE | N,E,S,W | COMPLEX | E(S,W), W(E,N) | ✓ |

#### Multi-Diagonal 3-Path (4 combinations) - FORBIDDEN

All diagonal paths with no axial (NS/EW) through route. These lack a main route.

| Dec | Bin | #P | Tracks | Edges | Type | Switches | Valid | Blocked Reason |
|-----|-----|----|----|-------|------|----------|-------|----------------|
| 28 | 011100 | 3 | NE+NW+SE | N,E,S,W | MULTI-DIAG | N(E,W), E(N,S) | ✗ | No through route |
| 44 | 101100 | 3 | NE+NW+SW | N,E,S,W | MULTI-DIAG | N(E,W), W(N,S) | ✗ | No through route |
| 52 | 110100 | 3 | NE+SE+SW | N,E,S,W | MULTI-DIAG | E(N,S), S(E,W) | ✗ | No through route |
| 56 | 111000 | 3 | NW+SE+SW | N,E,S,W | MULTI-DIAG | S(E,W), W(N,S) | ✗ | No through route |

---

### Group 4: Four Paths (15 combinations)

#### Double Slips (4 combinations) - FORBIDDEN

A double slip has a crossing plus switches at two opposite edges. Each switch edge has 3 exits.

| Dec | Bin | #P | Tracks | 3-Way Edges | Valid | Blocked Reason |
|-----|-----|----|----|-------------|-------|----------------|
| 15 | 001111 | 4 | NS+EW+NE+NW | N→(E,S,W) | ✗ | Contains 3-way at N |
| 23 | 010111 | 4 | NS+EW+NE+SE | E→(N,S,W) | ✗ | Contains 3-way at E |
| 43 | 101011 | 4 | NS+EW+NW+SW | W→(N,E,S) | ✗ | Contains 3-way at W |
| 51 | 110011 | 4 | NS+EW+SE+SW | S→(N,E,W) | ✗ | Contains 3-way at S |

#### Scissors Crossovers (2 combinations) - Valid

Diagonal crossing plus switches. No edge has 3+ exits.

| Dec | Bin | #P | Tracks | Type | Switches | Valid |
|-----|-----|----|----|-------|----------|-------|
| 27 | 011011 | 4 | NS+EW+NW+SE | SCISSORS | N(S,W), E(S,W), S(N,E), W(N,E) | ✓ |
| 39 | 100111 | 4 | NS+EW+NE+SW | SCISSORS | N(E,S), E(N,W), S(N,W), W(E,S) | ✓ |

#### Double Cross (1 combination) - FORBIDDEN

All 4 curves, no straights. Each edge has exactly 2 exits but no through route.

| Dec | Bin | #P | Tracks | Type | Switches | Valid | Blocked Reason |
|-----|-----|----|----|-------|----------|-------|----------------|
| 60 | 111100 | 4 | NE+NW+SE+SW | DOUBLE_CROSS | N(E,W), E(N,S), S(E,W), W(N,S) | ✗ | No through route |

#### Complex 4-Path (8 combinations) - FORBIDDEN

These contain at least one edge with 3 exits.

| Dec | Bin | #P | Tracks | 3-Way Edge(s) | Valid | Blocked Reason |
|-----|-----|----|----|---------------|-------|----------------|
| 29 | 011101 | 4 | NS+NE+NW+SE | N→(E,S,W) | ✗ | Contains 3-way at N |
| 30 | 011110 | 4 | EW+NE+NW+SE | N→(E,W), E→(N,S,W) | ✗ | Contains 3-way at E |
| 45 | 101101 | 4 | NS+NE+NW+SW | N→(E,S,W) | ✗ | Contains 3-way at N |
| 46 | 101110 | 4 | EW+NE+NW+SW | N→(E,W), W→(N,E,S) | ✗ | Contains 3-way at W |
| 53 | 110101 | 4 | NS+NE+SE+SW | E→(N,S), S→(N,E,W) | ✗ | Contains 3-way at S |
| 54 | 110110 | 4 | EW+NE+SE+SW | E→(N,S,W) | ✗ | Contains 3-way at E |
| 57 | 111001 | 4 | NS+NW+SE+SW | S→(N,E,W) | ✗ | Contains 3-way at S |
| 58 | 111010 | 4 | EW+NW+SE+SW | W→(N,E,S) | ✗ | Contains 3-way at W |

---

### Group 5: Five Paths (6 combinations) - ALL FORBIDDEN

Every 5-path configuration has multiple edges with 3 exits.

| Dec | Bin | #P | Missing Track | 3-Way Edges | Valid | Blocked Reason |
|-----|-----|----|----|-------------|-------|----------------|
| 31 | 011111 | 5 | SW | N(E,S,W), E(N,S,W) | ✗ | Multiple 3-way switches |
| 47 | 101111 | 5 | SE | N(E,S,W), W(N,E,S) | ✗ | Multiple 3-way switches |
| 55 | 110111 | 5 | NW | E(N,S,W), S(N,E,W) | ✗ | Multiple 3-way switches |
| 59 | 111011 | 5 | NE | S(N,E,W), W(N,E,S) | ✗ | Multiple 3-way switches |
| 61 | 111101 | 5 | EW | N(E,S,W), S(N,E,W) | ✗ | Multiple 3-way switches |
| 62 | 111110 | 5 | NS | E(N,S,W), W(N,E,S) | ✗ | Multiple 3-way switches |

---

### Group 6: All Six Paths (1 combination) - FORBIDDEN

| Dec | Bin | #P | Tracks | 3-Way Edges | Valid | Blocked Reason |
|-----|-----|----|----|-------------|-------|----------------|
| 63 | 111111 | 6 | ALL | N,E,S,W all 3-way | ✗ | Every edge is 3-way |

---

## 5. Forbidden Combinations Summary

### Blocked Configurations (28 total)

| Category | Values | Count | Reason |
|----------|--------|-------|--------|
| 3-Way Switches | 13, 22, 42, 49 | 4 | Single edge with 3 exits - no physical hardware |
| Double Slips | 15, 23, 43, 51 | 4 | Cross + 3-way at one edge |
| Complex 4-Path | 29, 30, 45, 46, 53, 54, 57, 58 | 8 | Contains at least one 3-way |
| Multi-Diagonal (no through route) | 28, 44, 52, 56, 60 | 5 | All diagonal paths with no axial (NS/EW) through route |
| 5-Path | 31, 47, 55, 59, 61, 62 | 6 | Multiple 3-way switches |
| 6-Path (Full) | 63 | 1 | All edges are 3-way |
| **Total Forbidden** | | **28** | |

### Valid Configurations (36 total)

| Category | Values | Count |
|----------|--------|-------|
| Empty | 0 | 1 |
| Single Path | 1, 2, 4, 8, 16, 32 | 6 |
| Diamond (no switch) | 3, 24, 36 | 3 |
| Y-Switch (1 switch) | 5, 6, 9, 10, 12, 17, 18, 20, 33, 34, 40, 48 | 12 |
| WYE (3 switches) | 14, 21, 41, 50 | 4 |
| Complex 3-Path (2 switches) | 7, 11, 19, 25, 26, 35, 37, 38 | 8 |
| Scissors (4 switches) | 27, 39 | 2 |
| **Total Valid** | | **36** |

### Validation Function

```python
# All forbidden track_bits values
FORBIDDEN_TRACK_BITS = frozenset({
    # 3-way switches (3 paths, one edge has all 3)
    13, 22, 42, 49,
    # Double slips (4 paths, cross + 3-way)
    15, 23, 43, 51,
    # Complex 4-path with 3-way
    29, 30, 45, 46, 53, 54, 57, 58,
    # Multi-diagonal without through route
    28, 44, 52, 56, 60,
    # 5-path (multiple 3-ways)
    31, 47, 55, 59, 61, 62,
    # 6-path (full junction)
    63
})

def is_valid_track_bits(track_bits: int) -> bool:
    """Check if track_bits represents a valid configuration."""
    return track_bits not in FORBIDDEN_TRACK_BITS

def get_forbidden_reason(track_bits: int) -> Optional[str]:
    """Get the reason why a track_bits value is forbidden."""
    if track_bits in {13, 22, 42, 49}:
        return "3-way switch at single edge - no physical hardware exists"
    if track_bits in {15, 23, 43, 51}:
        return "Double slip contains 3-way switch"
    if track_bits in {29, 30, 45, 46, 53, 54, 57, 58}:
        return "Complex junction contains 3-way switch"
    if track_bits in {28, 44, 52, 56, 60}:
        return "Multi-diagonal without through route - all diagonal paths, no NS/EW"
    if track_bits in {31, 47, 55, 59, 61, 62}:
        return "Multiple 3-way switches required"
    if track_bits == 63:
        return "Full junction - every edge would need 3-way switch"
    return None
```

---

## 6. Track Type Classification

### Algorithm

```python
from enum import Enum

class TrackType(Enum):
    EMPTY = "EMPTY"
    SINGLE_PATH = "SINGLE_PATH"  # Any single path (curve/straight determined by neighbor)
    DIAMOND = "DIAMOND"
    SWITCH = "SWITCH"
    WYE = "WYE"
    COMPLEX = "COMPLEX"
    SCISSORS = "SCISSORS"
    DOUBLE_CROSS = "DOUBLE_CROSS"
    FORBIDDEN = "FORBIDDEN"

def classify_track_type(track_bits: int) -> TrackType:
    """Classify track geometry based on track_bits.

    IMPORTANT: This is geometry classification only.
    Whether a traversal is CURVE or STRAIGHT is determined by comparing
    orientation groups between connected tiles, NOT by the path shape.
    See is_curve_connection() for curve detection.
    """

    if track_bits in FORBIDDEN_TRACK_BITS:
        return TrackType.FORBIDDEN

    num_paths = bin(track_bits).count('1')

    if num_paths == 0:
        return TrackType.EMPTY

    if num_paths == 1:
        # All single paths return SINGLE_PATH.
        # Curve/straight is determined by neighbor connection, not path shape.
        return TrackType.SINGLE_PATH

    if num_paths == 2:
        # Diamonds have no switches (no shared edge)
        if track_bits in (3, 24, 36):
            return TrackType.DIAMOND
        return TrackType.SWITCH

    if num_paths == 3:
        # WYE: exactly 3 edges, each connected to other two
        if track_bits in (14, 21, 41, 50):
            return TrackType.WYE
        return TrackType.COMPLEX

    if num_paths == 4:
        if track_bits in (27, 39):
            return TrackType.SCISSORS
        if track_bits == 60:
            return TrackType.DOUBLE_CROSS
        return TrackType.COMPLEX  # Should not reach for valid configs

    return TrackType.FORBIDDEN  # 5+ paths
```

### Orientation Groups and Curve Detection

**Key Concept**: `TrackType` above is a geometry label only. Whether a traversal is a CURVE or STRAIGHT is decided by comparing orientation groups between **connected tiles**.

#### The 4 Orientation Groups

| Group | Paths in Group |
|-------|----------------|
| NS | North-South |
| EW | East-West |
| NE_SW | North-East, South-West |
| NW_SE | North-West, South-East |

```python
ORIENTATION_GROUPS = {
    frozenset({'N', 'S'}): 'NS',
    frozenset({'E', 'W'}): 'EW',
    frozenset({'N', 'E'}): 'NE_SW',
    frozenset({'S', 'W'}): 'NE_SW',
    frozenset({'N', 'W'}): 'NW_SE',
    frozenset({'S', 'E'}): 'NW_SE',
}
```

#### Curve Detection Rule

**A connection is a CURVE when the two connected paths are in DIFFERENT orientation groups.**
**A connection is STRAIGHT when the two connected paths are in the SAME orientation group.**

This applies to the connection between two tiles, not to a single tile in isolation.

#### Examples

| Current Tile Path | Neighbor Path | Current Group | Neighbor Group | Result |
|-------------------|---------------|---------------|----------------|--------|
| NW | SE | NW_SE | NW_SE | **STRAIGHT** (same group) |
| NW | EW | NW_SE | EW | **CURVE** (different groups) |
| NW | NS | NW_SE | NS | **CURVE** (different groups) |
| NS | NS | NS | NS | **STRAIGHT** (same group) |
| NS | NE | NS | NE_SW | **CURVE** (different groups) |
| EW | EW | EW | EW | **STRAIGHT** (same group) |
| NE | SW | NE_SW | NE_SW | **STRAIGHT** (same group) |

#### Curve Detection Algorithm

```python
def is_curve_connection(current_path_edges: tuple, neighbor_path_edges: tuple) -> bool:
    """
    Determine if the connection between two paths is a curve.

    Args:
        current_path_edges: (edge1, edge2) of path in current tile
        neighbor_path_edges: (edge1, edge2) of path in neighbor tile

    Returns:
        True if this is a CURVE (different orientation groups)
        False if this is STRAIGHT (same orientation group)
    """
    current_group = ORIENTATION_GROUPS.get(frozenset(current_path_edges))
    neighbor_group = ORIENTATION_GROUPS.get(frozenset(neighbor_path_edges))

    return current_group != neighbor_group
```

#### Python Helpers

- `ORIENTATION_GROUPS`: Maps edge pairs to their orientation group
- `TrackSegment.get_path_group(edge1, edge2)`: Get orientation group for a path
- `TrackSegment.is_curve_connection(exit_edge, neighbor_segment)`: Check if connection is a curve
- `TrackSegment.get_curve_type_at_edge(exit_edge, neighbor_segment)`: Returns 'CURVE', 'STRAIGHT', or 'UNKNOWN'

### Classification Table

| track_bits | Type | Orientation Group | Note |
|------------|------|-------------------|------|
| 0 | EMPTY | - | No paths |
| 1 | SINGLE_PATH | NS | Curve/straight determined by neighbor |
| 2 | SINGLE_PATH | EW | Curve/straight determined by neighbor |
| 4 | SINGLE_PATH | NE_SW | Curve/straight determined by neighbor |
| 8 | SINGLE_PATH | NW_SE | Curve/straight determined by neighbor |
| 16 | SINGLE_PATH | NW_SE | Curve/straight determined by neighbor |
| 32 | SINGLE_PATH | NE_SW | Curve/straight determined by neighbor |
| 3, 24, 36 | DIAMOND | - | Two paths, no shared edge |
| 5, 6, 9, 10, 12, 17, 18, 20, 33, 34, 40, 48 | SWITCH | - | Two paths, one shared edge |
| 14, 21, 41, 50 | WYE | - | Three paths, three edges |
| 7, 11, 19, 25, 26, 28, 35, 37, 38, 44, 52, 56 | COMPLEX | - | Three paths, two switches |
| 27, 39 | SCISSORS | - | Four paths, scissors crossover |
| 60 | DOUBLE_CROSS | - | Four diagonal paths |

---

## 7. Switch Detection

A **switch point** exists at an edge when 2 or more paths connect to that edge.

### Algorithm

```python
def get_switch_points(track_bits: int) -> Dict[str, List[str]]:
    """
    Compute switch points from track_bits.
    
    Returns:
        Dict mapping each switch edge to its sorted list of exit edges.
        Only edges with 2+ exits are included.
    
    Example:
        get_switch_points(5)  # NS+NE
        # Returns: {'N': ['E', 'S']}
        # N connects to both E (via NE) and S (via NS)
    """
    EDGE_MASKS = {'N': 13, 'E': 22, 'S': 49, 'W': 42}
    
    BIT_TO_EDGES = {
        0: ('N', 'S'),  # TRACK_NS
        1: ('E', 'W'),  # TRACK_EW
        2: ('N', 'E'),  # TRACK_NE
        3: ('N', 'W'),  # TRACK_NW
        4: ('S', 'E'),  # TRACK_SE
        5: ('S', 'W'),  # TRACK_SW
    }
    
    # Build exits for each edge
    edge_exits: Dict[str, List[str]] = {e: [] for e in 'NESW'}
    
    for bit in range(6):
        if track_bits & (1 << bit):
            edge1, edge2 = BIT_TO_EDGES[bit]
            edge_exits[edge1].append(edge2)
            edge_exits[edge2].append(edge1)
    
    # Return only edges with 2+ exits (switch points)
    # Sort exits alphabetically for consistent ordering
    return {
        edge: sorted(exits) 
        for edge, exits in edge_exits.items() 
        if len(exits) >= 2
    }
```

### Examples

```python
# track_bits = 5 (NS + NE)
# N: exits to S (via NS), E (via NE) → 2 exits → SWITCH
# E: exits to N (via NE) → 1 exit → not a switch
# S: exits to N (via NS) → 1 exit → not a switch
get_switch_points(5)
# Returns: {'N': ['E', 'S']}

# track_bits = 21 (NS + NE + SE) - WYE
# N: exits to S, E → SWITCH
# E: exits to N, S → SWITCH  
# S: exits to N, E → SWITCH
get_switch_points(21)
# Returns: {'N': ['E', 'S'], 'E': ['N', 'S'], 'S': ['E', 'N']}

# track_bits = 3 (NS + EW) - DIAMOND
# N: exits to S → 1 exit
# E: exits to W → 1 exit
# S: exits to N → 1 exit
# W: exits to E → 1 exit
get_switch_points(3)
# Returns: {} (no switches - it's a diamond crossing)
```

### Edge Connectivity

```python
def get_exits(track_bits: int, entry_edge: str) -> List[str]:
    """Get all possible exit edges for a given entry edge."""
    BIT_TO_EDGES = {
        0: ('N', 'S'), 1: ('E', 'W'), 2: ('N', 'E'),
        3: ('N', 'W'), 4: ('S', 'E'), 5: ('S', 'W'),
    }
    
    exits = []
    for bit in range(6):
        if track_bits & (1 << bit):
            e1, e2 = BIT_TO_EDGES[bit]
            if entry_edge == e1:
                exits.append(e2)
            elif entry_edge == e2:
                exits.append(e1)
    return sorted(exits)

def get_edges(track_bits: int) -> Set[str]:
    """Get all connected edges."""
    EDGE_MASKS = {'N': 13, 'E': 22, 'S': 49, 'W': 42}
    return {edge for edge, mask in EDGE_MASKS.items() if track_bits & mask}
```

---

## 8. Switch State Structure

The current implementation uses a **global per-tile switch state** rather than per-edge states.

### Structure

```python
_switch_state: int  # 0 = MAIN, 1 = DIVERGE
```

- **0 (MAIN)**: All switches on the tile route to "long" paths (NS or EW segments)
- **1 (DIVERGE)**: All switches on the tile route to "short" paths (diagonal segments)

This simplified model synchronizes all switches on a tile, which works well for most common switch configurations.

### Examples

```python
# track_bits = 5 (NS+NE) - Switch at N
# MAIN (0): N → S (via NS, the long path)
# DIVERGE (1): N → E (via NE, the short/diagonal path)
switch_state = 0  # Train from N exits to S
switch_state = 1  # Train from N exits to E

# track_bits = 7 (NS+EW+NE) - Complex with switches at N and E
# MAIN (0): All switches prefer long paths
#   N → S (via NS), E → W (via EW)
# DIVERGE (1): All switches prefer short paths
#   N → E (via NE), E → N (via NE)
switch_state = 0  # Through routes active
switch_state = 1  # Diagonal routes active
```

### Diagonal-Only Switches

For switches with **only diagonal paths** (no NS or EW), the MAIN/DIVERGE distinction uses **clockwise ordering** instead:

```python
CLOCKWISE_ORDER = ['N', 'E', 'S', 'W']

# track_bits = 12 (NE+NW) - Switch at N with exits to E and W
# Since both are diagonal, use clockwise order:
# MAIN (0): N → E (first in clockwise order)
# DIVERGE (1): N → W (second in clockwise order)
```

The `is_diagonal_only_switch` property identifies these cases.

### Get Active Exit

```python
def get_exits(self, entry_edge: str, use_switch_state: bool = True) -> List[str]:
    """Return exit edges for a given entry edge.

    Uses global switch_state if this is a switch tile:
    - 0 (MAIN): returns exit on long path (NS or EW) if available
    - 1 (DIVERGE): returns exit on short path (diagonals)

    For diagonal-only switches, uses clockwise ordering instead.
    """
    # ... (see track.py for full implementation)
```

### Toggle Switch

```python
def toggle_switch(self) -> bool:
    """Toggle global switch state between MAIN (0) and DIVERGE (1)."""
    if not self.is_switch:
        return False
    self._switch_state = 1 - self._switch_state
    return True
```

### Initialization

When `track_bits` changes, `switch_state` is automatically reset to 0 (MAIN) via `_sync_switch_states()`.

### Display Property

For UI display, the `switch_states` property returns a dict mapping each switch edge to the current state:

```python
@property
def switch_states(self) -> Dict[str, int]:
    """Per-entry-port switch states for display/debugging."""
    if not self.is_switch:
        return {}
    return {entry: self._switch_state for entry in self.switch_points.keys()}
```

---

## 9. Dead-End Handling

A **dead-end** (buffer stop) occurs when a path edge has no connected neighbor tile or the neighbor doesn't connect back.

### Design: Automatic Detection via Propagation

Dead-ends are **automatically detected** and stored as a cached field updated via the propagation system.

**Data Structure:**
```python
dead_end_ports: Set[str]  # Set of edge letters: {'N', 'E', 'S', 'W'}
```

> **DEPRECATED**: The `add_dead_end()` method is deprecated. Dead-ends are now auto-detected during network propagation. Setting them manually is no longer necessary or recommended.

### Dead-End Detection Algorithm

Dead-ends are automatically recomputed during propagation when:
1. A tile's `track_bits` changes
2. A neighbor tile is added/removed/modified

**For each edge in the tile's path edges:**
```
1. Get the neighbor tile in that direction
2. If no neighbor exists → edge is dead-end
3. If neighbor exists:
   - Check if neighbor has a path connecting back to this edge
   - If neighbor doesn't connect → edge is dead-end
   - If neighbor connects → edge is NOT a dead-end
```

### Example

```
Initial: Single tile at (5,5) with track_bits=2 (EW)
→ dead_end_ports = {'E', 'W'} (no neighbors)

Add tile at (6,5) with track_bits=2 (EW):
→ Tile (5,5): dead_end_ports = {'W'} (East now connects to neighbor)
→ Tile (6,5): dead_end_ports = {'E'} (West connects to (5,5), East has no neighbor)
```

### Constraints

**Correct constraint:** An edge is in `dead_end_ports` if:
- The edge has a path (bit set in `track_bits`), AND
- No neighbor connects back to that edge

### Validation

```python
def validate_dead_ends(track_bits: int, dead_end_ports: Set[str]) -> bool:
    """Validate that dead-ends are only on edges that have paths."""
    connected_edges = get_edges(track_bits)

    # All dead-ends must be on edges with paths
    for port in dead_end_ports:
        if port not in connected_edges:
            return False  # Dead-end on edge with no path - invalid
    return True
```

### RIBI Computation (DEPRECATED)

> **DEPRECATED**: RIBI is deprecated and has been removed from the codebase. Use `track_bits` and `EDGE_MASKS` directly instead. RIBI only tracked which edges were connected (4 bits, 16 values), while `track_bits` tracks which segments exist (6 bits, 64 values), providing complete geometric information.

For reference, the old RIBI formula included all edges that have paths:

```python
# DEPRECATED - Use track_bits and EDGE_MASKS instead
# EDGE_TO_RIBI = {'N': 1, 'E': 2, 'S': 4, 'W': 8}
#
# def compute_ribi(track_bits: int) -> int:
#     edges = get_edges(track_bits)
#     return sum(EDGE_TO_RIBI[e] for e in edges)
```

---

## 10. Common Configurations Quick Reference

### Single Paths (Orientation Groups)

Whether traversal is a curve depends on the **neighbor's orientation group**, not local shape.

| Name | track_bits | Binary | Orientation Group | Diagram |
|------|------------|--------|-------------------|---------|
| Vertical (N-S) | 1 | 000001 | NS | `│` |
| Horizontal (E-W) | 2 | 000010 | EW | `─` |
| N to E | 4 | 000100 | NE+SW | `└` |
| N to W | 8 | 001000 | NW+SE | `┘` |
| S to E | 16 | 010000 | NW+SE | `┌` |
| S to W | 32 | 100000 | NE+SW | `┐` |

**Curve Examples:**
- NS (group NS) → neighbor NE (group NE+SW) = **CURVE** (groups differ)
- NW (group NW+SE) → neighbor SE (group NW+SE) = **STRAIGHT** (same group)
- EW (group EW) → neighbor EW (group EW) = **STRAIGHT** (same group)
- NE (group NE+SW) → neighbor NS (group NS) = **CURVE** (groups differ)

### Diamond Crossings (No Switches)

| Name | track_bits | Binary | Paths |
|------|------------|--------|-------|
| Standard Cross | 3 | 000011 | N↔S, E↔W |
| Diagonal X (NW-SE) | 24 | 011000 | N↔W, S↔E |
| Diagonal X (NE-SW) | 36 | 100100 | N↔E, S↔W |

### Y-Switches (Common Turnouts)

| Description | track_bits | Binary | Switch | Exits |
|-------------|------------|--------|--------|-------|
| Straight + diverge NE | 5 | 000101 | N | E, S |
| Straight + diverge NW | 9 | 001001 | N | S, W |
| Straight + diverge SE | 17 | 010001 | S | E, N |
| Straight + diverge SW | 33 | 100001 | S | N, W |
| Horizontal + diverge N from E | 6 | 000110 | E | N, W |
| Horizontal + diverge S from E | 18 | 010010 | E | S, W |
| Horizontal + diverge N from W | 10 | 001010 | W | E, N |
| Horizontal + diverge S from W | 34 | 100010 | W | E, S |
| Curve N-E + Curve N-W | 12 | 001100 | N | E, W |
| Curve S-E + Curve S-W | 48 | 110000 | S | E, W |
| Curve N-E + Curve S-E | 20 | 010100 | E | N, S |
| Curve N-W + Curve S-W | 40 | 101000 | W | N, S |

### WYE Junctions

| Edges | track_bits | Binary | All Switches |
|-------|------------|--------|--------------|
| N, E, W | 14 | 001110 | N, E, W |
| N, E, S | 21 | 010101 | N, E, S |
| N, S, W | 41 | 101001 | N, S, W |
| E, S, W | 50 | 110010 | E, S, W |

### Complex Junctions

| track_bits | Paths | Switches |
|------------|-------|----------|
| 27 | NS+EW+NW+SE | N(S,W), E(S,W), S(E,N), W(E,N) |
| 39 | NS+EW+NE+SW | N(E,S), E(N,W), S(N,W), W(E,S) |
| 60 | NE+NW+SE+SW | N(E,W), E(N,S), S(E,W), W(N,S) |

---

## 11. Demo Migration Map

Current demo configurations mapped to new `track_bits` values:

### Dead-End Configurations

| Current | New | Description |
|---------|-----|-------------|
| `ribi=2, dead_end` | `track_bits=0, dead_end_ports={'E'}` | Buffer stop facing East |
| `ribi=8, dead_end` | `track_bits=0, dead_end_ports={'W'}` | Buffer stop facing West |
| `ribi=4, dead_end` | `track_bits=0, dead_end_ports={'S'}` | Buffer stop facing South |
| `ribi=1, dead_end` | `track_bits=0, dead_end_ports={'N'}` | Buffer stop facing North |

### Straight Track

| Current | New | Description |
|---------|-----|-------------|
| `ribi=10` (E+W) | `track_bits=2` | EW path (group: EW) |
| `ribi=5` (N+S) | `track_bits=1` | NS path (group: NS) |

### Diagonal Paths

| Current | New | Description |
|---------|-----|-------------|
| `ribi=3` (N+E) | `track_bits=4` | NE path (group: NE+SW) |
| `ribi=9` (N+W) | `track_bits=8` | NW path (group: NW+SE) |
| `ribi=6` (E+S) | `track_bits=16` | SE path (group: NW+SE) |
| `ribi=12` (S+W) | `track_bits=32` | SW path (group: NE+SW) |

### Junction/Switch at (3,0)

Current: `ribi=11` (N+E+W) with paths `[('E','W'), ('N','W')]`

New: `track_bits=10` (TRACK_EW + TRACK_NW = 2 + 8)
- Switch at W with exits [E, N]
- `switch_states = {'W': 0}` for main (W→E), or `{'W': 1}` for diverge (W→N)

### Code Migration Example

```python
# OLD: Creating a junction
segment = TrackSegment(x=3, y=0)
segment.add_path('EAST', 'WEST')
segment.add_path('NORTH', 'WEST')

# NEW: Creating same junction
segment = TrackSegment(
    x=3, y=0,
    track_bits=10,  # TRACK_EW | TRACK_NW
    switch_states={'W': 0}  # Main route: W→E
)
```

---

## 12. Engineering Checklist (handoff)

1. **Store geometry**: Single integer `track_bits` (0-63). Reject if in `FORBIDDEN_TRACK_BITS` (28 values).
2. **Dead-ends**: Cached field `dead_end_ports: Set[str]` auto-detected via propagation. Dead-ends occur when a path edge has no connecting neighbor. The `add_dead_end()` method is deprecated.
3. **Edges**: `edges = {edge for edge, mask in EDGE_MASKS.items() if track_bits & mask}`. RIBI is deprecated - use `track_bits` and `EDGE_MASKS` directly.
4. **Switch points**: For each set bit, add exits to both edges; keep only edges with `len(exits) ≥ 2`.
5. **Switch state**: Global per-tile `_switch_state: int` (0=MAIN, 1=DIVERGE). MAIN routes to long paths (NS/EW), DIVERGE routes to short paths (diagonals). For diagonal-only switches, uses clockwise ordering.
6. **Classification**: Use `classify_track_type(track_bits)` table; feature overrides use `signal_ids` for signals and `station_id` for stations.
7. **Curve detection**: Curves are orientation changes between traversals, considering the neighbor tile. Orientation groups: NS, EW, NE+SW, NW+SE. A traversal is a turn if the local path's group differs from the connected neighbor path's group; both segments take turn cost when groups differ.
8. **Pathfinding exits**: `get_exits(track_bits, entry_edge)` returns sorted exits; apply switch state to pick one for movement. Ensure the next tile accepts the incoming edge.
9. **Serialization**: Persist `track_bits`, `dead_end_ports`, `switch_state`, feature flags; compute the rest (type, edges, switch_points, num_paths, is_valid) on read.

---

## 13. Propagation and Cache Refresh

- Trigger: Any change to `track_bits` or `switch_states` on a tile, OR when a neighbor tile is added/removed/modified.
- Local recompute: Rebuild derived data (switch points, `path_types`, exits, `ribi`, classification) from the tile's single source of truth.
- Dead-end recomputation: Each tile checks its neighbors to determine which path edges are dead-ends (no connection). This happens during the propagation wave.
- DNS-style wave: Enqueue the four neighbors (N,E,S,W). For each dequeued neighbor, recompute its derived data if its connectivity or inbound acceptance changes; if it changes, enqueue its neighbors. Stop when the queue drains.
- Cache refresh: After the wave completes, rebuild or invalidate cached routing graphs/spatial indices covering the affected tiles so pathfinding sees consistent topology.
- Rationale: Prevents stale exits/turn costs when a neighbor adds/removes a path or dead-end, and avoids global recomputes for local edits.

## Appendix A: Full Valid Configurations Reference (36 total)

| Dec | Type | Tracks | Switches | Description |
|-----|------|--------|----------|-------------|
| 0 | EMPTY | - | - | Empty tile |
| 1 | SINGLE_PATH | NS | - | NS path (group: NS) |
| 2 | SINGLE_PATH | EW | - | EW path (group: EW) |
| 3 | DIAMOND | NS+EW | - | Standard crossing |
| 4 | SINGLE_PATH | NE | - | NE path (group: NE+SW) |
| 5 | SWITCH | NS+NE | N | Y-switch, N stem |
| 6 | SWITCH | EW+NE | E | Y-switch, E branch |
| 7 | COMPLEX | NS+EW+NE | N,E | Cross + NE |
| 8 | SINGLE_PATH | NW | - | NW path (group: NW+SE) |
| 9 | SWITCH | NS+NW | N | Y-switch, N stem |
| 10 | SWITCH | EW+NW | W | Y-switch, W branch |
| 11 | COMPLEX | NS+EW+NW | N,W | Cross + NW |
| 12 | SWITCH | NE+NW | N | Y-switch, N diverge |
| 14 | WYE | EW+NE+NW | N,E,W | WYE junction |
| 16 | SINGLE_PATH | SE | - | SE path (group: NW+SE) |
| 17 | SWITCH | NS+SE | S | Y-switch, S stem |
| 18 | SWITCH | EW+SE | E | Y-switch, E branch |
| 19 | COMPLEX | NS+EW+SE | S,E | Cross + SE |
| 20 | SWITCH | NE+SE | E | Y-switch, E diverge |
| 21 | WYE | NS+NE+SE | N,E,S | WYE junction |
| 24 | DIAMOND | NW+SE | - | Diagonal X |
| 25 | COMPLEX | NS+NW+SE | N,S | Diagonal cross + NS |
| 26 | COMPLEX | EW+NW+SE | E,W | Diagonal cross + EW |
| 27 | SCISSORS | NS+EW+NW+SE | N,E,S,W | Scissors crossover |
| 32 | SINGLE_PATH | SW | - | SW path (group: NE+SW) |
| 33 | SWITCH | NS+SW | S | Y-switch, S stem |
| 34 | SWITCH | EW+SW | W | Y-switch, W branch |
| 35 | COMPLEX | NS+EW+SW | S,W | Cross + SW |
| 36 | DIAMOND | NE+SW | - | Diagonal X |
| 37 | COMPLEX | NS+NE+SW | N,S | Diagonal cross + NS |
| 38 | COMPLEX | EW+NE+SW | E,W | Diagonal cross + EW |
| 39 | SCISSORS | NS+EW+NE+SW | N,E,S,W | Scissors crossover |
| 40 | SWITCH | NW+SW | W | Y-switch, W diverge |
| 41 | WYE | NS+NW+SW | N,S,W | WYE junction |
| 48 | SWITCH | SE+SW | S | Y-switch, S diverge |
| 50 | WYE | EW+SE+SW | E,S,W | WYE junction |

---

## Appendix B: Full Forbidden Configurations Reference (28 total)

| Dec | Bin | Paths | Blocked Reason |
|-----|-----|-------|----------------|
| 13 | 001101 | NS+NE+NW | 3-way switch at N (exits: E,S,W) |
| 15 | 001111 | NS+EW+NE+NW | 3-way switch at N (exits: E,S,W) |
| 22 | 010110 | EW+NE+SE | 3-way switch at E (exits: N,S,W) |
| 23 | 010111 | NS+EW+NE+SE | 3-way switch at E (exits: N,S,W) |
| 28 | 011100 | NE+NW+SE | Multi-diagonal without through route |
| 29 | 011101 | NS+NE+NW+SE | 3-way switch at N (exits: E,S,W) |
| 30 | 011110 | EW+NE+NW+SE | 3-way switch at N & E |
| 31 | 011111 | 5 paths | 3-way switches at N (E,S,W) & E (N,S,W) |
| 42 | 101010 | EW+NW+SW | 3-way switch at W (exits: N,E,S) |
| 43 | 101011 | NS+EW+NW+SW | 3-way switch at W (exits: N,E,S) |
| 44 | 101100 | NE+NW+SW | Multi-diagonal without through route |
| 45 | 101101 | NS+NE+NW+SW | 3-way switch at N (exits: E,S,W) |
| 46 | 101110 | EW+NE+NW+SW | 3-way switch at N & W |
| 47 | 101111 | 5 paths | 3-way switches at N (E,S,W) & W (N,E,S) |
| 49 | 110001 | NS+SE+SW | 3-way switch at S (exits: N,E,W) |
| 51 | 110011 | NS+EW+SE+SW | 3-way switch at S (exits: N,E,W) |
| 52 | 110100 | NE+SE+SW | Multi-diagonal without through route |
| 53 | 110101 | NS+NE+SE+SW | 3-way switch at S (exits: N,E,W) |
| 54 | 110110 | EW+NE+SE+SW | 3-way switch at E (exits: N,S,W) |
| 55 | 110111 | 5 paths | 3-way switches at E (N,S,W) & S (N,E,W) |
| 56 | 111000 | NW+SE+SW | Multi-diagonal without through route |
| 57 | 111001 | NS+NW+SE+SW | 3-way switch at S (exits: N,E,W) |
| 58 | 111010 | EW+NW+SE+SW | 3-way switch at W (exits: N,E,S) |
| 59 | 111011 | 5 paths | 3-way switches at S (N,E,W) & W (N,E,S) |
| 60 | 111100 | NE+NW+SE+SW | Multi-diagonal without through route |
| 61 | 111101 | 5 paths | 3-way switches at N (E,S,W) & S (N,E,W) |
| 62 | 111110 | 5 paths | 3-way switches at E (N,S,W) & W (N,E,S) |
| 63 | 111111 | 6 paths | All 4 edges are 3-way switches |
