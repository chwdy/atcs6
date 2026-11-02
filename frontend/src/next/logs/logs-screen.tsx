/**
 * The Logs screen (docs/ui-layout-rewrite-plan.md §8), shown when the Logs
 * tab is picked: a tab each for the event log, the detectors and
 * diagnostics, with the canvas on the right (static/css/next/logs.css moves
 * it there) so a reading can be matched to where the trains are.
 *
 * A click on the canvas picks what the tabs show: a train opens the
 * Diagnostics tab on it, a block opens the Detectors tab with its row
 * marked. Only the tab on show is mounted, and each tab polls only while
 * mounted, so a hidden tab and another screen ask the server for nothing.
 */
import { effect } from '@preact/signals';
import { render } from 'preact';

import type { NextPanelHost } from '../../editor/editor';
import { afterLayout, fitCanvas, type CanvasFitHost } from '../canvas-fit';
import { combinePanelHosts } from '../panel-hosts';
import { trainsOnTile } from '../operate/live';
import { screen } from '../state';
import { DetectorsTab } from './detectors-tab';
import { DiagnosticsTab } from './diagnostics-tab';
import { EventLogTab } from './event-log-tab';
import { LOGS_TABS, logsTab, showBlockInLogs, showLogsTab, showTrainInLogs, type LogsTab } from './state';
import type { ShowJson } from './train-readout';

/** What the screen needs from App. */
export interface LogsHost extends CanvasFitHost {
  editor: { nextPanelHost: NextPanelHost | null; showFullJSONViewer(data: any, title?: string): unknown };
}

const TAB_BODY: Record<LogsTab, (showJson?: ShowJson) => preact.JSX.Element> = {
  events: () => <EventLogTab />,
  detectors: () => <DetectorsTab />,
  diagnostics: (showJson) => <DiagnosticsTab showJson={showJson} />,
};

export function LogsScreen({ showJson }: { showJson?: ShowJson }) {
  if (screen.value !== 'logs') return null;
  const tab = logsTab.value;
  return (
    <section class="next-logs" aria-label="Logs">
      <div class="nlp-strip">
        <div class="nlp-tabs" role="tablist" aria-label="Logs">
          {LOGS_TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              id={`nlp-tab-${t.id}`}
              aria-selected={t.id === tab}
              aria-controls="nlp-tabpanel"
              class={t.id === tab ? 'nlp-tab-btn active' : 'nlp-tab-btn'}
              onClick={() => showLogsTab(t.id)}
            >
              <i class={t.icon} aria-hidden="true" />
              {t.label}
            </button>
          ))}
        </div>
      </div>
      <div class="nlp-body" id="nlp-tabpanel" role="tabpanel" aria-labelledby={`nlp-tab-${tab}`}>
        {TAB_BODY[tab](showJson)}
      </div>
    </section>
  );
}

/**
 * The Editor hook for this screen (see panel-hosts.ts). A click on a train
 * shows it in Diagnostics; a click on a block shows it in Detectors. Other
 * panels have no place here, so they are left to the panel host, the same as
 * on Locomotives.
 */
export function logsPanelHost(): NextPanelHost {
  const active = () => screen.peek() === 'logs';
  return {
    selectTile(tile) {
      if (!active()) return false;
      const trains = trainsOnTile(tile.x, tile.y);
      if (trains.length > 0) showTrainInLogs(trains[0]);
      else if (tile.block_section_id) showBlockInLogs(String(tile.block_section_id));
      return true;
    },
    showTrain(trainId) {
      if (!active()) return false;
      showTrainInLogs(String(trainId));
      return true;
    },
    showPanel: () => false,
    hidePanel: () => false,
    closePanel: () => {},
  };
}

/** Add the screen to today's workspace, next to the canvas. */
export function mountLogsScreen(host: LogsHost): void {
  const workspace = document.querySelector('.workspace');
  if (!workspace) return;
  const slot = document.createElement('div');
  slot.className = 'next-logs-slot';
  workspace.insertBefore(slot, workspace.firstChild);
  render(<LogsScreen showJson={(data, title) => host.editor.showFullJSONViewer(data, title)} />, slot);
  host.editor.nextPanelHost = combinePanelHosts(host.editor.nextPanelHost, logsPanelHost());

  // The canvas is narrower here: fit the layout into it on the way in, and
  // fit again on the way out, when it goes back to its usual size.
  let here = false;
  effect(() => {
    const now = screen.value === 'logs';
    if (now === here) return;
    here = now;
    afterLayout(() => fitCanvas(host));
  });
}
