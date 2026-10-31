/**
 * What the Logs panel remembers (docs/ui-layout-rewrite-plan.md §8): whether
 * it is open, its height, the last tab, and each tab's own settings (refresh
 * rate, filters). All of it is a per-viewer convenience kept in localStorage
 * under one key. Every read and write is wrapped in try/catch: storage can be
 * missing or refuse writes (private window, blocked site data), and the panel
 * must still work, just without remembering.
 */
import { signal } from '@preact/signals';

/** The panel's tabs, in strip order. */
export type LogsTab = 'events' | 'detectors' | 'diagnostics';

export const LOGS_TABS: { id: LogsTab; label: string; icon: string }[] = [
  { id: 'events', label: 'Event log', icon: 'ti ti-list-search' },
  { id: 'detectors', label: 'Detectors', icon: 'ti ti-activity-heartbeat' },
  { id: 'diagnostics', label: 'Diagnostics', icon: 'ti ti-microscope' },
];

export const DEFAULT_HEIGHT = 280;
export const MIN_HEIGHT = 140;
/** The panel never takes more than this share of the window, so the canvas
 *  and the screen above it always keep some room. */
export const MAX_HEIGHT_SHARE = 0.7;

const STORAGE_KEY = 'atcs6.nextLogsPanel.v1';

/** Each tab's own settings. Every field is optional: a missing or malformed
 *  one means "use the tab's default". */
export interface LogsPrefs {
  open?: boolean;
  height?: number;
  tab?: LogsTab;
  events?: { refreshHz?: number; level?: string; persisted?: boolean };
  detectors?: { refreshHz?: number; filter?: string };
  diagnostics?: {
    trainId?: string | null;
    refreshHz?: number;
    sortMode?: 'asc' | 'desc' | null;
    collapsedGroups?: string[];
  };
}

type Storage = Pick<globalThis.Storage, 'getItem' | 'setItem'>;

function defaultStorage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage;
  } catch {
    return null;
  }
}

export function readPrefs(storage: Storage | null = defaultStorage()): LogsPrefs {
  try {
    const raw = storage?.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** Merge `patch` into what is stored. A tab's settings are merged field by
 *  field, so saving one setting keeps the others. */
export function writePrefs(patch: LogsPrefs, storage: Storage | null = defaultStorage()): void {
  try {
    if (!storage) return;
    const current = readPrefs(storage);
    const next: LogsPrefs = { ...current, ...patch };
    for (const key of ['events', 'detectors', 'diagnostics'] as const) {
      if (patch[key]) next[key] = { ...current[key], ...patch[key] } as any;
    }
    storage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage refused the write; the panel keeps working without remembering.
  }
}

/** Keep a height between MIN_HEIGHT and MAX_HEIGHT_SHARE of the window. */
export function clampHeight(height: number, windowHeight: number): number {
  const max = Math.max(MIN_HEIGHT, Math.round(windowHeight * MAX_HEIGHT_SHARE));
  if (!Number.isFinite(height)) return Math.min(DEFAULT_HEIGHT, max);
  return Math.round(Math.min(max, Math.max(MIN_HEIGHT, height)));
}

function isTab(value: unknown): value is LogsTab {
  return LOGS_TABS.some((t) => t.id === value);
}

const saved = readPrefs();

/** Whether the panel is open. */
export const logsOpen = signal<boolean>(saved.open === true);
/** The tab on show while the panel is open. */
export const logsTab = signal<LogsTab>(isTab(saved.tab) ? saved.tab : 'events');
/** The panel's height in CSS pixels (clamped to the window when drawn). */
export const logsHeight = signal<number>(
  typeof saved.height === 'number' && Number.isFinite(saved.height) ? saved.height : DEFAULT_HEIGHT
);

export function toggleLogs(): void {
  logsOpen.value = !logsOpen.peek();
  writePrefs({ open: logsOpen.peek() });
}

export function closeLogs(): void {
  logsOpen.value = false;
  writePrefs({ open: false });
}

export function showLogsTab(tab: LogsTab): void {
  logsTab.value = tab;
  writePrefs({ tab });
}

export function setLogsHeight(height: number, save = true): void {
  logsHeight.value = height;
  if (save) writePrefs({ height });
}
