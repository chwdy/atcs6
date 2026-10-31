/**
 * Mounts the Operate screen (docs/ui-layout-rewrite-plan.md §5):
 * the left list, the right panel for the selection, and the row of train
 * cards along the bottom. Each draws only while the Operate tab is picked.
 *
 * While it shows, static/css/next/operate.css keeps the Train Monitor window
 * hidden: the train panel's Look-ahead section shows its graph.
 */
import { render } from 'preact';

import type { ComponentChildren } from 'preact';

import { screen } from '../state';
import { OperateLists } from './left-list';
import { watchLiveState } from './live';
import type { OperateHost } from './operate-state';
import { operatePanelHost } from './panel-host';
import { RightPanel } from './right-panel';
import { TrainCardsRow } from './train-cards';

function OnOperate({ children }: { children: ComponentChildren }) {
  return screen.value === 'operate' ? <>{children}</> : null;
}

function slot(className: string): HTMLDivElement {
  const el = document.createElement('div');
  el.className = className;
  return el;
}

export function mountOperateScreen(host: OperateHost): void {
  const workspace = document.querySelector('.workspace');
  if (!workspace) return;
  host.editor.nextPanelHost = operatePanelHost(host);
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
  render(<OnOperate><TrainCardsRow host={host} /></OnOperate>, cardsSlot);
}
