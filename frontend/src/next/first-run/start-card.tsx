/**
 * The first-run card: while the layout
 * has no track, a card over the map offers two ways to begin. It shows on
 * the Editor and Operate screens, the two that draw the map.
 */
import { signal } from '@preact/signals';

import { layoutEmpty, screen } from '../state';

export interface FirstRunActions {
  openLayout(): void | Promise<void>;
  drawFromScratch(): void;
}

/** Set by "Draw from scratch": the user chose to start with an empty map,
 *  so the card stays away for the rest of this page's life. */
export const startCardDismissed = signal(false);

export function StartCard({ actions }: { actions: FirstRunActions }) {
  if (layoutEmpty.value !== true || startCardDismissed.value) return null;
  if (screen.value !== 'editor' && screen.value !== 'operate') return null;

  return (
    <div class="first-run-card" role="dialog" aria-labelledby="first-run-title">
      <h2 id="first-run-title" class="first-run-title">
        This layout has no track yet
      </h2>
      <button type="button" class="first-run-option is-primary" onClick={() => void actions.openLayout()}>
        <i class="ti ti-folder-open first-run-icon" aria-hidden="true" />
        <span class="first-run-text">
          <span class="first-run-label">Open a layout</span>
          <span class="first-run-sub">One you saved before.</span>
        </span>
      </button>
      <button type="button" class="first-run-option" onClick={actions.drawFromScratch}>
        <i class="ti ti-pencil first-run-icon" aria-hidden="true" />
        <span class="first-run-text">
          <span class="first-run-label">Draw from scratch</span>
          <span class="first-run-sub">Opens the Editor with the Track tool picked.</span>
        </span>
      </button>
    </div>
  );
}
