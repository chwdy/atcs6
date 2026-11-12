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
- **Shared compositional primitives** → `components/*.css`.
  [status-blocks.css](../static/css/components/status-blocks.css) holds
  the status shapes (§6 banner + accent helpers, §7 chip, §10 empty state,
  §11 status header and copy button, §12 selected row);
  [design-system.css](../static/css/components/design-system.css) holds the
  §3 segmented control; [common.css](../static/css/components/common.css)
  holds the §3 buttons, the floating picker and the §11 entity card. Shared
  shell pieces — the floating window, the multi-select bar — live in
  `layout/*.css`. Every editor
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

The trigger for promoting a shape into the shared files (a status shape
into `status-blocks.css`) is **the second consumer**. When a primitive in a feature file is about to be
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
   than nothing — it costs space and gets ignored anyway. The same holds
   across cards: a fact about the whole screen ("the session is halted")
   belongs in one place, the top bar, not on every train card.

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

Where UI lives. Four screens picked from the top bar, and a fixed set of
regions — every feature has exactly one natural home. The plan behind them
is [ui-layout-rewrite-plan.md](ui-layout-rewrite-plan.md).

The top bar's screen tabs are the app's primary split. Each screen is one
of the operator's jobs:

- **Editor** — building the layout: track, switches, signals, tunnels,
  platforms, segments and blocks.
- **Hardware Setup** — the steps of setting up the hardware and going
  live (design_new.md "Setting up the hardware and going live").
- **Operate** — running trains: placing them, driving them, routes and
  paths.
- **Locomotives** — the Train Roster, and each locomotive's Speed Profile
  panel with its calibration group.
- **Logs** — watching what the system does: the event log, the detectors'
  raw readings, and one train's diagnostics, beside the canvas.

Picking a screen only changes what you see; the session state changes
only through its own buttons. The Editor screen is the Editor session
mode, and Operate, Locomotives and Logs are the operation mode. The tab reads
Operate in every operating environment (design_new.md "Session mode and
operating environment"), so the word "Simulation" in the UI only ever
names the operating environment.

| Region | What lives there |
|--------|------------------|
| **Top bar** | App-level commands on every screen: the Layout menu, the screen tabs, the status control with its one next-step button, Emergency stop, and the View and Settings menus ("Composing the top bar" below). The one region where the 32px `--control-height-sm` controls belong (§1 Layout). |
| **Canvas** | The layout itself — two stacked `<canvas>` layers ([canvas.css](../static/css/layout/canvas.css); §16 "Two-canvas implementation"). Shown on Editor, Operate and Logs, and on Locomotives while a calibration run is open. |
| **Editor tool bar** | One flat bar of the things you place, across the top of the Editor screen, with an options row under it for the picked tool's variants (§3 "Tool bar button"; [next/editor.css](../static/css/next/editor.css)). |
| **Right panel** | The selection, one record at a time, right of the canvas. On Editor it edits the selected entity; on Operate it shows a train, block or switch. One panel shows at a time — there are no tabs. Its pop-out button moves it into a floating window and back (§12 "Popping the right panel out"). |
| **Operate's left list and cards row** | The lists of trains, routes, paths, route runs, stations, blocks and switches in a column left of the canvas, which folds away; a card per train along the bottom of the screen, ending with + Place train and Hold all ([next/operate.css](../static/css/next/operate.css)). |
| **Hardware Setup steps** | The seven steps in a column, the picked step beside it, over the whole screen area ([next/hardware.css](../static/css/next/hardware.css)). |
| **Logs screen** | A tab strip (Event log, Detectors, Diagnostics) whose tab fills the left of the screen, with the canvas on the right at 40% of the width. A click on a train or block on the canvas picks what the tabs show ([next/logs.css](../static/css/next/logs.css)). |
| **Overlay layer** | Modals, toasts, floating pickers, menus (§13). |

Two floating windows are left over from the old layout (the plan's
§13.10): the Train Monitor window, which today's train panel opens on the
Editor screen (its readings now also show in the Logs screen's
Diagnostics tab), and the floating Virtual Bench, whose Place on canvas
has no other home yet. Do not add to them.

Placement rule for a new feature: places or shapes the layout → the
Editor tool bar, and its record's panel in the Editor right panel;
edits or commands one record → the right panel of its screen; watches
many trains at once → Operate's cards row or left list; part of setting
up the hardware → its Hardware Setup step; about a locomotive or its
speed profile → the Locomotives screen; a log, a raw reading, or
diagnostics over arbitrary state → a Logs screen tab; transient feedback
→ a toast; a blocking decision → a modal. Do not add a floating window:
the right panel's pop-out is the only one. If none of those fits, raise
it as a doc question before inventing a new region.

### Composing the top bar

One 48px row in two zones (`display: flex; justify-content:
space-between`):

- **Left** — the wordmark, the **Layout** menu (New, Open…, Save…), then
  the five screen tabs as a segmented control (§3). They are a segment,
  not a §3 tab strip, because they switch the whole screen rather than a
  pane inside one panel.
- **Right** — the status control, **Emergency stop**, then **View** and
  **Settings**.

The status control is the top bar's answer to "can my train move?". The
session state chip names the operating environment in muted text, then
the session state (`Real Hardware · Halted — not activated`). Right after
it sits one next-step button that changes with the state: **Activate**
(Setup, or Halted for `not_activated` or `hardware_failure`), **Play**
(Paused), **Pause** (Live), **Reset** (Halted for
`layout_emergency_cut_out`) or **Clear conflict** (Halted for
`runtime_failure`). Activate and Play are the filled accent CTA — the top
bar is a top-level page, where §3 allows a filled `--accent-color` button.
Reset is a warning button: `--warning-color` border and text on
`--bg-dark`, a solid warning fill on hover, for the same reason as the EB
Reset (§3 Emergency button) — releasing an emergency is a caution act, not
a go act. Pause and Clear conflict are ghost buttons. In Simulation a speed menu
(`1x`) follows. Clicking the chip opens Hardware Setup at step 1.

**Emergency stop** sits in the same place on every screen: a 32px top-bar
button with icon and label, outlined in the danger color at rest and filled
with it on hover. It stops the whole layout; it is not a train's EB button,
which §3 Emergency button specifies.

**View**
holds the two theme axes (§1 Themes), Show grid, and the canvas layers;
**Settings** (icon-only) holds Settings… and Restart server. Each menu is
a header-anchored menu (§13).

Within a zone, clusters are separated by a 1px hairline centred in the
gap (a `> * + *::before` pseudo-element). The top bar is the one region
that uses the 32px `--control-height-sm` controls (§1 Layout, Surfaces)
rather than the 28px panel height, and its action buttons follow the
icon + label rule in §3 Icons.

Reference: `TopBar` in
[next/top-bar.tsx](../frontend/src/next/top-bar.tsx); `.app-header` /
`.header-zone` / `.session-chip` / `.mode-switch` in
[layout/shell.css](../static/css/layout/shell.css); the status control,
Emergency stop and menus in
[layout/next-top-bar.css](../static/css/layout/next-top-bar.css).

---

## 1. Tokens

All tokens live in [base/tokens.css](../static/css/base/tokens.css). Refer to
them by variable name in CSS, never by hex literal.

### Themes (light + dark)

Light and dark are **co-equal first-class themes**, not a base palette plus a
dark override. The surface, text, and accent tokens are defined as two parallel
**literal-hex** sets — one under `:root[data-theme="dark"]`, one under
`:root[data-theme="light"]` — and a control selects between them. An inline
bootstrap script in `index.html` sets `data-theme` on `:root` before first
paint (default follows the OS `prefers-color-scheme`, then persists to
localStorage), so there is no flash. The hex columns in the tables below show
the **dark** set; the light set sits beside each one in `tokens.css`. Refer to
tokens by name and both themes resolve for free.

Three rules keep this working:

- **Literal hex per theme, not a `var()` ramp.** JS reads tokens at runtime via
  `getComputedStyle().getPropertyValue()`, and the browser returns the
  *unresolved* `var(...)` string — so a token whose value is `var(--other)`
  reads back as the literal text `"var(--other)"`, not a color. Every
  theme-varying token therefore holds a concrete hex. (Same reason the
  `--block-*` palette is literal hex; see Block / track status below.)
- **`--*-rgb` channel tokens for tints.** Each accent and status hue also has a
  companion `--<name>-rgb` token holding bare `R, G, B` channels
  (`--accent-rgb`, `--info-rgb`, `--success-rgb`, `--warning-rgb`,
  `--danger-rgb`), so CSS can compose a themed translucent tint as
  `rgba(var(--info-rgb), 0.08)`. Use these for alpha tints — never hardcode the
  channels (a literal `rgba(16, 18, 24, 0.6)` is a dark band that does not
  re-tint in light).
- **Panel surfaces come from surface tokens only.** A panel background, well, or
  hairline must use `--bg-dark` / `--bg-panel` / `--bg-hover` / `--border-color`,
  never a literal dark hex. A hardcoded `#1a1a2e` reads fine in dark and then
  paints a dark slab over a light panel. (`.control-section`, `.tile-list-container`,
  and `.route-item` each carried one of these and broke the light editors until
  tokenized.)

**The canvas is themed on its own axis.** The chrome and the layout canvas
carry two independent theme attributes on `:root`: `data-theme` selects the
chrome palette — dark / light, [themes.css](../static/css/base/themes.css) — and
`data-canvas-theme` selects the canvas palette — dark / light / blueprint,
[layout-themes.css](../static/css/base/layout-themes.css). The two switch
independently, so a light chrome over a blueprint canvas is a valid combination;
the canvas is not pinned to the chrome's light/dark choice. The canvas keeps its
own full token set — `--canvas-*` (ground, grid, track, and the state-mark hues
in Canvas state marks below), the `--block-*` state palette and the
`--train-body-*` tints — re-tuned per canvas theme to hold contrast on that theme's ground. The
renderer reads those tokens once into a cached palette
([canvas-palette.ts](../frontend/src/lib/canvas-palette.ts)) and repaints when the
canvas theme changes (CSS custom properties read back from JS as the unresolved
`var(...)` string, so the palette must hold concrete hex — the same literal-hex
rule as the chrome tokens above). Both axes are picked in the top bar's View menu
(§13 Header-anchored menu); the chrome default follows the OS `prefers-color-scheme`, and the canvas
defaults to dark — the original drawing-board.

### Surfaces

The hex shown is the dark set; `tokens.css` carries the light value beside it.

| Token | Hex (dark) | Use |
|-------|-----|-----|
| `--bg-dark` | `#111113` | App canvas, button backgrounds, count pills, panel wells (`.control-section`) |
| `--bg-panel` | `#1c1c20` | Panel shells, raised inset surfaces (tile lists) |
| `--bg-hover` | `#29292f` | Header strips, row hover |
| `--border-color` | `#34343c` | All hairline borders |

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
| `--success-color` | `#04d361` | OK / running / power notch |
| `--warning-color` | `#ffcd1e` | Caution / dwell / brake notch |
| `--danger-color` | `#e83f5b` | EB / fault |
| `--info-color` | `#00d4ff` | Stations / target speed / informational |

The accent palette doubles as the **status accent palette** for left-border
strips, banner tints, and badge backgrounds. Don't introduce new hues.

These tokens never stand for a block's state. A panel element that names a
block's state — a row stripe, a chip, a status-header edge, a legend swatch —
takes its colour from `getBlockStateColor` (Block / track status, below).

**Muted is not a hue.** The `-muted` variants mean "idle, nothing to report":
a `.chip-muted` is `--text-tertiary` text on `--bg-hover`, and an
`.accent-muted` stripe is `--border-color`. A §11 status header never uses
`.accent-muted`, because its edge always carries a hue (Status header block).
Muted *text* in prose means `--text-secondary`.

### Block / track status

The block-state palette has its own tokens. It reuses two accent hues
(FREE, RESERVED), retunes two (OCCUPIED, CAUTION), and adds two domain-only
hues for static availability flags. Track / block renderers
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
semantic — DISABLED is the only grey block state, and MANUAL the only
orange block state. (Other canvas marks use grey and orange too; their
tokens are in Canvas state marks below.)

The tokens are declared as literal hex (not `var(--info-color)`
references) so `getComputedStyle().getPropertyValue('--block-…')` reads
the resolved color directly. CSS custom properties are not substituted
when read from JS, so `var()` chains return the unresolved string. The hex
above is the dark canvas-theme set; the light and blueprint canvas themes
([layout-themes.css](../static/css/base/layout-themes.css)) re-tune the whole
`--block-*` palette to hold contrast on their grounds, just as the chrome themes
re-tune the surface tokens.

Canonical consumer: `getBlockStateColor(state, alpha)` in
[frontend/src/lib/helpers.ts](../frontend/src/lib/helpers.ts). Pass `alpha` < 1
to get the rgba form used for canvas overlays. Track/block renderers reach it
through this resolver rather than hard-coding hex at the call site.

### Canvas state marks

Every canvas mark that is not a block fill reads one of these tokens through
the cached canvas palette ([canvas-palette.ts](../frontend/src/lib/canvas-palette.ts)).
They live with `--block-*` in
[layout-themes.css](../static/css/base/layout-themes.css) and are re-tuned per
canvas theme the same way; the hex below is the dark canvas theme. The §1
accent tokens never paint the canvas (§14); these canvas tokens never paint
panel chrome.

| Token | Hex (dark) | Marks (§17) |
|-------|-----------|-------------|
| `--canvas-success` | `#04d361` | Green signal lamp, switch `M` badge, route run *set* |
| `--canvas-success-bright` | `#00ff88` | Route run *active* |
| `--canvas-caution` | `#ffd93d` | Yellow signal lamp, switch `D` badge, route run *setting* |
| `--canvas-warning` | `#ff9500` | Orange marks: route run *clearing*, selection outlines and build previews, the stuck-waiting leader |
| `--canvas-danger` | `#e83f5b` | Red signal lamp and its unknown-state fall-through, other red state marks |
| `--canvas-accent` / `--canvas-accent-bright` | `#8257e5` / `#c8a2ff` | Purple marks that are not a train body |
| `--canvas-info` | `#00d4ff` | Cyan marks |
| `--canvas-hold` | `#8b4513` | Brown: a train physically holds the switch; an emphasized route's lock circles |
| `--canvas-lock` | `#ff8c00` | Orange lock ring: a path holds the switch |
| `--train-body`, `-manual`, `-teardown`, `-eb`, `-unidentified` | `#ffffff`, `#ffe066`, `#c8a2ff`, `#8257e5`, `#8a8a99` | Train body tints (§17 Train mode) |
| `--canvas-highlight-locate` | `#ffd93d` | The locate flash (§17) |

A new mark picks from this table or the block table. A mark that needs a hue
neither has is a doc question first (§14 "No new colors").

### Layout

| Token | Value | Use |
|-------|-------|-----|
| `--control-height-xs` | `28px` | Default — every control inside a panel (buttons, selects, text/number inputs, segments, icon buttons) |
| `--control-height-sm` | `32px` | The top bar only (Surfaces). Panels, modal dialogs, and the Hardware Setup and Locomotives screens use `--control-height-xs` |
| `--control-radius-sm` | `6px` | All buttons, panel corners, cards, floating pickers |
| `--radius-xs` | `4px` | Inner elements — badges, small controls inside an already-bordered card |
| `--radius-md` | `8px` | Toasts, context and header-anchored menus, the hero CTA |
| `--radius-lg` | `12px` | Modal dialogs |
| `--radius-pill` | `999px` | Chips / pills (§7) |

### Spacing

Multiples of 2px, tokenized so new work composes from the same steps
the reference panels use. 1px is not a spacing step — it is the
hairline border width.

| Token | Value | Typical use |
|-------|-------|-------------|
| `--space-2` | `2px` | Gaps inside a control cluster (tab gap, tool bar gap) |
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
| `--z-shell` | `10` | Docked panels, sticky panel toolbars |
| `--z-window` | `900` | Floating chrome windows — a right panel popped out, and the Train Monitor and Virtual Bench windows. `WindowManager` stacks open windows within this band by focus order (topmost = most recently focused); the band sits below every picker/modal, so those always win. |
| `--z-picker` | `1000` | Floating pickers, context and header-anchored menus, modal backdrops |
| `--z-modal` | `1100` | Modal dialogs |
| `--z-restart` | `1200` | Full-app restart overlay |
| `--z-toast` | `10000` | Toasts — transient, never blocked |

### Shadows

Three shadows exist, all on overlays; nothing else casts one (§14).

| Shadow | Value | Use |
|--------|-------|-----|
| Floating | `0 10px 30px rgba(0, 0, 0, 0.45)` | Toasts, floating pickers, context and header-anchored menus, floating windows |
| Bottom-anchored | `0 -6px 20px rgba(0, 0, 0, 0.35)` | Panels docked to the bottom edge |
| Modal | `0 20px 60px rgba(0, 0, 0, 0.8)` | Modal dialogs, which sit above everything else |

### Type

| Token | Value |
|-------|-------|
| `--font-main` | `'Inter', sans-serif` |
| `--font-mono` | `'JetBrains Mono', monospace` |

The size ramp is five working sizes plus the hero display size:

| Token | Value | Role |
|-------|-------|------|
| `--font-size-micro` | `9px` | State-badge text (§7) |
| `--font-size-label` | `10px` | Field labels (§5), chips, disclosure summaries |
| `--font-size-body` | `11px` | Prose, tabs, checkbox labels, units |
| `--font-size-value` | `12px` | Field values, inputs, panel titles |
| `--font-size-title` | `14px` | Detail-page record titles (§11) |
| `--font-size-hero` | `28px` | Hero numbers (§9) |

13px appears in older panels but is not a step — it reads as "almost
body text" without being one; consolidate to 12px or 14px by role when
touching that CSS (see Known divergences). Modal titles and the top bar
may use larger type sizes; inside a panel, stay on the ramp.

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

- The **header** is `--bg-hover` with `padding: 8px 10px` and no border. Title is 12px Inter weight 600, letter-spacing `0.02em`. What the right panel's header holds — the parent link, Lists, Pop out and Close — is in §12 "Panel navigation and the unsaved-edits indicator"; its button style is in §3 "Flat ghost".
- The **toolbar** has `border-bottom: 1px solid var(--border-color)` only when it sits on the same surface as the body (`--bg-panel`) — that hairline is the only thing separating it from the body. When the toolbar shares the header's `--bg-hover`, drop the border-bottom; the bg-tint change to the body does the separation.
- The **body** uses `flex: 1 1 auto; overflow-y: auto` — it does the scrolling.
- Header and footer use `flex-shrink: 0` so they stay pinned.
- Footer banners collapse to 0px when their content is hidden — no padding overhead when there's nothing to show.

Reference: [`.tm-panel`](../static/css/features/train-monitor.css) is the
canonical example.

---

## 3. Buttons

Buttons are 28px tall (`--control-height-xs`) unless a subsection below
gives its own height (Hero CTA, Emergency button), radius 6px, and follow
the **ghost** pattern by default.

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
- **Action gets a label; utility goes icon-only.** In the top bar and
  other control rows, a primary action button takes icon + label (Layout,
  Play, Emergency stop, View); a pure utility takes an icon-only
  square (Settings, a panel's pop-out and close). Don't mix the two arbitrarily
  on one row — icon-only for some actions and text-only for others, with no
  rule, is the "the icons feel random" smell.
- **Verify the glyph class actually resolves.** Before shipping a `ti-*`
  class, confirm it exists in the vendored Tabler CSS
  (`vendor/tabler/tabler-icons.min.css`). A typo'd or non-existent class
  silently falls back to a system font and renders as a stray character —
  the same low-baseline "broken glyph" symptom as a stray Unicode
  pictograph. This is the class-name companion to the roster lookup above.

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
toolbar row, a tool bar — drop the per-button border. Affordance comes
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
background-color: rgba(var(--accent-rgb), 0.18);
color: var(--accent-hover);
```

This pattern requires the container's own background to be `--bg-dark` or
`--bg-panel` — the hover tint (`--bg-hover`) must read against something
darker for the state to be visible. Never place flat ghosts directly on a
`--bg-hover` surface (such as the header strip itself); always wrap them in
a segmented cluster or toolbar row that carries its own `--bg-dark` fill.
When a header needs a group of flat ghosts, they live inside a bordered
segmented container inside the header — not free-floating on the header's
`--bg-hover` surface. A standalone header button (the panel navigation in
§12) is a regular ghost, whose own `--bg-dark` fill reads against the header.

Rule of thumb: **the container is bordered, or the buttons are bordered —
never both.** When a button stands alone on the panel body (no container
row around it), use the regular ghost above. When it lives inside a
`--bg-dark` grouped cluster or `--bg-panel` toolbar, use the flat ghost. A
toolbar that shares the header's `--bg-hover` uses regular ghosts, for the
same reason as a standalone header button.

When the button sits inside a bordered card or list item, use this flat
ghost too — see §11 "Action buttons inside a card go flat."

### Tool bar button

The Editor screen's tool bar is one flat row of the things you place
(Select, Track, Switch, Signal, Tunnel, Platform, Segment, Block). Each
button is a flat ghost with an icon and a label, and its number key small
and muted in front of them.

```css
/* button */
height: var(--control-height-xs);
padding: 0 var(--space-10);
border: none;
background: transparent;
color: var(--text-secondary);

/* hover */
background-color: var(--bg-hover);
color: var(--text-primary);

/* active: the segmented control's accent tint */
background-color: rgba(var(--accent-rgb), 0.18);
color: var(--accent-hover);
```

- The number key is the button's place in the bar, and it never moves:
  keys 1–8 pick the same tool every time.
- No hover menus. A tool's variants (Track: Draw / Remove; Select: Click /
  Box / Lasso / Brush / Trace / Entity; Platform and Segment: New / Extend
  / Remove) sit in the options row under the bar, a segmented control
  that shows only the picked tool's variants. A tool with one fixed action
  shows a one-line hint there instead.
- Icons are Tabler glyphs, looked up through the icon roster like every
  other button — see Icons above.

Reference: `.ned-tool` and `.ned-options` in
[next/editor.css](../static/css/next/editor.css); `BAR_TOOLS` in
[next/editor/tools.ts](../frontend/src/next/editor/tools.ts).

### Hero CTA (driver-facing primary)

For the most important driver-facing action in a panel — the ATO Go /
Stop buttons. Same ghost + accent-tint shape as the regular ghost, just
bigger: 52px tall, 15px weight-700 uppercase text, 8px radius, generous
gap between leading glyph and label. Colors come from `--success-color`
(Go) or `--danger-color` (Stop) used as text + border + 10% bg tint on
hover. Reference: `.tc-btn-hero` in
[features/train-control.css](../static/css/features/train-control.css).

### Emergency button

For a train's emergency brake: the EB control in the train panel
(`.tc-eb-btn`). The top bar's layout-wide Emergency stop is specified in
"Composing the top bar". The control has a full lifecycle,
and every state is specced — this is the most safety-relevant control
on the screen, so nothing about it is left to improvisation.

```css
height: 40px;
background-color: rgba(var(--danger-rgb), 0.10);
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

A row of mutually-exclusive buttons inside a single `var(--bg-dark)` container with a 1px outer border, **no internal dividers**. Buttons inside are flat ghosts — the active accent tint and hover bg-tint give every cell its own legible state without needing vertical hairlines. Active button: `color: var(--accent-hover)`, `background-color: rgba(var(--accent-rgb), 0.18)`. Hover: same accent color, `var(--bg-hover)` background. The container's outline does the grouping. For when to reach for this over a select, tab strip, or checkbox, see §4 "Choosing a selection control."

**Equal-width by default; `fluid` for long labels.** By default each cell takes an equal share of the row. That breaks when the labels differ in length — `Normal | Manual | Disabled` forced into thirds clips the longest word to `Disable…`. When a small fixed set has labels too long for equal cells, add a `fluid` modifier: each cell sizes to its own label and the row wraps to a second line rather than clipping. The set is still small and fixed, so it stays a segment (§4) — `fluid` changes only how the labels fit, not the control. Reach for it whenever a label would otherwise be cut off; don't pre-shrink to a cryptic abbreviation to keep one row, and don't drop to a select just because a label is long.

Reference: `.switch-group` / `.switch-btn` (+ `.switch-group.fluid`, and the `.success` / `.danger` / `.warning` variants) in [components/design-system.css](../static/css/components/design-system.css) is the canonical segmented control — every panel that needs one consumes it directly. The active cell is the accent **tint** (`rgba(var(--accent-rgb), 0.18)` + `--accent-hover`), never a solid fill, and there are **no internal dividers** — the container outline groups the cells and the active tint marks the selection. The train-control HMI (`.tc-seg-btn`) and train-monitor view-distance (`.tm-seg-btn`) carry their own copies of this shape (see Known divergences).

**Explaining the options.** A one-line help string under the segment is the
default; when each option needs its own gloss, stack one line per option
instead of cramming them into a run-on sentence (`Manual = … Disabled = …`).
Each line leads with the option term one brightness tier above its
description, so the column of terms scans like a small legend. Keep it to one
short clause per option — an option that needs a paragraph belongs in a
tooltip, not under the control. Reference: `.seg-explain` /
`.seg-explain-term` in
[components/design-system.css](../static/css/components/design-system.css),
rendered by the `explanations` option of `renderAvailabilitySegment`
([helpers.ts](../frontend/src/lib/helpers.ts)) for the block editor's
availability control.

**The availability segment shows two flags as one pick.** An entity's
`disabled` and `manual` flags are independent and may both be true
(design_new.md, **Entity-editor UI controls**). A segment still fits because
the flags have a fixed precedence — `disabled` wins (§17 Block state fills) —
so together they mean one effective state. The segment lights **Disabled**
whenever `disabled` is true, **Manual** when only `manual` is true, and
**Normal** otherwise. Each cell writes both flags: Normal
`{disabled: false, manual: false}`, Manual `{disabled: false, manual: true}`,
Disabled `{disabled: true, manual: false}`. So picking Disabled clears
`manual`, and re-enabling the entity returns it to Normal. Flags with no
precedence between them stay checkboxes (§4).

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

Reference: the Logs screen's tabs, `.nlp-tabs` › `.nlp-tab-btn` in
[next/logs.css](../static/css/next/logs.css).

### Accent-tint CTA (the panel-safe "primary")

The one button in a section that should draw the eye — Apply, Save — without
a solid fill. Same ghost shape as the others, but the accent shows as a ~18%
tint that reads as "attention," not "shout." A transparent border keeps it
the same height as a `.btn-outline` beside it; hover deepens the tint and
adds the accent border.

```css
background-color: rgba(var(--accent-rgb), 0.18);
color: var(--accent-hover);
border: 1px solid transparent;

/* hover */
background-color: rgba(var(--accent-rgb), 0.28);
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

**Each shape has one chrome, and every instance uses it.** A checkbox is
always `.checkbox-custom`, a select is always the 28px panel field ("Text,
select, and number inputs" below), a segment is always the §3 segmented
control — never a browser-default control or a one-off restyle. Pick the shape
from the table, then take its chrome wholesale: a form that mixes a styled
select with a browser-default one reads as two systems.

**There is no radio-button control.** A single pick from a small fixed set
*is* the segmented control — that is the radio button of this system. (One
native `<input type="radio">` survives in the block-extend picker, which is
really a data-driven list; render that kind of thing as a select or a list
of flat-ghost rows, not radios. A flat-ghost list that keeps its pick on
screen marks the pick with §12 Selected row.)

**Tie-breakers:**

- *Segmented vs select — the 4–5-option gray zone.* This is about option
  *count*, not label length. Past ~5 options a segment row gets unscannable
  even when each cell is tiny — that's when to switch to a select. (Long
  labels on a small set are a layout problem, not a control problem: the
  `fluid` variant wraps them — see the two-axes note above.) And a set built
  from data (trains, route targets, stations) is always a select however
  short it happens to be today — its length isn't yours to fix at
  author-time.
- *Flags with a precedence.* Independent flags where one always overrides
  the other collapse to one effective state, so they are a segment over
  that state, not checkboxes — the availability segment (§3) is the case.
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
`--control-height-sm` (32px) row in the token table is for the top bar
only — in a sidebar at 280–440px wide, a 32px input stacks tall enough
that two of them push the rest of the panel off-screen, and modal and
screen forms keep the same 28px so one form language covers the app.

| Control      | Height | Padding | Font                |
|--------------|--------|---------|---------------------|
| Text input   | 28px   | 0 8px   | 12px Inter          |
| Select       | 28px   | 0 6px   | 12px Inter          |
| Number input | 28px   | 0 6px   | 12px mono, centered |

Background `var(--bg-dark)`, border `1px solid var(--border-color)`,
radius `var(--control-radius-sm)`, focus border `var(--accent-color)` —
the same chrome as the ghost button, so a form field and the button
next to it visually belong to the same family.

**Every select in a panel is this 28px field** — there is no second dropdown
chrome. Compose sidebar form rows from these primitives — `.input-large-*` for a
prominent field (a name or id) and `.input-small-*` for a compact
label-beside-input row — not from `.form-group`. `.form-group` is the
**modal-body** form-row primitive (§13); it has no sidebar styling, so a
panel that reaches for it falls back to the browser's default oversized
inputs instead of the 28px control language.

Reference: `.diagnostics-panel-field select` in
[features/diagnostics.css](../static/css/features/diagnostics.css).

**On a wide column, the field follows its label.** `.input-small-row`
spreads its children to both edges. That is right in a 280px sidebar, but
on a 700px column it pins a short field hundreds of pixels from its label
(Proximity). There, give the label a fixed width and let the field and its
unit follow it. Reference: `.sp-scalar-row` in
[features/speed-profiles.css](../static/css/features/speed-profiles.css).

**A value that can never be changed on this panel is plain text, not a
disabled input.** A disabled input still looks like a field, and a row of
greyed boxes reads as clutter. Show the value as text with its unit, and
say where it is changed. On a locomotive's own speed profile, Model and Max
voltage come from the model profile, so they show as text (`12 V`), and the
note at the top of the panel names the model profile where they are edited.
A control that is frozen only for now stays a dimmed input, because it can
be changed in another mode (Read-only in Operate mode, below). Reference:
`_scalarRowHtml` in
[editor-speed-profiles.ts](../frontend/src/editor/editor-speed-profiles.ts).

### Seamless inline edit

A field that reads as plain text until you click it, then becomes a normal
input. Use it where the value **is** the identity shown in place — the
display name in a §11 status header — so editing happens where the eye
already is, not in a separate "Name" field stacked below. It is a real
`<input>` the whole time; only its chrome changes with state:

- **Resting** — transparent background, transparent border, inheriting the
  type of the text line it stands in (the 11px `--text-secondary`
  display-name line). It looks like a label, not a box, so it doesn't add a
  second framed control to a header that already has a title.
- **Hover** — the border lifts to `--border-color`, announcing the field is
  editable before the click (a static label never does this).
- **Focus** — drops in the standard input chrome: `--bg-dark` fill,
  `--accent-color` border. Now it reads as the 28px field language above.

Pair it with the auto-save contract (§5C): it commits on blur, with the
field-edit flash (§18) as the only acknowledgement — an inline edit never
carries its own Save button. Give it the italic `--text-tertiary`
placeholder invitation ("Add a name…") so an empty value still teaches what
the field is (§19). Reference: `.status-header-name-input` in
[components/status-blocks.css](../static/css/components/status-blocks.css),
consumed by the segment-editor header in
[editor-segments.ts](../frontend/src/editor/editor-segments.ts).

### Checkbox

The on/off primitive — see "Choosing a selection control" above for when a
checkbox is the right shape rather than a segment or select. The native
input is visually hidden and a 14px box stands in for it: a 2px
`--border-color` outline on a dark fill with a 3px radius when unchecked,
filling with `--accent-color` plus a white check when checked, and a 2px
accent ring on focus. Label is 11px `--text-primary`. Hover lifts the box
border to the accent (`--accent-hover` once checked); a `.disabled` wrapper
disables it the shared way (Disabled controls, below).

- **A set of flags stacks into a grid, not a row of segments.** Independent
  flags inside a panel lay out as a 2-column grid so the eye runs down a
  column of labels. Each is its own boolean; none excludes another. The
  canvas layers (track bits, coordinates, block voltage, …) are flags of
  this kind too, but they are not in a panel: they are toggle rows in the
  top bar's View menu (§13 Header-anchored menu).
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
the multi-flag grid is `.debug-controls` in the same file (no panel uses it
since the canvas layers moved to the View menu).

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
  ran. Each edit here is independent and reversible, so auto-apply it and
  drop the Apply button (the auto-save contract, §5C "Avoid per-field Save
  buttons"); a rejected edit
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
`updateSecondaryEditorForTile`, `initBitEditor` + `_commitTrackBits` (the shared
commit path), and `initVisualTrackEditor` (canvas) in
[frontend/src/editor/](../frontend/src/editor/).

### Curve editor (continuous draggable plot)

The continuous sibling of "Two views of one value" above. Where the track-bits
editor pairs a clickable diagram with discrete checkboxes over a 6-bit value, the
speed-profile editor pairs a **draggable curve plot** with a **numeric point
list** over a continuous curve — both views write the same array of points. The
same container rule holds: the narrow numeric column hands the plot the rest of
the row width (`.sp-plot-block` is `minmax(108px, 30%) 1fr`), and the two views
sit straight in the section body, not in nested cards.

The plot's direct-manipulation grammar:

- **Drag a point** to move it. Endpoints are X-locked; an interior point stays
  strictly between its neighbours; Y clamps to the axis and keeps the curve
  ascending. The cursor reads the affordance — `grab` over a point, `grabbing`
  while dragging.
- **Left-click the line** between points inserts a new point there (cursor
  `copy`).
- **Right-click an interior point** removes it; the two endpoints are locked
  against removal.

Every edit auto-saves silently (§5C) — no Save button, no save banner, so nothing
shifts as a save settles — and the list and the plot stay in sync because they
are two views of the one points array. Render the plot at device resolution, as
the bit editor's canvas does (the "in-panel editor canvas" rule above).
Reference: the curve editor (`handleDown` / `handleMove` / `handleContext`) in
[editor-speed-profiles.ts](../frontend/src/editor/editor-speed-profiles.ts);
`.sp-plot-block`, `.sp-canvas`, `.sp-point-*`, `.sp-converter*` in
[features/speed-profiles.css](../static/css/features/speed-profiles.css).

### Slider / handle

For continuous (power %) or notched (master controller) inputs. Native
`<input type="range">` with custom track + thumb styling. Two patterns:

- **Plain slider** (`.tc-power-slider`) — 6px-tall `--border-color` track, circular thumb in the relevant accent (e.g. `--success-color` for a power slider).
- **Notched handle** (`.tc-handle-slider`) — 8px-tall track with a multi-stop gradient that encodes the notch zones (green=power, gray=neutral, yellow=brake, red=EB). Rectangular grab-handle thumb with an accent halo. Above the track, a 15-column tick grid renders the discrete notch labels; the active notch is highlighted with the same accent tint used by the segmented-control active state. The zone colors are master-controller convention — power / neutral / brake / emergency zones — not the §1 status semantics: yellow here means "brake zone," which is normal driving, not a warning. The handle gradient is the one place these hues are positional rather than status-bearing.

Disabled state across both: the shared rule (Disabled controls, below).
Reference: `.tc-power-slider`, `.tc-handle-slider` in
[features/train-control.css](../static/css/features/train-control.css).

### Disabled controls

A control that can't be used right now — button, select, input, checkbox,
slider — drops to `opacity: 0.4` with `cursor: not-allowed` and keeps its
shape, so the layout doesn't jump when it comes back. Say why it is off, in
its `title` or a one-line hint beside it. The structural lock below is a
different state: the panel still shows the record's values, so its frozen
controls dim less (`opacity: 0.55`) to stay readable.

### Read-only in Operate mode (the structural lock)

The physical layout is frozen once you are operating. Picking the Operate
or Locomotives screen turns the lock on, and picking Editor turns it off.

A click on the map in Operate opens Operate's own panel for what it hit: a
train, a block or a switch. Those panels are built from operational
controls only, so there is nothing in them to lock. Any other record opened
on Operate — a platform, station, signal, tunnel or segment, or one of the
lists — still shows its Editor panel in the right panel. That panel goes
**read-only for structural edits**: names, geometry (length / radius /
speed), the create / extend / remove / delete buttons, and signal and
tunnel config are all disabled. A one-line hint at the top of the panel
(`.sim-lock-hint`, info accent + lock glyph) says why, and each frozen
control dims to `opacity: 0.55`.

What stays live is the **operational** set — the controls a dispatcher uses
while running the railroad: the availability segment (Normal / Manual /
Disabled), the allowed-travel-direction segment, and panel navigation links
(flank cross-links, "Manage Logical Stations", "All Stations"). Operate's
block and switch panels hold the same set, plus the switch's **Throw to …**
button. These keep full contrast, so the live dispatch controls read
clearly against the dimmed structure. The split line is simple: **typed
property fields and structure-mutating buttons freeze; segmented dispatch
toggles and navigation stay live.**

This is one general mechanism, not a per-panel rule: a control opts out of
the freeze by carrying `data-sim-keep` (on itself or an ancestor), and the
shared `applyStructuralLock` helper disables everything else. The two segment
render helpers (`renderAvailabilitySegment`, `renderDirectionSegment`) carry
the marker, so all four entity panels (Block, Switch, Platform, Station) inherit it. Operational panels (path,
route, train, speed profile) never call the lock at all — the dispatcher
creates and runs those freely while operating ("full dispatcher freedom").

Reference: `applyStructuralLock` in
[lib/helpers.ts](../frontend/src/lib/helpers.ts); `.sim-lock-hint` /
`.sim-locked` in [layout/shell.css](../static/css/layout/shell.css); the
list of locked panel types (`STRUCTURAL`) in
[next/operate/panel-host.ts](../frontend/src/next/operate/panel-host.ts).

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
- Unit: 11px Inter (`--font-size-body`), `--text-secondary`
- Rows separated by `1px dotted var(--border-color)`

The **meaningful** identifier is the value. Internal keys that name no
entity the user works with (`run_number`, `path_id`, internal hashes) go in
chips (§7) or foot captions, not in the title slot — the user's eye is
looking for the entity, not the database key.

**Record identity — the id or the name first.** A record the user works
with has a stable id (`route_id`, a train id, a block id) and may have a
display name. Where records of one kind sit together — a card, a detail
page, a list of one kind — the id is the title, in mono, and the name is
the line beneath it: ids line up and stay unique, while names may repeat
or be empty. Where records of several kinds sit together — a context menu
— the name leads and the id is the quiet mono line beneath, because the
name tells the kinds apart and the id then tells two same-named records
apart (§13 Context menu). A record with no name shows its id alone. Either
way the id clamps and the name wraps (Overflow, below).

### B. Three-column row (right panel body)

```
┌─ name (mono, 11em) ─┬─ badge (4.5em) ─┬─ value (mono, 1fr) ─┐
```

Used for tabular data where each row has a categorical badge. Color-code
the badge background using the accent palette. Reference:
[`.diagnostics-row`](../static/css/features/diagnostics.css).

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

Auto-save needs a quiet acknowledgement in place of the button it removed.
Use the one-shot field-edit flash (§18) — an accent border pulse for an
accepted value, a danger pulse for a rejected one that reverts to the last
good value — not a toast: a toast on every blur is noise when six fields each
save themselves. A client-invalid entry (non-integer, out of range) reverts
and flashes danger without a round-trip; only an unexpected server error
earns a toast.

References: `.route-header-row.run-overrides-grid` in
[features/routes-paths.css](../static/css/features/routes-paths.css)
(3-row inner sequence — label / control / reset), consumed by the
Per-run overrides section in `editor-route-runs.ts`; and
`.route-header-row.route-policy-grid` in the same file (4-row inner
sequence — label / control / "Push to runs:" sublabel / soft-force
segment), consumed by the policy row in `editor-routes.ts`. That makes two
consumers, so by the promotion rule in "Where the primitives live" the
rules are due to move into a shared `.field-cluster*` family in
`components/status-blocks.css`, with each panel layering only its column
widths and per-cell content on top (see Known divergences).

### Overflow: identifiers vs names

Three kinds of text with three different answers — the doc used to
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

Pair with `title="${full string}"` so the value stays reachable — and, on a
canonical identity display, with a copy button (see [§11 Copy button on an
identifier](#copy-button-on-an-identifier)): hovering recovers the tail for
*reading*, but the user usually wants it for *carrying*.

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

**One-line status sentences** (a train's hold reason on its card): clamp to
one line with an ellipsis and put the whole sentence in a `title` tooltip.
The sentence explains a state the card already shows with its stripe and
chips, and the card must keep its width.

Do **not** clamp numbers, time-of-day, short status labels, or units —
those are sized to fit and should never get an ellipsis. If a numeric
value won't fit, the column is too narrow, not the value.

The `min-width: 0` line is the rule most often missed when clamping.
Flex children default to `min-width: auto`, which lets their content
expand the cell and silently defeats `overflow: hidden`. Set it
explicitly on every flex child whose content is supposed to truncate, and
on every flex item between it and the box whose width is fixed. The train
card's hold line had it, but the card, itself a flex item in the cards
row, did not, so a long hold reason widened the whole card to the length
of the sentence.
Reference: `.rr-card-route` (clamp, opaque id) in
[features/routes-paths.css](../static/css/features/routes-paths.css), and
`.status-header-name` (wrap, display name) in
[components/status-blocks.css](../static/css/components/status-blocks.css).

---

## 6. Status accents

Three patterns for showing a row's status:

1. **3px left-border** on the row (most common). Color matches the accent
   palette: green=ok, purple=selected, red=fault, yellow=warn,
   info=informational. A row that names a block's state takes its colour
   from `getBlockStateColor` instead (§1 Block / track status). This is the
   canonical status accent for cards, card-shaped list rows and status
   headers. A dense list of comparable records (§12 rows) marks status with
   the §12 status dot and its glyph instead.

2. **Banner**: full-width strip with a 3px left-border accent + ~10% opacity
   background tint of the same color. Use for one-line diagnostics
   (`speed_limit_reason`, `ATC: yellow signal at S-3`) or a label/value
   pair (e.g. `Locked by · PATH_…` on a switch panel — `.banner-label`
   over `.banner-value`). Banners belong in the panel **footer** for
   running diagnostics; an inline-in-body banner is also fine when it
   surfaces a single high-priority detail for the record on screen.
   Use a warning banner for an outcome the operator did not cause: a speed
   pass that ended on its own ("The pass ended: The emergency brake is on.
   Nothing was written.") sits in a `.banner-warn` above the step's
   buttons. The same kind of line for something the operator did (they
   pressed Stop) stays plain text, because it tells them nothing new.
   Canonical home: `.banner` / `.banner-{warn,danger}` / `.banner-label` /
   `.banner-value` / `.banner-text` in
   [components/status-blocks.css](../static/css/components/status-blocks.css).

3. **Source badge**: a small color-coded pill (used by the Logs screen's
   Diagnostics tab to show `stored / property / latch / external`). Background is the accent color
   at full opacity, text is `--bg-dark` so it reads on every accent (e.g.
   `--info-color` bg + `--bg-dark` text).

Where a stripe's colour comes from: a fixed state uses an `.accent-*`
helper class; a colour the renderer works out at run time (an entity card
following a block, switch or signal) is set inline as `--card-accent`. A row
that can also be picked sets `--row-status-color` to the same colour, which
`.row-selected` reads to keep the left edge (§12 Selected row).

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

For state badges (`▶ ATO RUNNING`), tint the background at
~15% opacity of the relevant accent and use the accent as the text color.
The EB-active badge is the one state badge with its own border and pulse;
it is specified with the Emergency button (§3).

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

**Colouring by kind.** A chip or card stripe that names what *kind* of
entity something is — not its state — uses one map:

| Kind | Variant |
|------|---------|
| Block, train | `.chip-accent` / `--accent-color` (purple) |
| Platform, station | `.chip-info` / `--info-color` |
| Switch | `.chip-warn` |
| Signal | `.chip-success` |
| Raw tile | `.chip-muted` |

A kind chip always carries the kind's word or the record's id, so the hue
is a second channel and never the only one; a state mark never uses this
map. Users: the §11 entity card (static identities) and the §11
multi-select summary.

---

## 8. Resizers and drag affordances

- **Column resizer**: 6px wide, `background-color: transparent`, hover/active
  → `var(--accent-color)`.
- **Row resizer / drag handle**: 6px tall version of the same.
- Cursor: `ew-resize` for column, `ns-resize` for row, `move` for whole-panel drag.
- **Double-click-to-fit**: double-clicking a floating window's header resizes
  it to its content's natural size (clamped to the viewport and to the
  window's min/max), without moving its top-left anchor — the same corner it
  grows from when dragged. Only the header triggers this. It applies to the
  right panel while it is popped out and to the two floating windows left
  from the old layout (Surfaces). Shared by every floating window (`FloatingWindow.fitToContent`,
  [window-manager.ts](../frontend/src/lib/window-manager.ts)).
- **Coarse-pointer sizing**: resize-handle and drag-handle hit areas scale up
  under `matchMedia('(pointer: coarse)')` — a touch/stylus hardware panel —
  via `coarsePointerScale()` in
  [dpi.ts](../frontend/src/lib/dpi.ts). A hit-target concession, not a
  layout or zoom change; canvas rendering separately scales its backing
  store by `devicePixelRatio` for crispness (§16), which this is not — the
  two are unrelated DPI concerns.

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
- **Error in a row**: on a card or card-shaped row, `border-left: 3px solid var(--danger-color)` + `background-color: rgba(var(--danger-rgb), 0.08)` + value text in `var(--danger-color)`; in a §12 row, the red status dot with its `✕` glyph.
- **A failed read is never an empty list.** A list the panel reads from the server shows "Loading…" until the first answer, and the empty line only after a read that worked. When no read has worked yet, it says so with `.empty-state-error` ("Could not read the routes: … Trying again."). When a later read fails, it keeps the last list and puts a §6 `.banner-warn` over it saying the list may be out of date. A picker fed by a read follows the same rule in its first option ("Reading the routes…", "Could not read the routes"). Reference: `PolledBody` in [next/operate/left-list.tsx](../frontend/src/next/operate/left-list.tsx); `routePickerPrompt` in [next/operate/train-panel.tsx](../frontend/src/next/operate/train-panel.tsx).

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
rounded shape its hard edge. References: `.rr-card`, `.path-row`.

### Status-accented card

Standard card + a 3px left-border drawn from the status palette
(`--info-color`, `--success-color`, etc.). The strip says "this card is
about X" without needing a header chip. Optional 4%-tinted linear
gradient on the left edge reinforces the strip.

```css
border-left: 3px solid var(--info-color);
background: linear-gradient(180deg, rgba(var(--info-rgb), 0.04), var(--bg-dark) 70%);
```

References: the **`.entity-card`
family** in [components/common.css](../static/css/components/common.css) —
the compact (4–6px padding) card used for the entity readouts in the
Editor right panel's tile, block and station panels. Operate's train,
block and switch panels open with a §11 status header instead. It is
**one** shared container, not five per-entity
classes: the 3px left-border is driven by a `--card-accent` custom property
the renderer sets inline — a dynamic state color for block
(`getBlockStateColor`) / switch / signal (aspect), or a static palette value
for fixed identities from the §7 kind map (station = `--info-color`, train =
`--accent-color`). Inner parts: `.entity-card-dot`
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
its left-border always carries a meaningful accent color from the palette (for
a block's state, from `getBlockStateColor`) — initialize it to whatever the
component's idle or default state maps to.
Using `--border-color` here is wrong: it makes the block visually
indistinguishable from a standard card border and defeats the callout intent.

For an alert state, layer a horizontal gradient wash over the bg:
`linear-gradient(90deg, rgba(var(--danger-rgb), 0.10), var(--bg-hover) 60%)`
turns the emergency state into a tinted but still readable block.
References: `.tc-status-headline`, `.tc-section-task`.

### Copy button on an identifier

Every canonical identity display — the id in a status header, in an entity
card, in a train-control headline — carries a copy button immediately to its
right. Entity ids are not read so much as *carried*: into a filter box, a bug
report, an API call. They clamp to an ellipsis (see [§5 Overflow: identifiers
vs names](#overflow-identifiers-vs-names)), so the differentiating tail is
exactly the part hardest to retype by eye.

Wrap the id and its button in `.id-copy-group` and call
`copyIdButton(id, 'Segment ID')` from
[lib/copy-id.ts](../frontend/src/lib/copy-id.ts). The wrapper is load-bearing:
the clamp needs `min-width: 0` on every flex ancestor, so the group declares it
and the button keeps its intrinsic width while the id gives way.

```html
<div class="id-copy-group">
    <div class="status-header-id font-mono" title="${id}">${id}</div>
    ${copyIdButton(id, 'Segment ID')}
</div>
```

Three rules this button breaks on purpose, all for the same reason — it is an
inline *text* affordance riding next to an 11–14px identifier, not a control-row
button:

- **20px square, not the §3 28px control height.** A 28px box inside a dense
  entity card sets the row height off the button instead of off its content.
  Radius is `--radius-xs`, the §1 token for small controls inside cards.
- **Hover is an accent tint, not a surface swap.** These sit on both
  `--bg-dark` (entity card) and `--bg-hover` (status header); a
  `rgba(var(--accent-rgb), 0.16)` tint is the one treatment that reads against
  both, where the §3 flat ghost's `--bg-hover` fill would vanish on the header.
- **Resting state is transparent with `--text-tertiary`.** The id is the
  content; the button is chrome, and only steps forward on hover.

Acknowledgement is doubled on purpose: the glyph flips to `ti-copy-check` in
`--success-color` for 1.2s (local — answers "did *this* button fire?") and a
toast names the value (answers "*which* id did I just take?" when several are
on screen).

Copying must never also activate whatever the id sits inside. The single
delegated handler in `copy-id.ts` listens on `document` in the **capture**
phase, so `stopPropagation()` cuts the event off before it reaches the
clickable card or row below. Panels wire nothing themselves — they rebuild
their `innerHTML` on every poll, and a per-panel handler would need re-binding
after each render.

Reference: `.id-copy-group` / `.copy-id-btn` in
[components/status-blocks.css](../static/css/components/status-blocks.css).

Not every appearance of an id gets a button. **List rows do not** — a list
exists to navigate into the detail panel, and the detail panel's identity
display is where the copy lives. A button per row is clutter that competes with
the row's own click target.

### Disclosure (collapsible card)

A card whose body collapses behind a native `<details>` / `<summary>`
toggle. Used for "I might want to look at this, but not by default"
(the train panel's Details, advanced settings). The card itself follows the Standard
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

1. **Title row** — primary text (the record's mono id, per §5 "Record identity")
   on the left, flat-ghost action right-aligned. The action is flat
   because the card already carries the border (no box-in-a-box).
2. **Subtitle line** — secondary text (the long display name, which
   wraps per §5 Overflow) on its own line.
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

- **Title row** — primary identifier of the record (§5 "Record
  identity"; mono, 14px, weight 700) on the left, state pill (§7 accent-tinted) on the right.
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
- **Chip row** — one §7 chip per entity, coloured by the §7 kind map
  (block purple, platform info, switch warn, signal success, raw tile
  muted). List the
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

### Composing a step-by-step screen

A job the operator does in a fixed order gets a list of steps, not a
dialog or a set of tabs. The Hardware Setup screen, which walks
design_new.md "Setting up the hardware and going live", is the reference.

- **The list on the left names every step, in order.** Each row shows the
  step's number in a round mark, its title, and one muted line of where it
  stands ("0 of 6 chips proven", "In Setup"). A done step's number turns
  into a check. A step the current choice does not need shows `–`, and
  opening it says why ("Simulation never reads the Hardware Setup, so it
  skips this step"). The picked step is a selected row (§12).
- **Marks are worked out, never stored.** The list reads the same facts the
  steps act on — the session state, the saved setup, the chip proofs — and
  decides every mark from them. A step done somewhere else (Activate in the
  top bar) shows as done here too, and nothing asks the operator to tick a
  step off.
- **Opening the screen shows the first step that is not done.** After that
  it keeps the step the operator picked.
- **The picked step reads top to bottom:** its number and title, with a
  Done chip once it is done; a short intro in plain words saying what the
  step is for; the step's content; and a `Next: <title>` outline button.
  The step's main action is the accent-tint CTA (§3). When that action is
  not available now, a warning note says why and links to the step that
  fixes it ("Enter Setup in step 3").

Reference: `SETUP_STEPS` / `stepMarks` / `firstOpenStep` in
[next/hardware/steps.ts](../frontend/src/next/hardware/steps.ts);
`StepFrame` in
[next/hardware/screen.tsx](../frontend/src/next/hardware/screen.tsx);
`.nhs-step*` in [next/hardware.css](../static/css/next/hardware.css).

**When there is no room for a step list beside the step, the steps hold
their own controls in one column.** The calibration group on the
Locomotives screen is the reference. That screen already has the locomotive
list on the left and, during a run, the map on the right, so a second list
column would squeeze both. The marks follow the rules above.

- **Each step holds the control that does it.** The step's title comes
  first, and the row that does the step sits right under it. A list of
  steps at the top with every control in a separate form below makes the
  operator read "pick a block" and then go looking for the block picker.
  That is what made the calibration group feel cramped: every step's
  controls were on screen at once, in a different order from the steps.
- **A step that is not current shows one line:** its value and the one
  button that opens it (the lighting-voltage row: `1.5 V` and Calibrate).
  The operator can redo a done step from there without opening anything
  else.
- **Only the current step opens.** It is shaded `--bg-hover`, its mark gets
  the accent ring, and its open run shows its working controls: the voltage
  stepper, the block picker, the pass progress, or Save and Promote. The
  other steps are muted, and a step still to come has a lighter title.
- **The hint shows only while nothing is open.** It says how to start the
  current step ("Press Calibrate speed, pick a block on the map or in the
  list, then Start."). Once a run is open, its own controls and text say
  what to do, and the hint would be out of date.
- **A control that serves several steps moves with the job.** The speed
  row sits under step 3 while the pass is set up, under step 4 while it
  runs, and under step 5 while the result waits to be committed.
- **Values stored on the record stay out of the steps.** The steps are a
  job the operator does; the record's own settings are not part of it. Put
  each setting beside the thing it sets: Max voltage is the 100% point of
  the voltage ↔ power curve, so it sits in that curve's section, and Top
  speed sits in the power ↔ speed section.
- **Labels inside the steps share one width** (160px), so the values and
  selects of every step start at the same x. When the column is narrower
  than 420px, each label goes on its own line above its control (§4,
  Control row layout).

Reference: `CalibrationSteps` / `stepRows` in
[next/locomotives/calibration-steps.tsx](../frontend/src/next/locomotives/calibration-steps.tsx),
which moves the Speed Profile panel's rows into their steps after each draw;
`calibrationSteps` in
[next/locomotives/model.ts](../frontend/src/next/locomotives/model.ts);
`.nloco-step*` in [next/locomotives.css](../static/css/next/locomotives.css).

### One button for a run of checks

When a job is many independent checks that need no one watching, offer one
button and end in one summary, not a button per check. **Test the rig** in
Hardware Setup step 4 is the reference: it scans the bus, checks every
block's detectors and throws every switch.

- **The button sits in a row with one muted line beside it.** At rest the
  line says what the run does and that it asks nothing. While it runs, the
  button reads `Testing…` and is disabled, and the line says what the run
  is doing now ("Checking BLK_3 (2 of 5)…"). When the run cannot start, the
  line says why ("Save the setup in step 2 first").
- **A failed check does not stop the run.** One bad wire must not hide the
  state of the others, so every check runs and each failure becomes a line.
- **The summary opens with one sentence** — "2 checks failed.", "Nothing
  failed. Some checks still need you.", "Every check passed." — then up to
  three groups of §12 rows, each shown only when it has lines, in this
  order: **Failed** (red status dot, ✕), **Still needs you** (warning dot,
  hand), **Proved** (success dot, check). Failures come first
  because they are what the operator acts on.
- **Each line names the thing and says what to do next.** "BLK_3: its bit
  did not change while the block was driven. Check the wire from the
  block's detector to hc165-1 bit 7." Lines that say the same thing about
  several records merge into one ("BLK_1, BLK_2 and BLK_3: each bit
  followed the drive."), so the summary stays short (Repetition blindness).
- **The run updates the records it checked.** A chip the run proved shows
  as proven on its own card too, so the summary is not the only place the
  result lives.

Reference: `runRigTest` in
[next/hardware/rig-test.ts](../frontend/src/next/hardware/rig-test.ts);
`RigTest` / `RigSummary` in
[next/hardware/screen.tsx](../frontend/src/next/hardware/screen.tsx);
`.nhs-rig-*` in [next/hardware.css](../static/css/next/hardware.css).

### A card the operator acts on (the train card)

Operate's cards row has one card per train, and it is the reference for a
card that stands for one record the operator acts on as a whole (Container
matches data):

- **Left edge:** a §6 status stripe that says whether the train needs the
  operator, so that train stands out when the eye runs along the row.
  This stripe answers "does the operator need to act?", not "what is the
  train doing?", so for this edge it overrides the §1 accent meanings: a
  dwell is info here, though §1 calls dwell a warning. A list that shows
  train *state* — the §12 Assigned Trains row — keeps the §1 meanings. It is
  read from the train's hold reason (design_new.md "Hold reason"):
  danger for the emergency brake or cut-out or a lost train; warning when it
  waits on the operator or a failed plan (unidentified, ATO not started, no
  route, planning failed); info when the railway makes it wait (a dwell, the
  end of its reservation, the ATC limit, a pending action, a calibration
  run); success while it moves; none when it stands by choice or the whole
  session is stopped, which the top bar already says.
- **Head line:** the train's name, then its `train_status` and `ato_status`
  as §7 state chips.
- **Hero:** the speed, large, with its unit, and the ATC limit beside it in
  small text — the number the driver watches next to the number that bounds
  it.
- **One line of why** while the train stands: its hold reason, cut with an
  ellipsis and shown whole in the tooltip (§5 Overflow, one-line status
  sentences). A question-1 reason (the session
  is Halted, in Setup, or Paused) is left off the card: it is the same for
  every train and the top bar already shows it. The train panel still shows
  it in full.
- **The card keeps its width.** A long hold reason ends in an ellipsis; it
  never widens the card (see [§5 Overflow](#overflow-identifiers-vs-names):
  `min-width: 0` on the card as well as on the line).
- **One main control,** chosen from the train's state: Go or Stop for ATO,
  Start route, Power on, Release, or a small handle for manual driving. The
  card carries only the next thing to do; every control lives in the
  train's right panel.
- **A click on the card selects the train,** the same as picking it on the
  map, and a pick on the map marks the card. A click on the card's control
  acts and does not select. To the keyboard the card is a button: Enter or
  Space selects it.
- **The row folds to one line per card.** A button at the end of the row,
  after + Place train and Hold all, folds it to about half the height: each
  card becomes one line of name, chips, speed and ATC limit, and the
  control, and its hold reason moves to the card's hover text. A folded card
  is as wide as its line, so the chips never squeeze the name; only a name
  longer than 180px ends in an ellipsis, with the full name in its tooltip.
  This is the one exception to §5 Overflow's wrap rule for names: the
  folded row exists to keep each card on one line. The buttons sit side by side when
  folded and stacked when not.
- **The end buttons stay in view while the row scrolls.** They are a sticky
  group, and the row's right padding is on that group rather than on the
  scrolling row, so no card shows between the group and the edge.

Reference: `TrainCardsRow` in
[next/operate/train-cards.tsx](../frontend/src/next/operate/train-cards.tsx);
`cardControl`, `cardHoldLine` and `trainCardTone` in
[next/operate/train-display.ts](../frontend/src/next/operate/train-display.ts);
`cardsFolded` in
[next/operate/operate-state.ts](../frontend/src/next/operate/operate-state.ts);
`.op-card*` in [next/operate.css](../static/css/next/operate.css).

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

- **Status dot** (leftmost, ~6px) — hue channel, always paired with a
  glyph: red + `✕` = fail, amber + `!` = anomaly, dim with no glyph = ok.
  The hue makes the column scannable down the left edge (Column
  alignment); the glyph keeps the state readable without the hue (§14
  "Don't let hue carry state alone").
- **Ordinal** (`3`, `4`, `5`) — dim mono, narrow, `--text-tertiary`.
  A stable reference anchor for "the third step," not the hero.
- **Verb** (`STOP` / `DWELL`) — bold accent-tinted chip (`.chip-state`, §7), the
  categorical filter the eye scans first (Scan order). Sized for the
  filter pass, not the identity pass.
- **Noun** (`STA_DDB6BE` / `MAIN_WEST`) — bright mono, medium-large,
  `--text-primary`. The identity — what the row IS.
- **Modifiers** (`platform · fwd`) — small, `--text-secondary`.
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
  dot and its glyph). Red `✕` = fail, amber `!` = anomaly, dim / none = ok.
  Encodes *correctness*.
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
file). Two consumers means the rules are due to move into a shared
`.list-row*` family in `components/status-blocks.css` (the promotion rule
in "Where the primitives live"; see Known divergences), with each panel
layering only its own column-set and per-cell content on top — keep the
grid container, status dot and glyph, verb chip, action column, and row
hover identical across panels so the eye trains once.

Worked example — the assigned-trains row uses the same skeleton with
the time column swapped for an `Unassign` action button:

```
●  T1   DWELL   Station 1    forward · 7s left   [Unassign]
●  T2   STOP    S2 South     forward             [Unassign]
●  T3   IDLE    queued       —                   [Unassign]
```

- **Dot** — warn (amber, `!`) while dwelling, danger (red, `✕`) when the
  live status payload is missing, transparent with no glyph when ordinary
  running. Same
  hue channel rules as the route-runs version; "noteworthy" means a
  hand to play (not "important row" — that's the brightness channel).
- **Identity** (`T1` etc.) — bright mono. The train ID is the row's
  stable identity, sits in the Hero-per-row stable-identity slot.
- **Verb** (`DWELL` / `STOP` / `IDLE`) — a `.chip-state` with the
  `.chip-info` / `.chip-warn` / `.chip-muted` variant (§7), the same as
  the route-runs verb; trains the eye across both editors.
- **Modifier** — derived dwell-remaining + direction. Sanity-cap the
  remaining-seconds value before display (here: suppress anything
  outside `0…3600`) — the §14 "Don't render unit-mismatched numbers
  raw" rule.
- **Action** — `Unassign` is a ghost button, **not** danger-colored:
  unassign is reversible. Danger (red) is reserved for irreversible
  destructive actions (Delete Route). Mixing the two devalues the hue
  for the genuinely destructive case (§6).

### Selected row (single-select lists)

When a list lets the user pick one row and keeps that pick on screen — the
saved-layout list, an entity picker, any future track / tile / hardware
target list — the chosen row carries one standard mark: an `--accent-color`
border and a faint `rgba(var(--accent-rgb), 0.15)` accent-tint fill. This is
the panel-chrome "selection / active" purple (§1 Accents) — the same hue that
marks the §11 multi-select summary — so "this one is picked" looks the same in
every list.

- **Border-plus-tint, never a solid fill.** The accent lives in the 1px
  border and a 15%-alpha wash. A fully accent-filled row would out-shout its
  own text and collide with the accent-tint CTA language (§3). Row hover stays
  the ordinary `--bg-hover`, and the picked mark reads brighter than hover so a
  hovered-but-unpicked row never looks chosen.
- **One picked row at a time.** Choosing a row clears the mark on the previous
  one. A list where several rows stay active at once is a different pattern — a
  multi-select, whose §11 summary callout (not a per-row tint) carries the set.
- **The mark is a state, not a layout.** This adds only the selected *state*;
  the row's contents stay whatever container the data calls for — a §11
  card-as-row or a §12 comparable-record row. Don't build a bespoke
  selected-row layout.
- **A status edge survives the pick.** A row with a §6 left stripe sets its
  colour in `--row-status-color`; the selected mark recolours the other
  three sides and leaves the left edge that colour, so picking a train card
  never hides whether the train needs the operator.
- **One class, everywhere.** Every list adds `.row-selected` to its picked
  row and writes no selected rule of its own. The rule doubles the class so
  it outranks a list's own base and `:hover` rules whatever order the
  stylesheets load in.

Reference: `.row-selected` in
[components/status-blocks.css](../static/css/components/status-blocks.css),
used by the saved-layout list ([app.ts](../frontend/src/app.ts)), Operate's
left list and train cards, the Hardware Setup step list, the
Locomotives list, and the Logs screen's Detectors tab (the block picked on
the canvas).

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

### Panel navigation and the unsaved-edits indicator

Navigation up and out of a panel lives in the right panel's header — one
implementation for every panel — never inside any one panel's body:

```
[← Routes]  Route Editor                    [≡] [⧉] [×]
```

- **Parent link** — when the open panel is the detail view of a record
  whose list panel exists, the header's left slot is a ghost button
  labeled with the destination (`← Routes`), never the ambiguous word
  "Back." It opens the list. List panels, and detail views with no list
  home (signals, platforms), show no parent link. Keyboard: Alt+Left.
- **Lists** (`ti-list`, Editor screen) — a menu of the lists that used to
  be tool-rail buttons: Switches, Stations, Blocks.
- **Pop out** (`ti-external-link`) — moves the panel into a floating
  window and back (`ti-layout-sidebar-right` while it floats); see
  "Popping the right panel out" below.
- **Close** — clears the selection. Keyboard: Ctrl/Cmd+W.
- **Button style** — the parent link and the Lists / Pop out / Close
  buttons are each a regular ghost (§3) standing alone on the header; the
  three icons are icon-only squares.

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
- **Refresh** — icon-only flat ghost (the toolbar is its container, §3). Polls already keep the panel in
  sync, but an explicit affordance gives the user "I clicked something;
  did the server hear me?" confidence.
- **Save** — accent-tinted background and a `*` suffix when dirty. Not
  a primary-fill button; the tint reads as "attention" without
  competing with destructive actions for visual weight.

---

### Popping the right panel out

The right panel of the Editor and Operate screens has one pop-out button
in its header. It moves the panel into a floating window — drag, resize,
double-click-to-fit, the `--z-window` band — and the same button docks it
back to the right of the canvas. The window adopts the panel's own
element, so popping out moves nothing and redraws nothing: every panel
inside keeps working. Whether it floats, and where, is remembered per
screen. It is the only floating window a screen keeps
(ui-layout-rewrite-plan.md §12, decision 2): there are no tabs, no
pinned groups and no split-off windows.

Reference: `RightPanel` in
[next/operate/right-panel.tsx](../frontend/src/next/operate/right-panel.tsx)
and `EditorPanel` in
[next/editor/right-panel.tsx](../frontend/src/next/editor/right-panel.tsx),
both on `FloatingWindow`
([window-manager.ts](../frontend/src/lib/window-manager.ts);
[floating-window.css](../static/css/layout/floating-window.css)).

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
z-index: var(--z-modal);
```

The header strip uses the same purple gradient as `.secondary-sidebar-header`
(`background: linear-gradient(135deg, rgba(var(--accent-rgb), 0.1), rgba(var(--accent-rgb), 0.05))`)
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
shadow. Options inside are flat-ghost rows: `--bg-hover` on hover, as §3.

```css
position: fixed;
z-index: var(--z-picker);
padding: 4px;
background: var(--bg-panel);
border: 1px solid var(--border-color);
border-radius: var(--control-radius-sm);
box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45);
```

Reference: `.resource-picker` in
[components/common.css](../static/css/components/common.css).

### Context menu

A small menu shown at the cursor on **right-click**, listing the actions or
targets under the pointer. Distinct from the Floating picker above: the picker
disambiguates a *left*-click that landed on overlapping targets, while the
context menu is the *right*-click action menu. Same panel-language shell —
`--bg-panel` bg, hairline border, drop shadow — with three inner parts: an
optional small uppercase section heading, a hairline separator between sections,
and flat-ghost item rows (icon · identity · right-aligned muted detail). An item
may carry an `accent` or `danger` emphasis for a primary or destructive action;
otherwise it reads as a plain flat ghost.

**Row identity.** A row that names a record shows its display name, with the
record's stable id on a quiet mono second line beneath — the mixed-kind case
of §5 "Record identity". The id line is not
decoration: names repeat across kinds — a block and its own member segment carry
one name by construction — so name-only rows form the indistinguishable run the
§3 tab strip also forbids. A record with no name is labelled by its id alone and
takes no second line. Names show **in full**: the label wraps rather than
ellipsizing, and the menu is wide enough (340px) that wrapping is the exception.

It stacks on `--z-picker` with the floating shadow (§1). One menu exists at a
time — opening another closes it — and it closes on outside
pointer-down, Escape, window blur, or after an item is chosen. It opens at the
cursor and nudges back inside the viewport when it would overflow an edge.

The mechanism is generic: callers pass sections of `{ label, sublabel, icon,
detail, onSelect, emphasis }` items and nothing in the menu knows about tiles.
The editor's per-tile menu — every train / switch / block / segment / signal /
platform on the clicked tile, plus inline actions like throwing a switch — is
one caller. Reference: `showContextMenu` in
[lib/context-menu.ts](../frontend/src/lib/context-menu.ts); `.tile-context-menu`
(+ `-heading`, `-sep`, `-item`, `-text`, `-sub`, `-item--accent`,
`-item--danger`) in
[components/context-menu.css](../static/css/components/context-menu.css).

### Header-anchored menu

A popover that drops from a top-bar button instead of the cursor — the
Layout, View, Settings and speed menus. It reuses the context menu's
shell (`.tile-context-menu`) but hangs under its trigger, lined up with
the button's left or right edge, and closes on outside click or Escape. Like the context menu it stacks on
`--z-picker`, with the floating shadow (§1).

A menu has three kinds of row: an **action** closes the menu and runs; a
**choice** shows a check on the current option, closes and runs (a theme,
a speed); a **toggle** shows a check while it is on and keeps the menu
open, so several can be flipped in one go (Show grid, the canvas layers).
Rows group into sections with an optional heading (`Editor theme`,
`Layers · Blocks`). A menu reads its rows each time it opens, so it always
shows the current state.

Reference: `MenuButton` in [next/menu.tsx](../frontend/src/next/menu.tsx);
`.ntb-menu` in
[layout/next-top-bar.css](../static/css/layout/next-top-bar.css).

---

## 14. Don'ts

- **No color emoji anywhere in the UI.** The icon system is Tabler (see §3 Icons); monochrome Unicode glyphs (`▸ ▾ ×`) remain fine for tiny structural chrome — carets, a select-arrow decoration, a close-X on a tab. Pictographic glyphs Inter doesn't carry (`⟳` / `↻` rotate arrows, play/pause triangles) fall back to Arial and misalign inside a button box — use Tabler for those. Both render in the CSS `color` property and respect the token palette. Color emoji (`🚦 🚉 📋 📊 🚨`) render as color pictographs regardless of `color:` and fight with the palette — never use them in buttons, headers, labels, or any panel chrome. Quick test: if setting `color: red` on the character turns it red, it's a monochrome symbol and is OK; if it stays a colorful picture, it's an emoji and is not.
- **No new colors.** If you can't reach for a token, the design doesn't have a slot for it yet — pause and discuss instead of inventing.
- **Don't cross the canvas/panel hue firewall.** The canvas tokens (`--canvas-*`, `--block-*`, `--train-body-*`) paint canvas geometry and §17 state marks. In panel chrome the `--block-*` tokens appear only through `getBlockStateColor`, on an element that names a block state; the other canvas tokens never appear there. The §1 accent palette tints panel chrome; it never paints canvas geometry. The two palettes deliberately tune the "same" hue differently (OCCUPIED `#ef4444` vs `--danger-color` `#e83f5b`) — mixing them re-merges what was split on purpose. Panel UI that *describes* a block state (a chip, a legend swatch) goes through `getBlockStateColor`, not a hand-picked accent.
- **Don't let hue carry state alone.** Roughly 8% of male users can't reliably separate red from green, and this product's audience skews male — that's a real slice of the user base, not a compliance checkbox. Every state mark pairs its hue with a second channel: position (the §6 accent is always a left border; a signal lamp sits on its stick), shape or text (the switch badge carries `M`/`D`; chips carry words), or brightness (dim = inactive). A mark whose *only* meaning is red-vs-amber at 6px fails the check — give it a glyph, a label, or a position the eye can read without the hue.
- **No freelance ornaments inside a shared shape.** The same logic that bars new colors bars adding visual elements (an inner status dot, a leading icon, a divider rule, a decorative glyph) to a canonical primitive the doc already specifies. The §7 chip is text on a tinted pill — not "text + a glowing dot." If you find yourself reaching for an extra mark to make a component read better, pause: either the component is wrong for the job (pick a different one) or the doc is missing a slot (raise it as a doc question). Quick test: would I have to mint a new class to render this? Then it's freelance.
- **No solid filled buttons in panels.** Use the ghost + accent-tint pattern.
- **Don't border the container *and* every child.** If the container has a border (segmented cluster, tool grid, tab strip, card), the children inside are flat ghosts. If the children need their own borders (standalone buttons on the panel body), the container is unbordered. Doubling the borders reads as fenced-off boxes inside fenced-off boxes — the rendered weight is double what the structure needs.
- **No nested panels.** A panel inside a panel inside a panel is a sign the structure is wrong.
- **No multi-paragraph banners.** A footer banner is a single line of diagnostic text. Anything longer belongs in a tooltip or a disclosure.
- **No `box-shadow` for emphasis.** Use border + background tint. The only shadows are the three overlay shadows in §1 Shadows.
- **Don't hide the audit trail by default.** When the most common next user question is "what just happened?" — after activate, assign, or any state change — the history / audit panel must be visible without a click. Compactness comes from capping per-entity row counts (top 10 per train, top N per session), not from hiding the whole section. Reference: `_historyMaxRunsPerTrain = 10` in `editor-routes.ts`. The wrong polarity (hide-by-default) trains users to assume the data isn't there.
- **Don't render unit-mismatched numbers raw.** When a numeric field is supposed to be a duration but the backend can leak an absolute timestamp through it (different clock units, an off-by-1000 ms-vs-s mix, a wall-clock value where a sim-clock value belongs), sanity-cap the display in the UI: implausible values are hidden, not rendered as "dwell 1779949962s." The backend fix is separate; the UI must not propagate the garbage. Quick test: write down the plausible range for this field and assert against it before formatting (e.g. dwell-remaining: `Number.isFinite(v) && v >= 0 && v <= 3600`).

---

## 15. Reference panels

When in doubt, copy the structure of one of these:

- **Shared compositional primitives** → [components/status-blocks.css](../static/css/components/status-blocks.css) — the §6 banner, §7 chip, §11 status header, and §6 accent helpers. Editor panels consume these classes directly (`.status-header`, `.chip`, `.banner`, `.accent-*`) instead of redefining the shapes per feature.
- **Detail-page editor** → [editor-route-runs.ts](../frontend/src/editor/editor-route-runs.ts) + [features/routes-paths.css](../static/css/features/routes-paths.css) — the worked reference for composing a `status-header` + stats block + list of rows inside an editor sidebar. Route-run-specific bits (`.rr-card`, `.rr-summary`, `.rr-task-*`) layer on top of the shared primitives.
- **Long edit-and-act panel** → [editor-routes.ts](../frontend/src/editor/editor-routes.ts) + the `.re-toolbar` / `.atr-*` rules in `features/routes-paths.css` — the worked reference for a panel that mixes editing (tasks, mode, policy) with live monitoring (assigned trains, history). Demonstrates: sticky top toolbar with Save + dirty indicator + Refresh; assigned-trains list as a row-shaped second consumer of the `.rr-task-*` skeleton; history default-visible with per-entity cap; Force checkbox adjacent to the Assign button it modifies.
- **Floating window chrome** → [layout/floating-window.css](../static/css/layout/floating-window.css) + [window-manager.ts](../frontend/src/lib/window-manager.ts) — the shared header (title + close, or an adopted header with its own buttons) + content area + 8-direction resize handles, draggable, resizable, double-click-to-fit. Header has no border; relies on `--bg-hover` against `--bg-panel`. The right panels' pop-out composes this instead of redefining it.
- **Logs screen tab content** → [diagnostics.css](../static/css/features/diagnostics.css) — toolbar + scrolling body, shown in the Logs screen's Diagnostics tab. Toolbar keeps `border-bottom` because it shares `--bg-panel` with the body (per the §2 toolbar rule: same surface → hairline separator needed).
- **Look-ahead graph** → [train-monitor.css](../static/css/features/train-monitor.css) — the graph, view-distance control and Upcoming list the Operate train panel's Look-ahead section shows. The segmented view-distance control is a bordered container of flat ghosts (no internal dividers).
- **Right panel form** → [train-control.css](../static/css/features/train-control.css) — sectioned vertical stack inside a right panel. Status rows use the 3px left-border accent only, no surrounding 1px box.

**Not a reference — outside this doc.** The standalone debug page
([static/debug.html](../static/debug.html), styled by
[debug.css](../static/css/debug.css)) is a developer diagnostic surface with its
own dark code-tool palette (a GitHub-dark `#0d1117` ground and a JSON tree
viewer). It is intentionally not governed by the design language — don't copy its
chrome into the product UI, and don't tokenize it to match.

---

## 16. Canvas labels

Labels rendered directly on the canvas (station, platform, train) — distinct
from the inline field labels in §5. These labels coexist with track chassis,
block state colors, signals, and moving trains, so they need rules the
panel-chrome vocabulary doesn't cover: zoom-aware borders, z-order under
moving things, opaque fills that survive overlap.

### Tier hierarchy

Pick the tier by liveness:

- **Static identity** — quietest, two forms:
  - **Platform name** — stamped directly on the platform body bar: white
    text, no box, no border, rotated to run along the bar and sized to the
    bar thickness (≈ 7px at scale 1). The bar is its own label surface, so a
    both-sides platform shows the name on both bars.
  - **Station name** — a small chip (opaque dark fill + thin neutral outline
    + white text, 9px) at the centre of the platform group. It slides to
    clear space just outside the group when the centre would sit on a bar, so
    it never covers a platform's on-bar name.
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

The platform name has no fill of its own — it rides on the opaque platform
body bar (the muted structural fill, not a state hue), which already blocks
what's behind it. The rest of this rule is about the label *chips* (station,
train).

**Label chips use a near-opaque dark base** (alpha ≥ 0.85) so an overlapping
chip doesn't bleed through and read as visual mush. The categorical color
comes from:

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
| Station default   | `rgba(15, 15, 18, 0.92)`      |
| Station hover     | `rgba(50, 35, 5, 0.92)`       |
| Train             | `rgba(32, 26, 50, 0.92)`      |

References: `_drawLabel` and `_placeLabel` in
[renderer-stations.ts](../frontend/src/renderer/renderer-stations.ts); `drawTrainLabel`
in [renderer-trains.ts](../frontend/src/renderer/renderer-trains.ts);
`drawPlatformNameOnBody` in
[renderer-platforms.ts](../frontend/src/renderer/renderer-platforms.ts).

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
2. Platform bodies + on-bar names (identity tier)
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
`#rail-canvas` (z-index 2, owns the pointer) painted on top by `_drawLive`
— the platform/station identity labels (the station chip re-evaluates on every
mousemove for its hover state), route overlays, trains + train labels, and the
select overlay.
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
| Platform names (first) | no | `'platform'` |
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
- **Station chip over a platform name.** The platform name lives on the bar,
  so the station chip — which anchors at the platform-group centre — would
  cover the nearer bar's name whenever two platforms sit close together. The
  fix gives the *station* chip the positional freedom: it keeps the centre
  when that's clear of every bar, else slides to the nearest clear slot just
  outside the group (`_placeLabel`, candidates = centre + four around the
  group bounding box). The platform name has no candidates — the bar is a
  fixed surface — so only one of the two needs to move. This is its own
  geometry check, separate from the train-label registry above.

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

The token rule extends here: state marks consume the §1 canvas tokens —
`--block-*` through `getBlockStateColor`, every other mark through the
cached canvas palette (§1 Canvas state marks). The one mark still
hard-coded at its call site is a Known divergence to burn down, not a
license to add more.

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
4·scale grey ring. Aspects: green `--canvas-success`, yellow
`--canvas-caution`, red `--canvas-danger` — and red is also the fall-through for any state value the
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
status color. Emphasized routes add brown (`--canvas-hold`) lock circles
on their locked switches — a separate mark from the switch's own lock ring
(Switch state, below), drawn only while the route is emphasized. Reachable-destination highlights are amber diamonds. A
conflict tile flashes — red fill, white outline, white `×`, blinking
every 200ms — the only blinking mark on the canvas (see §18 Motion). That
flash is one preset of a general `flashTiles` primitive: `flashConflictTiles`
is the red-strobe-with-`×` case, and a quieter **locate** flash — a solid
`--canvas-highlight-locate` fill, one shot, no `×` — fires when the user clicks a tile in an
editor list to find it on the map. Locate stays solid and non-repeating, so
the conflict strobe remains the only *blinking* mark.

### Switch state

A switch shows its thrown position as a center badge
([renderer-tracks.ts](../frontend/src/renderer/renderer-tracks.ts)):
a green `M` circle when the main leg is set, a yellow `D` when the
diverging leg is set. The leg not taken draws thin and dark so the set
leg reads as the only continuous path. Lock rings around the badge:
brown (`--canvas-hold`) = a train physically holds the switch, orange
(`--canvas-lock`) = a path holds it for an upcoming move.

### V2 trains on the layout

The [v2 train display](../design_v2.md#seeing-trains-on-the-layout) uses one
track-aligned line style, a direction arrow and an ID. Use the canvas palette's
train body, background, label and accent colors. The emphasis changes line weight and
the label border as well as color, so it remains visible across layout themes.

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

**Unidentified trains.** An unidentified train — something the detectors
found that the app did not put there and cannot drive — outranks every
tint above: steel `--train-body-unidentified`, drawn with a **dashed**
outline and fill instead of the usual solid stroke. The dash carries the
meaning; the tint alone would read as just one more mode. It takes no
mode hue at all, because it has no cab, no mode and no brake to report,
and it must never borrow EB's purple — it is not a fault, it is an
unknown. In panels the same rule holds: the trains-list card and the
train panel show an `UNIDENTIFIED` state chip on the muted accent and
render none of the operator controls, since the API refuses every one of
them for it. Reference: `trainBodyFill` in
[renderer-trains.ts](../frontend/src/renderer/renderer-trains.ts),
`renderUnidentifiedTrainListItem` / `renderUnidentifiedTrainPanel` in
[editor-trains.ts](../frontend/src/editor/editor-trains.ts).

Do not confuse this with a **ghosted** train, which is the opposite case
— a train the operator placed whose detected blocks all read clear, so
the app holds an identity with no current behind it. It keeps its own
identity and its retained span, and is marked with a question mark
rather than the steel dash (design_new.md, **Power** → Ghosted trains).
"Ghost" elsewhere in this document always means the button pattern (§3)
or a placement preview (below), never a train.

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

### Debug overlays

A separate, toggle-gated layer of diagnostic marks drawn over the world layer
when their canvas layers are on (the View menu's Layers sections; `LAYERS` in
[lib/canvas-layers.ts](../frontend/src/lib/canvas-layers.ts)):
per-tile track-bit dots and a numeric bits readout, `(x, y)` coordinate labels,
N/E/S/W edge labels, switch-state badges, and dead-end markers. These are
developer aids, not production state — off by default, and they never carry
safety meaning. They read the cached canvas palette (`canvas-palette.ts`), so
they re-tint with the canvas theme, and draw on each world-layer flush.
Reference: `RendererDebugHandler` (`drawDebugOverlays`, `highlightDeadEnds`) in
[renderer-debug.ts](../frontend/src/renderer/renderer-debug.ts).

### Rules

- **State marks consume tokens.** New marks pick from the §1 canvas
  tables (Block / track status, Canvas state marks). A mark that needs a
  hue they don't have is a doc question first (§14 "No new colors"). The
  one hard-coded colour left, the route conflict flash, is a Known
  divergence — not a colour to copy.
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
| Field-edit flash | One-shot 0.7s border flash — accent (saved) / danger (rejected) | Auto-save acknowledgement on a panel field (§5C) |
| Locate flash | One-shot solid tile fill, ~650ms, `--canvas-highlight-locate` | Pointing at a tile from a list — clicking a block-editor tile row (§17) |
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

An empty layout is the biggest empty state, so it gets a card of its own.
On the Editor and Operate screens, a layout with no track shows a centred
card with three choices:

- **Start from a loop** builds a small oval with one train and a route
  round it, then opens Operate with that train picked — three clicks from
  an empty app to a moving train.
- **Open a layout** opens the saved-layout list.
- **Draw from scratch** opens the Editor with the Track tool picked and
  keeps the card away.

Reference: `StartCard` in
[next/first-run/start-card.tsx](../frontend/src/next/first-run/start-card.tsx).

### Every edit is reversible or confirmed

The editing contract (§5C) is auto-apply for independent, reversible
field edits: segment clicks and field changes save immediately, with no
Save step to forget. A long editor whose edits only make sense together
(Route Editor, Path Editor) uses one sticky Save instead (§12). The
contract has two halves, and the second half is what makes the first safe:

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

**A reason from the server is just the reason.** The panel writes the
sentence around it ("The pass ended: … Nothing was written."). A server
reason that carries its own "calibration pass ended:" makes the panel say
"ended" twice, so the server sends only the reason, and names the thing
that failed ("the emergency brake is on"), not a general "conditions no
longer hold".

**Tooltips carry the how.** Every tool and non-obvious control gets a
`title`: what it does, then how to use it — the Editor options row's
Trace ("Click two pieces of track to select the track between them…") is
the pattern. A newcomer should never have to leave the screen to learn a
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

Desktop, mouse-first. Keyboard accelerators exist and are named where
the control is — in its tooltip, or as the key badge on an Editor tool
bar button — so there is no hidden-shortcut layer a newcomer must
discover:

- Number keys 1–8 pick the Editor tool bar's tools, in bar order.
- Space clears the selection; Delete removes selected tracks.
- Alt+Left opens a detail panel's parent list, and Ctrl/Cmd+W closes the
  panel. Both act on the Editor right panel's panels only.
- Enter or Space selects a focused train card. Controls are sized
for pointer precision (28px controls, 14px checkboxes, 6px resizers);
touch is out of scope.

---

## Known divergences

Where the shipped code still differs from this doc. Fix an entry when
its trigger fires — not in a drive-by patch — and delete the row in the
same change.

| What | Where | Target | Trigger |
|------|-------|--------|---------|
| Private copies of the segmented control | `.tc-seg-btn` in [train-control.css](../static/css/features/train-control.css) (also used by Operate's train panel, [next/operate/train-panel.tsx](../frontend/src/next/operate/train-panel.tsx)), `.tm-seg-btn` in [train-monitor.css](../static/css/features/train-monitor.css) | Fold onto `.switch-btn` (§3) | Next touch of either panel |
| Pre-token literals — spacing, type sizes, radii, z-index written as raw values | all of `static/css` | Compose from the §1 scale tokens (`--space-*`, `--font-size-*`, `--radius-*`, `--z-*`); consolidate off-ramp values (13px type, 5/7/9px gaps, 3px small radii) to the nearest step | When touching the declaration |
| The route conflict flash hard-codes its colour (`232, 63, 91`) | `renderer-routes.ts` | Read `--canvas-danger` from the canvas palette (§17 Rules) | Next touch of renderer-routes.ts |
| Shapes with two consumers not yet promoted — the `.rr-task-*` / `.atr-*` rows (with their `.rr-task-verb-*` chips) and the two §5C field clusters | [routes-paths.css](../static/css/features/routes-paths.css) | A shared `.list-row*` and `.field-cluster*` family in status-blocks.css; verbs as `.chip-state` (§7, §12) | Next touch of either consumer |
| Dense-row status dots carry hue only | `.rr-task-status` / `.atr-*` in [routes-paths.css](../static/css/features/routes-paths.css) | Pair each dot with its glyph (§12) | With the `.list-row*` promotion |
| The top bar's Reset is the accent CTA | [next/top-bar.tsx](../frontend/src/next/top-bar.tsx) | The warning button (Composing the top bar) | Next top-bar touch |
| Fixed-dark code surface in a panel — JSON viewer well + neon-green text | `.json-viewer-content` / `.json-content-view` (`#0d1020` / `#00ff88`) in [routes-paths.css](../static/css/features/routes-paths.css) | Either tokenize to surface tokens, or formally bless as a constant "code block" surface like the canvas (§1 Themes) | Next View-Full-JSON / routes-paths touch |
