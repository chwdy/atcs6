# Reference: Track Display and Rendering

> **Last Verified:** 2026-11-15

## Context

This note explains how the page draws track on the canvas. What the drawing means to the person
running trains is in `design_v2.md`, "Seeing trains on the layout". The colours and the look are in
`docs/DESIGN_LANGUAGE.md`. This note covers only how the code does it.

The code is in `frontend/src/renderer/`:

| File | What it draws |
|:-----|:--------------|
| `renderer-tracks.ts` | The track, each block's state line, switches and their marks |
| `track-path.ts` | One line of track with round bends, shared by everything that follows the track |
| `running-train-overlay.ts` | Each train's border, chevron and tag |
| `renderer-debug.ts` | The bar across an end of track, and the debug overlays |

---

## 1. What a tile holds

A tile's track is a 6-bit number, `track_bits` (0–63). Each bit is one segment, and a segment runs
from the middle of one tile edge to the middle of another.

| Bit | Value | Segment | Edges it joins |
|:---:|:-----:|:-------:|:---------------|
| 0 | 1 | **NS** | North and South |
| 1 | 2 | **EW** | East and West |
| 2 | 4 | **NE** | North and East |
| 3 | 8 | **NW** | North and West |
| 4 | 16 | **SE** | South and East |
| 5 | 32 | **SW** | South and West |

`getActiveSegments(track_bits)` in `frontend/src/lib/constants.ts` returns the segments as pairs of
edge names. The full table of values, the edge masks and the values that cannot be built are in
`Reference_6Bit_Track_System.md`.

---

## 2. Where a tile is on screen

The grid is drawn turned 45°, so a tile is a diamond.

```ts
// frontend/src/lib/camera.ts
tileSize = gridSize * scale
screenX  = centerX + panX + (gridX + gridY + 1) * tileSize / 2
screenY  = centerY + panY + (gridX - gridY) * tileSize / 2
```

`gridToScreenX/Y` give the centre of the tile. The middle of each tile edge is a quarter of a tile
away from it:

| Edge | Neighbour across it | Offset from the tile centre | On screen |
|:----:|:--------------------|:----------------------------|:----------|
| N | y + 1 | `(+tileSize/4, −tileSize/4)` | up and right |
| E | x + 1 | `(+tileSize/4, +tileSize/4)` | down and right |
| S | y − 1 | `(−tileSize/4, +tileSize/4)` | down and left |
| W | x − 1 | `(−tileSize/4, −tileSize/4)` | up and left |

So a segment looks like this on screen:

| Segment | On screen |
|:-------:|:----------|
| NS | 45° line, top right to bottom left |
| EW | 45° line, bottom right to top left |
| NE | vertical, on the right of the tile |
| SW | vertical, on the left of the tile |
| NW | horizontal, along the top of the tile |
| SE | horizontal, along the bottom of the tile |

---

## 3. Sizes

All in `frontend/src/lib/constants.ts`, in pixels at `scale = 1`. Each is multiplied by the zoom.

| Constant | Value | What it sets |
|:---------|:-----:|:-------------|
| `TRACK_LINE_WIDTH` | 10 | How thick the track is drawn, the same in every direction |
| `TRACK_STATUS_WIDTH` | 2 | How thick a block's state line is |
| `TRACK_BEND_RADIUS` | 12 | The radius of a bend |
| `TRACK_WIDTH` | 6 | How far a block's state line stops short of each end of the block. Also sizes the gap beside a switch leg and the selection highlight |
| `DEBUG_BLOCK_WIDTH` | 12 | The width of the debug overlays that colour track by block or by segment |

---

## 4. Track is drawn as lines, not tile by tile

Drawing each tile's segment on its own leaves a notch or an overlap wherever two segments meet at
an angle. So `drawTracks` first joins the segments into lines and then strokes each line once.

1. **Collect pieces.** Each segment of each tile becomes a piece (`tilePiece`): its two screen
   points and a name for each of its two tile edges. An edge has one name whichever tile asks
   (`edgeName`), so two pieces that meet on a shared edge carry the same name there.
2. **Link pieces into lines** (`linkPieces`). Where exactly two pieces share an edge, they join. A
   line ends where it meets no other piece, or where three or more pieces meet. Pieces left over
   after that form closed loops.
3. **Carry on past a three-way meeting.** Where three or more pieces meet, each line is extended
   along one piece of the neighbouring tile. The canvas then joins the line to that piece instead
   of leaving a square end with a notch beside it. The extra length lies on top of track the same
   stroke already draws.
4. **Trace each line with round bends** (`traceTrackLine` in `track-path.ts`). Each bend is an arc
   of radius `TRACK_BEND_RADIUS`. An arc uses at most half of the shorter straight on either side of
   its bend, so two bends close together never overlap.
5. **Stroke all the lines of one colour in a single stroke** (`strokeLinePieces`), so lines that
   overlap do not darken each other.

The train's border and a block's state line are traced by the same `traceTrackLine` with the same
points, so their bends lie exactly on the track's bends.

---

## 5. What `drawTracks` draws, in order

1. **All track** in the track colour (`--canvas-track`), `TRACK_LINE_WIDTH` thick, with square
   ends.
2. **The switch legs not set**, each as a filled four-cornered shape in the dim track colour
   (`--canvas-track-dim`). See section 6.
3. **Each block's state line**, `TRACK_STATUS_WIDTH` thick with round ends, along the middle of the
   block's track. The colour comes from `blockTrackColor`: occupied if the block's detector reads
   occupied, otherwise reserved if a train claims it, otherwise free. A tile with no block, or a
   block the snapshot says nothing about, gets no line. The line's open ends are pulled back by
   `TRACK_WIDTH`, so the lines of two neighbouring blocks never touch and the gap shows where one
   block ends.
4. **A dot on each crossing tile** (`track_bits = 3`), radius 4, in `--canvas-accent`.
5. **A bar across each end of track** (`highlightDeadEnds` in `renderer-debug.ts`), in
   `--canvas-warning`. It is as thick as a state line and its inner face sits on the end of the
   track, so it never covers the track.

`drawSwitchMarks` runs straight after `drawTracks`.

---

## 6. Switches

`switchArms` works out which segments of a switch tile are the set leg and which are not. On a
regular switch the straight-through segment is the set leg in position 0 (main) and the turning
segment is the set leg in position 1 (diverge). A switch with no straight-through segment takes
its first exit clockwise for position 0 and its second for position 1.

| The switch is | How its legs are drawn |
|:--------------|:-----------------------|
| Plain, held or manual | The set leg whole, in the track colour. The other leg dim, from its outer edge to just short of the set leg |
| Position not known | Every leg dim, edge to edge |
| Alternate arrivals on | Every leg whole, because the runtime picks the leg |

**The leg not set.** Its end is cut parallel to the set leg, and a strip of the background shows
between the two. The strip is `TRACK_WIDTH / 2` wide. The leg never stops more than 85% of its
length short of the shared edge. Track-colour is drawn under the leg as far as its shorter side, so
the canvas joins the leg to the track beyond its outer edge.

**The state line on a switch.** The set leg carries the state line of the tile's block, edge to
edge, with no gap at the ends.

**The mark** (`switchMark`, `drawSwitchMarks`). A switch that is not plain gets one round badge
beside its tile, on the side no track leaves by. A switch can be in several conditions at once; the
badge shows the first one that applies:

| Order | Condition | Badge | Colour |
|:-----:|:----------|:------|:-------|
| 1 | Held | lock | `--train-line`, because a train holds it |
| 2 | Position not known | `?` | `--canvas-text-dim` |
| 3 | Manual | hand | `--canvas-text-dim` |
| 4 | Alternate arrivals on | two circling arrows | `--canvas-text-dim` |

The badge is a circle filled with the background colour, with a radius between 7 and 11 pixels
whatever the zoom. The lock, hand and arrows are glyphs from the Tabler icon font.

**The M/D letters.** `drawSwitchBadgeAt` draws a small M or D dot on each switch. It is drawn only
when the "Switch States" debug toggle is on, which is off by default.

---

## 7. Trains

`running-train-overlay.ts` draws on top of the track.

- **The border.** One stroke along the track of the train's blocks, from its rear block to its
  front block, across any switch between them. It is drawn in three passes: a dark stroke slightly
  wider than the track (`--train-line-edge`), a white stroke as wide as the track (`--train-line`),
  then the middle is cut out again with `destination-out`. What is left is a thin white line along
  each edge of the track with a dark line outside it, and the track and its state line show through
  the middle. The border has its own canvas layer, so the cut does not remove the track.
- **A claimed block ahead** carries the border on to the middle of that block, where a thin chevron
  closes it.
- **The chevron.** A thick chevron on the middle tile of the front block points the way the train
  faces. It reaches past both edges of the track.
- **The tag** sits beside that chevron (`tagSide`). Its fill is the train's state
  (`trainTagColor`).
- **A lost train** has a dashed border and no chevron.
- **A highlighted train** has a border twice as thick, in `--train-highlight`.

---

## 8. Other things drawn along the track

| What | Function | How |
|:-----|:---------|:----|
| Track the track tool is about to lay | `drawPreviewTracks` | Linked and traced like real track, in `--canvas-caution` at 80% |
| A selection, or a block being dragged out | `drawTileExtentHighlight` | A wide soft stroke and a thin bright stroke over each selected tile's segments, and a tick across each edge where the track leaves the selection. Drawn tile by tile, with round ends |
| Track coloured by block or by segment (debug) | `drawBlockGroupingColors`, `drawSegmentIdColors` | Under the track, `DEBUG_BLOCK_WIDTH` wide, using `drawGridSegment` |
| The mark where one segment ends and the next begins (debug) | `drawSegmentBoundaryMarkers` | A short piece at the tile edge, using `drawGridSegment` |

`drawGridSegment` is the older way of drawing a segment: a filled four-cornered shape on one tile.
Only these debug drawings still use it.
