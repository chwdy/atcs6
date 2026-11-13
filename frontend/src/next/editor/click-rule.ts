/**
 * What a plain Select click on a tile opens on the Editor screen: the thing you placed there, not the
 * tile. The tile's own panel (track bits under Advanced) stays one
 * right-click away, through "Inspect tile".
 *
 * Installed as Editor.tileClickPanel while the Editor screen shows. It also
 * runs when the editor re-selects a tile after an edit (refreshAndSelect).
 */

/** The editor members this rule uses. */
export interface ClickRuleEditor {
  selectedTool: string;
  _currentEditorType: string | null;
  _currentEditorId: string | null;
  showSwitchEditor(switchId: string): unknown;
  showSegmentEditor(segmentId: string): unknown;
  blockGroupingHandler: { showDetail(blockId: string): unknown };
  hideSecondarySidebar(): unknown;
}

/** Open the panel for `tile`. Returns false to leave it to today's behavior
 *  (the Tile Editor). */
export function openForTileClick(editor: ClickRuleEditor, tile: any): boolean {
  // The open tile panel re-draws itself after a track-bit change by
  // selecting its own tile again; let it.
  if (editor._currentEditorType === 'tile' && editor._currentEditorId === `${tile.x},${tile.y}`) {
    return false;
  }
  // An empty grid square: nothing is selected.
  if (tile.isVirtual) {
    void editor.hideSecondarySidebar();
    return true;
  }
  if (tile.switch_id) {
    void editor.showSwitchEditor(tile.switch_id);
    return true;
  }
  if (tile.segment_id) {
    void editor.showSegmentEditor(tile.segment_id);
    return true;
  }
  if (tile.block_section_id) {
    void editor.blockGroupingHandler.showDetail(tile.block_section_id);
    return true;
  }
  return false;
}
