/**
 * The first-run card (docs/ui-layout-rewrite-plan.md §9): while the layout
 * has no track, a card over the map offers three ways to begin. It shows on
 * the Editor and Operate screens, the two that draw the map.
 *
 * "Start from a loop" builds a small oval with one train and a route round
 * it (see starter-loop.ts), so the shortest path to a moving train is this
 * button, then Start route on the train's card, then Play.
 */
import { signal } from '@preact/signals';
import { useState } from 'preact/hooks';

import { layoutEmpty, operationEnvironment, screen } from '../state';

export interface FirstRunActions {
  startFromLoop(): Promise<void>;
  openLayout(): void | Promise<void>;
  drawFromScratch(): void;
}

/** Set by "Draw from scratch": the user chose to start with an empty map,
 *  so the card stays away for the rest of this page's life. */
export const startCardDismissed = signal(false);

export function StartCard({ actions }: { actions: FirstRunActions }) {
  const [busy, setBusy] = useState(false);
  if (layoutEmpty.value !== true || startCardDismissed.value) return null;
  if (screen.value !== 'editor' && screen.value !== 'operate') return null;

  const inSimulation = (operationEnvironment.value ?? 'simulation') === 'simulation';
  const startFromLoop = async () => {
    setBusy(true);
    try {
      await actions.startFromLoop();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="first-run-card" role="dialog" aria-labelledby="first-run-title">
      <h2 id="first-run-title" class="first-run-title">
        This layout has no track yet
      </h2>
      <button type="button" class="first-run-option is-primary" disabled={busy} onClick={() => void startFromLoop()}>
        <i class="ti ti-circle-dashed first-run-icon" aria-hidden="true" />
        <span class="first-run-text">
          <span class="first-run-label">{busy ? 'Building the loop…' : 'Start from a loop'}</span>
          <span class="first-run-sub">
            An oval with one train and a route round it, ready to run.
            {!inSimulation && ' Switches the run environment to Simulation.'}
          </span>
        </span>
      </button>
      <button type="button" class="first-run-option" disabled={busy} onClick={() => void actions.openLayout()}>
        <i class="ti ti-folder-open first-run-icon" aria-hidden="true" />
        <span class="first-run-text">
          <span class="first-run-label">Open a layout</span>
          <span class="first-run-sub">One you saved before.</span>
        </span>
      </button>
      <button type="button" class="first-run-option" disabled={busy} onClick={actions.drawFromScratch}>
        <i class="ti ti-pencil first-run-icon" aria-hidden="true" />
        <span class="first-run-text">
          <span class="first-run-label">Draw from scratch</span>
          <span class="first-run-sub">Opens the Editor with the Track tool picked.</span>
        </span>
      </button>
    </div>
  );
}
