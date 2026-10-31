/**
 * Under ?ui=next the Logs panel's tabs replace today's Event Log, Raw
 * Occupancy and Diagnostics windows (docs/ui-layout-rewrite-plan.md §8), so
 * those windows are not built at all: no hidden window keeps polling, and
 * one left open in today's layout does not reappear over the new one.
 *
 * The same test as `isNextUI` in next/mount.tsx, kept separate so the old
 * panels can ask it without loading the new layout's components.
 */
export function logsPanelReplacesWindows(search: string = window.location.search): boolean {
  return new URLSearchParams(search).get('ui') === 'next';
}
