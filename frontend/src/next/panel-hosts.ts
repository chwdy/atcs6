/**
 * Join the canvas-click hooks of several screens into the one
 * `Editor.nextPanelHost`. Each screen's hook answers only while its own
 * screen shows and returns false otherwise, so the first one that takes a
 * request has it; a request no screen takes goes on to the panel host in
 * editor-ui.ts (which the Editor screen shows).
 */
import type { NextPanelHost } from '../editor/editor';

export function combinePanelHosts(...hosts: (NextPanelHost | null | undefined)[]): NextPanelHost {
  const list = hosts.filter((h): h is NextPanelHost => !!h);
  return {
    selectTile: (tile) => list.some((h) => h.selectTile(tile)),
    showPanel: (title, content) => list.some((h) => h.showPanel(title, content)),
    hidePanel: () => list.some((h) => h.hidePanel()),
    closePanel: (editorType, editorId) => list.forEach((h) => h.closePanel(editorType, editorId)),
  };
}
