// resource-picker.ts — a small floating menu that lists labeled options at a
// screen position and fires a callback when one is chosen.
//
// Originally the route editor's waypoint resource picker (shown when a clicked
// tile carried more than one targetable resource — platform / station / block).
// Extracted here so any tool that needs a "which thing here?" choice can reuse
// the same look and behaviour. Current callers:
//   - RouteEditorHandler waypoint-add (editor-routes.ts)
//   - Editor entity-select tool (editor.ts)
//
// Only one picker is open at a time, so the open menu and its outside-click
// dismiss handler live in module state. Opening a new picker (or calling
// dismissResourcePicker) tears down any existing one first.

export interface ResourceOption {
  label: string;
  // A style hint; renders as a `resource-option-<variant>` class on the button
  // (e.g. 'all' for a "select all" row). Optional — plain options omit it.
  variant?: string;
  // Callers attach their own fields (type, id, …); the picker only reads label
  // and variant and hands the whole object back to onPick.
  [key: string]: any;
}

let activePicker: HTMLElement | null = null;
let activeDismiss: ((e: MouseEvent) => void) | null = null;

/** Remove the open picker (if any) and its outside-click listener. */
export function dismissResourcePicker(): void {
  if (activePicker) {
    activePicker.remove();
    activePicker = null;
  }
  if (activeDismiss) {
    document.removeEventListener('mousedown', activeDismiss);
    activeDismiss = null;
  }
}

/**
 * Show a floating menu of `options` at (screenX, screenY). Picking an option
 * dismisses the menu and calls `onPick(option)`. A mousedown outside the menu
 * dismisses it without picking.
 */
export function showResourcePicker(
  screenX: number,
  screenY: number,
  options: ResourceOption[],
  onPick: (opt: ResourceOption) => void
): void {
  dismissResourcePicker();
  const menu = document.createElement('div');
  menu.className = 'resource-picker';
  menu.style.left = `${screenX}px`;
  menu.style.top = `${screenY}px`;

  options.forEach((opt) => {
    const btn = document.createElement('button');
    btn.className = 'resource-option';
    if (opt.variant) btn.classList.add(`resource-option-${opt.variant}`);
    btn.textContent = opt.label;
    btn.onclick = (e) => {
      e.stopPropagation();
      dismissResourcePicker();
      onPick(opt);
    };
    menu.appendChild(btn);
  });

  document.body.appendChild(menu);
  activePicker = menu;

  activeDismiss = (e: MouseEvent) => {
    if (activePicker && !activePicker.contains(e.target as Node)) {
      dismissResourcePicker();
    }
  };
  // Defer so the click that opened the menu doesn't immediately close it.
  setTimeout(() => document.addEventListener('mousedown', activeDismiss!), 0);
}
