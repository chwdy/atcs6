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
| `--text-primary` | `#e1e1e6` | Values, titles |
| `--text-secondary` | `#a8a8b3` | Labels, captions, muted prose |

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
| `--control-height-xs` | `28px` | All toolbar buttons, segments, icon buttons |
| `--control-height-sm` | `32px` | Form inputs, larger buttons |
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
│ Toolbar    (border-bottom)          │  optional secondary control row
├─────────────────────────────────────┤
│ Body       (--bg-panel, scroll)     │  the main content
├─────────────────────────────────────┤
│ Footer     (border-top, --bg-dark)  │  status banners / metadata
└─────────────────────────────────────┘
```

- The **header** is `--bg-hover` with `padding: 8px 10px`, `border-bottom: 1px solid var(--border-color)`. Title is 12px Inter weight 600, letter-spacing `0.02em`.
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

### Segmented control

A row of mutually-exclusive buttons inside a single `var(--bg-dark)` container with hairline `var(--border-color)` dividers between buttons. Active button: `color: var(--accent-hover)`, `background-color: rgba(130, 87, 229, 0.18)`. Hover: same accent color, `var(--bg-hover)` background.

### Don't use solid-color filled buttons in panels.

Reserve filled `var(--accent-color)` buttons for top-level page CTAs, not panel toolbars. The ghost + accent-tint pattern keeps panels visually quiet.

---

## 4. Field rows

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

- Label: 10px UPPERCASE, `letter-spacing: 0.06em`, `--text-secondary`
- Value: mono, 12px, weight 600, `--text-primary` by default
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

## 5. Status accents

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

## 6. Pill counts and status chips

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

## 7. Resizers and drag affordances

- **Column resizer**: 6px wide, `background-color: transparent`, hover/active
  → `var(--accent-color)`.
- **Row resizer / drag handle**: 6px tall version of the same.
- Cursor: `ew-resize` for column, `ns-resize` for row, `move` for whole-panel drag.

---

## 8. Numeric displays

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

## 9. Empty / loading / error states

- **Empty**: `padding: 16px 12px; text-align: center; color: var(--text-secondary); font-style: italic; font-size: 11px;` ("No elements ahead", "No path assigned", etc.)
- **Loading**: same shape, replace italic with subdued spinner glyph if needed.
- **Error in a row**: `border-left: 3px solid var(--danger-color)` + `background-color: rgba(232, 63, 91, 0.08)` + value text in `var(--danger-color)`.

---

## 10. Don'ts

- **No emojis in chrome.** Single Unicode glyphs (`▣ ⇋ 🚦 🚉` for content, `↻ ⊞ ⊟ ×` for chrome) are OK. Don't sprinkle 📋 / 📊 / 🚨 across buttons — they read as noise.
- **No new colors.** If you can't reach for a token, the design doesn't have a slot for it yet — pause and discuss instead of inventing.
- **No solid filled buttons in panels.** Use the ghost + accent-tint pattern.
- **No nested panels.** A panel inside a panel inside a panel is a sign the structure is wrong.
- **No multi-paragraph banners.** A footer banner is a single line of diagnostic text. Anything longer belongs in a tooltip or a disclosure.
- **No `box-shadow` for emphasis.** Use border + background tint. The only shadow tokens are the panel drop-shadows (`0 10px 30px rgba(0,0,0,0.45)` for floating, `0 -6px 20px rgba(0,0,0,0.35)` for bottom-anchored).

---

## 11. Reference panels

When in doubt, copy the structure of one of these:

- **Floating debug panel** → [inspector.css](../static/css/features/inspector.css) — header + toolbar + scrolling body + footer, draggable, resizable.
- **Bottom dock** → [train-monitor.css](../static/css/features/train-monitor.css) — header + multi-column body + status footer, full-width, fixed height.
- **Sidebar form** → [train-control.css](../static/css/features/train-control.css) — sectioned vertical stack inside a secondary sidebar.
