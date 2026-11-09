/**
 * The top of the Locomotives screen's middle column: the picked train, set,
 * model or Default profile, before the Speed Profile panel (design_new.md
 * "SpeedProfile", Models, sets and instance profiles).
 *
 * - A train shows its one name, where its profile came from, a select that
 *   gives it the Default profile or a set, and Reset.
 * - A set shows its name, its model, the trains copied from it, and Delete.
 * - A model shows its name and Delete, which takes its sets.
 * - The Default profile is fixed.
 */
import type { ComponentChildren } from 'preact';
import { useEffect, useState } from 'preact/hooks';

import { api } from '../../lib/api';
import { showConfirmModal } from '../../lib/helpers';
import { profileChoices } from '../../lib/profile-choices';
import { showToast } from '../../lib/toast';
import {
  formatNumber,
  formatStamp,
  measuredAt,
  modelOf,
  profileById,
  setsOf,
  trainSource,
  trainsFrom,
  type ModelSummary,
  type ProfileSummary,
  type TrainSummary,
} from './model';
import { models, profiles, selection, trains } from './state';

function Field({ label, children }: { label: string; children: ComponentChildren }) {
  return (
    <div class="tm-field">
      <dt class="tm-field-label">{label}</dt>
      <dd class="tm-field-value">{children}</dd>
    </div>
  );
}

/** A text value saved when the field loses focus or Enter is pressed. A save
 *  that returns false was cancelled and puts the old value back quietly. */
function InlineText({
  id,
  value,
  placeholder,
  onSave,
}: {
  id: string;
  value: string;
  placeholder: string;
  onSave: (next: string) => Promise<boolean | void>;
}) {
  const [text, setText] = useState(value);
  useEffect(() => setText(value), [value]);
  const commit = async () => {
    const next = text.trim();
    if (next === value) return;
    try {
      if ((await onSave(next)) === false) setText(value);
    } catch (err: any) {
      setText(value);
      showToast(err.message || 'Failed to save', 'error', 5000);
    }
  };
  return (
    <input
      id={id}
      type="text"
      class="input-small-field nloco-inline-input"
      value={text}
      placeholder={placeholder}
      onInput={(e) => setText(e.currentTarget.value)}
      onBlur={() => void commit()}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.currentTarget as HTMLInputElement).blur();
      }}
    />
  );
}

function Measured({ at }: { at: string | null }) {
  return at ? <span>{formatStamp(at)}</span> : <span class="nloco-muted">not measured</span>;
}

// ------------------------------------------------------------------ train ----

export function TrainDetails({ train }: { train: TrainSummary }) {
  const all = profiles.value;
  const [pick, setPick] = useState('');
  useEffect(() => setPick(''), [train.id]);
  const instance = profileById(train.speedProfileId, all);
  const parent = profileById(instance?.parent_profile_id ?? null, all);
  const canReset = instance?.parent_profile_id != null && parent != null;

  // A rename writes the train's `car_number` (design_new.md "Train Entity",
  // A locomotive has one name).
  const rename = async (next: string) => {
    await api.updateTrain(train.id, { car_number: next });
    trains.value = trains.peek().map((t) => (t.id === train.id ? { ...t, carNumber: next, name: next || t.id } : t));
  };
  // Giving the train a profile copies it into the train's own instance
  // profile (design_new.md "SpeedProfile", Giving a train a profile).
  const give = async () => {
    if (!pick) return;
    try {
      await api.assignTrainSpeedProfile(train.id, Number(pick));
      setPick('');
      showToast('Profile copied into this train', 'success', 3000);
    } catch (err: any) {
      showToast(err.message || 'Failed to give the profile', 'error', 5000);
    }
  };
  const reset = async () => {
    const confirmed = await showConfirmModal({
      title: 'Reset profile',
      message: `Copy ${trainSource(train, all, models.value)} back into ${train.name}?`,
      detail: 'Every calibration and change on this train that was not promoted is lost.',
      confirmText: 'Reset',
      confirmVariant: 'danger',
    });
    if (!confirmed) return;
    try {
      await api.resetTrainSpeedProfile(train.id);
      showToast('Profile reset', 'success', 3000);
    } catch (err: any) {
      showToast(err.message || 'Failed to reset', 'error', 5000);
    }
  };

  return (
    <section class="nloco-card" aria-label="Train">
      <div class="nloco-card-head">
        <span class="nloco-card-kind">Train</span>
      </div>
      <dl class="nloco-fields">
        <Field label="Name">
          <InlineText id="nloco-name" value={train.carNumber} placeholder={train.id} onSave={rename} />
        </Field>
        <Field label="Profile from">
          <span>{trainSource(train, all, models.value)}</span>
        </Field>
        <Field label="Measured">
          <Measured at={measuredAt(train.speedProfileId, all)} />
        </Field>
        <Field label="Length">{formatNumber(train.lengthCm, 'cm')}</Field>
        <Field label="Status">{train.status}</Field>
      </dl>
      <div class="nloco-card-row">
        <select
          id="nloco-give-profile"
          class="input-small-field"
          value={pick}
          onChange={(e) => setPick(e.currentTarget.value)}
          aria-label="Profile to copy into this train"
        >
          <option value="">Give it another profile…</option>
          {profileChoices(all, models.value).map((o) => (
            <option key={o.id} value={o.id}>
              {o.label}
            </option>
          ))}
        </select>
        <button type="button" class="btn btn-outline btn-sm" disabled={!pick} onClick={() => void give()}>
          Copy in
        </button>
        <button
          type="button"
          class="btn btn-outline btn-sm"
          disabled={!canReset}
          title={canReset ? 'Copy the profile this train got back into it' : 'Its set was deleted, or it came from a scene'}
          onClick={() => void reset()}
        >
          Reset
        </button>
      </div>
    </section>
  );
}

// -------------------------------------------------------------------- set ----

export function SetDetails({ set }: { set: ProfileSummary }) {
  const all = profiles.value;
  const model = modelOf(set, models.value);
  const from = trainsFrom(set.profile_id, trains.value, all);
  const rename = async (next: string) => {
    if (!next) throw new Error('A set needs a name');
    await api.updateSpeedProfile(set.profile_id, { set_name: next });
    profiles.value = profiles.peek().map((p) => (p.profile_id === set.profile_id ? { ...p, set_name: next } : p));
  };
  const remove = async () => {
    const confirmed = await showConfirmModal({
      title: 'Delete set',
      message: `Delete ${model?.name || 'this model'} · ${set.set_name || '—'}?`,
      detail: from.length
        ? `${from.map((t) => t.name).join(', ')} keep their own copies, but can no longer be reset to it. This cannot be undone.`
        : 'This cannot be undone.',
      confirmText: 'Delete',
      confirmVariant: 'danger',
    });
    if (!confirmed) return;
    try {
      await api.deleteSpeedProfile(set.profile_id);
      profiles.value = profiles.peek().filter((p) => p.profile_id !== set.profile_id);
      selection.value = null;
    } catch (err: any) {
      showToast(err.message || 'Failed to delete the set', 'error', 5000);
    }
  };
  return (
    <section class="nloco-card" aria-label="Set">
      <div class="nloco-card-head">
        <span class="nloco-card-kind">Set</span>
        <span class="nloco-card-actions">
          <button type="button" class="rr-flat-btn rr-flat-btn-danger" onClick={() => void remove()}>
            Delete
          </button>
        </span>
      </div>
      <dl class="nloco-fields">
        <Field label="Set name">
          <InlineText id="nloco-set-name" value={set.set_name} placeholder="e.g. 1" onSave={rename} />
        </Field>
        <Field label="Model">
          {model ? (
            <button
              type="button"
              class="rr-flat-btn"
              onClick={() => (selection.value = { kind: 'model', modelId: model.model_id })}
            >
              {model.name}
            </button>
          ) : (
            <span class="nloco-muted">No model</span>
          )}
        </Field>
        <Field label="Measured">
          <Measured at={set.calibrated_at} />
        </Field>
        <Field label="Trains from it">
          {from.length ? <span>{from.map((t) => t.name).join(', ')}</span> : <span class="nloco-muted">none</span>}
        </Field>
      </dl>
      <div class="nloco-muted nloco-form-note">
        A train given this set runs on its own copy, so a change here reaches no train until it is given the set again
        or reset.
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ model ----

export function ModelDetails({ model }: { model: ModelSummary }) {
  const all = profiles.value;
  const sets = setsOf(model.model_id, all);
  const rename = async (next: string) => {
    if (!next) throw new Error('A model needs a name');
    await api.renameLocomotiveModel(model.model_id, next);
    models.value = models.peek().map((m) => (m.model_id === model.model_id ? { ...m, name: next } : m));
  };
  const remove = async () => {
    const confirmed = await showConfirmModal({
      title: 'Delete model',
      message: `Delete ${model.name}${sets.length ? ` and its ${sets.length === 1 ? 'set' : `${sets.length} sets`}` : ''}?`,
      detail: 'Trains on the layout keep their own copies, but those copied from these sets can no longer be reset. This cannot be undone.',
      confirmText: 'Delete',
      confirmVariant: 'danger',
    });
    if (!confirmed) return;
    try {
      await api.deleteLocomotiveModel(model.model_id);
      models.value = models.peek().filter((m) => m.model_id !== model.model_id);
      profiles.value = profiles.peek().filter((p) => p.model_id !== model.model_id);
      selection.value = null;
    } catch (err: any) {
      showToast(err.message || 'Failed to delete the model', 'error', 5000);
    }
  };
  return (
    <section class="nloco-card" aria-label="Model">
      <div class="nloco-card-head">
        <span class="nloco-card-kind">Model</span>
        <span class="nloco-card-actions">
          <button type="button" class="rr-flat-btn rr-flat-btn-danger" onClick={() => void remove()}>
            Delete
          </button>
        </span>
      </div>
      <dl class="nloco-fields">
        <Field label="Name">
          <InlineText id="nloco-model-name" value={model.name} placeholder="As written on the box" onSave={rename} />
        </Field>
        <Field label="Sets">
          {sets.length ? (
            <span class="nloco-inline">
              {sets.map((s) => (
                <button
                  key={s.profile_id}
                  type="button"
                  class="rr-flat-btn"
                  onClick={() => (selection.value = { kind: 'set', profileId: s.profile_id })}
                >
                  {s.set_name || '—'}
                </button>
              ))}
            </span>
          ) : (
            <span class="nloco-muted">none</span>
          )}
        </Field>
      </dl>
    </section>
  );
}

// ---------------------------------------------------------------- default ----

export function DefaultDetails() {
  const from = trainsFrom(0, trains.value, profiles.value);
  return (
    <section class="nloco-card" aria-label="Default profile">
      <div class="nloco-card-head">
        <span class="nloco-card-kind">Default profile</span>
      </div>
      <dl class="nloco-fields">
        <Field label="Trains from it">
          {from.length ? <span>{from.map((t) => t.name).join(', ')}</span> : <span class="nloco-muted">none</span>}
        </Field>
      </dl>
      <div class="nloco-muted nloco-form-note">
        The Default profile is fixed. Any train may start from it; its curve is drawn to each train's own top speed.
      </div>
    </section>
  );
}
