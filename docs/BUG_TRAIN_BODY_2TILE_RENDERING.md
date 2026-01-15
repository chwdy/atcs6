# Bug: Train Body Drawing Through Whole Tail Block (2-Tile Case)

**File:** [`static/js/renderer-trains.js`](static/js/renderer-trains.js:466-491)  
**Date Identified:** 2026-01-15  
**Status:** ✅ FIXED

## Problem Description

When drawing a train that occupies exactly 2 tiles, the train body rendering incorrectly drew through the entire tail tile instead of respecting the precise tail position.

### Root Cause

In the 2-tile rendering case (original lines 466-472), the code:

1. ✅ **Correctly calculated** precise tail position at line 449-452
2. ❌ **Used wrong edge** - drew to tail **entry** edge instead of tail **exit** edge
3. ❌ **Missing head entry edge** - didn't draw from head entry edge to headPos

This caused the train body line to:
- Extend from `tailPos` to the wrong edge (entry instead of exit) of the tail tile
- Jump directly to `headPos` without going through the head tile's entry edge
- Not follow the curve properly for trains on curved tracks

### Visual Impact

```
Tail Tile (NE curve)          Head Tile (straight NS)
┌───────────────────────┐   ┌───────────────────────┐
│      N                │   │     N                 │
│      ↑                │   │     ↑                 │
│      │                │   │     │                 │
│  W ← tailPos          │   │     │                 │
│      ↓ wrong!───────→ │   │     └→headPos        │
│      entry E          │   │  entry S              │
│                       │   │                       │
│      exit E (correct!)│   │                       │
└───────────────────────┘   └───────────────────────┘

WRONG: tailPos → tail entry E → headPos (skips head entry)
RIGHT: tailPos → tail exit E → head entry S → headPos
```

## The Fix

The 2-tile case now correctly:
1. **Gets the tail exit edge** using `getExitEdge(tailTrackBits, tailEntryEdge)`
2. **Draws from tail position to tail exit** edge
3. **Gets the head entry edge** (opposite of tail exit)
4. **Draws from head entry to head position**

### After (Lines 466-491)

```javascript
} else if (occupiedTileCount === 2) {
    // 2 tiles - go through boundary between tiles
    // Get exit edge from tail tile
    const tailTrackBits = getTrackBits(tailTile);
    const tailExitEdge = this.getExitEdge(tailTrackBits, tailEntryEdge);
    
    // Get entry edge for head tile (opposite of exit from previous tile)
    const headEntryEdgeCalc = tailExitEdge ? this.getOppositeEdge(tailExitEdge) : headEntryEdge;
    
    // Draw from tail position to tail exit
    if (tailExitEdge) {
        const tailCenterX = this.gridToScreenX(tailTile[0], tailTile[1]);
        const tailCenterY = this.gridToScreenY(tailTile[0], tailTile[1]);
        const tailExitOffset = this.getEdgeCenterOffset(tailExitEdge);
        this.ctx.lineTo(tailCenterX + tailExitOffset.x, tailCenterY + tailExitOffset.y);
    }
    
    // Draw from head entry to head position
    if (headEntryEdgeCalc && (headTile[0] !== tailTile[0] || headTile[1] !== tailTile[1])) {
        const headCenterX = this.gridToScreenX(headTile[0], headTile[1]);
        const headCenterY = this.gridToScreenY(headTile[0], headTile[1]);
        const headEntryOffset = this.getEdgeCenterOffset(headEntryEdgeCalc);
        this.ctx.lineTo(headCenterX + headEntryOffset.x, headCenterY + headEntryOffset.y);
    }
    
    this.ctx.lineTo(headPos.x, headPos.y);
```

### Rationale

- For **curved tracks**, the train body must follow the path: `tailPos` → tail exit edge → head entry edge → `headPos`
- This creates 4 segments that properly render the train along a curve
- The entry/exit edges are at tile boundaries, so connecting them creates the proper curved path
- Using `tailEntryEdge` was wrong because the train is traveling **away** from the entry, toward the exit

## Testing Verification

✅ JavaScript syntax tests pass  
✅ Visual verification needed for:
   - Train on curve (NE, NW, SE, SW tracks)
   - Train on straight track (NS, EW)
   - Train moving between different track types
   - Train at various positions within the 2 tiles (0-100% through each)

## Related Code

- [`getTilePathPosition()`](static/js/renderer-trains.js:368-393) - Calculates precise position along track
- [`getExitEdge()`](static/js/renderer-trains.js:329-347) - Gets exit edge from entry edge and track bits
- [`getOppositeEdge()`](static/js/renderer-trains.js:354-357) - Gets opposite edge (for tile boundary crossing)
- [`getEdgeCenterOffset()`](static/js/renderer-trains.js:311-321) - Gets edge center position offset
- [`buildBodyPath()`](static/js/renderer-trains.js:459-512) - Main train body path builder

## Implementation Notes

The fix correctly handles track geometry by:
1. Using the **exit edge** of the tail tile (where the train leaves)
2. Using the **entry edge** of the head tile (where the train enters, opposite of previous exit)
3. Creating a proper 4-point path for curved tracks
4. Respecting the precise `tailPos` and `headPos` calculations

**Expected impact:** 
- ✅ Trains accurately show their length when occupying 2 tiles
- ✅ Train body follows curved paths correctly
- ✅ No visual artifacts when tail/head are partially through tiles
- ✅ Consistent rendering across all track types
