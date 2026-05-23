# Design Language

This document captures the visual vocabulary used by the redesigned panels
(inspector, train monitor, train control). It is the single source of truth
for new UI work — match these patterns and tokens so panels feel like they
belong to the same product.

The complementary `STYLE_GUIDE.md` covers utility classes and component
helpers; this doc describes how those primitives compose into the panel
language you see in [features/inspector.css](../static/css/features/inspector.css)
and [features/train-monitor.css](../static/css/features/train-monitor.css).

---

## 1. Tokens

All tokens live in [base/tokens.css](../static/css/base/tokens.css). Refer to
them by variable name in CSS, never by hex literal.

### Surfaces

| Token | Hex | Use |
|-------|-----|-----|
| `--bg-dark` | `#121214` | App canvas, button backgrounds, count pills |
| `--bg-panel` | `#1e1e24` | Panel shells |
| `--bg-hover` | `#2a2a35` | Header strips, row hover |
| `--border-color` | `#333340` | All hairline borders |

### Text

| Token | Hex | Use |
|-------|-----|-----|
| `--text-primary` | `#e1e1e6` | Values and other content that should read first; panel/section titles |
| `--text-secondary` | `#a8a8b3` | Section subtitles, captions, muted prose |
| `--text-tertiary` | `#83838f` | Field labels — the small uppercase descriptor sitting next to a value |

These three are a deliberate hierarchy of brightness, not interchangeable greys.
The **value** is the brightest thing in a field row so the eye lands on the
content; the **field label** is the dimmest so it recedes; **section subtitles**
sit in between as structural headers. Keeping field labels at `--text-secondary`
(as an earlier version did) put labels and values within ~1.8:1 of each other and
made the values hard to pick out — `--text-tertiary` widens that gap while staying
above the 4.5:1 AA floor against the panel background.

### Accents

| Token | Hex | Semantic |
|-------|-----|----------|
| `--accent-color` | `#8257e5` | Selection / active state (purple) |
| `--accent-hover` | `#9466ff` | Hover, active text on tinted bg |
| `--success-color` | `#04d361` | Free / OK / running / power notch |
| `--warning-color` | `#ffcd1e` | Caution / yellow signal / dwell / brake notch |
| `--danger-color` | `#e83f5b` | Occupied / red signal / EB / fault |
| `--info-color` | `#00d4ff` | Stations / target speed / informational |

The accent palette doubles as the **status accent palette** for left-border
strips, banner tints, and badge backgrounds. Don't introduce new hues.

### Layout

| Token | Value | Use |
|-------|-------|-----|
| `--control-height-xs` | `28px` | Default — every control inside a panel (buttons, selects, text/number inputs, segments, icon buttons) |
| `--control-height-sm` | `32px` | Top-level forms outside the panel shell, larger CTAs |
| `--control-radius-sm` | `6px` | All buttons, panel corners, cards |

### Type

| Token | Value |
|-------|-------|
| `--font-main` | `'Inter', sans-serif` |
| `--font-mono` | `'JetBrains Mono', monospace` |

---

## 2. The four panel parts

Every panel is composed of the same four optional parts, in this vertical
order:

```
┌─────────────────────────────────────┐
│ Header     (--bg-hover, 8px 10px)   │  title + tools, drag handle if floating
├─────────────────────────────────────┤
│ Toolbar    (--bg-panel, optional)   │  optional secondary control row
├─────────────────────────────────────┤
│ Body       (--bg-panel, scroll)     │  the main content
├─────────────────────────────────────┤
│ Footer     (border-top, --bg-dark)  │  status banners / metadata
└─────────────────────────────────────┘
```

The `├──┤` separators above are conceptual — they show the four parts. In
the rendered UI, parts are distinguished by background tint, not by drawn
hairlines: the header's lighter `--bg-hover` reads against the body's
`--bg-panel`; the footer's darker `--bg-dark` reads against the body. The
header has **no border** of its own.

- The **header** is `--bg-hover` with `padding: 8px 10px` and no border. Title is 12px Inter weight 600, letter-spacing `0.02em`.
- The **toolbar** has `border-bottom: 1px solid var(--border-color)` only when it sits on the same surface as the body (`--bg-panel`) — that hairline is the only thing separating it from the body. When the toolbar shares the header's `--bg-hover`, drop the border-bottom; the bg-tint change to the body does the separation.
- The **body** uses `flex: 1 1 auto; overflow-y: auto` — it does the scrolling.
- Header and footer use `flex-shrink: 0` so they stay pinned.
- Footer banners collapse to 0px when their content is hidden — no padding overhead when there's nothing to show.

Reference: [`.tm-panel`](../static/css/features/train-monitor.css) is the
canonical example.

---

## 3. Buttons

All buttons in the panel language are 28px tall (`--control-height-xs`),
radius 6px, and follow the **ghost** pattern by default.

### Ghost button (the default)

```css
height: var(--control-height-xs);
background-color: var(--bg-dark);
color: var(--text-primary);
border: 1px solid var(--border-color);
border-radius: var(--control-radius-sm);

/* hover */
border-color: var(--accent-color);
color: var(--accent-hover);
```

Use a single Unicode glyph for icon buttons (`↻` refresh, `⊞` expand, `⊟` collapse, `{ }` JSON, `×` close). When you need a label, keep it ≤ 4 characters in mono.

### Flat ghost (in-container button)

For buttons sitting inside a container that already groups them — a header
strip, a toolbar row, a segmented cluster, a tool sidebar — drop the
per-button border. Affordance comes from a hover bg-tint, not a hairline.

```css
height: var(--control-height-xs);
background-color: transparent;
color: var(--text-primary);
border: none;
border-radius: var(--control-radius-sm);

/* hover */
background-color: var(--bg-hover);
color: var(--accent-hover);

/* active / selected */
background-color: rgba(130, 87, 229, 0.18);
color: var(--accent-hover);
```

Rule of thumb: **the container is bordered, or the buttons are bordered —
never both.** When a button stands alone on the panel body (no container
row around it), use the regular ghost above. When it lives in a row with
siblings inside a header, toolbar, or grouped cluster, use the flat ghost.

### Tool button (icon grid tile)

A specialised flat ghost for the icon grid in the left tool sidebar. Used
when the page needs a dense, scannable palette of single-glyph tools
rather than a labelled control row.

```css
/* tile */
width: 38px;
height: 38px;
padding: 2px;
background-color: var(--bg-dark);
border: none;
border-radius: var(--control-radius-sm);
color: var(--text-secondary);
display: flex;
flex-direction: column;
align-items: center;
justify-content: center;
gap: 1px;

/* hover */
background-color: var(--bg-hover);
color: var(--accent-hover);

/* active */
background-color: rgba(130, 87, 229, 0.18);
color: var(--accent-hover);
```

- The tile is taller than the standard 28px control because it stacks an icon glyph (~16px) above a tiny label (~8px) — that vertical stack is the whole point of the pattern.
- No border on the tile itself. The `--bg-dark` fill against the panel's `--bg-panel` is what gives each tile its shape; grouping comes from the category dividers above the grid, not from per-tile boxes.
- Icons are single Unicode glyphs (no emoji). Labels are 1-2 words at ~8px.

Reference: `.tool-btn` in
[components/tools.css](../static/css/components/tools.css).

### Hero CTA (driver-facing primary)

For the most important driver-facing action in a panel — the ATO Go /
Stop buttons. Same ghost + accent-tint shape as the regular ghost, just
bigger: 52px tall, 15px weight-700 uppercase text, 8px radius, generous
gap between leading glyph and label. Colors come from `--success-color`
(Go) or `--danger-color` (Stop) used as text + border + 10% bg tint on
hover. Reference: `.tc-btn-hero` in
[features/train-control.css](../static/css/features/train-control.css).

### Emergency button

For the EB / emergency-stop control. Always tinted `--danger-color`:
default is a 10%-tint danger bg + 1px danger border + danger text; hover
escalates to a **solid danger fill** with dark text. This is the one
place a solid fill is permitted inside a panel — emergencies need to
shout. When EB is active, the panel shows a pulsing badge elsewhere (see
the pill rules in §7) and the button itself toggles to a yellow reset
variant.

```css
height: 40px;
background-color: rgba(232, 63, 91, 0.10);
color: var(--danger-color);
border: 1px solid var(--danger-color);
text-transform: uppercase;
letter-spacing: 0.08em;
font-weight: 700;

/* hover */
background-color: var(--danger-color);
color: var(--bg-dark);
```

Reference: `.tc-eb-btn` in
[features/train-control.css](../static/css/features/train-control.css).

### Segmented control

A row of mutually-exclusive buttons inside a single `var(--bg-dark)` container with a 1px outer border, **no internal dividers**. Buttons inside are flat ghosts — the active accent tint and hover bg-tint give every cell its own legible state without needing vertical hairlines. Active button: `color: var(--accent-hover)`, `background-color: rgba(130, 87, 229, 0.18)`. Hover: same accent color, `var(--bg-hover)` background. The container's outline does the grouping.

### Tab strip

A row of mutually-exclusive labels that switches the body content below.
Use this for **content switching** (panes); use the segmented control for
switching a setting inside a single pane.

- **Container** — a flex row with `padding: 0 8px`, `gap: 2px`, and `border-bottom: 1px solid var(--border-color)`. That bottom rule is the divider between the strip and the content beneath; it's the only border in the strip. Keep the horizontal padding tight (8px) so tabs sit near the strip's left edge rather than floating inset.
- **Tabs** — no border, no fill by default. 11px Inter, `--text-secondary`, `padding: 6px 6px`. Padding is deliberately tight: a tab is text + a click target, not a button — let the words speak.
- **Hover** — `background-color: var(--bg-hover)`, `color: var(--text-primary)`.
- **Active** — `color: var(--accent-hover)`, plus a 2px `border-bottom: var(--accent-color)` with `margin-bottom: -1px` so the active underline overlaps the strip's bottom rule (the active tab "owns" that segment of the divider). Inactive tabs carry a 2px transparent `border-bottom` so the active underline doesn't shift heights between tabs.

Reference: `.secondary-sidebar-tabs` / `.secondary-tab` in
[layout/shell.css](../static/css/layout/shell.css).

### Don't use solid-color filled buttons in panels.

Reserve filled `var(--accent-color)` buttons for top-level page CTAs, not panel toolbars. The ghost + accent-tint pattern keeps panels visually quiet.

---

## 4. Form controls in panels

Text inputs, selects, and number inputs inside a sidebar panel match
the button language at 28px (`--control-height-xs`). The
`--control-height-sm` (32px) row in the token table is for top-level
forms outside a panel shell — in a sidebar at 280–440px wide, a 32px
input stacks tall enough that two of them push the rest of the panel
off-screen.

| Control      | Height | Padding | Font                |
|--------------|--------|---------|---------------------|
| Text input   | 28px   | 0 8px   | 12px Inter          |
| Select       | 28px   | 0 6px   | 12px Inter          |
| Number input | 28px   | 0 6px   | 12px mono, centered |

Background `var(--bg-dark)`, border `1px solid var(--border-color)`,
radius `var(--control-radius-sm)`, focus border `var(--accent-color)` —
the same chrome as the ghost button, so a form field and the button
next to it visually belong to the same family.

Reference: `.inspector-panel-field select` in
[features/inspector.css](../static/css/features/inspector.css).

### Slider / handle

For continuous (power %) or notched (master controller) inputs. Native
`<input type="range">` with custom track + thumb styling. Two patterns:

- **Plain slider** (`.tc-power-slider`) — 6px-tall `--border-color` track, circular thumb in the relevant accent (e.g. `--success-color` for a power slider).
- **Notched handle** (`.tc-handle-slider`) — 8px-tall track with a multi-stop gradient that encodes the notch zones (green=power, gray=neutral, yellow=brake, red=EB). Rectangular grab-handle thumb with an accent halo. Above the track, a 15-column tick grid renders the discrete notch labels; the active notch is highlighted with the same accent tint used by the segmented-control active state.

Disabled state across both: `opacity: 0.4`, `cursor: not-allowed`.
Reference: `.tc-power-slider`, `.tc-handle-slider` in
[features/train-control.css](../static/css/features/train-control.css).

---

## 5. Field rows

Two patterns, picked by data type:

### A. Stacked label / value (compact sidebars, info columns)

```html
<div class="tm-field">
  <dt class="tm-field-label">ATC Limit</dt>
  <dd class="tm-field-value">
    <span class="tm-val tm-val-danger">80</span>
    <span class="tm-val-unit">km/h</span>
  </dd>
</div>
```

- Label: 10px UPPERCASE, `letter-spacing: 0.06em`, weight 500, `--text-tertiary` — the label is the quiet half of the pair; it should recede, not compete with the value
- Value: mono, 12px, weight 600, `--text-primary` by default — this is the dominant element of the row
- Status-tinted variants: `.tm-val-success`, `.tm-val-warn`, `.tm-val-danger`, `.tm-val-info`
- Unit: 10px Inter, `--text-secondary`
- Rows separated by `1px dotted var(--border-color)`

### B. Three-column row (inspector body)

```
┌─ name (mono, 11em) ─┬─ badge (4.5em) ─┬─ value (mono, 1fr) ─┐
```

Used for tabular data where each row has a categorical badge. Color-code
the badge background using the accent palette. Reference:
[`.inspector-row`](../static/css/features/inspector.css).

---

## 6. Status accents

Three patterns for showing a row's status:

1. **3px left-border** on the row (most common). Color matches the accent
   palette: green=ok, purple=selected/reserved, red=fault, yellow=warn,
   info=informational.

2. **Banner**: full-width strip with a 3px left-border accent + ~10% opacity
   background tint of the same color. Use for one-line diagnostics
   (`speed_limit_reason`, `ATC: yellow signal at S-3`). Banners belong in
   the panel **footer**, not inside scrollable columns.

3. **Source badge**: a small color-coded pill (used by inspector to show
   `stored / property / latch / external`). Background is the accent color
   at full opacity, text is a deep complementary color (e.g.
   `var(--accent-blue)` bg + `#001a22` text).

---

## 7. Pill counts and status chips

Small numeric or short-text chips used in headers and toolbars:

```css
background-color: var(--bg-dark);
color: var(--text-secondary);
border: 1px solid var(--border-color);
border-radius: 999px;
padding: 1px 8px;
font-size: 10px;
font-family: var(--font-mono);
```

For state badges (`▶ ATO RUNNING`, `EB ACTIVE`), tint the background at
~15% opacity of the relevant accent and use the accent as the text color.

---

## 8. Resizers and drag affordances

- **Column resizer**: 6px wide, `background-color: transparent`, hover/active
  → `var(--accent-color)`.
- **Row resizer / drag handle**: 6px tall version of the same.
- Cursor: `ew-resize` for column, `ns-resize` for row, `move` for whole-panel drag.

---

## 9. Numeric displays

### Hero number (big readout)

For a single dominant metric (current speed, count):

```css
font-family: var(--font-mono);
font-size: 28px;
font-weight: 700;
line-height: 1;
letter-spacing: -0.01em;
color: var(--success-color);   /* or the relevant accent */
```

Pair with a small unit (`km/h`, `m`) in 11px `var(--font-main)` and `var(--text-secondary)`.

### Inline numeric

In a row, use the field-value pattern (12px mono, weight 600). Don't mix
sizes across a single row.

---

## 10. Empty / loading / error states

- **Empty**: `padding: 16px 12px; text-align: center; color: var(--text-secondary); font-style: italic; font-size: 11px;` ("No elements ahead", "No path assigned", etc.)
- **Loading**: same shape, replace italic with subdued spinner glyph if needed.
- **Error in a row**: `border-left: 3px solid var(--danger-color)` + `background-color: rgba(232, 63, 91, 0.08)` + value text in `var(--danger-color)`.

---

## 11. Cards

Bordered content containers used inside panel bodies. Four variants by use:

### Standard card

```css
display: flex;
flex-direction: column;
gap: 6px;
padding: 8px 10px;
background-color: var(--bg-dark);
border: 1px solid var(--border-color);
border-radius: var(--control-radius-sm);
```

For grouped info readouts (Route Plan, Next Stop) and list rows (route
list, path list). The default `--bg-dark` fill against the body's
`--bg-panel` gives the card its definition; the 1px border gives the
rounded shape its hard edge. References: `.tc-card`, `.route-row`,
`.path-row`.

### Status-accented card

Standard card + a 3px left-border drawn from the status palette
(`--info-color`, `--success-color`, etc.). The strip says "this card is
about X" without needing a header chip. Optional 4%-tinted linear
gradient on the left edge reinforces the strip.

```css
border-left: 3px solid var(--info-color);
background: linear-gradient(180deg, rgba(0, 212, 255, 0.04), var(--bg-dark) 70%);
```

References: `.tc-card-destination` (full card), `.station-summary-row`
and `.train-summary-row` (compact 4px-padding variants for the Properties
pane in the editor's left sidebar — same `--bg-dark` fill, same border,
shorter padding for dense stacks).

### Status header block

A prominent callout at the top of a panel body — `--bg-hover` bg
(brighter than the rest of the body, so the eye lands here first), 3px
dynamic left accent driven by state, 6px radius. Used for "the most
important state right now" — train OBC power state, current task.

```css
padding: 10px 12px;
background-color: var(--bg-hover);
border-left: 3px solid var(--border-color);  /* state-driven color */
border-radius: var(--control-radius-sm);
```

For an alert state, layer a horizontal gradient wash over the bg:
`linear-gradient(90deg, rgba(232, 63, 91, 0.10), var(--bg-hover) 60%)`
turns the emergency state into a tinted but still readable block.
References: `.tc-status`, `.tc-section-task`.

### Disclosure (collapsible card)

A card whose body collapses behind a native `<details>` / `<summary>`
toggle. Used for "I might want to look at this, but not by default"
(debug zone, advanced settings). The card itself follows the Standard
card styling. Summary row is 10px UPPERCASE weight-600 `--text-secondary`
with a rotating `▸` caret that points down when open.

Reference: `.tc-disclosure` in
[features/train-control.css](../static/css/features/train-control.css).

---

## 12. Overlays

Floating UI that sits above the panel layer.

### Modal dialog

For confirmations and selector dialogs. Centered fixed-position card
above a translucent backdrop. Body uses the standard field-row pattern;
footer holds confirm / cancel buttons right-aligned. The 2px accent
border + drop shadow set it apart from panel chrome.

```css
position: fixed;
top: 50%;
left: 50%;
transform: translate(-50%, -50%);
background: var(--bg-panel);
border: 2px solid var(--accent-color);
border-radius: 12px;
box-shadow: 0 20px 60px rgba(0, 0, 0, 0.8);
min-width: 420px;
max-width: 520px;
z-index: 1001;
```

The header strip reuses the purple gradient from the secondary sidebar
header so floating dialogs feel like they belong to the same product.
Reference: `.block-modal` in
[components/common.css](../static/css/components/common.css).

### Toast notification

Transient slide-in from the top-right for "X succeeded" / "X failed"
feedback. 250-400px wide, anchored to a fixed `.toast-container`. Color
is set by a 4px left-border (success / warning / danger / info from the
accent palette) plus a matching icon glyph.

```css
background: var(--bg-panel);
border-radius: 8px;
padding: 12px 16px;
border-left: 4px solid var(--accent-color);  /* status-driven */
box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45);
```

Auto-dismisses after a few seconds with a slide-out animation. Reference:
`.toast` / `.toast-container` in
[components/common.css](../static/css/components/common.css).

### Floating picker

A small popover near the cursor when a click hits something with
multiple targetable items (e.g. a tile that contains a platform AND a
block). Panel-language styling — `--bg-panel` bg, hairline border, drop
shadow. Options inside are flat-ghost rows that pick up the accent
border on hover.

```css
position: fixed;
z-index: 1000;
padding: 4px;
background: var(--bg-panel);
border: 1px solid var(--border-color);
border-radius: var(--control-radius-sm);
box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45);
```

Reference: `.route-resource-picker` in
[features/routes-paths.css](../static/css/features/routes-paths.css).

---

## 13. Don'ts

- **No emojis in chrome.** Single Unicode glyphs (`▣ ⇋ 🚦 🚉` for content, `↻ ⊞ ⊟ ×` for chrome) are OK. Don't sprinkle 📋 / 📊 / 🚨 across buttons — they read as noise.
- **No new colors.** If you can't reach for a token, the design doesn't have a slot for it yet — pause and discuss instead of inventing.
- **No solid filled buttons in panels.** Use the ghost + accent-tint pattern.
- **Don't border the container *and* every child.** If the container has a border (segmented cluster, tool grid, tab strip), the children inside are flat ghosts. If the children need their own borders (standalone buttons on the panel body), the container is unbordered. Doubling the borders reads as fenced-off boxes inside fenced-off boxes — the rendered weight is double what the structure needs.
- **No nested panels.** A panel inside a panel inside a panel is a sign the structure is wrong.
- **No multi-paragraph banners.** A footer banner is a single line of diagnostic text. Anything longer belongs in a tooltip or a disclosure.
- **No `box-shadow` for emphasis.** Use border + background tint. The only shadow tokens are the panel drop-shadows (`0 10px 30px rgba(0,0,0,0.45)` for floating, `0 -6px 20px rgba(0,0,0,0.35)` for bottom-anchored).

---

## 14. Reference panels

When in doubt, copy the structure of one of these:

- **Floating debug panel** → [inspector.css](../static/css/features/inspector.css) — header + toolbar + scrolling body + footer, draggable, resizable. Header has no border; relies on `--bg-hover` against `--bg-panel`. Toolbar keeps `border-bottom` because it sits on `--bg-panel` (same as body).
- **Bottom dock** → [train-monitor.css](../static/css/features/train-monitor.css) — header + multi-column body + status footer, full-width, fixed height. Header has no border; the segmented view-distance control inside the header is a bordered container of flat ghosts (no internal dividers).
- **Sidebar form** → [train-control.css](../static/css/features/train-control.css) — sectioned vertical stack inside a secondary sidebar. Status rows use the 3px left-border accent only, no surrounding 1px box.
- **Tab strip** → [layout/shell.css](../static/css/layout/shell.css) `.secondary-sidebar-tabs` — the row has one `border-bottom`; tabs themselves are flat with an accent underline on active.
