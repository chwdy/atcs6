/**
 * Mounts the Operate screen:
 * the left list, the right panel for the selection, and the row of train
 * cards along the bottom. Each draws only while the Operate tab is picked.
 */
import { effect, signal } from '@preact/signals';
import { RunningTrainOverlay } from '../../renderer/running-train-overlay';
import { render } from 'preact';

import type { ComponentChildren } from 'preact';

import { combinePanelHosts } from '../panel-hosts';
import { screen, sessionState } from '../state';
import { OperateLists } from './left-list';
import { liveVersion, runningTrains, usesRunningTrains, watchLiveState } from './live';
import type { OperateHost } from './operate-state';
import { operatePanelHost } from './panel-host';
import { RightPanel } from './right-panel';
import { RunningTrainsRow, type TrainCardInteraction } from './running-trains';

function OnOperate({ children }: { children: ComponentChildren }) {
  return screen.value === 'operate' ? <>{children}</> : null;
}

/** The cards along the bottom: one per train. */
function CardsRow({ interaction }: { interaction: TrainCardInteraction }) {
  liveVersion.value;
  return <RunningTrainsRow interaction={interaction} />;
}

function slot(className: string): HTMLDivElement {
  const el = document.createElement('div');
  el.className = className;
  return el;
}

export function mountOperateScreen(host: OperateHost): void {
  const workspace = document.querySelector('.workspace');
  if (!workspace) return;
  // The Logs screen's hook is already there; each answers only on its own screen.
  host.editor.nextPanelHost = combinePanelHosts(host.editor.nextPanelHost, operatePanelHost(host));
  watchLiveState();

  // The list and the panel sit in the canvas row; CSS orders them around the
  // canvas. They go first in the page so that, when one of today's panels is
  // shown inside the right panel, a lookup by id finds it before any copy
  // left in today's hidden inspector.
  const listSlot = slot('op-slot op-slot-list');
  const panelSlot = slot('op-slot op-slot-panel');
  workspace.prepend(listSlot, panelSlot);
  // The cards row goes under the canvas row, inside the screen area, so a
  // panel along the very bottom of the window stays below it.
  const cardsSlot = slot('op-slot op-slot-cards');
  workspace.after(cardsSlot);

  render(<OnOperate><OperateLists host={host} /></OnOperate>, listSlot);
  render(<OnOperate><RightPanel host={host} /></OnOperate>, panelSlot);
  const hovered = signal<string | null>(null);
  const focused = signal<string | null>(null);
  const selected = signal<string | null>(null);
  const renderer = host.editor.renderer as import('../../renderer/renderer').Renderer;
  const trainsOnMap = () => screen.value === 'operate' && usesRunningTrains() ? runningTrains() : [];
  const overlay = new RunningTrainOverlay(() => ({
    trains: trainsOnMap(),
    emphasized: hovered.value ?? focused.value ?? selected.value,
  }));
  renderer.registerOverlay(overlay);
  let lastLayout = renderer.currentNetwork?.layout_version;
  let wasLive = sessionState.value?.value === 'live';
  effect(() => {
    liveVersion.value;
    const layout = renderer.currentNetwork?.layout_version;
    const live = sessionState.value?.value === 'live';
    const reset = layout !== lastLayout || (live && !wasLive);
    lastLayout = layout;
    wasLive = live;
    const ids = new Set(trainsOnMap().map((train) => train.train_id));
    for (const state of [hovered, focused, selected]) {
      if (reset || (state.value && !ids.has(state.value))) state.value = null;
    }
    renderer.requestRedraw({ world: false, live: true, reason: 'running-trains' });
  });
  const interaction: TrainCardInteraction = {
    selected,
    hover: (id) => { hovered.value = id; },
    focus: (id) => { focused.value = id; },
    select: (id) => { selected.value = selected.value === id ? null : id; },
  };
  host.editor.operateTrainClick = (x, y) => {
    if (screen.value !== 'operate' || !usesRunningTrains()) return false;
    const tile = renderer.screenToGrid(x, y);
    const onSwitch = Boolean(renderer.trackMap[`${tile.x},${tile.y}`]?.is_switch);
    const id = overlay.trainAt(x, y, onSwitch);
    if (!id || !trainsOnMap().some((train) => train.train_id === id)) {
      selected.value = null;
      return false;
    }
    selected.value = id;
    Array.from(cardsSlot.querySelectorAll<HTMLElement>('[data-train-id]'))
      .find((card) => card.dataset.trainId === id)
      ?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    return true;
  };
  render(<OnOperate><CardsRow interaction={interaction} /></OnOperate>, cardsSlot);
}
