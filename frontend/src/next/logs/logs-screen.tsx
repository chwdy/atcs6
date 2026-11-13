/**
 * The Logs screen, shown when the Logs
 * tab is picked: a tab each for the event log and the detectors, with the
 * canvas on the right (static/css/next/logs.css moves
 * it there) so a reading can be matched to where the trains are.
 *
 * A click on a block on the canvas opens the Detectors tab with its row
 * marked. Only the tab on show is mounted, and each tab polls only while
 * mounted, so a hidden tab and another screen ask the server for nothing.
 */
import { effect } from '@preact/signals';
import { render } from 'preact';

import type { NextPanelHost } from '../../editor/editor';
import { afterLayout, fitCanvas, type CanvasFitHost } from '../canvas-fit';
import { combinePanelHosts } from '../panel-hosts';
import { screen } from '../state';
import { DetectorsTab } from './detectors-tab';
import { EventLogTab } from './event-log-tab';
import { LOGS_TABS, logsTab, showBlockInLogs, showLogsTab, type LogsTab } from './state';

/** What the screen needs from App. */
export interface LogsHost extends CanvasFitHost {
  editor: { nextPanelHost: NextPanelHost | null };
}

const TAB_BODY: Record<LogsTab, () => preact.JSX.Element> = {
  events: () => <EventLogTab />,
  detectors: () => <DetectorsTab />,
};

export function LogsScreen() {
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
        {TAB_BODY[tab]()}
      </div>
    </section>
  );
}

/**
 * The Editor hook for this screen (see panel-hosts.ts). A click on a block
 * shows it in Detectors. Other panels have no place here, so they are left
 * to the panel host.
 */
export function logsPanelHost(): NextPanelHost {
  const active = () => screen.peek() === 'logs';
  return {
    selectTile(tile) {
      if (!active()) return false;
      if (tile.block_section_id) showBlockInLogs(String(tile.block_section_id));
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
  render(<LogsScreen />, slot);
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
