import { describe, expect, it, vi } from 'vitest';

import { openForTileClick, type ClickRuleEditor } from './click-rule';

function fakeEditor(over: Partial<ClickRuleEditor> = {}): ClickRuleEditor {
  return {
    selectedTool: 'inspect',
    _currentEditorType: null,
    _currentEditorId: null,
    showSwitchEditor: vi.fn(),
    showSegmentEditor: vi.fn(),
    blockGroupingHandler: { showDetail: vi.fn() },
    hideSecondarySidebar: vi.fn(),
    ...over,
  };
}

const track = { x: 3, y: 5, segment_id: 'SEG_1', block_section_id: 'BLK_1' };

describe('what a plain click on a tile opens on the Editor screen', () => {
  it('opens the segment, not the tile', () => {
    const ed = fakeEditor();
    expect(openForTileClick(ed, track)).toBe(true);
    expect(ed.showSegmentEditor).toHaveBeenCalledWith('SEG_1');
  });

  it('opens the block when the track has no segment', () => {
    const ed = fakeEditor();
    expect(openForTileClick(ed, { x: 1, y: 1, block_section_id: 'BLK_2' })).toBe(true);
    expect(ed.blockGroupingHandler.showDetail).toHaveBeenCalledWith('BLK_2');
  });

  it('opens the switch on a switch tile (a switch tile has no segment)', () => {
    const ed = fakeEditor();
    expect(openForTileClick(ed, { x: 7, y: 5, switch_id: 'SW_7_5', segment_id: null })).toBe(true);
    expect(ed.showSwitchEditor).toHaveBeenCalledWith('SW_7_5');
  });

  it('clears the selection on an empty grid square', () => {
    const ed = fakeEditor();
    expect(openForTileClick(ed, { x: 0, y: 0, isVirtual: true })).toBe(true);
    expect(ed.hideSecondarySidebar).toHaveBeenCalled();
  });

  it('lets the open tile panel re-draw itself for its own tile', () => {
    const ed = fakeEditor({ _currentEditorType: 'tile', _currentEditorId: '3,5' });
    expect(openForTileClick(ed, track)).toBe(false);
    expect(ed.showSegmentEditor).not.toHaveBeenCalled();
    // Another tile still opens its segment.
    expect(openForTileClick(ed, { ...track, x: 4 })).toBe(true);
  });

  it('falls back to the tile panel for track in no segment or block', () => {
    expect(openForTileClick(fakeEditor(), { x: 2, y: 2 })).toBe(false);
  });
});
