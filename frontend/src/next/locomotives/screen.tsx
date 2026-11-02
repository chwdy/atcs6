/**
 * The Locomotives screen (docs/ui-layout-rewrite-plan.md §6), shown when the
 * Locomotives tab is picked.
 *
 * Left: the locomotives the operator owns (the Train Roster), placed trains
 * not in it, and the model profiles. Middle: the picked locomotive and its
 * Speed Profile panel with the calibration group. Right, only while a
 * calibration run is open or being set up: today's layout canvas, because
 * step 3 names the measured block on the map and the operator watches the
 * pass (static/css/next/locomotives.css moves it there).
 */
import { effect } from '@preact/signals';
import { render } from 'preact';
import { useEffect, useRef } from 'preact/hooks';

import { afterLayout, fitCanvas, type CanvasFitHost } from '../canvas-fit';
import { screen } from '../state';
import { PlacedTrainDetails, RosterEntryDetails, RosterEntryForm } from './details';
import { LocomotiveList } from './list';
import { calibrationRunShown, entryName, panelTarget, placedTrainFor, selectionForTrain } from './model';
import { bindSpeedProfilePanel, type PanelBinding, type SpeedProfilePanel } from './panel';
import { calibration, roster, selection, trains } from './state';

/** What the screen needs from App. */
export interface LocomotivesHost extends CanvasFitHost {
  editor: { speedProfileHandler?: SpeedProfilePanel };
}

let revealScreen: (() => void) | null = null;

/**
 * Show the Locomotives screen with this train's locomotive picked: its
 * roster entry when it was placed from one, otherwise the train itself. The
 * Speed Profile panel opens with the train named in its locomotive row.
 * Operate's Calibrate button calls this (plan §5).
 */
export function openLocomotiveForTrain(trainId: string): void {
  selection.value = selectionForTrain(trainId, roster.peek(), trains.peek());
  revealScreen?.();
}

function Middle({ panel }: { panel: PanelBinding | null }) {
  const slot = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (panel && slot.current) slot.current.appendChild(panel.el);
  }, [panel]);

  const sel = selection.value;
  const target = panelTarget(sel, roster.value, trains.value);
  let title = 'Locomotives';
  let top = null;
  if (sel?.kind === 'new') {
    title = 'Add locomotive';
    top = <RosterEntryForm existing={null} />;
  } else if (sel?.kind === 'roster') {
    const entry = roster.value.find((e) => e.entry_id === sel.entryId);
    if (entry) {
      title = entryName(entry);
      top = <RosterEntryDetails key={entry.entry_id} entry={entry} train={placedTrainFor(entry, trains.value)} />;
    } else {
      top = <div class="empty-state empty-state-warn">This locomotive is no longer in the Train Roster.</div>;
    }
  } else if (sel?.kind === 'train') {
    const train = trains.value.find((t) => t.id === sel.trainId);
    if (train) {
      title = train.name;
      top = <PlacedTrainDetails train={train} />;
    } else {
      top = <div class="empty-state empty-state-warn">This train is no longer on the layout.</div>;
    }
  } else if (sel?.kind === 'profile') {
    title = sel.profileId;
  }

  return (
    <section class="nloco-main" aria-label="Locomotive">
      <div class="nloco-head">
        <h2>{title}</h2>
        {target.profileId && (
          <span class="nloco-head-sub">
            Speed Profile panel · <span class="font-mono">{target.profileId}</span>
          </span>
        )}
      </div>
      <div class="nloco-main-body">
        {!sel && <div class="empty-state">Pick a locomotive or a profile on the left.</div>}
        {top}
        {(sel?.kind === 'roster' || sel?.kind === 'train') && top && !target.profileId && (
          <div class="empty-state">This locomotive has no speed profile yet. It gets one when it is placed on the layout.</div>
        )}
        <div ref={slot} class="nloco-panel-slot" />
      </div>
    </section>
  );
}

export function LocomotivesScreen({ panel }: { panel: PanelBinding | null }) {
  return (
    <div class="nloco">
      <LocomotiveList />
      <Middle panel={panel} />
    </div>
  );
}

/**
 * Mount the screen into today's workspace, next to the canvas. `reveal`
 * picks the Locomotives tab. Returns a function that unmounts it.
 */
export function mountLocomotivesScreen(host: LocomotivesHost, reveal: () => void): () => void {
  revealScreen = reveal;
  const workspace = document.querySelector('.workspace');
  if (!workspace) return () => {};
  const root = document.createElement('div');
  root.className = 'next-locomotives-slot';
  workspace.insertBefore(root, workspace.firstChild);

  const handler = host.editor.speedProfileHandler;
  const panel = handler ? bindSpeedProfilePanel(handler, reveal) : null;
  render(<LocomotivesScreen panel={panel} />, root);

  // Show the picked locomotive's profile in the panel. The panel is asked
  // again only when the profile or the train to name changes, so a poll that
  // changes a train's speed does not reopen it (which would end its run).
  let applied: { profileId: string | null; trainId?: string | null } = { profileId: null };
  const stopShowing = effect(() => {
    const sel = selection.value;
    const target = panelTarget(sel, roster.value, trains.value);
    if (!panel) return;
    if (sel?.kind === 'profile' && sel.fromPanel) {
      applied = { profileId: sel.profileId };
      return;
    }
    if (target.profileId === applied.profileId && target.trainId === applied.trainId) return;
    applied = target;
    if (target.profileId) void panel.show(target.profileId, target.trainId);
    else if (panel.shown()) panel.close();
  });

  // The canvas shows on the right while a run is open or being set up. It
  // is fitted to its new size, and fitted again when it goes back to its
  // usual place on another screen.
  let fittedHere = false;
  const stopCanvas = effect(() => {
    const here = screen.value === 'locomotives';
    const shown = here && calibrationRunShown(calibration.value);
    if (shown) document.documentElement.dataset.locoCanvas = 'shown';
    else delete document.documentElement.dataset.locoCanvas;
    if (shown && !fittedHere) {
      fittedHere = true;
      afterLayout(() => fitCanvas(host));
    } else if (!here && fittedHere) {
      fittedHere = false;
      afterLayout(() => fitCanvas(host));
    }
  });

  return () => {
    stopShowing();
    stopCanvas();
    if (panel?.shown()) panel.close();
    handler?.setHost(null);
    render(null, root);
    root.remove();
    delete document.documentElement.dataset.locoCanvas;
    revealScreen = null;
  };
}
