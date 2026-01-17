# Frontend Style Guide

**Version:** 1.0  
**Last Updated:** 2026-01-16

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

<!-- Compact header (flexbox, for inline elements) -->
<div class="compact-header">
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

### Info Box with Header

```html
<div class="info-box-track">
    <label class="text-track font-bold block mb-4">Track Bits</label>
    <div class="text-2xl font-bold text-track">42</div>
    <div class="text-muted text-sm">6 segments</div>
</div>
```

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

If you're unsure which class to use, check `static/css/style.css` for the full list of available classes and their definitions. Look for similar existing UI elements in the codebase and follow the same pattern.

For new patterns not covered by this guide, add them to the design system in `style.css` and document them here.
