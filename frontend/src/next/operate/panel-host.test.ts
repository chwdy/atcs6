// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { stateManager } from '../../lib/state-manager';
import { screen } from '../state';
import { selection, type OperateHost } from './operate-state';
import { operatePanelHost, setSelection, wrappedPanelElement, wrappedPanelKey } from './panel-host';

function fakeHost(): OperateHost {
  const editor: any = {
    editorMode: 'sim',
    _currentEditorType: null,
    _currentEditorId: null,
    _activeTabGroupForRestore: null,
    renderer: { clearBlockDirectionOverlay: vi.fn() },
    uiHandler: { homeTabGroup: { cleanupPanelSubscriptions: vi.fn() } },
  };
  return { editor, selectTrain: vi.fn(), deselectTrain: vi.fn() } as unknown as OperateHost;
}

describe('operatePanelHost', () => {
  let host: OperateHost;

  beforeEach(() => {
    host = fakeHost();
    screen.value = 'operate';
    selection.value = null;
    document.body.appendChild(wrappedPanelElement());
    stateManager.updateFromNetwork({
      trains: [{ id: 'T1' }, { id: 'U1', is_unidentified: true }],
      controllers_by_train_id: { T1: { span: { occupied_tiles: [[3, 4]] } }, U1: { span: null } },
    });
  });

  afterEach(() => {
    selection.value = null;
    wrappedPanelElement().innerHTML = '';
    wrappedPanelElement().remove();
    stateManager.updateFromNetwork({ trains: [], controllers_by_train_id: {} });
    screen.value = 'editor';
  });

  it('leaves everything to today\'s inspector on the other screens', () => {
    screen.value = 'editor';
    const panels = operatePanelHost(host);
    expect(panels.selectTile({ x: 3, y: 4 })).toBe(false);
    expect(panels.showTrain('T1')).toBe(false);
    expect(panels.showPanel('Route Editor', '<p>x</p>')).toBe(false);
    expect(panels.hidePanel()).toBe(false);
  });

  it('opens the train under a clicked tile, else its switch, else its block', () => {
    const panels = operatePanelHost(host);
    expect(panels.selectTile({ x: 3, y: 4, block_section_id: 'B1' })).toBe(true);
    expect(host.selectTrain).toHaveBeenCalledWith('T1');

    panels.selectTile({ x: 9, y: 9, switch_id: 'SW1', block_section_id: 'B1' });
    expect(selection.value).toEqual({ kind: 'switch', id: 'SW1' });

    panels.selectTile({ x: 9, y: 8, block_section_id: 'B2' });
    expect(selection.value).toEqual({ kind: 'block', id: 'B2' });

    panels.selectTile({ x: 0, y: 0 });
    expect(selection.value).toBeNull();
  });

  it('draws a train itself, but leaves an unidentified train to today\'s adoption panel', () => {
    const panels = operatePanelHost(host);
    expect(panels.showTrain('T1')).toBe(true);
    expect(selection.value).toEqual({ kind: 'train', id: 'T1' });
    expect(panels.showTrain('U1')).toBe(false);
  });

  it('shows one of today\'s panels as it is, read-only when it edits structure', () => {
    // applyStructuralLock reads the mode from App.
    (window as any).app = { editorMode: 'sim' };
    const panels = operatePanelHost(host);
    host.editor._currentEditorType = 'station-group-detail';
    host.editor._currentEditorId = 'STN_1';
    expect(panels.showPanel('Station Editor', '<div><input id="x"></div>')).toBe(true);
    expect(selection.value).toEqual({ kind: 'panel', title: 'Station Editor' });
    expect(document.getElementById('x')).not.toBeNull();
    expect(wrappedPanelElement().classList.contains('sim-locked')).toBe(true);
    expect(wrappedPanelKey.value).toEqual({ type: 'station-group-detail', id: 'STN_1' });
    delete (window as any).app;
  });

  it('lets today\'s inspector restore its own saved tabs', () => {
    host.editor._activeTabGroupForRestore = {};
    expect(operatePanelHost(host).showPanel('Tile Editor', '<p></p>')).toBe(false);
  });

  it('closes the shown panel when its handler asks for that type and id', () => {
    const panels = operatePanelHost(host);
    host.editor._currentEditorType = 'path-setup';
    panels.showPanel('Create Path', '<p>setup</p>');
    panels.closePanel('route-editor', 'R1');
    expect(selection.value?.kind).toBe('panel');
    panels.closePanel('path-setup', null);
    expect(selection.value).toBeNull();
    expect(wrappedPanelElement().innerHTML).toBe('');
    expect(host.editor.uiHandler.homeTabGroup!.cleanupPanelSubscriptions).toHaveBeenCalled();
  });

  it('lets App drop the selected train when the selection moves off it', () => {
    setSelection(host, { kind: 'train', id: 'T1' });
    setSelection(host, { kind: 'train', id: 'T2' });
    expect(host.deselectTrain).not.toHaveBeenCalled();
    setSelection(host, { kind: 'block', id: 'B1' });
    expect(host.deselectTrain).toHaveBeenCalledTimes(1);
  });
});
