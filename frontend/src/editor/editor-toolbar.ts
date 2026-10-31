// editor-toolbar.ts — picks the editor's active tool.
//
// Lifted out of the Editor god-class. The tool-contribution builders
// (coreTools / _collectToolContributions) and the tool-adjacent editor state
// (selectedTool, _toolsById, modes, handlers) stay on the Editor; this handler
// reaches them via `this.editor`. The buttons that pick a tool are the Editor
// screen's tool bar (frontend/src/next/editor/).
//
// Owned by the Editor (`new ToolbarHandler(this)`).
import type { EditorHost } from './types';

export class ToolbarHandler {
  editor: EditorHost;
  constructor(editor: EditorHost) {
    this.editor = editor;
  }

  /** Rebuild the id → contribution lookup from the core tools and every
   *  registered plugin's tools. */
  refreshTools(): void {
    const contributions = this.editor._collectToolContributions();
    this.editor._toolsById = new Map(contributions.map((c: any) => [c.id, c]));
  }

  /**
   * Pick a tool by its contribution id. The Editor screen's tool bar
   * (frontend/src/next/editor/tool-bar.tsx) calls this.
   */
  async selectTool(newTool: string): Promise<void> {
    const contribution = this.editor._toolsById.get(newTool);
    const oldTool = this.editor.selectedTool;

    // Action tools (panels/dialogs): run and leave the active tool unchanged.
    if (contribution?.kind.type === 'action') {
      contribution.kind.run();
      return;
    }

    // Exit any active mode before switching tools.
    if (this.editor.activeMode) {
      await this.editor.exitMode();
    }

    // Auto-toggle segment debug when entering/leaving segment (geometry) tools.
    const isSegmentTool = ['segment', 'extend-segment'].includes(newTool);
    const wasSegmentTool = ['segment', 'extend-segment'].includes(oldTool);
    if (isSegmentTool !== wasSegmentTool) {
      this.editor.renderer.showSegmentColorsOnly = isSegmentTool;
      this.editor.host?.refreshNetwork();
    }

    // Auto-toggle the block-grouping color overlay with the assign tool. It also
    // stays on while a block panel is open (those set it themselves), so
    // leaving the assign tool clears it only when no such panel is open.
    const isPowerTool = newTool === 'block-assign';
    const wasPowerTool = oldTool === 'block-assign';
    if (isPowerTool !== wasPowerTool) {
      const panelOpen =
        this.editor._currentEditorType === 'block-list' ||
        this.editor._currentEditorType === 'block-detail';
      this.editor.renderer.showBlockGroupingColors = isPowerTool || panelOpen;
      this.editor.renderer.requestRedraw({ world: true, live: true, reason: 'block-grouping-layer' });
    }

    if (contribution?.kind.type === 'mode') {
      // enterMode sets the active tool via _setSelectedTool(mode.toolName).
      await this.editor.enterMode(contribution.kind.factory());
    } else {
      this.editor._setSelectedTool(newTool);
    }

    this.editor.previewPath = null;
    this.editor.previewTracks = null;
    // Drop any lingering build/hover ghost from the previous tool.
    this.editor.renderer.setBuildPreview(null);
    this.editor.renderer.setHoverPreview(null);
    this.editor.hoverPosition = null;
    this.editor.hoverValid = false;
    this.editor.renderer.requestRedraw();
  }
}
