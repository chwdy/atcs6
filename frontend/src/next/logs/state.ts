/**
 * What the Logs screen (docs/ui-layout-rewrite-plan.md §8) shows and
 * remembers: the tab on show, the block a click on the canvas picked, and
 * each tab's own settings (refresh rate, filters). The settings
 * are a per-viewer convenience kept in localStorage under one key. Every read
 * and write is wrapped in try/catch: storage can be missing or refuse writes
 * (private window, blocked site data), and the screen must still work, just
 * without remembering.
 */
import { signal } from '@preact/signals';

/** The screen's tabs, in strip order. */
export type LogsTab = 'events' | 'detectors';

export const LOGS_TABS: { id: LogsTab; label: string; icon: string }[] = [
  { id: 'events', label: 'Event log', icon: 'ti ti-list-search' },
  { id: 'detectors', label: 'Detectors', icon: 'ti ti-activity-heartbeat' },
];

// The name is from when Logs was a bottom panel; it is kept so the settings
// saved then still apply.
const STORAGE_KEY = 'atcs6.nextLogsPanel.v1';

/** Each tab's own settings. Every field is optional: a missing or malformed
 *  one means "use the tab's default". */
export interface LogsPrefs {
  tab?: LogsTab;
  events?: { refreshHz?: number; level?: string; persisted?: boolean };
  detectors?: { refreshHz?: number; filter?: string };
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
    for (const key of ['events', 'detectors'] as const) {
      if (patch[key]) next[key] = { ...current[key], ...patch[key] } as any;
    }
    storage.setItem(STORAGE_KEY, JSON.stringify(next));
  } catch {
    // Storage refused the write; the screen keeps working without remembering.
  }
}

function isTab(value: unknown): value is LogsTab {
  return LOGS_TABS.some((t) => t.id === value);
}

const saved = readPrefs();

/** The tab on show. */
export const logsTab = signal<LogsTab>(isTab(saved.tab) ? saved.tab : 'events');

/** The block a click on the canvas picked. The Detectors tab marks its row
 *  and scrolls it into view. Not saved: it means something only for the
 *  click that picked it. */
export const pickedBlockId = signal<string | null>(null);

export function showLogsTab(tab: LogsTab): void {
  logsTab.value = tab;
  writePrefs({ tab });
}

/** Open the Detectors tab with `blockId`'s row marked (a click on a block on
 *  the canvas). */
export function showBlockInLogs(blockId: string): void {
  pickedBlockId.value = blockId;
  showLogsTab('detectors');
}
