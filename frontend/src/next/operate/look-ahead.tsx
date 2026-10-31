/**
 * The train panel's Look-ahead section: today's Train Monitor graph and its
 * Upcoming list (panels/train-monitor.ts), shown inside the right panel.
 *
 * Wrapped, not ported. The monitor is a floating window over static markup
 * in index.html (#train-monitor-bar) that App feeds while it is showing.
 * While this section is open, the graph, the Upcoming list and the view
 * distance buttons are moved in here, the monitor is told to show this train
 * so App keeps feeding it, and its own window stays hidden on the Operate
 * screen (static/css/next/operate.css). When the section closes they go back
 * where they came from and the monitor hides.
 */
import { useLayoutEffect, useRef } from 'preact/hooks';

import type { OperateHost } from './operate-state';

/** The monitor parts this section shows, in the order they are laid out. */
const PARTS = ['.tm-panel .view-distance-controls', '#train-monitor-canvas-wrap', '#monitor-upcoming-container'];

export function LookAhead({ host, trainId }: { host: OperateHost; trainId: string }) {
  const slot = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const monitor = host.trainMonitor;
    const into = slot.current;
    if (!monitor || !into) return;
    const homes = PARTS.map((sel) => document.querySelector<HTMLElement>(sel))
      .filter((el): el is HTMLElement => !!el)
      .map((el) => ({ el, parent: el.parentNode, next: el.nextSibling }));
    homes.forEach(({ el }) => into.appendChild(el));

    // The same request as today's "Monitor →" button: select the train and
    // show the monitor, which starts App's look-ahead fetches for it.
    host.selectTrain(trainId, { showMonitor: true });
    monitor.resizeCanvas?.();

    // The graph is a canvas: its backing store follows the section's width.
    const canvasWrap = document.getElementById('train-monitor-canvas-wrap');
    const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(() => monitor.resizeCanvas?.()) : null;
    if (canvasWrap) observer?.observe(canvasWrap);

    return () => {
      observer?.disconnect();
      for (const { el, parent, next } of homes) parent?.insertBefore(el, next);
      monitor.hide?.();
    };
  }, [trainId]);

  return (
    <div class="op-look-ahead">
      <div class="op-look-ahead-parts" ref={slot} />
    </div>
  );
}
