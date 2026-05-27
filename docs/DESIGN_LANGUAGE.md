# Design Language

This document captures the visual vocabulary used by the redesigned panels
(inspector, train monitor, train control). It is the single source of truth
for new UI work — match these patterns and tokens so panels feel like they
belong to the same product.

The complementary `STYLE_GUIDE.md` covers utility classes and component
helpers; this doc describes how those primitives compose into the panel
language you see in [features/inspector.css](../static/css/features/inspector.css)
and [features/train-monitor.css](../static/css/features/train-monitor.css).

### Where the primitives live

Three layers, with one rule per layer.

- **Tokens** → [base/tokens.css](../static/css/base/tokens.css) (§1). Refer to
  them by variable name in CSS, never by hex literal.
- **Shared compositional primitives** → [components/status-blocks.css](../static/css/components/status-blocks.css)
  (§6 banner + accent helpers, §7 chip, §11 status header). Every editor
  panel that needs a status header, a chip, a banner, or a left-border
  accent consumes these classes directly — `.status-header`, `.chip`,
  `.banner`, `.accent-*`. Don't redefine the same shape under a feature
  prefix in `features/*.css`; that creates a second source of truth and
  the two drift apart.
- **Feature implementations** → `features/{inspector,train-monitor,train-control,routes-paths}.css`
  (§15). Layer feature-specific composition on top of the shared
  primitives — a route-run-shaped card (`.rr-card`), a train-monitor
  status grid (`.tm-*`), a switch-editor toggle row. These files **do
  not** redefine the §6/§7/§11 shapes; they consume them.

The trigger for promoting a shape into `status-blocks.css` is **the
second consumer**. When a primitive in a feature file is about to be
copied — even with a different prefix — for a new panel, that's the
moment to extract it instead. One copy is "the reference
implementation"; two copies is duplication waiting to drift.

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

### Block / track status

The block-state palette is the one domain palette that gets its own tokens.
It reuses the accent hues for the four primary states and adds two
domain-only hues for static availability flags. Track / block renderers
consume these tokens; do not hard-code hex literals at the call site.

| State | Token | Hex | Semantic |
|-------|-------|-----|----------|
| FREE | `--block-free` | `#00d4ff` (= `--info-color`) | Clear — no train, no plan |
| RESERVED | `--block-reserved` | `#04d361` (= `--success-color`) | Go / enter — the next train may proceed |
| OCCUPIED | `--block-occupied` | `#ef4444` | Train physically present |
| CAUTION | `--block-caution` | `#fde047` | Restricted entry |
| DISABLED | `--block-disabled` | `#6c6c78` | Out of service (physical absence) |
| MANUAL | `--block-manual` | `#ff9500` | Manual override active |

The FREE / RESERVED mapping is deliberate: green is the "enter" signal a
driver reads on the canvas, so it lives on the state the next train is
cleared into. FREE inherits cyan from `--info-color` because an empty
block is informational, not a green-light.

OCCUPIED and CAUTION diverge from the §1 accent palette by design. The
`--danger-color` `#e83f5b` is a coral-pink tuned to harmonize with the
accent purple in panel chrome — it reads as "alert pill" rather than
"stop signal." On the canvas, where a red block is meant to read as a
train physically present (a railway-aspect red), `#ef4444` is purer and
more signal-correct. CAUTION sits at `#fde047` (lighter than
`--warning-color` lemon) so that the three warm hues — OCCUPIED red,
CAUTION yellow, MANUAL orange — space cleanly on the hue wheel and also
separate by lightness. The accent-tuned lemon `#ffcd1e` and the MANUAL
orange `#ff9500` were too close at canvas alpha. DISABLED and MANUAL are
standalone hex (not aliases) because no §1 accent matches their
semantic — DISABLED is the only place a desaturated grey is correct, and
MANUAL is the only orange in the system.

The tokens are declared as literal hex (not `var(--info-color)`
references) so `getComputedStyle().getPropertyValue('--block-…')` reads
the resolved color directly. CSS custom properties are not substituted
when read from JS, so `var()` chains return the unresolved string.

Canonical consumer: `getBlockStateColor(state, alpha)` in
[static/js/helpers.js](../static/js/helpers.js). Pass `alpha` < 1 to get
the rgba form used for canvas overlays.

### Layout

| Token | Value | Use |
|-------|-------|-----|
| `--control-height-xs` | `28px` | Default — every control inside a panel (buttons, selects, text/number inputs, segments, icon buttons) |
| `--control-height-sm` | `32px` | Top-level forms **outside the panel shell** only, larger CTAs — do not use inside sidebar panels |
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

### Icons

The icon system is **Tabler Icons**, vendored at
[`vendor/tabler/tabler-icons.min.css`](../static/vendor/tabler/tabler-icons.min.css)
and used as a font glyph:

```html
<i class="ti ti-arrow-narrow-right"></i>
```

Because Tabler is a font, each icon inherits its container's `color` and
`font-size` — it respects the token palette and scales with the
surrounding text the same way Unicode glyphs did, just with a much larger
named vocabulary.

- **Look up the action before picking a glyph.**
  [`static/icon-selections.json`](../static/icon-selections.json) is the
  running roster of which Tabler icon backs which use case
  (`select → pointer`, `extend → arrows-horizontal`, etc.). Re-use the
  existing entry before introducing a new one; if the use case isn't
  listed, add it in the same patch so the next caller can find it.
  Cross-panel consistency matters more than picking the perfect glyph
  for one screen.
- **Tabler is the default; Unicode is for tiny structural chrome.** Reach
  for `▸ ▾ ×` carets only where adding an `<i>` would be overhead — a
  disclosure caret, a select-arrow decoration, the close `×` on a tab.
  Otherwise prefer Tabler so the vocabulary stays consistent.
- **No inline `<svg>` in new components.** If Tabler is missing the icon
  you need, that's a roster question — propose adding it to
  `icon-selections.json` rather than pasting raw SVG into a component file.

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

Icon-only buttons (refresh, close, expand) take an icon as their only child — see [Icons](#icons) above for the icon system. When no icon fits the action, use a short mono label instead (≤ 4 characters, e.g. `JSON`).

### Flat ghost (in-container button)

For buttons sitting inside a grouped container — a segmented cluster, a
toolbar row, a tool sidebar — drop the per-button border. Affordance comes
from a hover bg-tint, not a hairline.

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

This pattern requires the container's own background to be `--bg-dark` or
`--bg-panel` — the hover tint (`--bg-hover`) must read against something
darker for the state to be visible. Never place flat ghosts directly on a
`--bg-hover` surface (such as the header strip itself); always wrap them in
a segmented cluster or toolbar row that carries its own `--bg-dark` fill.
When a header needs buttons, they live inside a bordered segmented container
inside the header — not free-floating on the header's `--bg-hover` surface.

Rule of thumb: **the container is bordered, or the buttons are bordered —
never both.** When a button stands alone on the panel body (no container
row around it), use the regular ghost above. When it lives inside a
`--bg-dark` grouped cluster or `--bg-panel` toolbar, use the flat ghost.

When the button sits inside a bordered card or list item, use this flat
ghost too — see §11 "Action buttons inside a card go flat."

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

The **meaningful** identifier is the value. Opaque internal ids
(`run_number`, `path_id`, internal hashes) go in chips (§7) or foot
captions, not in the title slot — the user's eye is looking for the
entity, not the database key.

### B. Three-column row (inspector body)

```
┌─ name (mono, 11em) ─┬─ badge (4.5em) ─┬─ value (mono, 1fr) ─┐
```

Used for tabular data where each row has a categorical badge. Color-code
the badge background using the accent palette. Reference:
[`.inspector-row`](../static/css/features/inspector.css).

### Overflow: identifiers vs names

Two different problems with two different answers — the doc used to
collapse them, but they need different handling.

**Opaque identifiers** (`route_id`, `run_number`, internal hashes,
target ids): clamp to one line with ellipsis and surface the full
string in a `title` tooltip. The user uses these for cross-reference,
not for reading; the differentiating tail is recoverable on hover.

```css
overflow: hidden;
text-overflow: ellipsis;
white-space: nowrap;
min-width: 0;   /* required on flex children — without it the cell ignores overflow */
```

Pair with `title="${full string}"` so the value stays reachable.

**Human-readable display names** (route names, station names, anything
written for humans to *read*): wrap to multiple lines instead. The
reader is meant to read the full string, and ellipsis truncates exactly
the part that differentiates one from another — "Demo v2: East Terminal
→ East Branch Term…" hides the destination, which is the whole point of
the name.

```css
word-break: break-word;
overflow-wrap: anywhere;
/* no white-space: nowrap, no overflow: hidden */
```

Do **not** clamp numbers, time-of-day, short status labels, or units —
those are sized to fit and should never get an ellipsis. If a numeric
value won't fit, the column is too narrow, not the value.

The `min-width: 0` line is the rule most often missed when clamping.
Flex children default to `min-width: auto`, which lets their content
expand the cell and silently defeats `overflow: hidden`. Set it
explicitly on every flex child whose content is supposed to truncate.
Reference: `.rr-card-route` (clamp, opaque id) in
[features/routes-paths.css](../static/css/features/routes-paths.css), and
`.status-header-name` (wrap, display name) in
[components/status-blocks.css](../static/css/components/status-blocks.css).

---

## 6. Status accents

Three patterns for showing a row's status:

1. **3px left-border** on the row (most common). Color matches the accent
   palette: green=ok, purple=selected/reserved, red=fault, yellow=warn,
   info=informational. This is the canonical status accent for new dense
   list rows. The older status-column pattern (a leading ~50px column with
   a colored dot + label, used by `.route-row` and `.path-row`) predates
   this rule and is not the template for new work — when those rows are
   reworked, they should migrate to the left-border accent.

2. **Banner**: full-width strip with a 3px left-border accent + ~10% opacity
   background tint of the same color. Use for one-line diagnostics
   (`speed_limit_reason`, `ATC: yellow signal at S-3`) or a label/value
   pair (e.g. `Locked by · PATH_…` on a switch panel — `.banner-label`
   over `.banner-value`). Banners belong in the panel **footer** for
   running diagnostics; an inline-in-body banner is also fine when it
   surfaces a single high-priority detail for the record on screen.
   Canonical home: `.banner` / `.banner-{warn,danger}` / `.banner-label` /
   `.banner-value` / `.banner-text` in
   [components/status-blocks.css](../static/css/components/status-blocks.css).

3. **Source badge**: a small color-coded pill (used by inspector to show
   `stored / property / latch / external`). Background is the accent color
   at full opacity, text is a deep complementary color (e.g.
   `--info-color` bg + `#001a22` text).

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

**All chips in a row share the same shell.** `--bg-dark` bg, 1px
`--border-color` border, 999px radius, 10px mono, 1px 8px padding. Only
`color` and `background-color` differ between variants — accent-tinted
state chips drop the border (the tinted fill is the affordance) but keep
every other dimension. Don't mint per-feature chip classes (no
`.foo-direction-pill`, `.foo-error-chip`); add modifier classes
(`-success`, `-warn`, `-danger`, `-muted`) on a single base class
instead. A row of mismatched chip shapes reads as cluttered even when
each one is individually correct. The canonical base class is `.chip` /
`.chip-state` / `.chip-{success,info,warn,danger,muted}` in
[components/status-blocks.css](../static/css/components/status-blocks.css)
— every editor panel consumes these directly.

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

Bordered content containers used inside panel bodies. Four variants by use.

**Action buttons inside a card go flat.** The card already carries the
border, so a 1px-bordered Edit / Delete button inside would read as a
box-in-a-box. Use the Flat ghost pattern from §3 — transparent bg, no
border, hover bg-tint as the affordance. Destructive actions keep their
danger-color text and pick up a 10%-tinted danger bg on hover.

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
border-left: 3px solid <state-driven accent>;  /* never --border-color */
border-radius: var(--control-radius-sm);
```

Because this component exists to surface the most important state right now,
its left-border always carries a meaningful accent color from the palette —
initialize it to whatever the component's idle or default state maps to.
Using `--border-color` here is wrong: it makes the block visually
indistinguishable from a standard card border and defeats the callout intent.

For an alert state, layer a horizontal gradient wash over the bg:
`linear-gradient(90deg, rgba(232, 63, 91, 0.10), var(--bg-hover) 60%)`
turns the emergency state into a tinted but still readable block.
References: `.tc-status`, `.tc-section-task`.

### Disclosure (collapsible card)

A card whose body collapses behind a native `<details>` / `<summary>`
toggle. Used for "I might want to look at this, but not by default"
(debug zone, advanced settings). The card itself follows the Standard
card styling. Summary row is 10px UPPERCASE weight-600 `--text-secondary`
with a rotating `▸` caret that points down when open. (`--text-secondary`
rather than the `--text-tertiary` used for field labels: a disclosure
summary is a structural toggle that controls section visibility, not a
field descriptor, so it sits one brightness tier higher.)

Reference: `.tc-disclosure` in
[features/train-control.css](../static/css/features/train-control.css).

### Composing a dense row

When a card surfaces a long identifier plus multiple chips plus an
action, the parts go in this vertical order so the user's eye lands on
the meaningful entity before the metadata:

1. **Title row** — primary text (mono identifier of the entity, per §5A)
   on the left, flat-ghost action right-aligned. The action is flat
   because the card already carries the border (no box-in-a-box).
2. **Subtitle line** — secondary text (the long display name, ellipsised
   per §5 Overflow) on its own line.
3. **Chip row** — small mono pills (§7) in fixed order: state chip first
   (accent-tinted), then identifier / direction / progress / error chips.
4. **Foot caption** — single 10px `--text-tertiary` line for the row's
   timestamp or relationship metadata (`started 1:23 PM`, `assigned to
   …`). Optional.

When the card is the detail page rather than a list row, the
composition is different enough to deserve its own subsection — see
"Composing a detail page top" below.

Reference: `.rr-card` in
[features/routes-paths.css](../static/css/features/routes-paths.css).

### Composing a detail page top

When the page is a single record (a route run, a train, a path) rather
than a list of records, the top section pairs a Status header block
(this section, above) with a stats block directly below it.

**1. Status header block** — the §11 chrome (`--bg-hover` bg + dynamic
3px accent), populated with:

- **Title row** — primary identifier of the record (mono, 14px, weight
  700) on the left, state pill (§7 accent-tinted) on the right.
- **Identity line** — `Role #N · related_id` in inline mono, one tier
  smaller than the title. Names what the record IS so the reader doesn't
  have to guess — `Run #1000001` is unambiguous; bare `#1000001` is not
  (see §12 principle 7).
- **Display name** — the human-readable name. Wraps freely, never
  ellipsis (see §5 "Overflow: identifiers vs names").

**2. Stats block** — a Standard card directly below the header, with:

- **Hero stat** (§9 hero number) at top — `X / Y unit` for progress
  counts, or a single big number for a single metric. Color by record
  state (success / info / warn / muted from §1 Accents).
- **Metadata rows** — 1–3 short rows of compact prose with `·`
  separators. Group related fields per row (timing on one, mode &
  direction on another, planning indices on a third). Spell words out
  when there's room — `forward · east` over `F·E` (§12 principle 3). The
  compact form lives in the `title` tooltip for power users who want it.

This pattern replaces the older §5A field-row dl for detail pages.
Edge-pinned label/value pairs (`STARTED ........... 2:42:37 PM`) violate
proximity (§12 principle 1) and waste vertical real estate — the hero +
metadata-strip variant carries the same information in roughly half the
height with no dotted leaders.

Reference: the canonical header block lives in
[components/status-blocks.css](../static/css/components/status-blocks.css)
as `.status-header` + `.status-header-{row,id,name,ident,role}` +
`.status-sep`, with the `.accent-{success,info,warn,danger,muted}`
left-border helpers from the same file. The stats block (route-runs
specific) is `.rr-summary` in
[features/routes-paths.css](../static/css/features/routes-paths.css).

---

## 12. Composing list rows (rows vs cards)

When the list is dense, sequential, comparable records — log entries,
journey steps, run history — the right container is a row, not a card.
Reach for this section *before* picking chrome; the wrong container can't
be fixed by polishing the cards inside it. The §11 "Composing a dense
row" subsection covers what goes inside a card-as-list-row when cards
genuinely are the right shape; this section covers when they aren't and
how to lay out the row-shaped alternative.

1. **Respect proximity.** Labels and their values must touch, not pin to
   opposite edges of a wide container. The "STARTED" label and its
   timestamp belong next to each other, not at left and right margins of
   a 600px row. If you need leader dots to span the gap, the layout
   itself is wrong — leader dots are a confession that the gap should
   not exist.

2. **Match container to data shape.** Cards are for objects you act on
   as a whole (a contact, a message, a route in the route list). Rows
   are for homogeneous comparable records (log entries, journey steps,
   run history). Lists are for sequences. Picking the wrong container is
   structural — no amount of color, spacing, or chip polish saves a list
   of forty cards when the user wanted to scan one column down the page.

3. **Density follows task.** Scanning-heavy views need high density.
   Detail / profile views can be airier. Decide once based on what the
   user is doing on this surface, not on what looks tasteful in a single
   row viewed alone. A spacious row that reads clean by itself is
   miserable when there are forty of them stacked — but the inverse
   trap is real too: don't cram a detail view onto one cryptic line just
   because you *can*. Multi-row metadata is fine when space allows;
   `forward · east` across two readable rows beats `F·E` on one
   compressed row. Density serves readability, not the other way around.

4. **Pick a hero per row; for mixed-task views, split the hero.** The
   hero is whatever answers the user's primary question on this row. For
   mixed views — debug, monitor, and post-hoc review all on one screen —
   the hero splits into two layers: a stable row identity (same shape on
   every row, recognizable at a glance) plus per-task scan signals as
   columns (a duration column for the debugger, a status dot column for
   the monitor). The user picks their task by picking which column to
   scan, not by switching modes.

5. **Visual hero is not the same as row name.** The eye scans a list in
   two passes: a categorical filter ("show me only the DWELLs"), then a
   specific lookup ("which place is this STOP at?"). Visual weight
   should follow scan order — the verb (action type) often deserves more
   weight than the noun (place / identifier), even though the noun is
   what you'd call the row's "name." Same logic as log viewers bolding
   ERROR / WARN / INFO over the message text.

6. **Position carries semantic weight.** Left edge = state filter /
   status. Right edge = numbers (right-alignable). Middle = identity.
   Top-right corner = primary action territory. Don't put low-stakes
   metadata in high-stakes positions — a "minor" pill floated top-right
   miscommunicates importance before a single pixel is colored.

7. **Format is the label.** A timestamp doesn't need "STARTED" written
   next to it. A colored dot doesn't need "STATE" written next to it. An
   arrow `→` between two times replaces "STARTED" and "FINISHED." But
   format tells you the *type*, not always the *role* — `#1000001` is
   unambiguously a number, but a reader can't tell *which* number
   (run? route? task index?) without a `Run` prefix. Drop labels where
   format makes the role obvious; keep them where it doesn't.

8. **Repetition trains the eye to skip.** Anything that repeats
   identically on every row becomes invisible after the second instance.
   So either promote it to a single column header at the top, or remove
   it. Repeating it inline on every row is *worse* than nothing — it
   costs space and gets ignored anyway.

9. **Show derived values, not just inputs.** If the user has to do math
   from your data — duration from two timestamps, percentage from two
   numbers, delta from two states — you've shipped half the feature.
   Compute the answer; the inputs become supporting evidence.

10. **Align comparable values into a column.** Whatever field users
    compare across rows must live in the same x-position on every row.
    Cards-of-rows defeats this; row-shaped lists honor it. Comparison
    should be the easy case, not the hard one.

11. **Bandages reveal structural bugs.** Leader dots to span a gap.
    Decorative dividers added to fill awkward whitespace. Pills floated
    to balance an empty corner. Whenever you reach for a visual
    band-aid, stop — the layout underneath is the bug. Fix the
    structure, don't paper over it.

### Putting it together

A list of route-run steps, refactored from edge-pinned cards into
compact rows. The header carries the column names once; the rows below
share the same x-positions so a downward scan reads as a single column.

```
#    type    target          detail              time
●  3 STOP    STA_DDB6BE      platform · F-(9,4)-N    1:33:59 PM
                                                     1:34:15 PM   16s
•  4 DWELL   STA_DDB6BE      platform · either       1:34:15 PM
                                                     1:34:17 PM    2s
●  5 STOP    MAIN_WEST       block · R-(6,2)-W       1:34:17 PM    ← currently running
```

When both start and finish exist, the time cell stacks them on two lines — vertical sequence implies "and then." The duration sits next to the finish on line 2 because that's the moment the duration becomes known. While a task is still running, the cell collapses to one line (just the start time); the row's brightness signals ongoing. Finished rows are taller than running/pending rows by design — finished history earns the extra vertical room.

- **Status dot** (leftmost, ~6px) — hue channel. Red = fail, amber =
  anomaly, dim = ok. The color is the label (principle 7); the column
  is scannable down the left edge (principle 10).
- **Ordinal** (`3`, `4`, `5`) — muted mono, narrow, `--text-tertiary`.
  A stable reference anchor for "the third step," not the hero.
- **Verb** (`STOP` / `DWELL`) — bold accent-tinted chip, the
  categorical filter the eye scans first (principle 5). Sized for the
  filter pass, not the identity pass.
- **Noun** (`STA_DDB6BE` / `MAIN_WEST`) — bright mono, medium-large,
  `--text-primary`. The identity — what the row IS.
- **Modifiers** (`platform · fwd`) — small, muted `--text-secondary`.
  Useful when present, invisible when not. Never repeated when empty
  (principle 8).
- **Timestamps + derived duration** — when both start and finish exist,
  stack them on two lines (start on top, finish + duration on bottom);
  vertical sequence is the label (principle 7). The duration is the
  brighter element next to the finish because that's the answer the
  debugger is scanning for (principle 9), and the moment the duration
  becomes known. Right-aligned so durations align down a column
  (principle 10). Running tasks collapse to a single line (start time
  only) — the row's brightness signals ongoing, so no arrow is needed.
- **Current row** — brightness channel only. No marker column —
  instead, the running row's text goes full-bright and the row picks up
  a subtle `--bg-hover` background tint. In a post-hoc replay no row is
  "live" and the whole list reads muted; status dots carry the story.
- **Header row** — carries `#`, `type`, `target`, `detail`, `time` once
  at the top in `--text-tertiary` 10px uppercase. Never repeated inline
  (principle 8).

### Color channels

Two color channels can coexist on one row without confusing each other
because they operate on different parts at different intensities:

- **Hue channel** — small saturated mark at the row edge (the status
  dot). Red = fail, amber = anomaly, dim / none = ok. Encodes
  *correctness*.
- **Brightness channel** — ambient row-wide level (text foreground +
  subtle background tint). Bright = currently active, muted = past /
  completed. Encodes *liveness*.

A row can carry a red status dot AND a bright row treatment without the
two reading as one thing, because hue lives in a tiny saturated mark and
brightness lives in the ambient surface.

---

## 13. Overlays

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

The header strip uses the same purple gradient as `.secondary-sidebar-header`
(`background: linear-gradient(135deg, rgba(130, 87, 229, 0.1), rgba(130, 87, 229, 0.05))`)
so floating dialogs feel like they belong to the same product.
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

## 14. Don'ts

- **No color emoji anywhere in the UI.** The icon system is Tabler (see §3 Icons); monochrome Unicode glyphs (`▸ ▾ × ↻`) remain fine for tiny structural chrome. Both render in the CSS `color` property and respect the token palette. Color emoji (`🚦 🚉 📋 📊 🚨`) render as color pictographs regardless of `color:` and fight with the palette — never use them in buttons, headers, labels, or any panel chrome. Quick test: if setting `color: red` on the character turns it red, it's a monochrome symbol and is OK; if it stays a colorful picture, it's an emoji and is not.
- **No new colors.** If you can't reach for a token, the design doesn't have a slot for it yet — pause and discuss instead of inventing.
- **No freelance ornaments inside a shared shape.** The same logic that bars new colors bars adding visual elements (an inner status dot, a leading icon, a divider rule, a decorative glyph) to a canonical primitive the doc already specifies. The §7 chip is text on a tinted pill — not "text + a glowing dot." If you find yourself reaching for an extra mark to make a component read better, pause: either the component is wrong for the job (pick a different one) or the doc is missing a slot (raise it as a doc question). Quick test: would I have to mint a new class to render this? Then it's freelance.
- **No solid filled buttons in panels.** Use the ghost + accent-tint pattern.
- **Don't border the container *and* every child.** If the container has a border (segmented cluster, tool grid, tab strip, card), the children inside are flat ghosts. If the children need their own borders (standalone buttons on the panel body), the container is unbordered. Doubling the borders reads as fenced-off boxes inside fenced-off boxes — the rendered weight is double what the structure needs.
- **No nested panels.** A panel inside a panel inside a panel is a sign the structure is wrong.
- **No multi-paragraph banners.** A footer banner is a single line of diagnostic text. Anything longer belongs in a tooltip or a disclosure.
- **No `box-shadow` for emphasis.** Use border + background tint. The only shadow tokens are the panel drop-shadows (`0 10px 30px rgba(0,0,0,0.45)` for floating, `0 -6px 20px rgba(0,0,0,0.35)` for bottom-anchored).

---

## 15. Reference panels

When in doubt, copy the structure of one of these:

- **Shared compositional primitives** → [components/status-blocks.css](../static/css/components/status-blocks.css) — the §6 banner, §7 chip, §11 status header, and §6 accent helpers. Editor panels consume these classes directly (`.status-header`, `.chip`, `.banner`, `.accent-*`) instead of redefining the shapes per feature.
- **Detail-page editor** → [editor-route-runs.js](../static/js/editor-route-runs.js) + [features/routes-paths.css](../static/css/features/routes-paths.css) — the worked reference for composing a `status-header` + stats block + list of rows inside an editor sidebar. Route-run-specific bits (`.rr-card`, `.rr-summary`, `.rr-task-*`) layer on top of the shared primitives.
- **Floating debug panel** → [inspector.css](../static/css/features/inspector.css) — header + toolbar + scrolling body + footer, draggable, resizable. Header has no border; relies on `--bg-hover` against `--bg-panel`. Toolbar keeps `border-bottom` because it shares `--bg-panel` with the body (per the §2 toolbar rule: same surface → hairline separator needed).
- **Bottom dock** → [train-monitor.css](../static/css/features/train-monitor.css) — header + multi-column body + status footer, full-width, fixed height. Header has no border; the segmented view-distance control inside the header is a bordered container of flat ghosts (no internal dividers).
- **Sidebar form** → [train-control.css](../static/css/features/train-control.css) — sectioned vertical stack inside a secondary sidebar. Status rows use the 3px left-border accent only, no surrounding 1px box.
- **Tab strip** → [layout/shell.css](../static/css/layout/shell.css) `.secondary-sidebar-tabs` — the row has one `border-bottom`; tabs themselves are flat with an accent underline on active.

---

## 16. Canvas labels

Labels rendered directly on the canvas (station, platform, train) — distinct
from the inline field labels in §5. These labels coexist with track chassis,
block state colors, signals, and moving trains, so they need rules the
panel-chrome vocabulary doesn't cover: zoom-aware borders, z-order under
moving things, opaque fills that survive overlap.

### Tier hierarchy

Pick the tier by liveness:

- **Static identity** (platform, station) — quietest. Opaque dark fill +
  thin neutral outline + white text. Size carries hierarchy within the tier
  (station 9px / platform 7px). The two share chrome so they read as one
  family at different scales.
- **Live identity** (train ID) — one tier up. Opaque dark base mixed with
  ~15% accent + thin accent border + white text. The train's motion is what
  reads as "alive"; the label just carries the categorical color, restrained.
- **Hover / user attention** — top tier. Opaque dark base mixed with ~15%
  accent + saturated accent border + full name at one font tier larger.
  Earns the louder treatment because it's one label at a time and the user
  is actively pointing.

State indicators (block colors, signal aspects, route overlays) sit in a
separate visual channel — saturated hue marks at the entity's location, no
label box. They coexist with these labels via the brightness vs. hue split
in §12 "Color channels".

### Fills are opaque; color lives in the border

**Permanent canvas labels use a near-opaque dark base** (alpha ≥ 0.85) so an
overlapping label doesn't bleed through and read as visual mush. The
categorical color comes from:

1. The **border** — saturated accent (`rgba(R, G, B, 0.9)`).
2. A **subtle mix into the dark base** — e.g., the train label fill is
   `rgba(32, 26, 50, 0.92)`, a dark base with ~15% purple mixed in; opaque
   enough to block what's behind, tinted enough to read as categorically
   purple.

The rule rules out §7's state-badge pattern of a saturated solid fill
(yellow / purple / amber). That treatment is reserved for transient state
badges, not permanent labels — when a label uses solid saturation, it
"steals" a state hue from the canvas alert palette and identity ends up
shouting louder than the actual changing state.

Concrete fills currently in use:

| Label             | Fill                          |
|-------------------|-------------------------------|
| Platform default  | `rgba(15, 15, 18, 0.85)`      |
| Platform hover    | `rgba(15, 15, 18, 0.92)`      |
| Station default   | `rgba(15, 15, 18, 0.92)`      |
| Station hover     | `rgba(50, 35, 5, 0.92)`       |
| Train             | `rgba(32, 26, 50, 0.92)`      |

References: `_drawLabel` in
[renderer-stations.js](../static/js/renderer-stations.js); `drawTrainLabel`
in [renderer-trains.js](../static/js/renderer-trains.js); `drawPlatformLabel`
in [renderer-platforms.js](../static/js/renderer-platforms.js).

### Borders are screen-pixel-capped

Border widths use a screen-pixel cap, not `lineWidth = n * scale`. The naive
form looks fine at default zoom but at scale 6+ explodes into a 12–15 px
wall that overwhelms the chassis underneath. The cap formula:

```js
// Static identity (default)
Math.min(Math.max(0.8, s * 0.25), 1.5);
// Live identity (train)
Math.min(Math.max(1.0, s * 0.35), 2.0);
// Hover / emphasised
Math.min(Math.max(1.2, s * 0.4), 2.5);
```

Pick the cap by tier — live and hover are slightly thicker so the visual
hierarchy survives at every zoom. The same formula shape applies to
highlight strokes (below).

### Z-order is liveness-based

Draw order in [renderer.js](../static/js/renderer.js) `drawNetwork`:

1. Track chassis + block colors (bottom)
2. Platform bodies + platform labels (identity tier)
3. **Station labels — default, non-hovered** (identity tier)
4. Signals, debug overlays, block detail labels (state)
5. Route overlays
6. Trains + train labels (live)
7. **Station label — hover overlay** (user attention, top)
8. Box / lasso select overlay

The rule: **static identity sits under anything live or state-bearing.** A
train passing through a station paints *over* the station's name — the
station's identity is permanent context, the train is the thing the user is
watching.

The exception is the hover state. When the user is actively pointing at a
station label, that one label gets a second pass at the very top of the
stack so it wins over the moving train. `drawStations()` runs at step 3 for
all non-hovered labels; `drawHoverOverlay()` runs at step 7 for just the
hovered one.

### Highlight strokes

When a hovered label points to member entities (station label → platform
bodies), the members get a "highlight stroke" outlining their shape. Rules:

- **Same accent color as the hovered label's border.** The label and its
  members read as one connected gesture (one focus).
- **Lower opacity than the label** (~0.7 vs 0.9). Highlights are *context*,
  not the focus.
- **Screen-pixel-capped stroke width** — same cap pattern as label borders:
  `Math.min(Math.max(1.5, s * 0.5), 2.5)`.
- **Closed paths with round joins and caps**, not per-segment strokes. The
  naive per-segment form caps each line's endpoint independently and the
  corners come out chunky.
- **No fill tint over entity bodies that carry their own state** (trains,
  blocks). A tint over a train would visually steal the train's state
  color — the §12 hue-fights-hue problem.

Reference: `_drawPlatformHighlights` in
[renderer-stations.js](../static/js/renderer-stations.js).

### Overlap

Two labels at the same screen position is acceptable when the higher-tier
label wins via z-order + opaque fill: the lower-tier label is partially
hidden but the foreground label stays fully legible. The opacity rule above
(no bleed-through) is what makes this work — without opaque fills, two
overlapping labels mix into illegible mush instead of one winning cleanly.

Physical collision avoidance — repositioning lower-tier labels around
higher-tier ones — is out of scope for the current system. If overlap
proves genuinely confusing in a real layout, raise it as a doc question;
don't freelance a one-off nudge.

### Don'ts

- **No saturated solid fills** on permanent canvas labels. That's the §7
  state-badge pattern; it belongs to transient alerts.
- **No `lineWidth = n * scale`** without a screen-pixel cap. At any zoom
  above ~3 the border becomes a wall.
- **No fill-tint highlight** over an entity that carries its own state
  (train, block). Hue-fights-hue, §12.
- **No new label tier** for a single new use case. If a fourth tier seems
  necessary, pause and raise the doc question. Three tiers cover everything
  the canvas currently has.
