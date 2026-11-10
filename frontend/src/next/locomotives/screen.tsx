/**
 * The Locomotives screen (docs/ui-layout-rewrite-plan.md §6), shown when the
 * Locomotives tab is picked.
 *
 * Left: the trains on the layout, then the models with their sets and the
 * Default profile. Middle: the picked train, set or model, and the Speed
 * Profile panel with the calibration group for a train or a set. Right, only
 * while a
 * calibration run is open or being set up: today's layout canvas, because
 * step 3 names the measured block on the map and the operator watches the
 * pass (static/css/next/locomotives.css moves it there).
 */
import { effect } from '@preact/signals';
import { render } from 'preact';
import { useEffect, useRef } from 'preact/hooks';

import { afterLayout, fitCanvas, type CanvasFitHost } from '../canvas-fit';
import { screen } from '../state';
import { DefaultDetails, ModelDetails, SetDetails, TrainDetails } from './details';
import { LocomotiveList } from './list';
import { calibrationRunShown, modelOf, panelTarget, profileById, selectionForTrain } from './model';
import { bindSpeedProfilePanel, type PanelBinding, type SpeedProfilePanel } from './panel';
import { calibration, models, profiles, selection, trains } from './state';

/** What the screen needs from App. */
export interface LocomotivesHost extends CanvasFitHost {
  editor: { speedProfileHandler?: SpeedProfilePanel };
}

let revealScreen: (() => void) | null = null;

/**
 * Show the Locomotives screen with this train picked. The Speed Profile panel
 * opens on the train's own profile with the train named in its locomotive
 * row. Operate's Calibrate button calls this (plan §5).
 */
export function openLocomotiveForTrain(trainId: string): void {
  selection.value = selectionForTrain(trainId);
  revealScreen?.();
}

function Middle({ panel }: { panel: PanelBinding | null }) {
  const slot = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (panel && slot.current) slot.current.appendChild(panel.el);
  }, [panel]);

  const sel = selection.value;
  let title = 'Locomotives';
  let top = null;
  if (sel?.kind === 'train') {
    const train = trains.value.find((t) => t.id === sel.trainId);
    if (train) {
      title = train.name;
      top = <TrainDetails key={train.id} train={train} />;
    } else {
      top = <div class="empty-state empty-state-warn">This train is no longer on the layout.</div>;
    }
  } else if (sel?.kind === 'set') {
    const set = profileById(sel.profileId, profiles.value);
    if (set && set.model_id != null) {
      title = `${modelOf(set, models.value)?.name || 'No model'} · ${set.set_name || '—'}`;
      top = <SetDetails key={set.profile_id} set={set} />;
    } else {
      top = <div class="empty-state empty-state-warn">This set is no longer stored.</div>;
    }
  } else if (sel?.kind === 'model') {
    const model = models.value.find((m) => m.model_id === sel.modelId);
    if (model) {
      title = model.name;
      top = <ModelDetails key={model.model_id} model={model} />;
    } else {
      top = <div class="empty-state empty-state-warn">This model is no longer stored.</div>;
    }
  } else if (sel?.kind === 'default') {
    title = 'Default';
    top = <DefaultDetails />;
  }

  return (
    <section class="nloco-main" aria-label="Locomotive">
      <div class="nloco-head">
        <h2>{title}</h2>
      </div>
      <div class="nloco-main-body">
        {!sel && <div class="empty-state">Pick a train, a set or a model on the left.</div>}
        {top}
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
  // again when the profile or the train to name changes, so a poll that
  // changes a train's speed does not reopen it (which would end its run).
  // It is also asked again when it is not showing the picked profile and no
  // load of it is on its way: a load that failed (the server restarting, a
  // train just adopted) would otherwise leave the panel blank until another
  // train is picked. The next network update that changes a train, or a
  // click on the picked row, asks again.
  let applied: { profileId: number | null; trainId?: string | null } = { profileId: null };
  let loading: number | null = null;
  const stopShowing = effect(() => {
    const sel = selection.value;
    const target = panelTarget(sel, trains.value);
    if (!panel) return;
    if (sel?.kind === 'set' && sel.fromPanel) {
      applied = { profileId: sel.profileId };
      return;
    }
    const sameAsAsked = target.profileId === applied.profileId && target.trainId === applied.trainId;
    const drawnOrOnItsWay =
      target.profileId == null || panel.shown() === target.profileId || loading === target.profileId;
    if (sameAsAsked && drawnOrOnItsWay) return;
    applied = target;
    if (target.profileId) {
      const profileId = target.profileId;
      loading = profileId;
      void panel.show(profileId, target.trainId).finally(() => {
        if (loading === profileId) loading = null;
      });
    } else if (panel.shown()) panel.close();
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
