/**
 * The pure parts of the Logs panel's Diagnostics tab: the shape of
 * `GET /api/inspector/entity/train/<id>`, value formatting, sorting, and the
 * keys that name each row and each folded part of a value.
 *
 * Copied from today's panels/diagnostics.ts, where the same functions sit
 * inside the window's closure and cannot be imported or tested. That file is
 * deleted with the other old windows in plan phase 8.
 */

/** One property the inspector reports. Read defensively: the panel only reads
 *  these members and tolerates any of them missing. */
export interface InspectorItem {
  name: string;
  value: unknown;
  error?: string | null;
  source?: string;
  computed_from?: string[];
  formula_doc?: string;
  writer?: string;
  consumer?: string;
  type?: string;
}

export interface InspectorGroup {
  name: string;
  items?: InspectorItem[];
}

export interface InspectorSnapshot {
  id?: string;
  label?: string;
  fetched_at?: number;
  groups?: InspectorGroup[];
}

export type SortMode = 'asc' | 'desc' | null;

export const REFRESH_OPTIONS = [
  { label: '4 Hz', hz: 4 },
  { label: '1 Hz', hz: 1 },
  { label: 'Every 2s', hz: 0.5 },
  { label: 'Every 5s', hz: 0.2 },
  { label: 'On-demand', hz: 0 },
];

export function fmtValue(value: unknown): string {
  if (value === null || value === undefined) return String(value);
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** A plain value cut to 80 characters for its row; the full value goes in the
 *  tooltip. */
export function shortValue(value: unknown): string {
  const full = fmtValue(value);
  return full.length <= 80 ? full : full.slice(0, 78) + '…';
}

/** An object or array: drawn as a tree that folds, not as one line. */
export function isContainer(value: unknown): value is object {
  return value !== null && typeof value === 'object';
}

/** The one-line stand-in for a folded object or array: `[3]`, `{5} a, b, c, …`. */
export function containerSummary(value: object): string {
  if (Array.isArray(value)) return `[${value.length}]`;
  const keys = Object.keys(value);
  if (keys.length === 0) return '{}';
  const preview = keys.slice(0, 3).join(', ');
  return `{${keys.length}} ${preview}${keys.length > 3 ? ', …' : ''}`;
}

/** Names one row across reads: its group and its property name. */
export function rowKey(group: InspectorGroup, item: InspectorItem): string {
  return `${group.name}::${item.name}`;
}

/** Every row of a snapshot by its row key, for comparing against the next read. */
export function indexSnapshot(snapshot: InspectorSnapshot | null): Map<string, InspectorItem> {
  const out = new Map<string, InspectorItem>();
  for (const group of snapshot?.groups ?? []) {
    for (const item of group.items ?? []) out.set(rowKey(group, item), item);
  }
  return out;
}

/** Whether a row's value differs from the same row in the previous read. A row
 *  that reports an error, or that was not in the previous read, never counts. */
export function valueChanged(item: InspectorItem, previous: InspectorItem | undefined): boolean {
  if (previous === undefined) return false;
  if (item.error !== undefined && item.error !== null) return false;
  return JSON.stringify(previous.value) !== JSON.stringify(item.value);
}

/** Rows A→Z or Z→A by name, or in the order the server sent (null). */
export function sortItems(items: InspectorItem[], mode: SortMode): InspectorItem[] {
  if (!mode) return items;
  const sorted = items.slice().sort((a, b) => {
    const an = String(a.name || '').toLowerCase();
    const bn = String(b.name || '').toLowerCase();
    return an < bn ? -1 : an > bn ? 1 : 0;
  });
  return mode === 'desc' ? sorted.reverse() : sorted;
}

/** The sort button cycles A→Z, Z→A, the server's order. */
export function nextSortMode(mode: SortMode): SortMode {
  return mode === null ? 'asc' : mode === 'asc' ? 'desc' : null;
}

/** The key of one object or array inside a row's value, so its folded or
 *  unfolded state survives the next read. `path` is `root`, `root.a`,
 *  `root.a[2]`, and so on. */
export function pathKey(row: string, path: string): string {
  return `${row}::${path}`;
}

/** The keys of every object and array inside `value`, outermost first. */
export function containerPaths(value: unknown, row: string, path = 'root'): string[] {
  if (!isContainer(value)) return [];
  const out = [pathKey(row, path)];
  if (Array.isArray(value)) {
    value.forEach((child, i) => out.push(...containerPaths(child, row, `${path}[${i}]`)));
  } else {
    for (const [k, child] of Object.entries(value)) out.push(...containerPaths(child, row, `${path}.${k}`));
  }
  return out;
}

/** What the server says about where a property comes from, for its tooltip. */
export function itemMeta(item: InspectorItem): string {
  const parts: string[] = [];
  if (item.computed_from) parts.push('computed_from: ' + item.computed_from.join(', '));
  if (item.formula_doc) parts.push(item.formula_doc);
  if (item.writer) parts.push('writer: ' + item.writer);
  if (item.consumer) parts.push('consumer: ' + item.consumer);
  if (item.type) parts.push('type: ' + item.type);
  return parts.join('\n');
}
