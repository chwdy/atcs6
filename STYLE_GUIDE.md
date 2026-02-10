# Frontend Style Guide

**Version:** 1.3  
**Last Updated:** 2026-02-10

This guide documents the design system for the railway control system frontend. Use these CSS classes instead of inline styles to maintain consistency across the application.

---

## Design Principles

1. **Consistency**: All similar UI elements should look identical
2. **Semantic naming**: Class names describe purpose, not appearance
3. **Reusability**: Define once in CSS, use everywhere
4. **Maintainability**: Change styles in one place, updates everywhere
5. **No inline styles**: Use CSS classes instead (except for dynamic values)

---

## Typography Scale

Use these classes for font sizes to maintain clear visual hierarchy:

| Class | Size | Usage |
|-------|------|-------|
| `.text-2xl` | 24px | Major values (track bits display, important numbers) |
| `.text-xl` | 16px | Panel titles, main headings |
| `.text-lg` | 14px | Section headers, subheadings |
| `.text-base` | 13px | Default text, property values |
| `.text-sm` | 12px | Labels, metadata, secondary info |
| `.text-xs` | 11px | Helper text, small labels |
| `.text-xxs` | 10px | Badges, micro UI elements |
| `.text-tiny` | 9px | Ultra-small text (use sparingly) |

**Example:**
```html
<div class="text-xl">Track Properties</div>
<label class="text-sm">Track Bits</label>
<div class="text-2xl">42</div>
<p class="text-xs">Click to edit</p>
```

---

## Info Display Scale

Use the info display classes for non-editable information hierarchy across panels.

| Class | Intent | Typical size |
|-------|--------|--------------|
| `.info-display-large` | Primary operational info | label 12px, value 14px |
| `.info-display-medium` | Header-level metadata | label 11px, value 13px |
| `.info-display-small` | Secondary operational info | label 10px, value 12px |
| `.info-display-compact` | Dense debug/diagnostics | label/value/meta 10px |

Supporting classes:

| Class | Purpose |
|-------|---------|
| `.info-label` | Semantic label text (uppercase metadata label) |
| `.info-value` | Semantic value text (mono display value) |
| `.info-meta` | Secondary explanation text under a value |
| `.info-display-row` | Single-line label/value pair row |

Mapping for current UI:

1. **Train debug panel** → `.info-display-compact`
2. **Track compact header (`(x, y) - type`)** → `.info-display-medium`
3. **Train control ATO path/destination** → `.info-display-small`
4. **Track bits panel + train control info cards** → `.info-display-large` (and above for hero values like `.text-2xl`)

Example:
```html
<div class="info-display-small">
    <div class="info-label">Path</div>
    <div class="info-value text-info">P_MAIN_01</div>
</div>

<div class="info-display-row info-display-compact">
    <span class="info-label">Dead Ends</span>
    <span class="info-value text-warning">N, W</span>
</div>
```

---

## Color Palette (Semantic)

### Text Colors

| Class | Color | Usage |
|-------|-------|-------|
| `.text-primary` | #e1e1e6 | Main body text |
| `.text-secondary` | #a8a8b3 | Labels, secondary information |
| `.text-muted` | #888 | Dim text, metadata |
| `.text-white` | #fff | Bright white text |
| `.text-dim` | #666 | Very dim text |
| `.text-accent` | #8257e5 (purple) | Accent elements, highlights |
| `.text-success` | #04d361 (green) | Success states, confirmations |
| `.text-track` | #00ff88 (bright green) | Track-related elements |
| `.text-danger` | #e83f5b (red) | Errors, dangerous actions |
| `.text-warning` | #ffcd1e (yellow) | Warnings, caution states |
| `.text-info` | #00d4ff (cyan) | Information, neutral highlights |
| `.text-station` | #ffd93d (yellow) | Station-related elements |
| `.text-block` | #ff79c6 (pink) | Block-related elements |

**Example:**
```html
<span class="text-success">Connected</span>
<span class="text-danger">Error</span>
<span class="text-track">42 track bits</span>
```

### Background Colors

| Class | Color | Usage |
|-------|-------|-------|
| `.bg-dark` | #121214 | Darkest background |
| `.bg-panel` | #1e1e24 | Panel backgrounds |
| `.bg-hover` | #2a2a35 | Hover states, section backgrounds |
| `.bg-input` | #0d1020 | Input field backgrounds |
| `.bg-section` | #1a1a2e | Section container backgrounds |
| `.bg-track` | #2a3a2e | Track-related info boxes |
| `.bg-signal` | #2a2a3e | Signal-related info boxes |
| `.bg-warning` | #3a2a1a | Warning box backgrounds |

---

## Component Classes

### Headers

```html
<!-- Panel title (16px, bold) -->
<h2 class="panel-title">Properties</h2>

<!-- Section header (14px, bold, with background) -->
<div class="section-header">Track Information</div>

<!-- Compact header (medium info display) -->
<div class="compact-header info-display-medium">
    <span>(0, 0) - Track</span>
    <span class="badge badge-new">NEW</span>
</div>
```

### Property Displays

```html
<!-- Property item container -->
<div class="property-item">
    <label class="property-label">Track Bits</label>
    <div class="property-value">42</div>
</div>

<!-- Read-only value display -->
<div class="value-display value-display-mono text-track">
    (5, 10)
</div>

<!-- Editable input -->
<input type="text" class="property-input" placeholder="Enter name...">
```

### Info Boxes

```html
<!-- Track-related info (green tint) -->
<div class="info-box-track">
    <label class="text-track font-bold">Track Bits</label>
    <div>Content here</div>
</div>

<!-- Signal-related info (purple tint) -->
<div class="info-box-signal">
    <label class="text-info font-bold">Switch State</label>
    <div>Content here</div>
</div>

<!-- Warning box (orange tint, left border) -->
<div class="info-box-warning text-warning">
    ⚠ Warning message
</div>
```

### Badges

```html
<span class="badge badge-new">NEW</span>
<span class="badge badge-success">ACTIVE</span>
<span class="badge badge-danger">ERROR</span>
<span class="badge badge-warning">CAUTION</span>
```

### Status Displays

```html
<!-- Success status (green) -->
<div class="status-display status-display-success">
    ✓ Connected
</div>

<!-- Danger status (red) -->
<div class="status-display status-display-danger">
    ⚠ Disconnected
</div>

<!-- Warning status (yellow) -->
<div class="status-display status-display-warning">
    WAITING
</div>

<!-- Info status (purple) -->
<div class="status-display status-display-info">
    STOPPED
</div>
```

---

## Secondary Panel Standards

Use these rules for all editor panels (routes, paths, trains, blocks, signals, switches):

1. **Panel shell first**: group content into `.control-section` blocks with consistent spacing.
2. **Section rhythm**: header + content + actions (avoid random per-section spacing).
3. **Compact controls**: use `.btn-sm` / `.btn-xs` for mouse-driven tool actions.
4. **Dense metadata rows**: prefer 2-column grids (`.grid .grid-cols-2`) over long single-column stacks.
5. **No static inline styles**: use CSS classes for spacing, font size, borders, and alignment.
6. **Dynamic inline style allowed only for state values** via CSS variables, for example:

```html
<div class="train-status-dot" style="--state-color: #04d361;"></div>
```

Recommended panel classes:

| Purpose | Class |
|--------|-------|
| Section container | `.control-section` |
| Section divider | `.control-section-divider` |
| Section title | `.control-section-header` |
| Two-column properties | `.property-grid` + `.grid-cols-2` |
| Compact icon/action buttons | `.btn-xs` + `.btn-icon-only` |
| Empty state card | `.routes-empty-state` / `.route-empty-dashed` |

---

## Utility Classes

### Spacing

```html
<!-- Margin bottom -->
<div class="mb-4">4px bottom margin</div>
<div class="mb-8">8px bottom margin</div>
<div class="mb-12">12px bottom margin</div>
<div class="mb-16">16px bottom margin</div>
<div class="mb-20">20px bottom margin</div>

<!-- Margin top -->
<div class="mt-4">4px top margin</div>
<div class="mt-8">8px top margin</div>

<!-- Padding -->
<div class="p-4">4px padding all sides</div>
<div class="p-8">8px padding</div>
<div class="p-12">12px padding</div>
```

### Flexbox

```html
<!-- Flex container -->
<div class="flex items-center gap-8">
    <span>Item 1</span>
    <span>Item 2</span>
</div>

<!-- Flex column -->
<div class="flex flex-col gap-4">
    <div>Row 1</div>
    <div>Row 2</div>
</div>

<!-- Space between -->
<div class="flex justify-between">
    <span>Left</span>
    <span>Right</span>
</div>
```

### Font Weight

```html
<span class="font-bold">Bold text</span>
<span class="font-semibold">Semi-bold text</span>
<span class="font-normal">Normal weight</span>
```

### Borders

```html
<div class="border-success">2px green border</div>
<div class="border-warning">2px yellow border</div>
<div class="border-l-danger">3px left red border</div>
<div class="border-track">1px bright green border</div>
```

### Border Radius

```html
<div class="rounded">4px border radius</div>
<div class="rounded-sm">3px border radius</div>
<div class="rounded-lg">6px border radius</div>
```

### Display & Width

```html
<div class="block">Block display</div>
<span class="inline-block">Inline-block</span>
<div class="w-full">100% width</div>
<div class="text-center">Centered text</div>
```

---

## Common Patterns

### Property Item with Editable Input

```html
<div class="property-item">
    <label class="property-label">Station Name</label>
    <input type="text" id="station-name" class="property-input" 
           placeholder="Enter name...">
</div>
```

### Property Item with Read-Only Value

```html
<div class="property-item">
    <label class="property-label">Position</label>
    <div class="value-display value-display-mono text-info">
        (5, 10)
    </div>
</div>
```

### Status Indicator

```html
<div class="property-item">
    <label class="property-label">Status</label>
    <div class="status-display status-display-success">
        ✓ RUNNING
    </div>
</div>
```

### Action Buttons

```html
<div class="flex gap-10 mt-20">
    <button class="btn-primary text-lg font-bold w-full">
        Save
    </button>
    <button class="btn-danger text-lg font-bold">
        Delete
    </button>
</div>
```

### Button Size Scale

Use this size scale for consistent control density across panels:

| Class | Size | Height | Padding | Border | Typical use |
|-------|------|--------|---------|--------|-------------|
| `.btn-size-tight` | Tiny | 18px | 0 | None | Tab labels, tiny chip-like controls |
| `.btn-size-sm` | Small | Auto | 2px 8px | 1px | Secondary actions (e.g. open monitor) |
| `.btn-size-md` | Medium | Auto | 4px 12px | None* | Main control actions, editor save/apply |
| `.btn-size-lg` | Large | 40px | 0 16px | 1px | High-priority actions (emergency controls) |

**Border Rule:** Medium buttons have no border by default, **except** `.btn-outline` variants which keep their border for visibility on dark backgrounds.

**Usage Patterns:**

```html
<!-- Large button (emergency, primary actions) -->
<button class="btn btn-danger btn-size-lg">🚨 EMERGENCY STOP</button>

<!-- Medium button (main actions, no border) -->
<button class="btn btn-primary btn-size-md">Save</button>
<button class="btn btn-success btn-size-md">▶ Go</button>

<!-- Medium outline button (keeps border) -->
<button class="btn btn-outline btn-size-md">ATO</button>
<button class="btn btn-outline btn-size-md">Cab A</button>

<!-- Small button (secondary actions) -->
<button class="btn btn-info btn-size-sm">📊 Open Monitor</button>

<!-- Tight button (tabs, chips) -->
<button class="btn btn-secondary btn-size-tight">Tab 1</button>
```

**Panel Mapping:**
- **Header navigation**: `.btn-size-tight` (tabs)
- **Secondary actions** (open monitor, refresh): `.btn-size-sm`
- **Main controls** (power, cab, mode, go/stop): `.btn-size-md` (solid = no border, outline = with border)
- **Emergency controls**: `.btn-size-lg`
- **Editor actions** (save, delete, apply): `.btn-size-md`

### Info Box with Header

```html
<!-- Track-related info (green tint) -->
<div class="info-box-track">
    <label class="text-track font-bold block mb-4">Track Bits</label>
    <div class="text-2xl font-bold text-track">42</div>
    <div class="text-muted text-sm">6 segments</div>
</div>
```

---

## Input Box Size Scale

Consistent input sizing creates visual rhythm in forms:

| Class | Size | Height | Padding | Font | Typical use |
|-------|------|--------|---------|------|-------------|
| `.input-tiny` | Tiny | 24px | 4px 8px | 11px | Compact numeric inputs, IDs |
| `.input-small` | Small | 28px | 6px 10px | 12px | Short text fields, coordinates |
| `.property-input` | Medium (default) | 32px | 8px 12px | 13px | Standard form inputs |
| `.input-large` | Large | 40px | 10px 16px | 14px | Primary fields (train names) |
| `.input-full` | Full width | - | - | - | Block-level inputs |

**Examples:**

```html
<!-- Large input (prominent field) -->
<div class="input-large-container">
    <label class="input-large-label">Train Name</label>
    <input type="text" class="input-large-field" value="T-001" readonly>
</div>

<!-- Medium input (standard) -->
<div class="property-item">
    <label class="property-label">Station Name</label>
    <input type="text" class="property-input" placeholder="Enter name...">
</div>

<!-- Small input (compact) -->
<div class="input-small-row">
    <label class="input-small-label">Radius (cm)</label>
    <input type="number" class="input-small-field" value="100">
</div>

<!-- Tiny input (minimal) -->
<input type="text" class="property-input input-tiny" value="42">
```

---

## Info Display Scale

Use the info display classes to create clear information hierarchy across panels. These define the relationship between labels, values, and metadata text.

### Display Size Classes

| Class | Label Size | Value Size | Meta Size | Use Case |
|-------|------------|------------|-----------|----------|
| `.info-display-large` | 12px | 14px | 13px | Primary operational info cards |
| `.info-display-medium` | 11px | 13px | 12px | Header metadata, coordinates |
| `.info-display-small` | 10px | 12px | 11px | Secondary operational info |
| `.info-display-compact` | 10px | 10px | 10px | Dense debug/diagnostics |

### Supporting Classes

| Class | Purpose |
|-------|---------|
| `.info-label` | Semantic label (uppercase, muted color) |
| `.info-value` | Semantic value (mono font, primary color) |
| `.info-meta` | Secondary explanation text |
| `.info-display-row` | Flex row for label/value pairs |

### Usage Mapping

```html
<!-- Large: Main info cards (train status, track bits) -->
<div class="info-box-track info-display-large">
    <div class="flex justify-between items-center mb-4">
        <span class="info-label text-muted">State</span>
        <span class="badge badge-warning text-xxs">Block B-12</span>
    </div>
    <div class="text-2xl font-bold train-state-display">RUNNING</div>
    <div class="flex justify-between gap-6 mt-4">
        <div class="flex flex-col items-center text-center flex-1">
            <span class="info-label text-muted">Position</span>
            <span class="info-value text-info">(5, 10)</span>
        </div>
        <div class="flex flex-col items-center text-center flex-1">
            <span class="info-label text-muted">Direction</span>
            <span class="info-value text-station">N</span>
        </div>
        <div class="flex flex-col items-center text-center flex-1">
            <span class="info-label text-muted">Speed</span>
            <span class="info-value text-track">45.5 km/h</span>
        </div>
    </div>
</div>

<!-- Medium: Header metadata (coordinates, type) -->
<div class="compact-header info-display-medium">
    <span>(0, 0) - Straight Track</span>
    <span class="badge badge-new">NEW</span>
</div>

<!-- Small: Secondary info (ATO path/destination) -->
<div class="info-display-small">
    <div class="info-label text-muted mb-2">Path</div>
    <div class="info-value text-info">P_MAIN_01</div>
    <div class="info-label text-muted mt-4 mb-2">Destination</div>
    <div class="info-value text-station">Station A - Platform 1</div>
</div>

<!-- Compact: Debug info rows -->
<div class="info-display-row info-display-compact">
    <span class="info-label">Dead Ends</span>
    <span class="info-value text-warning">N, W</span>
</div>

<div class="train-debug-panel info-display-compact">
    <div class="train-debug-row">
        <span class="train-debug-label">Train ID:</span>
        <span class="train-debug-value">T-001</span>
    </div>
    <div class="train-debug-row">
        <span class="train-debug-label">Block:</span>
        <span class="train-debug-value text-block">B-05</span>
    </div>
</div>
```

### Context-Specific Patterns

**Train Control Panel:**
- Main info card (state, position, direction, speed): `.info-display-large`
- Onboard computer status (power, mode, handle): `.info-display-large`
- ATO path/destination: `.info-display-small`
- Debug panel: `.info-display-compact`

**Track Properties Panel:**
- Header (coordinates, type): `.info-display-medium`
- Track bits display: `.info-display-large` (with `.text-2xl` for the value)
- Switch/signal metadata: `.info-display-small`
- Dead ends: `.info-display-compact`

**Route/Path Panels:**
- Status cards: `.info-display-large`
- Route statistics: `.info-display-small`
- Block list items: `.info-display-compact`

---

## When to Use Inline Styles

**Use inline styles ONLY for:**

1. **Dynamic values** that change based on data:
   ```html
   <div style="border-color: ${blockColor};">...</div>
   <span style="color: ${stateColor};">${status}</span>
   ```

2. **Conditional backgrounds** with complex logic:
   ```html
   <div style="background: ${isActive ? '#1a3a2a' : '#3a3a1a'};">
   ```

3. **Layout flexbox values**:
   ```html
   <div style="flex: 0 0 33%;">...</div>
   ```

**DO NOT use inline styles for:**
- Fixed colors (use semantic classes)
- Fixed font sizes (use typography scale)
- Padding/margin (use utility classes)
- Border radius (use utility classes)
- Display properties (use utility classes)

---

## Migration Checklist

When adding new UI elements:

- [ ] Use typography classes (`.text-*`) instead of `font-size: *px`
- [ ] Use semantic color classes (`.text-success`) instead of `color: #04d361`
- [ ] Use component classes (`.property-item`) instead of custom divs
- [ ] Use utility classes (`.flex`, `.mb-8`) instead of inline styles
- [ ] Only use inline styles for dynamic values
- [ ] Test visual appearance matches design system

---

## Examples from Codebase

### Before (inline styles):
```html
<div style="margin-bottom: 16px;">
    <label style="display: block; font-size: 12px; color: #aaa; margin-bottom: 8px;">
        Station Name
    </label>
    <input type="text" id="name" 
           style="width: 100%; padding: 10px 12px; background: #0d1020; 
                  border: 1px solid #3a3a4e; color: #fff; 
                  border-radius: 6px; font-size: 13px;">
</div>
```

### After (CSS classes):
```html
<div class="property-item">
    <label class="property-label">Station Name</label>
    <input type="text" id="name" class="property-input">
</div>
```

**Benefits:**
- 70% less code
- Consistent styling
- Easy to change globally
- Self-documenting (class names describe purpose)

---

## CSS Variables Reference

The following CSS variables are available in `:root`:

```css
--bg-dark: #121214;
--bg-panel: #1e1e24;
--bg-hover: #2a2a35;
--border-color: #333340;
--text-primary: #e1e1e6;
--text-secondary: #a8a8b3;
--accent-color: #8257e5;
--accent-hover: #9466ff;
--danger-color: #e83f5b;
--success-color: #04d361;
--warning-color: #ffcd1e;
--font-main: 'Inter', sans-serif;
--font-mono: 'JetBrains Mono', monospace;
```

Use these in custom inline styles when needed:
```html
<div style="background: var(--bg-hover);">...</div>
```

---

## Questions?

If you're unsure which class to use, start with `static/css/style.css` (entrypoint) and then check the imported modules in `static/css/base/`, `static/css/layout/`, `static/css/components/`, and `static/css/features/`. Look for similar existing UI elements in the codebase and follow the same pattern.

For new patterns not covered by this guide, add them to the appropriate modular stylesheet and keep `style.css` as the import entrypoint. Document new patterns here.
