/**
 * Two additions to the Speed Profile panel's calibration group.
 *
 * `CalibrationSteps`, at the top of the group: the five steps of
 * design_new.md "Calibrating a locomotive" with the current step marked. The
 * group's own rows below do the work; this only says where the operator is.
 *
 * `WhileCalibrating`, at the end of the group: while the named train reads
 * `CALIBRATING`, the train commands the spec refuses, disabled with that
 * reason, next to the two that stay available (emergency brake, and power off
 * once stopped), each of which ends the run.
 */
import { useState } from 'preact/hooks';

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

export function CalibrationSteps() {
  const states = calibrationSteps(calibration.value);
  return (
    <div class="nloco-steps">
      <ol class="nloco-step-list" aria-label="Calibrating a locomotive">
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
              {states[i] === 'current' && <span class="nloco-step-hint">{step.hint}</span>}
            </span>
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
