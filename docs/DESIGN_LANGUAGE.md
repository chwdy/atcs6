# Design Language

This document is the single source of truth for the product's UI — the
composition principles, the token palette, the control and panel
vocabulary, and the canvas rendering rules. It governs every surface:
editor panels, the canvas, overlays, and the shell around them. Match
these patterns and tokens so every surface feels like the same product.

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

## Principles

Eleven composition rules that govern every surface — panels, rows,
forms, cards, canvas labels. They were learned on dense list views, so
the examples lean that way, but sections throughout this doc cite them
by name for forms, detail pages, and canvas work too.

1. **Proximity.** Labels and their values must touch, not pin to
   opposite edges of a wide container. The "STARTED" label and its
   timestamp belong next to each other, not at left and right margins of
   a 600px row. If you need leader dots to span the gap, the layout
   itself is wrong — leader dots are a confession that the gap should
   not exist.

2. **Container matches data.** Cards are for objects you act on
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

4. **Hero per row.** Pick a hero per row — whatever answers the user's
   primary question on this row. For mixed-task views — debug, monitor,
   and post-hoc review all on one screen — the hero splits into two
   layers: a stable row identity (same shape on every row, recognizable
   at a glance) plus per-task scan signals as columns (a duration column
   for the debugger, a status dot column for the monitor). The user
   picks their task by picking which column to scan, not by switching
   modes.

5. **Scan order.** The visual hero is not the same as the row name. The
   eye scans a list in two passes: a categorical filter ("show me only
   the DWELLs"), then a specific lookup ("which place is this STOP
   at?"). Visual weight should follow scan order — the verb (action
   type) often deserves more weight than the noun (place / identifier),
   even though the noun is what you'd call the row's "name." Same logic
   as log viewers bolding ERROR / WARN / INFO over the message text.

6. **Position semantics.** Position carries semantic weight: left edge
   = state filter / status. Right edge = numbers (right-alignable).
   Middle = identity. Top-right corner = primary action territory.
   Don't put low-stakes metadata in high-stakes positions — a "minor"
   pill floated top-right miscommunicates importance before a single
   pixel is colored.

7. **Format is the label.** A timestamp doesn't need "STARTED" written
   next to it. A colored dot doesn't need "STATE" written next to it. An
   arrow `→` between two times replaces "STARTED" and "FINISHED." But
   format tells you the *type*, not always the *role* — `#1000001` is
   unambiguously a number, but a reader can't tell *which* number
   (run? route? task index?) without a `Run` prefix. Drop labels where
   format makes the role obvious; keep them where it doesn't.

8. **Repetition blindness.** Anything that repeats identically on every
   row becomes invisible after the second instance — repetition trains
   the eye to skip. So either promote it to a single column header at
   the top, or remove it. Repeating it inline on every row is *worse*
   than nothing — it costs space and gets ignored anyway.

9. **Derived values.** Show derived values, not just inputs. If the
   user has to do math from your data — duration from two timestamps,
   percentage from two numbers, delta from two states — you've shipped
   half the feature. Compute the answer; the inputs become supporting
   evidence. When the derived value is *spatial* — what a
   `forward`/`reverse` token means on the map — draw it where the thing
   is (an arrow on the canvas while the editor is open) instead of
   spelling out coordinates in the panel.

10. **Column alignment.** Whatever field users compare across rows must
    live in the same x-position on every row. Cards-of-rows defeats
    this; row-shaped lists honor it. Comparison should be the easy
    case, not the hard one.

11. **No bandages.** Bandages reveal structural bugs: leader dots to
    span a gap, decorative dividers added to fill awkward whitespace,
    pills floated to balance an empty corner. Whenever you reach for a
    visual band-aid, stop — the layout underneath is the bug. Fix the
    structure, don't paper over it.

---

## Surfaces

Where UI lives. One mode toggle, and a fixed set of regions — every
feature has exactly one natural home.

The top bar's `Editor | Simulation` toggle is the app's primary split:
**Editor** is for building the layout (drawing track, configuring
blocks, switches, stations), **Simulation** is for running it (driving
trains, monitoring, routes). A surface that edits structure belongs to
Editor mode; a surface that observes or commands live trains belongs to
Simulation.

| Region | What lives there |
|--------|------------------|
| **Top bar** | App-level commands: play/pause, the mode toggle, New / Save / Load, panel toggles, settings. The one region where the 32px `--control-height-sm` controls belong (§1 Layout). |
| **Canvas** | The layout itself — two stacked `<canvas>` layers ([canvas.css](../static/css/layout/canvas.css); §16 "Two-canvas implementation"). |
| **Left sidebar** | Build tools (the §3 tool-tile grid) and the Properties pane for the current selection ([tools.css](../static/css/components/tools.css)). |
| **Secondary sidebar** | The working set: every open editor and list panel as a tab (§3 working-set tab strip; [shell.css](../static/css/layout/shell.css)). Detail editing and list browsing happen here. |
| **Bottom dock** | Continuous monitoring across many entities at once ([train-monitor.css](../static/css/features/train-monitor.css)). |
| **Floating panels** | Spot-check diagnostics that overlay anything; draggable, resizable ([inspector.css](../static/css/features/inspector.css)). |
| **Overlay layer** | Modals, toasts, floating pickers (§13). |

Placement rule for a new feature: edits one record at a time → a
secondary-sidebar tab; watches many records continuously → the bottom
dock; diagnostic spot-checks over arbitrary state → the floating-panel
pattern; builds or selects structure on the map → the left sidebar;
transient feedback → a toast; a blocking decision → a modal. If none of
those fits, raise it as a doc question before inventing a new region.

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
[frontend/src/lib/helpers.ts](../frontend/src/lib/helpers.ts). Pass `alpha` < 1
to get the rgba form used for canvas overlays. Track/block renderers reach it
through this resolver rather than hard-coding hex at the call site.

### Layout

| Token | Value | Use |
|-------|-------|-----|
| `--control-height-xs` | `28px` | Default — every control inside a panel (buttons, selects, text/number inputs, segments, icon buttons) |
| `--control-height-sm` | `32px` | Top-level forms **outside the panel shell** only, larger CTAs — do not use inside sidebar panels |
| `--control-radius-sm` | `6px` | All buttons, panel corners, cards |
| `--radius-xs` | `4px` | Inner elements — badges, small controls inside an already-bordered card |
| `--radius-md` | `8px` | Floating overlays — toasts, the hero CTA |
| `--radius-lg` | `12px` | Modal dialogs |
| `--radius-pill` | `999px` | Chips / pills (§7) |

### Spacing

Multiples of 2px, tokenized so new work composes from the same steps
the reference panels use. 1px is not a spacing step — it is the
hairline border width.

| Token | Value | Typical use |
|-------|-------|-------------|
| `--space-2` | `2px` | Gaps inside a control cluster (tab gap, tool-tile inner gap) |
| `--space-4` | `4px` | Chip-row gaps, tight padding |
| `--space-6` | `6px` | Card inner gaps, form row gaps |
| `--space-8` | `8px` | The standard padding unit — header and body padding |
| `--space-10` | `10px` | Panel horizontal padding |
| `--space-12` | `12px` | Section padding, column gaps |
| `--space-16` | `16px` | Empty-state padding, large section separation |

Existing CSS predates these tokens and spells the same values as
literals; adopt the token when touching a declaration, and consolidate
one-off odd values (5px, 7px, 9px gaps) to the nearest step on the
same touch (see Known divergences).

### Z-index

The layering registry. Pick the token for the kind of surface; never
hand-pick a number between layers.

| Token | Value | Layer |
|-------|-------|-------|
| `--z-canvas-world` | `1` | World canvas — track, blocks, signals (§16) |
| `--z-canvas-live` | `2` | Live canvas — trains, labels, selection (§16) |
| `--z-canvas-overlay` | `3` | DOM overlays pinned to the canvas |
| `--z-shell` | `10` | Sidebars, the bottom dock, sticky panel toolbars |
| `--z-picker` | `1000` | Floating pickers, modal backdrops |
| `--z-modal` | `1100` | Modal dialogs |
| `--z-restart` | `1200` | Full-app restart overlay |
| `--z-inspector` | `9000` | Floating debug panels |
| `--z-toast` | `10000` | Toasts — transient, never blocked |

The shipped layering has one inversion: the floating inspector (9000)
sits above modal dialogs (1100), so a blocking modal can be covered by
a debug panel. The modal should win (see Known divergences).

### Type

| Token | Value |
|-------|-------|
| `--font-main` | `'Inter', sans-serif` |
| `--font-mono` | `'JetBrains Mono', monospace` |

The size ramp is five working sizes plus the hero display size:

| Token | Value | Role |
|-------|-------|------|
| `--font-size-micro` | `9px` | State-badge text (§7), tool-tile labels |
| `--font-size-label` | `10px` | Field labels (§5), chips, disclosure summaries |
| `--font-size-body` | `11px` | Prose, tabs, checkbox labels, units |
| `--font-size-value` | `12px` | Field values, inputs, panel titles |
| `--font-size-title` | `14px` | Detail-page record titles (§11) |
| `--font-size-hero` | `28px` | Hero numbers (§9) |

13px appears in older panels but is not a step — it reads as "almost
body text" without being one; consolidate to 12px or 14px by role when
touching that CSS (see Known divergences). Top-level chrome outside the
panel shell (modal titles, the top bar) may use larger sizes; inside a
panel, stay on the ramp.

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
  Otherwise prefer Tabler so the vocabulary stays consistent. The trap
  with Unicode in an action button is rendering: Inter doesn't ship many
  pictographic glyphs (e.g. `⟳` / `↻` for refresh), so the browser falls
  back to Arial and Arial's baseline sits the glyph low inside the 28px
  box. The button reads as "misaligned" even though the box-model
  alignment is exact. Tabler glyphs render through the icon font, which
  centers cleanly. If a refresh / rotate / arrow / play action wants a
  glyph, use the Tabler equivalent (`ti-refresh`, etc.) — never the
  Unicode pictograph.
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
- Icons are Tabler glyphs (`<i class="ti ti-… tool-icon"></i>`), looked up through the icon roster like every other button — see Icons above. Labels are 1-2 words at ~8px.

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

For the EB / emergency-stop control. The control has a full lifecycle,
and every state is specced — this is the most safety-relevant control
on the screen, so nothing about it is left to improvisation.

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

- **Armed (no EB):** the danger-tint button above. The solid-fill hover
  is the one solid fill permitted inside a panel — emergencies need to
  shout.
- **EB active:** the status header shows the pulsing EB badge — danger
  text on a 15% danger tint with a 1px danger border, driven by
  `tc-pulse-danger` (0.8s infinite, opacity 1 → 0.55 → 1; pulse is
  reserved for this tier, see §18 Motion). The button row swaps to the
  reset variant.
- **Reset** (`.tc-eb-reset-btn`): same 40px height and uppercase
  weight-700 type as the EB button, but `--bg-dark` bg with
  `--warning-color` border + text; hover escalates to a solid warning
  fill with dark text. Yellow because releasing an emergency is a
  caution act, not a go act — the operator is confirming the danger has
  passed, not being invited to move.

Reference: `.tc-eb-btn`, `.tc-eb-reset-btn`, `.tc-eb-badge` in
[features/train-control.css](../static/css/features/train-control.css).

### Segmented control

A row of mutually-exclusive buttons inside a single `var(--bg-dark)` container with a 1px outer border, **no internal dividers**. Buttons inside are flat ghosts — the active accent tint and hover bg-tint give every cell its own legible state without needing vertical hairlines. Active button: `color: var(--accent-hover)`, `background-color: rgba(130, 87, 229, 0.18)`. Hover: same accent color, `var(--bg-hover)` background. The container's outline does the grouping. For when to reach for this over a select, tab strip, or checkbox, see §4 "Choosing a selection control."

**Equal-width by default; `fluid` for long labels.** By default each cell takes an equal share of the row. That breaks when the labels differ in length — `Available | Manual | Disabled` forced into thirds clips the longest word to `Disable…`. When a small fixed set has labels too long for equal cells, add a `fluid` modifier: each cell sizes to its own label and the row wraps to a second line rather than clipping. The set is still small and fixed, so it stays a segment (§4) — `fluid` changes only how the labels fit, not the control. Reach for it whenever a label would otherwise be cut off; don't pre-shrink to a cryptic abbreviation to keep one row, and don't drop to a select just because a label is long.

Reference: `.switch-group` / `.switch-btn` (+ `.switch-group.fluid`, and the `.success` / `.danger` / `.warning` variants) in [components/design-system.css](../static/css/components/design-system.css) is the canonical segmented control — every panel that needs one consumes it directly. The active cell is the accent **tint** (`rgba(130, 87, 229, 0.18)` + `--accent-hover`), never a solid fill, and there are **no internal dividers** — the container outline groups the cells and the active tint marks the selection. The train-control HMI (`.tc-seg-btn`) and train-monitor view-distance (`.tm-seg-btn`) carry their own copies of this shape (see Known divergences).

### Tab strip

A row of mutually-exclusive labels that switches the body content below.
Use this for **content switching** (panes); use the segmented control for
switching a setting inside a single pane. The full chooser decision — tab
strip vs segment vs select vs checkbox — lives in §4 "Choosing a selection
control."

- **Container** — a flex row with `padding: 0 8px`, `gap: 2px`, and `border-bottom: 1px solid var(--border-color)`. That bottom rule is the divider between the strip and the content beneath; it's the only border in the strip. Keep the horizontal padding tight (8px) so tabs sit near the strip's left edge rather than floating inset.
- **Tabs** — no border, no fill by default. 11px Inter, `--text-secondary`, `padding: 6px 6px`. Padding is deliberately tight: a tab is text + a click target, not a button — let the words speak.
- **Hover** — `background-color: var(--bg-hover)`, `color: var(--text-primary)`.
- **Active** — `color: var(--accent-hover)`, plus a 2px `border-bottom: var(--accent-color)` with `margin-bottom: -1px` so the active underline overlaps the strip's bottom rule (the active tab "owns" that segment of the divider). Inactive tabs carry a 2px transparent `border-bottom` so the active underline doesn't shift heights between tabs.

**Working-set variant (editor sidebar).** The secondary sidebar's strip
holds an open-ended, data-driven set of open panels, not a fixed pane
set, so it relaxes two rules: each tab carries a close `×`, and the row
never wraps — it stays one line and scrolls horizontally, so every open
tab stays reachable and the strip never eats the panel. Tabs are titled
by the record they hold (`BLK_588D28`, `#1000001`), never by the
editor's name — two open blocks must read apart at a glance. List tabs
(all trains, all routes) keep their list name, in bold, so collections
read apart from instances. Labels clamp with ellipsis (opaque
identifiers, §5 Overflow); the tab tooltip carries the full lineage
(`Blocks › BLK_588D28`).

Reference: `.secondary-sidebar-tabs` / `.secondary-tab` in
[layout/shell.css](../static/css/layout/shell.css).

### Accent-tint CTA (the panel-safe "primary")

The one button in a section that should draw the eye — Apply, Save — without
a solid fill. Same ghost shape as the others, but the accent shows as a ~18%
tint that reads as "attention," not "shout." A transparent border keeps it
the same height as a `.btn-outline` beside it; hover deepens the tint and
adds the accent border.

```css
background-color: rgba(130, 87, 229, 0.18);
color: var(--accent-hover);
border: 1px solid transparent;

/* hover */
background-color: rgba(130, 87, 229, 0.28);
border-color: var(--accent-color);
```

Use at most one per section, on that section's main action; everything else
stays a plain ghost (`.btn-outline`), and destructive actions use the
danger-tint `.btn-danger`. This is the shared class for the §12 "Save"
treatment. Reference: `.btn-accent` in
[components/common.css](../static/css/components/common.css).

### Don't use solid-color filled buttons in panels.

Reserve filled `var(--accent-color)` buttons for top-level page CTAs, not panel toolbars. The ghost + accent-tint pattern (above) keeps panels visually quiet.

---

## 4. Form controls in panels

### Choosing a selection control

Before styling anything, pick the *shape* by the kind of choice the user is
making. Two questions settle it. The shapes themselves are defined
elsewhere — tab strip and segmented control in §3, select and checkbox in
this section — this is the rule for choosing between them.

```
Independent on/off flags, or picking one option?
│
├─ independent on/off (any combination valid) ───────► Checkbox
│                                                       (one, or a column of several)
│
└─ picking exactly one of N
   │
   ├─ the pick swaps which content/pane is shown ─────► Tab strip          (§3)
   │
   └─ the pick sets a value on the current pane
      │
      ├─ small fixed set (2–4),
      │  all worth showing at a glance ───────────────► Segmented control  (§3)
      │     (long labels → size-to-content + wrap,
      │      the `fluid` variant — still a segment)
      │
      └─ many / variable-length
         / data-driven set ───────────────────────────► Select (dropdown)
```

Two independent axes, don't conflate them. **Cardinality** (how many
options, how open the set) picks segment-vs-select: small + fixed +
known-at-author-time → segment; large / variable / data-driven → select.
**Label length** only picks the segment's *layout*, never the control: a
small fixed set with a long label stays a segment and switches from
equal-width cells to the size-to-content `fluid` variant (§3) that wraps
instead of clipping. A long label is not a reason to reach for a dropdown.

| Control | The choice is… | Reach for it when | Defined in |
|---------|----------------|-------------------|------------|
| **Checkbox** | on / off | one independent boolean, or a set of flags where any combination is valid | below |
| **Segmented control** | one of N | the set is small (2–4), fixed, and worth showing all at once (long labels → `fluid` variant, §3) | §3 |
| **Select (dropdown)** | one of N | the set is large, variable-length, or data-driven | below |
| **Tab strip** | which pane | the pick swaps the content shown, not a stored value | §3 |

**There is no radio-button control.** A single pick from a small fixed set
*is* the segmented control — that is the radio button of this system. (One
native `<input type="radio">` survives in the block-extend picker, which is
really a data-driven list; render that kind of thing as a select or a list
of flat-ghost rows, not radios.)

**Tie-breakers:**

- *Segmented vs select — the 4–5-option gray zone.* This is about option
  *count*, not label length. Past ~5 options a segment row gets unscannable
  even when each cell is tiny — that's when to switch to a select. (Long
  labels on a small set are a layout problem, not a control problem: the
  `fluid` variant wraps them — see the two-axes note above.) And a set built
  from data (trains, route targets, stations) is always a select however
  short it happens to be today — its length isn't yours to fix at
  author-time.
- *Checkbox vs a 2-cell segment.* If the unchecked state needs its own name
  to be understood, it is not a checkbox. `[Wait | Skip]` is a segment
  because "Wait" is a real labeled choice, not "the absence of Skip." Use a
  checkbox only when "off" is self-evidently just "not on" (Force, a debug
  overlay).
- *Placement of a modifier.* A checkbox or segment that changes what a
  button will do sits in the same flex cluster as that button, never a row
  away — see §12 "Action modifiers must touch their action."

### Spatial inputs (set on the map, not typed)

Some fields are spatial — a tile location, a facing direction — and the
honest place to set them is the map, not a typed coordinate or a free
compass list. This is the input mirror of the Derived-values principle's
spatial note (Principle 9): if the value lives somewhere on the canvas,
put the user there.

- **Location is picked, not typed.** A "Pick on map" ghost button (§3,
  with the `map-pin` glyph) arms a placement mode; the next canvas click
  sets the value. The panel shows the result as a read-only mono
  coordinate with a "Re-pick" affordance — never a pair of raw X / Y
  number inputs, which make the user read a position off the canvas and
  transcribe it back.
- **Offer only what the geometry allows.** A direction field reads its
  options from the thing under the cursor, not the full compass. The
  Add-Train "Facing" segment is built from the clicked track's real exits
  (`getTrackExits`), so a straight E–W track offers `Cab A · → East` /
  `Cab B · → West` and nothing the track can't hold. The set is read from
  data but stays a **segment**, not a select: a tile's exits are bounded
  by geometry to a tiny fixed few, so the data-driven→select tie-breaker
  above (which is about open-ended sets — trains, stations) does not
  apply. When the count isn't the usual two (a switch tile), the cells
  fall back to plain direction labels.
- **Preview the result where it lands.** While a spatial field is being
  set, draw what it will produce on the canvas — see §17 "Selection and
  previews" for the placement ghost that shows a new train's footprint and
  facing before commit.

Reference: `showAddTrainDialog` and the `place-train` mode in
[editor-trains.ts](../frontend/src/editor/editor-trains.ts).

### Control row layout

A zone of segmented controls (Power / Mode / Cab in the train panel)
follows two rules so nothing clips when the sidebar narrows.

**Stack the label above a full-width segment** when the segment has three
or more cells, or whenever the sidebar gets narrow. A label sitting
*beside* its segment competes with it for horizontal room — a 3-button
segment on a 240px sidebar then splits into cells too small to read, and
the longest option clips. Stacking hands the segment the full content
width, so the longest label can't clip at any size. **Same-category
controls share one layout:** if Power and Mode stack, Cab stacks too — a
lone control laid out unlike its siblings reads as a mistake even when
each is fine on its own. Reference: `.tc-controls-pair` in
[features/train-control.css](../static/css/features/train-control.css) —
label-above cells that flow and wrap, with the wider 3-cell Cab taking
its own full line.

**Keep navigation off a setting's row.** A link that jumps to another
panel (`Monitor →`, `Route Run →`) is not part of the setting beside it.
Sharing the row makes the segment and the links `flex: 1` siblings that
split the width evenly and starve the control — the
Container-matches-data and Position-semantics traps (see Principles),
in a form row instead of a list row. Give
navigation its own row below the controls.

### Text, select, and number inputs

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

Compose sidebar form rows from these primitives — `.input-large-*` for a
prominent field (a name or id) and `.input-small-*` for a compact
label-beside-input row — not from `.form-group`. `.form-group` is the
**modal-body** form-row primitive (§13); it has no sidebar styling, so a
panel that reaches for it falls back to the browser's default oversized
inputs instead of the 28px control language.

Reference: `.inspector-panel-field select` in
[features/inspector.css](../static/css/features/inspector.css).

### Checkbox

The on/off primitive — see "Choosing a selection control" above for when a
checkbox is the right shape rather than a segment or select. The native
input is visually hidden and a 14px box stands in for it: a 2px
`--border-color` outline on a dark fill with a 3px radius when unchecked,
filling with `--accent-color` plus a white check when checked, and a 2px
accent ring on focus. Label is 11px `--text-primary`. Hover lifts the box
border to the accent (`--accent-hover` once checked); a `.disabled` wrapper
drops the whole control to 40% opacity.

- **A set of flags stacks into a grid, not a row of segments.** Independent
  display toggles — the debug panel's Track Bits / Coordinates / Signals /
  … — lay out as a 2-column grid so the eye runs down a column of labels.
  Each is its own boolean; none excludes another.
- **Use the styled box, never a bare native input.** `.checkbox-custom` is
  the canonical chrome; a raw `<input type="checkbox">` renders the OS
  default, which ignores the token palette and breaks the dark theme. Every
  checkbox in the editor panels uses `.checkbox-custom`, including the
  Assign-row "Force" checkbox.
- **Mixed / "some but not all" → indeterminate.** When a checkbox edits a
  flag across a multi-item selection and the items disagree, set the input's
  `indeterminate` property: `.checkbox-custom` fills with a horizontal accent
  dash (the standard tri-state). The first explicit click clears it and
  commits a definite value — and only that click marks the field "touched,"
  so an untouched mixed flag is never written back to the selection.

Reference: `.checkbox-wrapper` / `.checkbox-custom` / `.checkbox-label` in
[components/design-system.css](../static/css/components/design-system.css);
the multi-flag grid is `.debug-controls` in the same file.

### Two views of one value (visual editor + control set)

Some values get edited two ways at once — a spatial **visual editor** (a
clickable diagram) beside a precise **control set** (a column of
checkboxes), both writing the same value. The track-bits editor pairs a
clickable compass with six segment checkboxes over one 6-bit value. Four
rules keep the pairing clean:

- **One container, not one box per view.** The section the pair lives in
  *is* the container. Don't wrap each view in its own titled, bordered
  card — that is boxes-inside-boxes (§14 "No nested panels" / "Don't
  border the container *and* every child") and the sub-headings only
  repeat the section title (Repetition blindness). Lay the two views straight
  into the section body.
- **The narrow control column hands the spatial view its width.** Stack
  the controls in a single narrow column — about one label wide — not the
  2-column flag grid the Checkbox rule above defaults to. The diagram then
  takes the rest of the row and reads large, which is the whole point of a
  spatial editor. Put any value readout full-width *below* the pair so its
  length can't widen the column and squeeze the diagram.
- **One validated commit path for both views.** Route a checkbox toggle
  and a diagram click through the same commit function, so the same guards
  and the same "this removes a dependent entity" confirmation fire
  whichever view the user touched. Separate paths drift — ours had the
  diagram auto-saving while the checkboxes waited for an Apply button, and
  the diagram's path silently skipped the confirmation the button's path
  ran. Auto-apply every edit and drop the Apply button (the auto-save
  contract, §5C "Avoid per-field Save buttons"); a rejected edit
  re-renders from the server, which reverts the optimistic view.
- **An in-panel editor canvas is not a map label.** §16 governs labels
  drawn on the map; an editor canvas inside a panel has two rules of its
  own. (1) The clickable line must read at least as bright as the
  decoration around it — the toggle-able segments were drawn dimmer than
  the diagram's own frame, so they looked un-clickable; the affordance
  outranks the chrome. (2) Render at device resolution when the canvas is
  a focal element — set the backing store to `size * devicePixelRatio`
  with `ctx.scale(dpr, dpr)` so a larger diagram stays crisp on retina;
  keep drawing in logical units and map clicks back through
  `logicalSize / rect.width`.

Reference: the Track Bits editor — `.track-bits-*` in
[components/design-system.css](../static/css/components/design-system.css);
`updateTrackBitsPanel`, `initBitEditor` + `_commitTrackBits` (the shared
commit path), and `initVisualTrackEditor` (canvas) in
[frontend/src/editor/](../frontend/src/editor/).

### Slider / handle

For continuous (power %) or notched (master controller) inputs. Native
`<input type="range">` with custom track + thumb styling. Two patterns:

- **Plain slider** (`.tc-power-slider`) — 6px-tall `--border-color` track, circular thumb in the relevant accent (e.g. `--success-color` for a power slider).
- **Notched handle** (`.tc-handle-slider`) — 8px-tall track with a multi-stop gradient that encodes the notch zones (green=power, gray=neutral, yellow=brake, red=EB). Rectangular grab-handle thumb with an accent halo. Above the track, a 15-column tick grid renders the discrete notch labels; the active notch is highlighted with the same accent tint used by the segmented-control active state. The zone colors are master-controller convention — power / neutral / brake / emergency zones — not the §1 status semantics: yellow here means "brake zone," which is normal driving, not a warning. The handle gradient is the one place these hues are positional rather than status-bearing.

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

### C. Multi-column form section

When a section holds 2–3 short independent fields that fit side by side
— `Acquisition`, `On waypoint unreachable`, `Priority` — lay them out
as N columns of `[label / control / action]` with **shared row tracks
across columns**. The label row sizes to the tallest label, the control
row to the tallest control, the action row to the tallest action. Eye
scans down a single horizontal band of controls regardless of whether
one label wraps to two lines and the others stay on one.

```
┌─ Acquisition ──────┬─ On waypoint ─────┬─ Priority (0-9) ─┐
│                    │ unreachable       │                  │
├────────────────────┼───────────────────┼──────────────────┤
│ [Best | All]       │ [Wait | Skip]     │ [ 5 ]            │
├────────────────────┼───────────────────┼──────────────────┤
│ [Reset]            │                   │                  │
└────────────────────┴───────────────────┴──────────────────┘
```

The CSS recipe is grid + subgrid: the parent declares columns and rows;
each column is a grid that inherits the parent's row tracks via
`grid-template-rows: subgrid`. Separate-grid columns (or plain
flex children) **don't** share row heights; subgrid is what makes the
horizontal band actually line up.

```css
.parent-row {
    display: grid;
    grid-template-columns: 1.5fr 1fr 80px;
    grid-template-rows: auto auto auto auto;  /* one row per inner element */
    column-gap: 12px;
    row-gap: 6px;
    align-items: start;
}

.parent-row > .column {
    display: grid;
    grid-row: 1 / -1;
    grid-template-rows: subgrid;
    min-width: 0;
}
```

`grid-template-rows` has one `auto` per element in each column's inner
sequence — three for `label / control / action`, four for
`label / control / sublabel / sub-control`, etc. Subgrid makes the row
heights snap across columns regardless of how many you list.

Pick column widths by content, not by equal flex. A segmented control
with long labels (`Best effort | All or nothing`) needs more `fr` than
one with short labels (`Wait | Skip`); a single-digit number input is a
fixed `80px`. Comparable cells share row position; they don't have to
share width. Equal `1fr 1fr` is the wrong default whenever the columns'
controls hold different content widths.

Per-column optional actions (a Reset button when the field carries an
override) live in the action row. When no column has an action, the
auto-sized row collapses to 0 px — no dead space when nothing is
overridden.

For 4+ fields, or when a label column would dominate the width, prefer
a stack of inline field rows instead (label · control · action on one
horizontal line per field). The multi-column shape works best for ~3
short fields where every column has the same internal shape.

**Avoid per-field Save buttons in this shape.** Three buttons that all
say "Save override" stacked side by side hit the Repetition-blindness
principle hard. If each field is independent and the
operation is reversible (Reset is always available), auto-save on
change is the cleaner contract: segment clicks and input `change`
events POST the override directly, no confirmation step. If explicit
confirmation is needed, one Save for the whole section beats N
parallel saves.

References: `.route-header-row.run-overrides-grid` in
[features/routes-paths.css](../static/css/features/routes-paths.css)
(3-row inner sequence — label / control / reset), consumed by the
Per-run overrides section in `editor-route-runs.ts`; and
`.route-header-row.route-policy-grid` in the same file (4-row inner
sequence — label / control / "Push to runs:" sublabel / soft-force
segment), consumed by the policy row in `editor-routes.ts`. When the
cluster appears in a third panel, promote the rules into a shared
`.field-cluster*` family in `components/status-blocks.css` and have
each panel layer only its column widths and per-cell content on top
(the promotion rule in "Where the primitives live").

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
   list rows. The older status-column pattern used by `.route-row` and
   `.path-row` predates this rule and is not the template for new work
   (see Known divergences).

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
border-radius: var(--radius-pill);
padding: 1px 8px;
font-size: 10px;
font-family: var(--font-mono);
```

For state badges (`▶ ATO RUNNING`, `EB ACTIVE`), tint the background at
~15% opacity of the relevant accent and use the accent as the text color.

**All chips in a row share the same shell.** `--bg-dark` bg, 1px
`--border-color` border, 999px radius, 10px mono, 1px 8px padding. Color
variants change only `color` and `background-color`. The `.chip-state`
badge treatment additionally sets `border-color: transparent` — the
tinted fill is the affordance, and the transparent border keeps the box
the same size as its bordered neighbors — and switches the type to 9px
uppercase weight-700 Inter so a state badge reads apart from a data
chip. Don't mint per-feature chip classes (no
`.foo-direction-pill`, `.foo-error-chip`); add modifier classes
(`-success`, `-warn`, `-danger`, `-muted`) on a single base class
instead. A row of mismatched chip shapes reads as cluttered even when
each one is individually correct. The canonical base class is `.chip` /
`.chip-state` / `.chip-{success,info,warn,danger,muted,accent}` in
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

- **Empty**: the canonical `.empty-state` class — `padding: 16px 12px; text-align: center; color: var(--text-secondary); font-style: italic; font-size: 11px;` ("No elements ahead", "No path assigned", etc.). Every panel consumes `.empty-state` directly instead of minting a per-feature empty class (`.trains-empty`, `.rr-empty`, `.routes-empty-state`, … had drifted into ten near-copies with different padding, some missing the italic, one hardcoding the grey). Canonical home: [components/status-blocks.css](../static/css/components/status-blocks.css).
- **Actionable empty** (`.empty-state-action`): when the panel is a list the user fills and the next step is obvious — no routes yet, no trains assigned — the empty state is a teaching surface, not a shrug. Same shape as `.empty-state` but not italic, stacking one plain-language line over one `.btn-accent` CTA ("No routes yet" / `Create route`). Use it wherever a creation or assignment action exists for the empty collection; pure readouts ("No elements ahead") keep the passive form. See §19 Learnability.
- **Loading**: same shape, replace italic with subdued spinner glyph if needed.
- **Status-colored empty**: `.empty-state-warn` / `.empty-state-error` keep the shape but recolor the text to `--warning-color` / `--danger-color` and drop the italic (the color carries the meaning) — e.g. "Train no longer exists", "Error: …".
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

References: `.tc-card-destination` (full card), and the **`.entity-card`
family** in [components/common.css](../static/css/components/common.css) —
the compact (4–6px padding) card used for every entity readout in the
Properties pane (block / signal / switch / station / train) and the
train-editor header. It is **one** shared container, not five per-entity
classes: the 3px left-border is driven by a `--card-accent` custom property
the renderer sets inline — a dynamic state color for block / switch / signal
(aspect), or a static palette value for fixed identities (station =
`--info-color`, train = `--accent-color`). Inner parts: `.entity-card-dot`
(10px status dot — named for the family so it doesn't collide with the 8px
debug `.status-dot`), `.entity-card-body`, `.entity-card-id` (mono
identifier), `.entity-card-meta` (secondary line, e.g. station name),
`.entity-card-state` (uppercase state). These are **cards, not §12 rows**:
each entity is a distinct object acted on as a whole (its own Edit button),
not a homogeneous record compared down a column.

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
  (see the Format-is-the-label principle).
- **Display name** — the human-readable name. Wraps freely, never
  ellipsis (see §5 "Overflow: identifiers vs names").

**2. Stats block** — a Standard card directly below the header, with:

- **Hero stat** (§9 hero number) at top — `X / Y unit` for progress
  counts, or a single big number for a single metric. Color by record
  state (success / info / warn / muted from §1 Accents).
- **Metadata rows** — 1–3 short rows of compact prose with `·`
  separators. Group related fields per row (timing on one, mode &
  direction on another, planning indices on a third). Spell words out
  when there's room — `forward · east` over `F·E` (Density follows
task). The
  compact form lives in the `title` tooltip for power users who want it.

This pattern replaces the older §5A field-row dl for detail pages.
Edge-pinned label/value pairs (`STARTED ........... 2:42:37 PM`) violate
the Proximity principle and waste vertical real estate — the hero +
metadata-strip variant carries the same information in roughly half the
height with no dotted leaders.

Reference: the canonical header block lives in
[components/status-blocks.css](../static/css/components/status-blocks.css)
as `.status-header` + `.status-header-{row,id,name,ident,role}` +
`.status-sep`, with the `.accent-{success,info,warn,danger,muted,accent}`
left-border helpers from the same file (`accent` = purple, for a selected /
active record). Those helpers must be declared **after** `.status-header` in
the stylesheet: `.status-header` sets its border color through the
`border-left` shorthand, so a same-specificity `.accent-*` override only
wins when it comes later in source order. The stats block (route-runs
specific) is `.rr-summary` in
[features/routes-paths.css](../static/css/features/routes-paths.css).

### Composing a multi-select / batch-action panel

When the panel acts on a **set** of selected entities at once — a drag-select
in the editor grabbing many tiles that span several blocks, platforms,
switches, and signals — the top is a selection-summary callout and the body
is grouped action sections. It is the multi-entity sibling of "Composing a
detail page top": the same `.status-header` callout, but it describes a
selection rather than one record.

**1. Title row** — the section heading (`SELECTION`) with a flat-ghost
`Clear` dismiss top-right (Position semantics). Keep `Clear` off the count line
so the counts get the full callout width.

**2. Summary callout** — a `.status-header` block with the `.accent-accent`
(purple = selected) left-border, holding two stacked rows:

- **Count line** — one `N type` segment per entity kind present, joined by
  `·`, zero-count kinds dropped. Each number is tinted to match that kind's
  chip hue, so the colour itself is the legend. Keep each `N type` segment
  un-breakable (`white-space: nowrap`) and put a **real space** around each
  separator — with no break opportunity between glued segments the line
  overflows instead of wrapping. Bind the `·` to the end of its segment so a
  wrap never orphans a separator at the start of the next line.
- **Chip row** — one §7 chip per entity, reusing the canonical variants by
  kind: block = `.chip-accent`, platform = `.chip-info`, switch =
  `.chip-warn`, signal = `.chip-success`, raw tile = `.chip-muted`. List the
  meaningful structural entities first and the bulk (tiles) last, then cap
  the row height and scroll — the structural chips stay visible; only the
  substrate scrolls out.

**3. Action sections** — stacked groups (hairline divider + 10px uppercase
label), ordered by the user's primary task on this selection. Each group's
main action is a single accent-tint `.btn-accent` CTA (§3); supporting
actions are plain `.btn-outline` ghosts; irreversible actions are the
danger-tint `.btn-danger`, kept together in a `Destructive` group at the
bottom. Batch property edits use one `Apply to N` button for the whole group
rather than per-field saves (§5C); an untouched or mixed field is left out of
the write (see §4 indeterminate checkbox).

Reference: `.ms-*` in [layout/shell.css](../static/css/layout/shell.css),
consumed by `editor-multi-select.ts`.

---

## 12. Composing list rows (rows vs cards)

When the list is dense, sequential, comparable records — log entries,
journey steps, run history — the right container is a row, not a card
(the Container-matches-data principle). Reach for this section *before*
picking chrome; the wrong container can't be fixed by polishing the
cards inside it. The §11 "Composing a dense row" subsection covers what
goes inside a card-as-list-row when cards genuinely are the right
shape; this section is the worked practice of the Principles section
for the row-shaped alternative.

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
  anomaly, dim = ok. The color is the label (Format is the label); the
  column is scannable down the left edge (Column alignment).
- **Ordinal** (`3`, `4`, `5`) — muted mono, narrow, `--text-tertiary`.
  A stable reference anchor for "the third step," not the hero.
- **Verb** (`STOP` / `DWELL`) — bold accent-tinted chip, the
  categorical filter the eye scans first (Scan order). Sized for the
  filter pass, not the identity pass.
- **Noun** (`STA_DDB6BE` / `MAIN_WEST`) — bright mono, medium-large,
  `--text-primary`. The identity — what the row IS.
- **Modifiers** (`platform · fwd`) — small, muted `--text-secondary`.
  Useful when present, invisible when not. Never repeated when empty
  (Repetition blindness).
- **Timestamps + derived duration** — when both start and finish exist,
  stack them on two lines (start on top, finish + duration on bottom);
  vertical sequence is the label (Format is the label). The duration is the
  brighter element next to the finish because that's the answer the
  debugger is scanning for (Derived values), and the moment the duration
  becomes known. Right-aligned so durations align down a column
  (Column alignment). Running tasks collapse to a single line (start time
  only) — the row's brightness signals ongoing, so no arrow is needed.
- **Current row** — brightness channel only. No marker column —
  instead, the running row's text goes full-bright and the row picks up
  a subtle `--bg-hover` background tint. In a post-hoc replay no row is
  "live" and the whole list reads muted; status dots carry the story.
- **Header row** — carries `#`, `type`, `target`, `detail`, `time` once
  at the top in `--text-tertiary` 10px uppercase. Never repeated inline
  (Repetition blindness).

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

### Reusing the row pattern across panels

The same dot · ordinal · verb · noun · modifier · action skeleton works
for every "homogeneous comparable record" view in the app. The route-runs
editor was the first consumer (`.rr-task-*` in routes-paths.css); the
route editor's Assigned Trains list became the second (`.atr-*`, same
file). When this skeleton appears in a third place, promote the rules
into a shared `.list-row*` family in `components/status-blocks.css` and
have each panel layer only its own column-set and per-cell content on
top — keep the grid container, status dot, verb chip, action column,
and row hover identical across panels so the eye trains once (the
promotion rule in "Where the primitives live").

Worked example — the assigned-trains row uses the same skeleton with
the time column swapped for an `Unassign` action button:

```
●  T1   DWELL   Station 1    forward · 7s left   [Unassign]
●  T2   STOP    S2 South     forward             [Unassign]
●  T3   IDLE    queued       —                   [Unassign]
```

- **Dot** — warn (amber) while dwelling, danger (red) when the live
  status payload is missing, transparent when ordinary running. Same
  hue channel rules as the route-runs version; "noteworthy" means a
  hand to play (not "important row" — that's the brightness channel).
- **Identity** (`T1` etc.) — bright mono. The train ID is the row's
  stable identity, sits in the Hero-per-row stable-identity slot.
- **Verb** (`DWELL` / `STOP` / `IDLE`) — same `verb-info` / `verb-warn`
  / `verb-muted` token set as `.rr-task-verb-*`; trains the eye across
  both editors.
- **Modifier** — derived dwell-remaining + direction. Sanity-cap the
  remaining-seconds value before display (here: suppress anything
  outside `0…3600`) — the §14 "Don't render unit-mismatched numbers
  raw" rule.
- **Action** — `Unassign` is a ghost button, **not** danger-colored:
  unassign is reversible. Danger (red) is reserved for irreversible
  destructive actions (Delete Route). Mixing the two devalues the hue
  for the genuinely destructive case (§6).

### Action modifiers must touch their action

Per the Proximity principle, a checkbox or toggle that modifies the
behavior of a button must live in the same visual cluster as the
button. A "Force reassign" checkbox two rows below an Assign button
trains users to ignore the checkbox — they see it as decoration, not as
an input that changes the next click. Put it in the same flex row:

```
[ select-train ▾ ]  [☐ Force]  [Assign]
[ Assign on map ]
```

Same principle covers any "execute with options" pattern: per-task
direction picker next to the move button, per-row enable toggle next to
the row action, etc.

### Editor navigation and the unsaved-edits indicator

Navigation up and out of an editor lives in the sidebar's shared header
chrome — one implementation for every panel — never inside any one
panel's body:

```
[← Routes]  Route Editor                    [pin] [×]
[Trains][T_NWY_1][Routes][RT_4F2A]   ← one tab row, scrolls
```

- **Parent link** — when the open panel is the detail view of a record
  whose list panel exists, the header's left slot is a ghost button
  labeled with the destination (`← Routes`), never the ambiguous word
  "Back." It opens the list, re-activating the list's tab when one is
  already open. List panels, and detail views with no list home
  (blocks, signals, platforms), show no parent link. Keyboard:
  Alt+Left.
- **Pin** — freezes the panel on its current record so canvas clicks
  don't retarget it. Tabler `ti-pin`, icon-only.
- **Tabs** — the working-set tab strip (§3) sits directly under the
  header. No breadcrumb row: the parent link covers "go up," the tab
  label names the record, and the tab tooltip carries the lineage.

A long editor panel (Route Editor, Path Editor) also shouldn't bury
Save in the bottom section. Sections describe what they edit; Save
applies to the whole panel, so it lives in a sticky top toolbar instead
of inside any one section. Reference: `.re-toolbar` in
`features/routes-paths.css`.

```
● unsaved      [⟳]  [Save *]
```

- **Dirty indicator** — warn-color `● unsaved` text in the spacer. No
  border, no badge — it's a status marker, not a button.
- **Refresh** — icon-only ghost button. Polls already keep the panel in
  sync, but an explicit affordance gives the user "I clicked something;
  did the server hear me?" confidence.
- **Save** — accent-tinted background and a `*` suffix when dirty. Not
  a primary-fill button; the tint reads as "attention" without
  competing with destructive actions for visual weight.

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
border-radius: var(--radius-lg);
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
border-radius: var(--radius-md);
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

- **No color emoji anywhere in the UI.** The icon system is Tabler (see §3 Icons); monochrome Unicode glyphs (`▸ ▾ ×`) remain fine for tiny structural chrome — carets, a select-arrow decoration, a close-X on a tab. Pictographic glyphs Inter doesn't carry (`⟳` / `↻` rotate arrows, play/pause triangles) fall back to Arial and misalign inside a button box — use Tabler for those. Both render in the CSS `color` property and respect the token palette. Color emoji (`🚦 🚉 📋 📊 🚨`) render as color pictographs regardless of `color:` and fight with the palette — never use them in buttons, headers, labels, or any panel chrome. Quick test: if setting `color: red` on the character turns it red, it's a monochrome symbol and is OK; if it stays a colorful picture, it's an emoji and is not.
- **No new colors.** If you can't reach for a token, the design doesn't have a slot for it yet — pause and discuss instead of inventing.
- **Don't cross the canvas/panel hue firewall.** The `--block-*` tokens paint canvas geometry and §17 state marks; they never appear in panel chrome. The §1 accent palette tints panel chrome; it never paints canvas geometry. The two palettes deliberately tune the "same" hue differently (OCCUPIED `#ef4444` vs `--danger-color` `#e83f5b`) — mixing them re-merges what was split on purpose. Panel UI that *describes* a block state (a chip, a legend swatch) goes through `getBlockStateColor`, not a hand-picked accent.
- **Don't let hue carry state alone.** Roughly 8% of male users can't reliably separate red from green, and this product's audience skews male — that's a real slice of the user base, not a compliance checkbox. Every state mark pairs its hue with a second channel: position (the §6 accent is always a left border; a signal lamp sits on its stick), shape or text (the switch badge carries `M`/`D`; chips carry words), or brightness (dim = inactive). A mark whose *only* meaning is red-vs-amber at 6px fails the check — give it a glyph, a label, or a position the eye can read without the hue.
- **No freelance ornaments inside a shared shape.** The same logic that bars new colors bars adding visual elements (an inner status dot, a leading icon, a divider rule, a decorative glyph) to a canonical primitive the doc already specifies. The §7 chip is text on a tinted pill — not "text + a glowing dot." If you find yourself reaching for an extra mark to make a component read better, pause: either the component is wrong for the job (pick a different one) or the doc is missing a slot (raise it as a doc question). Quick test: would I have to mint a new class to render this? Then it's freelance.
- **No solid filled buttons in panels.** Use the ghost + accent-tint pattern.
- **Don't border the container *and* every child.** If the container has a border (segmented cluster, tool grid, tab strip, card), the children inside are flat ghosts. If the children need their own borders (standalone buttons on the panel body), the container is unbordered. Doubling the borders reads as fenced-off boxes inside fenced-off boxes — the rendered weight is double what the structure needs.
- **No nested panels.** A panel inside a panel inside a panel is a sign the structure is wrong.
- **No multi-paragraph banners.** A footer banner is a single line of diagnostic text. Anything longer belongs in a tooltip or a disclosure.
- **No `box-shadow` for emphasis.** Use border + background tint. The only shadow tokens are the panel drop-shadows (`0 10px 30px rgba(0,0,0,0.45)` for floating, `0 -6px 20px rgba(0,0,0,0.35)` for bottom-anchored).
- **Don't hide the audit trail by default.** When the most common next user question is "what just happened?" — after activate, assign, or any state change — the history / audit panel must be visible without a click. Compactness comes from capping per-entity row counts (top 10 per train, top N per session), not from hiding the whole section. Reference: `_historyMaxRunsPerTrain = 10` in `editor-routes.ts`. The wrong polarity (hide-by-default) trains users to assume the data isn't there.
- **Don't render unit-mismatched numbers raw.** When a numeric field is supposed to be a duration but the backend can leak an absolute timestamp through it (different clock units, an off-by-1000 ms-vs-s mix, a wall-clock value where a sim-clock value belongs), sanity-cap the display in the UI: implausible values are hidden, not rendered as "dwell 1779949962s." The backend fix is separate; the UI must not propagate the garbage. Quick test: write down the plausible range for this field and assert against it before formatting (e.g. dwell-remaining: `Number.isFinite(v) && v >= 0 && v <= 3600`).

---

## 15. Reference panels

When in doubt, copy the structure of one of these:

- **Shared compositional primitives** → [components/status-blocks.css](../static/css/components/status-blocks.css) — the §6 banner, §7 chip, §11 status header, and §6 accent helpers. Editor panels consume these classes directly (`.status-header`, `.chip`, `.banner`, `.accent-*`) instead of redefining the shapes per feature.
- **Detail-page editor** → [editor-route-runs.ts](../frontend/src/editor/editor-route-runs.ts) + [features/routes-paths.css](../static/css/features/routes-paths.css) — the worked reference for composing a `status-header` + stats block + list of rows inside an editor sidebar. Route-run-specific bits (`.rr-card`, `.rr-summary`, `.rr-task-*`) layer on top of the shared primitives.
- **Long edit-and-act panel** → [editor-routes.ts](../frontend/src/editor/editor-routes.ts) + the `.re-toolbar` / `.atr-*` rules in `features/routes-paths.css` — the worked reference for a panel that mixes editing (tasks, mode, policy) with live monitoring (assigned trains, history). Demonstrates: sticky top toolbar with Save + dirty indicator + Refresh; assigned-trains list as a row-shaped second consumer of the `.rr-task-*` skeleton; history default-visible with per-entity cap; Force checkbox adjacent to the Assign button it modifies.
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
[renderer-stations.ts](../frontend/src/renderer/renderer-stations.ts); `drawTrainLabel`
in [renderer-trains.ts](../frontend/src/renderer/renderer-trains.ts); `drawPlatformLabel`
in [renderer-platforms.ts](../frontend/src/renderer/renderer-platforms.ts).

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

Draw order in [renderer.ts](../frontend/src/renderer/renderer.ts) `drawNetwork`:

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

**Two-canvas implementation.** The single conceptual stack above is realized on
two stacked canvases (see [canvas.css](../static/css/layout/canvas.css)): a
`#rail-world-canvas` (z-index 1, purely visual) painted by `_drawWorld` — track,
blocks, platform bodies, signals, debug overlays, block-detail labels — and a
`#rail-live-canvas` (z-index 2, owns the pointer) painted on top by `_drawLive`
— the platform/station identity labels (which re-evaluate on every mousemove for
hover), route overlays, trains + train labels, and the select overlay.
`drawNetwork` runs `_drawWorld` then `_drawLive`. Within-canvas order keeps the
rule above (a train paints over a station name; the hovered label gets its top
pass). The one consequence of the split: the interactive identity labels on the
live canvas sit above the world canvas's display-only signal / debug /
block-detail marks rather than beneath them.

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
[renderer-stations.ts](../frontend/src/renderer/renderer-stations.ts).

### Overlap and collision avoidance

Two labels at the same screen position is acceptable when the higher-tier
label wins via z-order + opaque fill: the lower-tier label is partially
hidden but the foreground label stays fully legible. The opacity rule above
(no bleed-through) is what makes this work — without opaque fills, two
overlapping labels mix into illegible mush instead of one winning cleanly.

That fallback alone isn't enough though. A train label parked permanently
on top of a station label still hides the station — even when fully
legible, the user can't read what they can't see. So labels with positional
freedom actively reposition to avoid overlap, on top of the z-order rule.

**The mechanism: shared per-frame label-rect registry.**
`renderer.labelBounds` is reset at the top of `_drawLive` each frame and
populated in draw order. Each handler pushes its label's axis-aligned rect
into the registry right after placement:

| Pushed by | Reads registry? | Tier label |
|-----------|-----------------|------------|
| Platform labels (first) | no | `'platform'` |
| Station labels (second, non-hovered only) | no | `'station'` |
| Train labels (third) | **yes** | `'train'` |

Only the train label currently has positional freedom (4 quadrant
candidates around the train head, picked by `_pickLabelPosition`), so only
the train label reads the registry. The picker's existing score adds a
fourth factor — overlap area with each entry in `labelBounds`, normalised
to the train label's own area and weighted so a single full overlap (-60)
outranks the saturated track distance (+30) and direction preference
(+8 max) combined.

Iteration order matters: trains push their chosen rect into the registry
*after* picking, so the next train placed this frame sees and avoids it
too. `network.trains.forEach` is deterministic, so behaviour is stable
across frames.

**What this rule does NOT cover:**

- **All-quadrants-blocked.** The picker picks the least-bad candidate.
  Partial overlap remains acceptable; if a layout regularly produces full
  overlap on every candidate, the next iteration is to expand the
  candidate set (8 quadrants or candidates further along the leader
  line). Don't add another candidate set without an actual collision a
  layout can produce.
- **Hovered station overlay.** Drawn at the very top of the stack after
  trains and isn't pushed into the registry — its expanded rect would
  shift train placement on every cursor move, which is the wrong
  trade. The hover state is meant to win z-order; that's the documented
  exception.
- **Station / platform label collision with each other.** Neither has
  positional freedom (stations sit at platform centroid, platforms hug
  their side), and their default positions rarely collide in current
  layouts. If they did, the answer would be to give one of them
  candidates, not to invent a fourth tier.

References: `_drawLive` reset in [renderer.ts](../frontend/src/renderer/renderer.ts);
`aabbOverlapArea` utility in the same file;
`_pickLabelPosition` in [renderer-trains.ts](../frontend/src/renderer/renderer-trains.ts);
the push calls in [renderer-platforms.ts](../frontend/src/renderer/renderer-platforms.ts)
and [renderer-stations.ts](../frontend/src/renderer/renderer-stations.ts).

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

---

## 17. Canvas state marks

§16 covers identity labels; this section covers the canvas's other
visual channel — the saturated hue marks that show **state**: block
fills, signal aspects, route overlays, switch position and locks,
train mode, selection. The two channels coexist by the
brightness-vs-hue split in §12 "Color channels": identity is quiet
chrome, state is the saturated mark at the entity's location.

The token rule extends here: state marks consume §1 tokens — the
`--block-*` palette already works this way through
`getBlockStateColor`. The shipped renderers still hard-code most other
state hex at the call site; that is a Known divergence to burn down,
not a license to add more.

### Block state fills

Track geometry fills with the §1 block palette. `blockTrackColor` in
[renderer-tracks.ts](../frontend/src/renderer/renderer-tracks.ts)
resolves availability first — DISABLED (grey) wins over MANUAL
(orange), and both override live state — then the live state
(occupied / reserved / free / caution) through `getBlockStateColor`.
An unrecognized state keeps the default track color rather than
guessing one.

### Signal aspects

A signal is a grey stick perpendicular to its track with a lamp at the
end ([renderer-signals.ts](../frontend/src/renderer/renderer-signals.ts)):
an inner circle (3·scale) filled with the aspect color inside a
4·scale grey ring. Aspects: green `#04d361`, yellow `#ffd93d`, red
`#e83f5b` — and red is also the fall-through for any state value the
renderer doesn't recognize, so an unknown or future aspect
(`SignalState` also defines `double_yellow`) fails to the most
restrictive lamp. That polarity is deliberate; keep it. If
`double_yellow` ever becomes a rendered aspect it gets the railway
treatment — two stacked yellow lamps — not a recolor.

### Route / path overlays

A route's tiles fill with a diamond per tile, colored by run status
([renderer-routes.ts](../frontend/src/renderer/renderer-routes.ts)):
pending grey, setting yellow, set green, active bright green, clearing
orange, completed dim grey, cancelled red — fills at ~0.2–0.4 alpha
with a matching border, plus per-tile direction arrows in the same
status color. Emphasized routes add brown lock circles on their locked
switches. Reachable-destination highlights are amber diamonds. A
conflict tile flashes — red fill, white outline, white `×`, blinking
every 200ms — the only blinking mark on the canvas (see §18 Motion).

### Switch state

A switch shows its thrown position as a center badge
([renderer-tracks.ts](../frontend/src/renderer/renderer-tracks.ts)):
a green `M` circle when the main leg is set, a yellow `D` when the
diverging leg is set. The leg not taken draws thin and dark so the set
leg reads as the only continuous path. Lock rings around the badge:
brown = a train physically holds the switch, orange = a path holds it
for an upcoming move.

### Train mode

The train body is a white polyline with a dark outline; the head is a
white triangle pointing in the direction of travel, the tail a white
square carrying the car count
([renderer-trains.ts](../frontend/src/renderer/renderer-trains.ts)).
The body tints by state, highest priority first: **saturated purple =
emergency brake active**, pale lavender = route teardown pending,
light yellow = manual driving, white = ATO. EB wins over every other
tint — the most safety-relevant state in the system must be findable
at a glance on the surface the operator actually watches, and nothing
else on the canvas paints a train-scale purple body (teardown's pale
lavender stays clearly lighter, so the two don't read as one). The
debug-only "stuck waiting" overlay draws a dashed orange leader from
the train head to the block it is waiting on.

### Selection and previews

Box / lasso selection fills tiles amber with an orange outline. Build
previews draw dashed: orange for track about to be placed, red for the
eraser, purple for a block path; invalid placements drop opacity.
These sit at the very top of the live layer (§16 draw order).

**Entity placement ghost.** Previewing a whole entity that will span many
tiles — a new train being placed — reuses the amber selection fill above,
not a dashed tool outline: the filled band reads as the footprint the
entity will occupy. Fill each tile's diamond (its four edge corners),
never a polyline through tile centers — on the diagonal staircase tiles
that make up most of the network, center-to-center segments zigzag, while
abutting diamonds tessellate into a clean band that follows the track. A
small filled arrow at the head marks the facing direction. The band stays
amber while the configured length fits and turns danger red when the track
runs out before the length is covered (consistent with "invalid
placements" above); the matching "track too short" note in the panel keeps
the state off hue alone (§14). Amber, **not** the train's purple — a
train-scale purple body is reserved for EB (see "Train mode" above), so a
placement aid must never paint one. The ghost is owned by its panel: it
clears when the panel closes or the placement commits. Reference:
`_drawTrainGhost` / `setTrainGhostPreview` in
[renderer.ts](../frontend/src/renderer/renderer.ts), fed by
`_computeGhostTiles` in
[editor-trains.ts](../frontend/src/editor/editor-trains.ts).

### Rules

- **State marks consume tokens.** New marks pick from §1. The existing
  hard-coded hex is a Known divergence, and `#00ff88` (route-active) /
  `#ffd93d` (signal yellow) are palette questions to resolve when
  their renderers are next touched — not colors to copy.
- **Hue means state, never identity.** Identity stays in the §16 label
  tiers. A mark that doesn't change with state doesn't get a saturated
  hue.
- **Fail restrictive.** When a state value is unknown, render the most
  restrictive mark (the signal red fall-through) or no mark at all
  (the block-fill null) — never guess a permissive state.
- **Blinking is reserved.** One blinking mark class exists (route
  conflict). Emergency-tier events only — see §18 Motion.

---

## 18. Motion

Animation is a scarce channel: the more things move, the less any one
motion means. The inventory is deliberately small.

| Motion | Spec | Used for |
|--------|------|----------|
| Control feedback | `transition` 0.15s on color / border-color / background-color | Hover and active states on buttons, tabs, chips |
| Surface feedback | `transition` 0.2s on opacity / border | Row hover, panel show / hide |
| Toast slide | Slide in from the right, slide out on dismiss (§13) | Transient confirmations |
| Modal entrance | One short float-in (`floatInFlex` in [modals.css](../static/css/components/modals.css)) | Dialog appearing |
| Danger pulse | `tc-pulse-danger` — 0.8s infinite, opacity 1 → 0.55 | The EB-active badge (§3). Nothing else |
| Conflict blink | 200ms on / off | Route conflict tiles on canvas (§17). Nothing else |
| Spinner | `restart-spin` | The blocking restart overlay |

Rules:

- **Feedback stays under 200ms.** Hover and active transitions run at
  0.15–0.2s; anything slower reads as lag, not polish.
- **Continuous motion is emergency-tier.** A pulse or blink that never
  stops is reserved for states that demand intervention: EB active (the
  panel badge), route conflict (the canvas tile). At most one pulsing
  element per panel — two pulsing things cancel each other out.
- **Nothing else loops.** Decorative ambient animation is noise on an
  operations screen.
- **Reduced motion.** When `prefers-reduced-motion: reduce` is set,
  looping pulse / blink degrade to their static high-contrast state —
  the badge holds solid danger, the conflict tile holds solid red + `×`.
  The CSS path is implemented in
  [base/motion.css](../static/css/base/motion.css): a universal reset
  holds every looping animation and transition at its end state, so the
  badge stays solid danger. Canvas marks are drawn in the renderer, which
  does not yet read the setting.

---

## 19. Learnability

The product serves two users at once: the operator who knows the
domain, and the newcomer building their first layout. The patterns
above optimize for the operator — dense rows, mono identifiers,
uppercase micro-labels. This section is the newcomer's layer: the
rules that make the same screens teachable without diluting them.

### Empty states teach

The first thing a newcomer sees of any panel is its empty state. Use
the §10 actionable empty state wherever the empty collection has an
obvious next action, and write the line in plain words ("No routes
yet — a route is a reusable plan of stops"), never a bare "No data."

### Every edit is reversible or confirmed

The editing contract (§5C) is auto-apply: segment clicks and field
changes save immediately, with no Save step to forget. The contract
has two halves, and the second half is what makes the first safe:

- **Destructive and hard-to-undo actions confirm first** — deleting a
  route, an edit that removes a dependent entity (§4 "one validated
  commit path"). The §13 modal names the consequence in plain words.
- **Everything else must be undoable by an obvious inverse action,
  visible where the action was** — Unassign beside Assign (§12), a
  Reset beside each overridden field (§5C), the list tab that reopens
  a closed record. If an auto-applied edit has no visible inverse,
  that is a design bug: add the inverse, or demote the edit to
  confirm-first.

There is no global undo stack; reversibility is per-action and local.

### Words

**Terminology is fixed** — one name per concept in every panel title,
chip, tooltip, and toast:

| Term | Means |
|------|-------|
| Tile | One grid cell of track geometry |
| Block | A stretch of track the signalling treats as one unit; occupancy and reservation live here |
| Switch | A junction with a main and a diverging leg |
| Signal | A lamp guarding entry to the track ahead |
| Platform | A stopping place on a track edge |
| Station | A named group of platforms |
| Train | The vehicle; drives in ATO (automatic) or manual mode |
| Route | A reusable plan of tasks — stop here, dwell, continue |
| Route run | One execution of a route by one train |
| Path | The locked track a train holds for its next movement; created by the system, not drawn by the user |

Never introduce a synonym in UI text ("section" for block, "turnout"
for switch, "journey" for run). If a label seems to need a different
word, that is a change to this table first.

**Sentence case for actions** ("Create route", "Apply to 3 blocks");
UPPERCASE only for the §5 label tier and §7 state chips. Spell words
out when there's room (Density follows task); the compact cryptic form
goes in the `title` tooltip, not on the screen.

**Tooltips carry the how.** Every tool and non-obvious control gets a
`title`: what it does, then how to use it — the tool sidebar's
"Trace (click two tracks to select the connecting path…)" is the
pattern. A newcomer should never have to leave the screen to learn a
control.

### The cursor answers "can I do this here?"

While a build tool is active, validity shows at the cursor before the
click: placement previews draw dashed in the tool's preview color, and
an invalid placement turns the preview red and drops its opacity (§17
Selection and previews). A click that silently does nothing is the
worst outcome — a newcomer reads it as "the app is broken." If a
placement is refused after the click, the refusal must be visible: a
toast naming the reason, not silence.

### Input model

Desktop, mouse-first. Keyboard accelerators exist (Space clears the
selection, Delete removes selected tracks, Alt+Left opens a detail
panel's parent list) and are named in the control tooltips — there is
no hidden-shortcut layer a newcomer must discover. Controls are sized
for pointer precision (28px controls, 14px checkboxes, 6px resizers);
touch is out of scope.

---

## Known divergences

Where the shipped code still differs from this doc. Fix an entry when
its trigger fires — not in a drive-by patch — and delete the row in the
same change.

| What | Where | Target | Trigger |
|------|-------|--------|---------|
| Leading status-column rows (~50px colored dot + label column) | `.route-row`, `.path-row` in [routes-paths.css](../static/css/features/routes-paths.css) | §6 3px left-border accent | Next rework of the route / path lists |
| Private copies of the segmented control | `.tc-seg-btn` in [train-control.css](../static/css/features/train-control.css), `.tm-seg-btn` in [train-monitor.css](../static/css/features/train-monitor.css) | Fold onto `.switch-btn` (§3) | Next touch of either panel |
| Pre-token literals — spacing, type sizes, radii, z-index written as raw values | all of `static/css` | Compose from the §1 scale tokens (`--space-*`, `--font-size-*`, `--radius-*`, `--z-*`); consolidate off-ramp values (13px type, 5/7/9px gaps, 3px small radii) to the nearest step | When touching the declaration |
| Floating inspector outranks modals (z-index 9000 over 1100) | [inspector.css](../static/css/features/inspector.css) | Modal layer wins over debug panels | Next inspector or modal rework |
| Canvas state marks hard-code hex — signal lamps, route status fills (incl. `#00ff88`), switch badges and locks, train mode tints | `renderer-{signals,routes,tracks,trains}.ts` | Consume §1 tokens / a tokenized canvas palette (§17 Rules) | When touching each renderer |
| Passive empty states where a creation action exists | route / train / run list panels | §10 `.empty-state-action` with a `.btn-accent` CTA (§19) | When touching each panel |
