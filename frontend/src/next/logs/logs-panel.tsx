/**
 * The Logs panel (docs/ui-layout-rewrite-plan.md §8): one panel along the
 * bottom of the window, opened from **Logs** in the top bar on any screen,
 * with a tab each for the event log, the detectors and diagnostics.
 *
 * It sits in the page's column below everything else, so opening it makes the
 * canvas and the screen above it shorter instead of covering them. Only the
 * tab on show is mounted, and each tab polls only while mounted, so a hidden
 * tab and a closed panel ask the server for nothing.
 */
import { render } from 'preact';
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';

import { coarsePointerScale, isCoarsePointer } from '../../lib/dpi';
import { DetectorsTab } from './detectors-tab';
import { DiagnosticsTab } from './diagnostics-tab';
import { EventLogTab } from './event-log-tab';
import {
  DEFAULT_HEIGHT,
  LOGS_TABS,
  clampHeight,
  closeLogs,
  logsHeight,
  logsOpen,
  logsTab,
  setLogsHeight,
  showLogsTab,
  type LogsTab,
} from './state';

/** How far one arrow-key press on the drag handle moves the panel's edge. */
const KEY_STEP = 16;

const TAB_BODY: Record<LogsTab, () => preact.JSX.Element> = {
  events: () => <EventLogTab />,
  detectors: () => <DetectorsTab />,
  diagnostics: () => <DiagnosticsTab />,
};

/** The window's height, kept current so the panel re-clamps its own height
 *  when the window shrinks. */
function useWindowHeight(): number {
  const [height, setHeight] = useState(() => window.innerHeight);
  useEffect(() => {
    const onResize = () => setHeight(window.innerHeight);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  return height;
}

export function LogsPanel() {
  const open = logsOpen.value;
  const windowHeight = useWindowHeight();
  const height = clampHeight(logsHeight.value, windowHeight);

  // <html data-logs="open"> and --next-logs-height let anything fixed to the
  // viewport (today's floating windows) keep clear of the panel.
  useLayoutEffect(() => {
    const root = document.documentElement;
    if (open) {
      root.dataset.logs = 'open';
      root.style.setProperty('--next-logs-height', `${height}px`);
    } else {
      delete root.dataset.logs;
      root.style.removeProperty('--next-logs-height');
    }
  }, [open, height]);

  if (!open) return null;
  return <OpenLogsPanel height={height} windowHeight={windowHeight} />;
}

function OpenLogsPanel({ height, windowHeight }: { height: number; windowHeight: number }) {
  const tab = logsTab.value;
  const handleScale = coarsePointerScale(isCoarsePointer());
  const drag = useRef<{ startY: number; startHeight: number } | null>(null);

  const onPointerDown = (e: PointerEvent) => {
    if (e.button !== 0) return;
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = { startY: e.clientY, startHeight: height };
  };
  const onPointerMove = (e: PointerEvent) => {
    if (!drag.current) return;
    // The handle is the panel's top edge: dragging up makes the panel taller.
    const next = drag.current.startHeight + (drag.current.startY - e.clientY);
    setLogsHeight(clampHeight(next, windowHeight), false);
  };
  const onPointerUp = (e: PointerEvent) => {
    if (!drag.current) return;
    (e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId);
    drag.current = null;
    setLogsHeight(clampHeight(logsHeight.peek(), windowHeight));
  };
  const onKeyDown = (e: KeyboardEvent) => {
    const step = e.key === 'ArrowUp' ? KEY_STEP : e.key === 'ArrowDown' ? -KEY_STEP : 0;
    if (!step) return;
    e.preventDefault();
    setLogsHeight(clampHeight(height + step, windowHeight));
  };

  return (
    <section
      class="next-logs"
      aria-label="Logs"
      style={{ height: `${height}px`, '--nlp-handle-scale': String(handleScale) }}
    >
      <div
        class="nlp-resizer"
        role="separator"
        aria-orientation="horizontal"
        aria-label="Resize the Logs panel"
        aria-valuenow={height}
        tabIndex={0}
        title="Drag to resize. Double-click to reset."
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDblClick={() => setLogsHeight(clampHeight(DEFAULT_HEIGHT, windowHeight))}
        onKeyDown={onKeyDown}
      />
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
        <button
          type="button"
          class="nlp-close"
          title="Close the Logs panel"
          aria-label="Close the Logs panel"
          onClick={closeLogs}
        >
          <i class="ti ti-x" aria-hidden="true" />
        </button>
      </div>
      <div class="nlp-body" id="nlp-tabpanel" role="tabpanel" aria-labelledby={`nlp-tab-${tab}`}>
        {TAB_BODY[tab]()}
      </div>
    </section>
  );
}

/** Add the Logs panel to the bottom of the page's column. */
export function mountLogsPanel(): void {
  const column = document.querySelector('.app-container');
  if (!column) return;
  const slot = document.createElement('div');
  slot.className = 'next-logs-slot';
  column.appendChild(slot);
  render(<LogsPanel />, slot);
}
