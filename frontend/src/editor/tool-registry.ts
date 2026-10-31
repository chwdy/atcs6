// tool-registry.ts — the data model + renderer for the editor's tool palette.
//
// Tools are declared as data (ToolContribution[]) and the toolbar is rendered
// from that data instead of from hardcoded markup. Each contribution names the
// tool, how a click on it dispatches (its `kind`), the section it shows under,
// and the editor modes it is available in. The core editor contributes the
// layout tools; plugins (e.g. SimEditorPlugin) contribute their own — so a
// layout-only host gets a clean palette by simply not registering the sim
// plugin.

export type ToolMode = 'edit' | 'sim';

export type ToolKind =
  // A stateful build/pick tool: clicking sets the active tool and the canvas
  // handlers read `editor.selectedTool` (track, signal, block, platform, inspect…).
  | { type: 'tool' }
  // A mode tool: clicking enters an editor mode built by `factory()`
  // (selection tools, path tools).
  | { type: 'mode'; factory: () => any }
  // A panel/dialog action: clicking runs `run()` and leaves the active tool
  // unchanged (switches list, add-train dialog, route runs…).
  | { type: 'action'; run: () => void };

export interface ToolContribution {
  /** Stable id; matches the `data-tool` attribute and the canvas handler checks. */
  id: string;
  /** Button text. */
  label: string;
  /** Tabler icon class, e.g. `ti ti-plus`. */
  icon: string;
  /** Long tooltip. */
  title: string;
  /** Section header the tool shows under; null = ungrouped (the default tool). */
  group: string | null;
  /** Editor modes the tool is shown in. */
  availableIn: ToolMode[];
  /** How a click on the tool dispatches. */
  kind: ToolKind;
}

/** Icon for each group's main button (the rail collapses a group to one button). */
const GROUP_ICONS: Record<string, string> = {
  Selection: 'ti ti-marquee-2',
  Network: 'ti ti-affiliate',
  Platform: 'ti ti-building-arch',
  Block: 'ti ti-stack-2',
  Path: 'ti ti-route',
  Train: 'ti ti-train',
  Route: 'ti ti-map-2',
  Power: 'ti ti-bolt',
};

export interface RenderToolbarOptions {
  /** Tool id to mark active. */
  activeToolId: string;
  /** Only tools whose `availableIn` includes this mode are rendered. */
  mode: ToolMode;
}

/**
 * Render `contributions` into `mount` as the tool rail: a vertical list of main
 * buttons. An ungrouped tool (group === null) is a direct rail button; each
 * group becomes one main button that opens a flyout popover of its tools.
 *
 * Only tools whose `availableIn` includes `mode` are rendered, and a group left
 * with no tools is omitted entirely — so operation (sim) mode shows a clean
 * palette. Existing children are replaced, so this is safe to call repeatedly.
 *
 * Each rail button's number (its badge and its number-key shortcut) is its
 * place among every contribution in every mode, so a button keeps its number
 * when the mode hides the buttons before it — Train is 8 in both modes, and
 * the numbers simply skip the hidden ones.
 */
export function renderToolGroups(
  contributions: ToolContribution[],
  mount: HTMLElement,
  { activeToolId, mode }: RenderToolbarOptions,
): void {
  const numbers = railNumbers(contributions);

  // Bucket the mode-available tools by group, preserving first-seen order.
  const order: (string | null)[] = [];
  const byGroup = new Map<string | null, ToolContribution[]>();
  for (const c of contributions) {
    if (!c.availableIn.includes(mode)) continue;
    let bucket = byGroup.get(c.group);
    if (!bucket) {
      bucket = [];
      byGroup.set(c.group, bucket);
      order.push(c.group);
    }
    bucket.push(c);
  }

  mount.replaceChildren();
  for (const group of order) {
    const tools = byGroup.get(group)!;
    if (group === null) {
      // Ungrouped (the default Inspect tool): a direct rail button.
      for (const c of tools) {
        mount.appendChild(buildRailButton(c, activeToolId, numbers.get(railKey(c))!));
      }
      continue;
    }
    mount.appendChild(buildGroup(group, tools, activeToolId, numbers.get(railKey(tools[0]))!));
  }
}

/** The rail button a contribution sits on: its own (ungrouped) or its group's. */
export function railKey(c: ToolContribution): string {
  return c.group === null ? `tool:${c.id}` : `group:${c.group}`;
}

/**
 * Every rail button's number, keyed by `railKey`: first-seen order across all
 * contributions, whatever their modes, so a button's number does not depend on
 * which mode is showing.
 */
export function railNumbers(contributions: ToolContribution[]): Map<string, number> {
  const numbers = new Map<string, number>();
  for (const c of contributions) {
    const key = railKey(c);
    if (!numbers.has(key)) numbers.set(key, numbers.size + 1);
  }
  return numbers;
}

/** A full-width rail button for a single tool (e.g. Inspect). */
function buildRailButton(
  c: ToolContribution,
  activeToolId: string,
  num: number,
): HTMLButtonElement {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = c.id === activeToolId ? 'tool-launch active' : 'tool-launch';
  btn.dataset.tool = c.id;
  btn.dataset.railNum = String(num);
  // The visible label is hidden in the compact rail (.sidebar--compact), so the
  // name has to survive as a tooltip / accessible name.
  btn.title = c.label;
  btn.setAttribute('aria-label', c.label);
  const numEl = document.createElement('span');
  numEl.className = 'tool-launch-num';
  numEl.textContent = String(num);
  numEl.setAttribute('aria-hidden', 'true');
  const icon = document.createElement('i');
  icon.className = `${c.icon} tool-launch-icon`;
  const label = document.createElement('span');
  label.className = 'tool-launch-label';
  label.textContent = c.label;
  btn.append(numEl, icon, label);
  return btn;
}

/** A group: a main rail button plus its (hidden) flyout popover of tool tiles. */
function buildGroup(
  group: string,
  tools: ToolContribution[],
  activeToolId: string,
  num: number,
): HTMLDivElement {
  const groupEl = document.createElement('div');
  groupEl.className = 'tool-group';
  groupEl.dataset.group = group;

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'tool-launch tool-group-btn';
  trigger.dataset.railNum = String(num);
  trigger.setAttribute('aria-haspopup', 'true');
  trigger.setAttribute('aria-expanded', 'false');
  // See buildRailButton: the compact rail hides the label, so name the group
  // on the button itself.
  trigger.title = group;
  trigger.setAttribute('aria-label', group);
  if (tools.some((t) => t.id === activeToolId)) trigger.classList.add('active');

  const icon = document.createElement('i');
  icon.className = `${GROUP_ICONS[group] ?? 'ti ti-folder'} tool-launch-icon`;
  const label = document.createElement('span');
  label.className = 'tool-launch-label';
  label.textContent = group;
  const caret = document.createElement('i');
  caret.className = 'ti ti-chevron-right tool-launch-caret';
  const numEl = document.createElement('span');
  numEl.className = 'tool-launch-num';
  numEl.textContent = String(num);
  numEl.setAttribute('aria-hidden', 'true');
  trigger.append(numEl, icon, label, caret);

  const popover = document.createElement('div');
  popover.className = 'tool-popover';
  popover.setAttribute('role', 'menu');
  // The group name is carried only by aria-label (no visible header row); the
  // popover sizes itself to its tool tiles, capped at four per row.
  popover.setAttribute('aria-label', group);

  for (const c of tools) popover.appendChild(buildTile(c, activeToolId));

  groupEl.append(trigger, popover);
  return groupEl;
}

/** A compact icon+label tile for a tool inside a group popover. */
function buildTile(c: ToolContribution, activeToolId: string): HTMLButtonElement {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = c.id === activeToolId ? 'tool-btn active' : 'tool-btn';
  btn.dataset.tool = c.id;
  const icon = document.createElement('i');
  icon.className = `${c.icon} tool-icon`;
  btn.append(icon, ` ${c.label}`);
  return btn;
}
