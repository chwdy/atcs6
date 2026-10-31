/**
 * The ?ui=next Editor screen (docs/ui-layout-rewrite-plan.md §4): the tool bar
 * and options row across the top of the canvas, and the right panel that
 * edits the selection.
 *
 * The canvas and its tools are today's editor (editor/editor.ts). This screen
 * replaces how a tool is picked and where the selection's panel shows; the
 * rules in static/css/next/editor.css hide today's tool rail and inspector
 * while it shows.
 */
import { effect } from '@preact/signals';
import { render } from 'preact';

import type { Editor } from '../../editor/editor';
import { screen } from '../state';
import { openForTileClick } from './click-rule';
import { editorScreenPlugin } from './modes';
import { EditorPanel, type PanelActions } from './right-panel';
import { activeToolId } from './state';
import { EditorToolBar, pickBarTool, type ToolBarActions } from './tool-bar';
import { barToolFor, barToolForKey } from './tools';

export function toolBarActions(editor: Editor): ToolBarActions {
  return {
    pick: (toolId) => {
      void editor.toolbarHandler.selectTool(toolId).then(() => {
        // Assign adds segments to the open block, so with no block open
        // the list comes up to pick one from.
        if (toolId === 'block-assign' && editor._currentEditorType !== 'block-detail') {
          void editor.showBlocksPanel();
        }
      });
    },
    showBlockList: () => void editor.showBlocksPanel(),
  };
}

export function panelActions(editor: Editor): PanelActions {
  return {
    close: () => void editor.hideSecondarySidebar(),
    showSwitches: () => void editor.showSwitchesPanel(),
    showStations: () => void editor.showStationsPanel(),
    showBlocks: () => void editor.showBlocksPanel(),
    openTileKey: () => (editor._currentEditorType === 'tile' ? editor._currentEditorId : null),
  };
}

/** Whether a key press was typed into a field rather than meant for the canvas. */
function typingInField(): boolean {
  const el = document.activeElement as HTMLElement | null;
  const tag = (el?.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || !!el?.isContentEditable;
}

/**
 * Number keys 1–8 pick the bar's tools. The listener runs in the capture
 * phase on window, ahead of today's tool-rail shortcut on document, and stops
 * the key there so the hidden rail does not also act on it.
 */
export function onEditorScreenKey(e: KeyboardEvent, actions: ToolBarActions): void {
  if (e.key < '1' || e.key > '9' || e.metaKey || e.ctrlKey || e.altKey || typingInField()) return;
  e.stopPropagation();
  const tool = barToolForKey(e.key);
  if (!tool) return;
  e.preventDefault();
  pickBarTool(tool, barToolFor(activeToolId.peek()), actions);
}

export function mountEditorScreen(editor: Editor): void {
  editor.registerPlugin(editorScreenPlugin);
  activeToolId.value = editor.selectedTool;
  editor.onToolChange = (toolId) => {
    activeToolId.value = toolId;
  };

  const bar = toolBarActions(editor);
  const onKey = (e: KeyboardEvent) => onEditorScreenKey(e, bar);
  const clickRule = (tile: any) => openForTileClick(editor, tile);

  // While the Editor screen shows: a plain click opens what was placed on
  // the tile, every panel opens in the one right panel, and the number keys
  // follow the bar.
  effect(() => {
    if (screen.value !== 'editor') return;
    editor.tileClickPanel = clickRule;
    editor.uiHandler.singlePanel = true;
    window.addEventListener('keydown', onKey, true);
    return () => {
      if (editor.tileClickPanel === clickRule) editor.tileClickPanel = null;
      editor.uiHandler.singlePanel = false;
      window.removeEventListener('keydown', onKey, true);
    };
  });

  // The bar spans the top of the screen area, above the canvas and the panel.
  const contentArea = document.querySelector('.content-area');
  const barSlot = document.createElement('div');
  barSlot.className = 'ned-bar-slot';
  contentArea?.insertBefore(barSlot, contentArea.firstChild);
  render(<EditorToolBar actions={bar} />, barSlot);

  // The panel sits right of the canvas in the workspace row.
  const workspace = document.querySelector('.workspace');
  const panelSlot = document.createElement('div');
  panelSlot.className = 'ned-panel-slot';
  workspace?.appendChild(panelSlot);
  render(<EditorPanel actions={panelActions(editor)} />, panelSlot);
}
