// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { screen } from '../state';
import { selection, type OperateHost } from './operate-state';
import { operatePanelHost, wrappedPanelElement, wrappedPanelKey } from './panel-host';

function fakeHost(): OperateHost {
  const editor: any = {
    editorMode: 'sim',
    _currentEditorType: null,
    _currentEditorId: null,
    renderer: { clearBlockDirectionOverlay: vi.fn() },
    uiHandler: { cleanupPanelSubscriptions: vi.fn() },
  };
  return { editor } as unknown as OperateHost;
}

describe('operatePanelHost', () => {
  let host: OperateHost;

  beforeEach(() => {
    host = fakeHost();
    screen.value = 'operate';
    selection.value = null;
    document.body.appendChild(wrappedPanelElement());
  });

  afterEach(() => {
    selection.value = null;
    wrappedPanelElement().innerHTML = '';
    wrappedPanelElement().remove();
    screen.value = 'editor';
  });

  it('leaves everything to today\'s inspector on the other screens', () => {
    screen.value = 'editor';
    const panels = operatePanelHost(host);
    expect(panels.selectTile({ x: 3, y: 4 })).toBe(false);
    expect(panels.showPanel('Block Editor', '<p>x</p>')).toBe(false);
    expect(panels.hidePanel()).toBe(false);
  });

  it('opens a clicked tile\'s switch, else its block', () => {
    const panels = operatePanelHost(host);
    expect(panels.selectTile({ x: 9, y: 9, switch_id: 'SW1', block_section_id: 'B1' })).toBe(true);
    expect(selection.value).toEqual({ kind: 'switch', id: 'SW1' });

    panels.selectTile({ x: 9, y: 8, block_section_id: 'B2' });
    expect(selection.value).toEqual({ kind: 'block', id: 'B2' });

    panels.selectTile({ x: 0, y: 0 });
    expect(selection.value).toBeNull();
  });

  it('shows one of today\'s panels as it is, read-only when it edits structure', () => {
    // applyStructuralLock reads the mode from App.
    (window as any).app = { editorMode: 'sim' };
    const panels = operatePanelHost(host);
    host.editor._currentEditorType = 'block-detail';
    host.editor._currentEditorId = 'BLK_1';
    expect(panels.showPanel('Block Editor', '<div><input id="x"></div>')).toBe(true);
    expect(selection.value).toEqual({ kind: 'panel', title: 'Block Editor' });
    expect(document.getElementById('x')).not.toBeNull();
    expect(wrappedPanelElement().classList.contains('sim-locked')).toBe(true);
    expect(wrappedPanelKey.value).toEqual({ type: 'block-detail', id: 'BLK_1' });
    delete (window as any).app;
  });

  it('closes the shown panel when its handler asks for that type and id', () => {
    const panels = operatePanelHost(host);
    host.editor._currentEditorType = 'block-list';
    panels.showPanel('Blocks', '<p>list</p>');
    panels.closePanel('switch', 'SW1');
    expect(selection.value?.kind).toBe('panel');
    panels.closePanel('block-list', null);
    expect(selection.value).toBeNull();
    expect(wrappedPanelElement().innerHTML).toBe('');
    expect(host.editor.uiHandler.cleanupPanelSubscriptions).toHaveBeenCalled();
  });
});
