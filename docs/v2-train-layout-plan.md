# V2 train display on the layout

Status: approved.

## Purpose

Make it easy to see which track belongs to each train, which way it will go,
and which train a control card operates. Follow `design_v2.md`, especially
“The evening v2 is for”, “Trains”, “Direction”, and “Data flow to the page”.
The display adds no setup before running a train.

## Proposed appearance and interaction

- Every train uses the same theme-dependent line style. Identity comes from
  its train ID, shown on the layout and its card. A user-written name may sit
  beside the ID on the card, but never replaces the matching ID.
- Draw a moderately thick line along the rails of every block belonging to
  the train. Add a contrasting outline and leave enough of the underlying
  track visible to read detector coloring.
- Draw one arrow at the outgoing end of the front block and a compact ID
  label nearby. The arrow follows the local track direction, including curves.
  It stays visible while stopped or waiting.
- The line covers the train's assigned blocks. It is not a drawing of its
  measured length or exact nose and tail position. Do not animate movement
  inside a block or estimate it from voltage.
- Retained blocks stay in the train's line even when their detectors read
  empty. The existing detector display continues to show their readings.
- A claimed block ahead is not part of the train line.
- Hovering a card emphasizes that train's line, arrow and label together.
  Keyboard focus within the card does the same, including focus on controls.
- Clicking a card's identity area selects it and keeps the emphasis. Clicking
  the train's map label or line selects the same card and brings it into view
  if needed. Map selection does not change voltage or direction.
- Hover or focus temporarily takes precedence over selection. Leaving the
  card restores the selected train's emphasis. Clearing selection removes it.
  Moving between controls within a card does not cause highlight flicker.
- A successful Reverse changes the arrow using the returned server state.
  A refused Reverse leaves it unchanged. Reversing several blocks moves the
  arrow to the new front block, not just the other end of the old front block.
- A lost train shows its retained block with a dashed line and `ID ?`.
  Hide its travel arrow while lost. Preserve its matching card and highlight.

## Implementation order

### 1. Record the agreed behavior

After approval, add the operator-visible behavior to `design_v2.md` and update
the applicable train-display sections of `docs/DESIGN_LANGUAGE.md`. V2's
behavior and the agreed design guide those edits; old simulation styling is
not a requirement. Keep the detailed rules in one place and cross-reference
them from the other document.

### 2. Build geometry from v2 data

Add a small geometry module under `frontend/src/renderer/`. Read the existing
v2 train entries and block geometry from the same current snapshot. The train's
`blocks` list provides membership and rear-to-front order; `direction` selects
the outgoing end of its front block. Block `ordered_tiles` provides the rail
walk. Reuse existing track edge/projection helpers where their behavior fits.

Produce separate paths for the occupied blocks, an arrow location and heading,
and a label anchor. Follow actual rail geometry rather than connecting tile
centers, which can zigzag on diagonal track. Handle single-tile blocks and
blocks whose stored order runs opposite to their neighbors.

Join neighboring blocks across switches only when the geometry identifies the
connecting track unambiguously; do not choose an arbitrary branch. Keep a gap
when that connection cannot be established. A connector indicates continuity
between the train's blocks, not detection on the switch. Break paths at tunnel
mouths rather than drawing across empty space. Missing geometry must not crash
the renderer or cause it to invent a position.

### 3. Add the v2 drawing layer and theme styling

Implement a v2 `RendererOverlay`, the existing interface for drawing additional
content over the layout, and register it through `frontend/src/app.ts`. Draw
at the existing train drawing phase. Enable it for v2 operation and ensure the
simulation overlay does not draw duplicate trains in that mode.

Read trains from the existing `runningTrains` store. Do not adapt them into the
old renderer's exact-position train structure. Repaint when membership,
direction, lost state, highlight, or layout theme changes, including live-only
snapshot updates.

Use `canvas-palette.ts` and `static/css/base/layout-themes.css`. Check dark,
light and blueprint separately. Reuse suitable tokens; add explicit normal,
outline or emphasis tokens in every theme if existing ones cannot give clear
contrast. No per-train colors or hard-coded drawing colors.

Keep line widths, arrow sizes and text readable with screen-pixel limits at
different zoom levels. Place labels using the renderer's existing label bounds
where practical. Keep switch indications readable. Use static emphasis rather
than blinking or pulsing.

### 4. Connect cards and map interaction

Update `next/operate/running-trains.tsx` and its mount wiring. Store hovered,
focused and selected v2 train IDs as UI state; derive the currently emphasized
ID from them. Request a live-layer redraw when that result changes.

Add map hit testing against the drawn lines and labels with a usable pointer
tolerance. Give labels priority; preserve switch controls at switch locations.
Use the same selection action for cards and map clicks. Keep control clicks
from accidentally changing selection, and avoid routing v2 selection through
the old simulation train panel.

Clear stale IDs when trains disappear, the layout/session resets, or the
Operate screen is left. A merge keeps selection only if its ID survives;
otherwise clear it. A split follows the surviving ID. Clear drawing and hit
targets together so an old train cannot remain clickable.

### 5. Verify the result

Targeted automated checks:

- Straight, curved, diagonal and single-tile blocks; both directions;
  multi-block reversal with opposite stored block orientations.
- Switch connections, ambiguous connections and tunnel breaks.
- Assigned-but-clear blocks remain drawn; claims are excluded; lost trains
  get their distinct mark; removed trains disappear.
- Hover/focus/selection precedence, moving between card controls, cleanup,
  map/card selection agreement and command refusal.
- Live-only snapshots and theme changes repaint without stale geometry.

Inspect in the browser on `http://localhost:5007` with representative v2 data:
two neighboring trains, a multi-block train, reverse, waiting and lost states.
Check all three layout themes, zoom in/out, nearby labels, switch controls,
pointer hover and keyboard focus. Use Virtual Hardware or test fixtures for
these checks; do not drive the real rig for visual verification.

Before project tests, activate `conda activate general`. Run targeted tests
while implementing, then use `./run_all_tests.sh` as the completion gate.
Start a development server with `--port 5007` if needed.

## Scope

This first change covers the block-based train drawing and its connection to
cards. Length editing, new train controls, route previews, runtime decisions,
detector rules and voltage behavior are outside this change. Existing payloads
appear sufficient; if a missing geometry fact requires a payload change,
document that fact explicitly instead of reconstructing runtime decisions in
the browser.
