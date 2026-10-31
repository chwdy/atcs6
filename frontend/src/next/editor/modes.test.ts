// @vitest-environment happy-dom
import { describe, expect, it, vi } from 'vitest';

import { ToolbarHandler } from '../../editor/editor-toolbar';
import { buildRemoveTrackMode, buildSwitchPickMode, editorScreenTools } from './modes';

function fakeEditor(tiles: Record<string, any>) {
  return {
    _tracksByKey: () => new Map(Object.entries(tiles)),
    $id: () => null,
    showSwitchEditor: vi.fn(),
    _bulkDeleteWithConfirm: vi.fn(async () => ({ removed: 1 })),
    refreshAndSelect: vi.fn(async () => {}),
  } as any;
}

describe('Switch tool', () => {
  it('opens the switch clicked', async () => {
    const ed = fakeEditor({ '7,5': { switch_id: 'SW_7_5' } });
    await buildSwitchPickMode(ed).onClick({ gridPos: { x: 7, y: 5 } });
    expect(ed.showSwitchEditor).toHaveBeenCalledWith('SW_7_5');
  });

  it('says how to make a switch when the tile has none', async () => {
    const ed = fakeEditor({ '3,5': { segment_id: 'SEG_1' } });
    expect(await buildSwitchPickMode(ed).onClick({ gridPos: { x: 3, y: 5 } })).toBe(true);
    expect(ed.showSwitchEditor).not.toHaveBeenCalled();
  });
});

describe('Track → Remove tool', () => {
  it('removes the clicked track through the delete-with-confirmation path', async () => {
    const ed = fakeEditor({ '3,6': {} });
    await buildRemoveTrackMode(ed).onClick({ gridPos: { x: 3, y: 6 } });
    expect(ed._bulkDeleteWithConfirm).toHaveBeenCalledWith([[3, 6]]);
    expect(ed.refreshAndSelect).toHaveBeenCalledWith(null);
  });

  it('changes nothing when the removal is cancelled', async () => {
    const ed = fakeEditor({ '7,5': {} });
    ed._bulkDeleteWithConfirm = vi.fn(async () => null);
    await buildRemoveTrackMode(ed).onClick({ gridPos: { x: 7, y: 5 } });
    expect(ed.refreshAndSelect).not.toHaveBeenCalled();
  });

  it('does nothing on an empty square', async () => {
    const ed = fakeEditor({});
    await buildRemoveTrackMode(ed).onClick({ gridPos: { x: 0, y: 0 } });
    expect(ed._bulkDeleteWithConfirm).not.toHaveBeenCalled();
  });
});

describe('picking a tool by id', () => {
  it('enters the mode of a contributed tool, as a rail button click would', async () => {
    const editor: any = {
      selectedTool: 'inspect',
      activeMode: null,
      _currentEditorType: null,
      renderer: {
        setBuildPreview: vi.fn(),
        setHoverPreview: vi.fn(),
        requestRedraw: vi.fn(),
      },
      enterMode: vi.fn(async (mode: any) => {
        editor.selectedTool = mode.toolName;
      }),
      _setSelectedTool: vi.fn(),
    };
    editor._toolsById = new Map(editorScreenTools(editor).map((c) => [c.id, c]));
    await new ToolbarHandler(editor).selectTool('switch-pick');
    expect(editor.enterMode).toHaveBeenCalledTimes(1);
    expect(editor.selectedTool).toBe('switch-pick');
  });
});
