/**
 * Mounts the first-run card over the map and gives it its actions.
 */
import { render } from 'preact';

import { type Screen } from '../state';
import { StartCard, startCardDismissed, type FirstRunActions } from './start-card';

/** The App methods the card's actions call. */
export interface FirstRunHost {
  editor: { toolbarHandler: { selectTool(toolId: string): Promise<void> | void } };
  openLoadLayoutDialog(): Promise<void>;
}

export function firstRunActions(host: FirstRunHost, showScreen: (screen: Screen) => void): FirstRunActions {
  return {
    openLayout: () => host.openLoadLayoutDialog(),
    drawFromScratch: () => {
      startCardDismissed.value = true;
      showScreen('editor');
      void host.editor.toolbarHandler.selectTool('track');
    },
  };
}

/** Put the card in the map area, centred over the canvas. */
export function mountFirstRun(host: FirstRunHost, showScreen: (screen: Screen) => void): void {
  const slot = document.createElement('div');
  slot.className = 'first-run-slot';
  document.querySelector('.canvas-container')?.appendChild(slot);
  render(<StartCard actions={firstRunActions(host, showScreen)} />, slot);
}
