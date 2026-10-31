/**
 * The top of the Locomotives screen's middle column: what the picked row is,
 * before its Speed Profile panel. A roster entry shows the fields the spec
 * gives a `RosterEntry` (design_new.md "Train Roster") and can be edited or
 * deleted; Add locomotive shows the same fields as an empty form.
 */
import type { ComponentChildren } from 'preact';
import { useState } from 'preact/hooks';

import { showConfirmModal } from '../../lib/helpers';
import { showToast } from '../../lib/toast';
import {
  emptyRosterForm,
  entryName,
  formatNumber,
  formatPrice,
  rosterFormFrom,
  rosterPayload,
  type RosterEntry,
  type RosterForm,
  type TrainSummary,
} from './model';
import { deleteRosterEntry, saveRosterEntry } from './roster-api';
import { roster, selection } from './state';

function Field({ label, children }: { label: string; children: ComponentChildren }) {
  return (
    <div class="tm-field">
      <dt class="tm-field-label">{label}</dt>
      <dd class="tm-field-value">{children}</dd>
    </div>
  );
}

function OnLayout({ train }: { train: TrainSummary | null }) {
  if (!train) return <span class="nloco-muted">Not placed</span>;
  return (
    <>
      <span class="font-mono">{train.name}</span> <span class="nloco-muted">· {train.status}</span>
    </>
  );
}

/** Keep the roster signal in step with a write until the next poll brings it. */
function rememberEntry(entry: RosterEntry): void {
  roster.value = [...roster.peek().filter((e) => e.entry_id !== entry.entry_id), entry];
}

export function RosterEntryDetails({ entry, train }: { entry: RosterEntry; train: TrainSummary | null }) {
  const [editing, setEditing] = useState(false);
  if (editing) return <RosterEntryForm existing={entry} onDone={() => setEditing(false)} />;

  const remove = async () => {
    const confirmed = await showConfirmModal({
      title: 'Delete locomotive',
      message: `Delete "${entryName(entry)}" from the Train Roster?`,
      detail: 'A train already placed from it stays on the layout, and its speed profile is kept.',
      confirmText: 'Delete',
      confirmVariant: 'danger',
    });
    if (!confirmed) return;
    try {
      await deleteRosterEntry(entry.entry_id);
      roster.value = roster.peek().filter((e) => e.entry_id !== entry.entry_id);
      selection.value = null;
    } catch (err: any) {
      showToast(err.message || 'Failed to delete the locomotive', 'error', 5000);
    }
  };

  return (
    <section class="nloco-card" aria-label="Locomotive">
      <div class="nloco-card-head">
        <span class="nloco-card-kind">Locomotive</span>
        <span class="nloco-card-actions">
          <button type="button" class="rr-flat-btn" onClick={() => setEditing(true)}>
            Edit
          </button>
          <button type="button" class="rr-flat-btn rr-flat-btn-danger" onClick={() => void remove()}>
            Delete
          </button>
        </span>
      </div>
      <dl class="nloco-fields">
        <Field label="Model">{entry.model || <span class="nloco-muted">—</span>}</Field>
        <Field label="Purchased">{entry.purchase_date || <span class="nloco-muted">—</span>}</Field>
        <Field label="Price">{formatPrice(entry.purchase_price)}</Field>
        <Field label="Length">{formatNumber(entry.consist_length_cm, 'cm')}</Field>
        <Field label="Top speed">{formatNumber(entry.max_speed, 'km/h')}</Field>
        <Field label="Min curve radius">{formatNumber(entry.min_radius_cm, 'cm')}</Field>
        <Field label="Max grade">{entry.max_slope ? `${entry.max_slope} %` : <span class="nloco-muted">not set</span>}</Field>
        <Field label="Speed profile">
          {entry.speed_profile_id ? (
            <span class="font-mono">{entry.speed_profile_id}</span>
          ) : (
            <span
              class="nloco-muted"
              title="No model profile carried this model when the entry was written, so it names none. A train placed from it runs on a profile of its own."
            >
              none linked
            </span>
          )}
        </Field>
        <Field label="On the layout">
          <OnLayout train={train} />
        </Field>
      </dl>
    </section>
  );
}

export function PlacedTrainDetails({ train }: { train: TrainSummary }) {
  return (
    <section class="nloco-card" aria-label="Train">
      <div class="nloco-card-head">
        <span class="nloco-card-kind">Placed train, not in the Train Roster</span>
      </div>
      <dl class="nloco-fields">
        <Field label="Train">
          <span class="font-mono">{train.id}</span>
        </Field>
        <Field label="Model">{train.model || <span class="nloco-muted">—</span>}</Field>
        <Field label="Length">{formatNumber(train.lengthCm, 'cm')}</Field>
        <Field label="Status">{train.status}</Field>
        <Field label="Speed profile">
          {train.speedProfileId ? <span class="font-mono">{train.speedProfileId}</span> : <span class="nloco-muted">—</span>}
        </Field>
      </dl>
    </section>
  );
}

const FIELDS: ReadonlyArray<{ key: keyof RosterForm; label: string; type: string; unit?: string; placeholder?: string }> = [
  { key: 'name', label: 'Name', type: 'text', placeholder: 'e.g. Class 47 #2' },
  { key: 'model', label: 'Model', type: 'text', placeholder: 'As written on the box' },
  { key: 'purchase_date', label: 'Purchase date', type: 'date' },
  { key: 'purchase_price', label: 'Purchase price', type: 'number' },
  { key: 'consist_length_cm', label: 'Length', type: 'number', unit: 'cm' },
  { key: 'max_speed', label: 'Top speed', type: 'number', unit: 'km/h' },
  { key: 'min_radius_cm', label: 'Min curve radius', type: 'number', unit: 'cm' },
  { key: 'max_slope', label: 'Max grade', type: 'number', unit: '%' },
];

/** Add a locomotive (`existing` null) or edit one. */
export function RosterEntryForm({ existing, onDone }: { existing: RosterEntry | null; onDone?: () => void }) {
  const [form, setForm] = useState<RosterForm>(() => (existing ? rosterFormFrom(existing) : emptyRosterForm()));
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  const submit = async (e: Event) => {
    e.preventDefault();
    const { body, errors: found } = rosterPayload(form, existing);
    setErrors(found);
    if (!body) return;
    setBusy(true);
    try {
      const saved = await saveRosterEntry(body);
      rememberEntry(saved);
      selection.value = { kind: 'roster', entryId: saved.entry_id };
      onDone?.();
    } catch (err: any) {
      setErrors([err.message || 'Failed to save the locomotive']);
    } finally {
      setBusy(false);
    }
  };

  const cancel = () => {
    if (existing) onDone?.();
    else selection.value = null;
  };

  return (
    <form class="nloco-card nloco-form" aria-label={existing ? 'Edit locomotive' : 'Add locomotive'} onSubmit={(e) => void submit(e)}>
      <div class="nloco-card-head">
        <span class="nloco-card-kind">{existing ? 'Edit locomotive' : 'Add locomotive'}</span>
      </div>
      {!existing && (
        <div class="nloco-muted nloco-form-note">
          When a model profile carries this model, the locomotive gets its own profile under it, so it starts from that
          measured curve.
        </div>
      )}
      <div class="nloco-form-fields">
        {FIELDS.map((f) => (
          <div class="input-small-row" key={f.key}>
            <label class="input-small-label input-small-label-inline" for={`nloco-${f.key}`}>
              {f.label}
            </label>
            <input
              id={`nloco-${f.key}`}
              name={f.key}
              type={f.type}
              class="input-small-field"
              value={form[f.key]}
              placeholder={f.placeholder}
              min={f.type === 'number' ? 0 : undefined}
              step={f.type === 'number' ? 'any' : undefined}
              onInput={(e) => setForm({ ...form, [f.key]: (e.currentTarget as HTMLInputElement).value })}
            />
            {f.unit && <span class="at-unit">{f.unit}</span>}
          </div>
        ))}
      </div>
      {errors.length > 0 && (
        <div class="banner banner-warn" role="alert">
          {errors.map((m) => (
            <div key={m}>{m}</div>
          ))}
        </div>
      )}
      <div class="nloco-form-actions">
        <button type="button" class="btn btn-outline btn-sm" onClick={cancel}>
          Cancel
        </button>
        <button type="submit" class="btn btn-accent btn-sm" disabled={busy}>
          {existing ? 'Save' : 'Add locomotive'}
        </button>
      </div>
    </form>
  );
}
