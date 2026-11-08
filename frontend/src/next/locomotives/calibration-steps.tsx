/**
 * Two additions to the Speed Profile panel's calibration group.
 *
 * `CalibrationSteps`, at the top of the group: the seven steps of
 * design_new.md "Calibrating a locomotive" with the current step marked. Each
 * step holds the group's row that does it (`STEP_ROWS`), moved in from the
 * panel's own HTML, so the operator reads a step's title and finds its
 * control right under it. A step that is not current shows its row's head
 * line only (the value and the button that opens the run); the current
 * step's open run shows its working controls there too.
 *
 * `WhileCalibrating`, at the end of the group: while the named train reads
 * `CALIBRATING`, the train commands the spec refuses, disabled with that
 * reason, next to the two that stay available (emergency brake, and power off
 * once stopped), each of which ends the run.
 */
import { useLayoutEffect, useRef, useState } from 'preact/hooks';

import type { CalibrationGroupSummary } from '../../editor/editor-speed-profiles';
import { api } from '../../lib/api';
import { showToast } from '../../lib/toast';
import {
  CALIBRATION_STEPS,
  REFUSED_WHILE_CALIBRATING,
  calibrationSteps,
  refusedReason,
  type TrainSummary,
} from './model';
import { calibration, trains } from './state';

/**
 * The panel rows each step holds, by step index. The speed row follows the
 * speed pass: step 2 names the measured block and starts it, steps 3 and 4
 * hold its two trials, step 5 its sweep, and step 6 the result to commit. The
 * lighting-voltage field sits with the lighting trial, and the ATO minimum row
 * is step 7.
 */
function stepRows(summary: CalibrationGroupSummary | null): string[][] {
  const stage: Record<string, number> = { lighting_trial: 2, starting_trial: 3, sweep: 4 };
  const view = summary?.view;
  const speed =
    view === 'speed-running' ? stage[summary?.passStage || ''] ?? 2 : view === 'speed-held' ? 5 : 1;
  const rows: string[][] = [
    ['.sp-cal-train-row', '#sp-cal-readiness'],
    [],
    ['#sp-lighting-row'],
    [],
    [],
    [],
    ['#sp-ato-row'],
  ];
  rows[speed].push('#sp-speed-row');
  return rows;
}

export function CalibrationSteps() {
  const summary = calibration.value;
  const states = calibrationSteps(summary);
  // The hint says how to start the current step. Once a run is open, the
  // run's own controls and text say what to do.
  const noRunOpen = !summary || summary.view === 'none';
  const list = useRef<HTMLOListElement>(null);

  // The panel draws its rows; Preact draws the steps. After each draw, put
  // every row into its step. The rows keep their ids, so the panel goes on
  // finding and redrawing them where they are.
  useLayoutEffect(() => {
    const ol = list.current;
    const group = ol?.closest('#sp-cal-group');
    if (!ol || !group) return;
    stepRows(summary).forEach((selectors, i) => {
      const body = ol.querySelector<HTMLElement>(`[data-step-body="${i}"]`);
      for (const sel of selectors) {
        const row = group.querySelector(sel);
        if (body && row && row.parentElement !== body) body.appendChild(row);
      }
    });
  });

  return (
    <div class="nloco-steps">
      <ol class="nloco-step-list" aria-label="Calibrating a locomotive" ref={list}>
        {CALIBRATION_STEPS.map((step, i) => (
          <li
            key={step.title}
            class={`nloco-step is-${states[i]}`}
            aria-current={states[i] === 'current' ? 'step' : undefined}
          >
            <span class="nloco-step-mark" aria-hidden="true">
              {states[i] === 'done' ? <i class="ti ti-check" /> : i + 1}
            </span>
            <span class="nloco-step-text">
              <span class="nloco-step-title">{step.title}</span>
              {states[i] === 'current' && noRunOpen && <span class="nloco-step-hint">{step.hint}</span>}
            </span>
            <div class="nloco-step-body" data-step-body={i} />
          </li>
        ))}
      </ol>
    </div>
  );
}

export function WhileCalibrating() {
  const summary = calibration.value;
  const train = summary?.trainId ? trains.value.find((t) => t.id === summary.trainId) ?? null : null;
  if (train?.status !== 'CALIBRATING') return null;
  return <RefusedCommands train={train} />;
}

function RefusedCommands({ train }: { train: TrainSummary }) {
  const [busy, setBusy] = useState(false);
  const reason = refusedReason(train.name);
  const run = async (label: string, send: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await send();
    } catch (err: any) {
      showToast(err.message || `${label} failed`, 'error', 5000);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div class="nloco-refused" role="group" aria-label={`${train.name} while calibrating`}>
      <div class="nloco-refused-head">
        <span class="chip chip-state chip-info">CALIBRATING</span>
        <span>The run drives {train.name}. These train commands are refused until it ends:</span>
      </div>
      <div class="nloco-refused-buttons">
        {REFUSED_WHILE_CALIBRATING.map((label) => (
          <button key={label} type="button" class="btn btn-outline btn-sm" disabled title={reason}>
            {label}
          </button>
        ))}
      </div>
      <div class="nloco-muted">{reason}</div>
      <div class="nloco-refused-buttons">
        <button
          type="button"
          class="btn btn-danger btn-sm"
          disabled={busy}
          title="Raise the emergency brake. It ends the run; nothing is written."
          onClick={() => void run('Emergency brake', () => api.emergencyBrake(train.id))}
        >
          Emergency brake
        </button>
        <button
          type="button"
          class="btn btn-outline btn-sm"
          disabled={busy || !train.stopped}
          title={
            train.stopped
              ? 'Power the train off. It ends the run; nothing is written.'
              : 'Power off is accepted only while the train is stopped.'
          }
          onClick={() => void run('Power off', () => api.trainPowerOff(train.id))}
        >
          Power off
        </button>
      </div>
      <div class="nloco-muted">Both stay available, and each ends the run.</div>
    </div>
  );
}
