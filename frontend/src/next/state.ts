/**
 * The server state the layout reads, as Preact signals.
 *
 * A signal is a value that re-draws only the components that read it when it
 * changes. `App.refreshNetwork` hands each network snapshot to
 * `updateFromNetwork`, so this is the one place the new layout gets session
 * state from; no component runs its own timer for it.
 *
 * See docs/ui-layout-rewrite-plan.md §10.
 */
import { signal } from '@preact/signals';

import type { SessionStateView } from '../lib/session-controls';

/** The four screens picked in the top bar (plan §2). */
export type Screen = 'editor' | 'hardware' | 'operate' | 'logs';

/** The session state (design_new.md "Session state"); null before the first snapshot. */
export const sessionState = signal<SessionStateView | null>(null);

/** The active operating environment: `virtual_hardware` or `real_hardware`. */
export const operationEnvironment = signal<string | null>(null);

/** The screen the top bar's tabs show as picked. */
export const screen = signal<Screen>('editor');

/** Whether the layout has no track yet. Null before the first snapshot, so
 *  nothing that waits for an empty layout flashes up while one loads. */
export const layoutEmpty = signal<boolean | null>(null);

/** Keep the picked tab in step when the editor mode changes some other way:
 *  Editor mode is the Editor tab, and Operate mode is the Operate tab unless
 *  Logs (which also runs in Operate mode) is already picked. */
export function followEditorMode(mode: 'edit' | 'sim'): void {
  if (mode === 'edit') screen.value = 'editor';
  else if (screen.peek() === 'editor') screen.value = 'operate';
}

function sameSession(a: SessionStateView | null, b: SessionStateView | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.value === b.value && a.halt_reason === b.halt_reason;
}

/**
 * Copy what the new layout reads out of a network snapshot. A signal is
 * written only when its value changed, so a poll that changes nothing
 * re-draws nothing.
 */
export function updateFromNetwork(network: any): void {
  const session: SessionStateView | null = network?.session_state ?? null;
  if (!sameSession(sessionState.peek(), session)) sessionState.value = session;
  const env: string | null = network?.operation_environment ?? null;
  if (operationEnvironment.peek() !== env) operationEnvironment.value = env;
  const empty = Array.isArray(network?.tracks) ? network.tracks.length === 0 : null;
  if (layoutEmpty.peek() !== empty) layoutEmpty.value = empty;
}
