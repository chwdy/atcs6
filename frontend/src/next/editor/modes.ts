/**
 * The two canvas tools the Editor screen adds to today's editor
 * (docs/ui-layout-rewrite-plan.md §4):
 *
 * - **Switch** (`switch-pick`): click a switch to open its panel. Today a
 *   switch is made by drawing a branch with the Track tool, and there was no
 *   tool that picks one.
 * - **Track → Remove** (`remove-track`): click a piece of track to remove it,
 *   through the same delete-with-confirmation path that Delete uses for a
 *   selection (Editor._bulkDeleteWithConfirm).
 *
 * Each is an editor mode (the object Editor.enterMode takes): the canvas
 * click handler hands every click to the active mode first. They reach the
 * editor as an EditorPlugin's tools, so ToolbarHandler.selectTool picks them
 * like any other tool.
 */
import { showToast } from '../../lib/toast';
import type { Editor, EditorPlugin } from '../../editor/editor';
import type { ToolContribution } from '../../editor/tool-registry';

interface ModeClick {
  gridPos: { x: number; y: number };
}

function setStatus(editor: Editor, text: string): void {
  const status = editor.$id('status-state');
  if (status) status.textContent = text;
}

export function buildSwitchPickMode(editor: Editor) {
  return {
    name: 'Switch',
    toolName: 'switch-pick',
    cancelMessage: 'Back to Select',
    onEnter: () => setStatus(editor, 'Switch: click a switch to edit it'),
    onClick: async ({ gridPos }: ModeClick) => {
      const tile = editor._tracksByKey().get(`${gridPos.x},${gridPos.y}`);
      if (tile?.switch_id) {
        await editor.showSwitchEditor(tile.switch_id);
      } else {
        showToast('No switch here. Draw a branch off existing track with Track to make one.', 'info', 3500);
      }
      return true;
    },
    onExit: () => setStatus(editor, 'Ready'),
  };
}

export function buildRemoveTrackMode(editor: Editor) {
  return {
    name: 'Remove track',
    toolName: 'remove-track',
    cancelMessage: 'Back to Select',
    onEnter: () => setStatus(editor, 'Remove track: click a piece of track to remove it'),
    onClick: async ({ gridPos }: ModeClick) => {
      if (!editor._tracksByKey().has(`${gridPos.x},${gridPos.y}`)) {
        showToast('No track here', 'info', 2000);
        return true;
      }
      try {
        const removed = await editor._bulkDeleteWithConfirm([[gridPos.x, gridPos.y]]);
        if (removed) await editor.refreshAndSelect(null);
      } catch (err: any) {
        showToast(`Could not remove the track: ${err?.message ?? err}`, 'error', 5000);
      }
      return true;
    },
    onExit: () => setStatus(editor, 'Ready'),
  };
}

/** The tool contributions for the two modes above. They are edit-only, and
 *  sit in their own group so today's tool rail (hidden on this screen) keeps
 *  its own buttons and numbers as they are. */
export function editorScreenTools(editor: Editor): ToolContribution[] {
  return [
    {
      id: 'switch-pick', label: 'Switch', icon: 'ti ti-arrows-split-2',
      title: 'Click a switch to edit it',
      group: 'Editor screen', availableIn: ['edit'],
      kind: { type: 'mode', factory: () => buildSwitchPickMode(editor) },
    },
    {
      id: 'remove-track', label: 'Remove track', icon: 'ti ti-eraser',
      title: 'Click a piece of track to remove it',
      group: 'Editor screen', availableIn: ['edit'],
      kind: { type: 'mode', factory: () => buildRemoveTrackMode(editor) },
    },
  ];
}

export const editorScreenPlugin: EditorPlugin = {
  register() {},
  tools: (editor) => editorScreenTools(editor),
};
