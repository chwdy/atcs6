/**
 * The Editor screen's own state, as signals (see next/state.ts for why
 * signals). Fed by the hooks mountEditorScreen installs on today's editor.
 */
import { signal } from '@preact/signals';

/** The editor's active tool id (Editor.selectedTool), e.g. `inspect`, `track`. */
export const activeToolId = signal<string>('inspect');

/** Whether the right panel shows a selection (today's panel is open). */
export const panelOpen = signal<boolean>(false);

/** Whether the right panel is popped out into a floating window. */
export const panelFloating = signal<boolean>(false);
