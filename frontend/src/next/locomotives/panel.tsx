/**
 * Hosting today's Speed Profile panel (editor/editor-speed-profiles.ts) on
 * the Locomotives screen.
 *
 * The panel keeps its name, its calibration group and its Calibrate controls
 * (design_new.md "Speed calibration", The panel), and it still builds its own
 * HTML; this phase only moves where it draws. Once a host is set, every way
 * of opening the panel draws here instead of in the secondary sidebar, so
 * only one copy of it is ever in the page.
 */
import { render } from 'preact';

import type { SpeedProfileEditorHandler } from '../../editor/editor-speed-profiles';
import { CalibrationSteps, WhileCalibrating } from './calibration-steps';
import { panelTarget, selectionForProfile } from './model';
import { roster, selection, setCalibration, trains } from './state';

export type SpeedProfilePanel = Pick<SpeedProfileEditorHandler, 'setHost' | 'showProfileEditor' | 'close'>;

export interface PanelBinding {
  /** The element the panel draws into. */
  el: HTMLElement;
  /** Show a profile, naming `trainId` in the locomotive row. */
  show(profileId: string, trainId: string | null): Promise<void>;
  /** Close the panel; this ends a run it shows (design_new.md "What ends a calibration run"). */
  close(): void;
  /** The profile the panel shows, or null. */
  shown(): string | null;
}

/**
 * Make `panel` draw into a new element. `reveal` shows the Locomotives
 * screen; it runs when another part of the app opens the panel (today's
 * Profiles tool, or a train panel's Edit profile button).
 */
export function bindSpeedProfilePanel(panel: SpeedProfilePanel, reveal: () => void): PanelBinding {
  const el = document.createElement('div');
  el.className = 'nloco-panel-host';
  let shown: string | null = null;
  /** Shows this screen asked for that are still on their way. */
  let asked = 0;
  let slots: HTMLElement[] = [];

  const unmountSteps = () => {
    for (const slot of slots) render(null, slot);
    slots = [];
  };
  // The five steps go at the top of the panel's calibration group, under its
  // heading, and the refused commands at its end. The panel redraws the
  // whole group each time it shows a profile, so both are put back after
  // each draw.
  const mountSteps = () => {
    unmountSteps();
    const group = el.querySelector('#sp-cal-group');
    if (!group) return;
    const top = document.createElement('div');
    group.insertBefore(top, group.firstElementChild?.nextSibling ?? null);
    render(<CalibrationSteps />, top);
    const end = document.createElement('div');
    group.appendChild(end);
    render(<WhileCalibrating />, end);
    slots = [top, end];
  };

  panel.setHost({
    el,
    showList: reveal,
    profileShown(profileId, restoring) {
      shown = profileId;
      mountSteps();
      if (asked > 0) return;
      // The panel opened a profile this screen did not ask for: it jumped to
      // the model profile (Edit model →, Promote to model), or another part
      // of the app opened it (a train panel's Edit profile). Pick it in the
      // list: a locomotive's own profile picks that locomotive, which shows
      // the profile again with its train named.
      const picked = panelTarget(selection.peek(), roster.peek(), trains.peek()).profileId;
      if (picked !== profileId) selection.value = selectionForProfile(profileId, roster.peek(), trains.peek());
      if (!restoring) reveal();
    },
    profileDeleted() {
      shown = null;
      unmountSteps();
      selection.value = null;
    },
    calibrationChanged: setCalibration,
  });

  return {
    el,
    async show(profileId, trainId) {
      asked += 1;
      try {
        await panel.showProfileEditor(profileId, { trainId });
      } finally {
        asked -= 1;
      }
    },
    close() {
      shown = null;
      unmountSteps();
      panel.close();
    },
    shown: () => shown,
  };
}
