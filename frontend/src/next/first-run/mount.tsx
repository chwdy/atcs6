/**
 * Mounts the first-run card over the map and gives it its actions.
 */
import { render } from 'preact';

import { api } from '../../lib/api';
import { showToast } from '../../lib/toast';
import { type Screen } from '../state';
import { buildStarterLoop } from './starter-loop';
import { StartCard, startCardDismissed, type FirstRunActions } from './start-card';

/** The App methods the card's actions call. */
export interface FirstRunHost {
  editor: { toolbarHandler: { selectTool(toolId: string): Promise<void> | void } };
  openLoadLayoutDialog(): Promise<void>;
  refreshNetwork(): Promise<void>;
  selectTrain(trainId: string): void;
  // Typed exactly as LocomotivesHost types it: NextHost extends both.
  renderer?: { fitToContent(tracks: any[], margin?: number, insets?: { left: number; bottom: number }): void; requestRedraw(opts?: any): void };
  currentNetwork?: any;
  _canvasChromeInsets?(): { left: number; bottom: number };
}

/** Frame the view on the layout's track. App does this only for the first
 *  layout it sees, so a loop built later would otherwise stay off screen. */
function frameTrack(host: FirstRunHost): void {
  const tracks = host.currentNetwork?.tracks;
  if (!host.renderer || !tracks?.length) return;
  host.renderer.fitToContent(tracks, undefined, host._canvasChromeInsets?.() ?? { left: 0, bottom: 0 });
}

/**
 * Train ids the starter loop must not use: every train on the layout, and
 * every id a route still names. Routes outlive a cleared layout and keep
 * their assigned train ids, and a new train with one of those ids would
 * inherit that old route.
 */
async function takenTrainIds(): Promise<string[]> {
  const [network, routes] = await Promise.all([api.getNetwork(), api.getRoutes()]);
  const routeList: any[] = Array.isArray(routes) ? routes : (routes?.routes ?? []);
  return [
    ...(network?.trains ?? []).map((t: any) => t.id),
    ...routeList.flatMap((r) => r?.assigned_train_ids ?? []),
  ];
}

export function firstRunActions(host: FirstRunHost, showScreen: (screen: Screen) => void): FirstRunActions {
  return {
    startFromLoop: async () => {
      try {
        const { trainId } = await buildStarterLoop(api, await takenTrainIds());
        await host.refreshNetwork();
        showScreen('operate');
        frameTrack(host);
        host.selectTrain(trainId);
        showToast(`Your loop is ready. Press Start route on ${trainId}.`, 'success', 8000);
      } catch (e: any) {
        await host.refreshNetwork();
        showToast(`${e?.message ?? e}. Start a new layout to try again.`, 'error', 8000);
      }
    },
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
