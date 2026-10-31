/**
 * The Locomotives screen's left column (plan §6): the locomotives the
 * operator owns (the Train Roster), placed trains that came from no roster
 * entry, then the model profiles. Picking a row shows its Speed Profile panel.
 */
import {
  entryName,
  formatNumber,
  modelProfiles,
  placedTrainFor,
  sortedRoster,
  trainsNotInRoster,
  type RosterEntry,
  type Selection,
  type TrainSummary,
} from './model';
import { profiles, roster, selection, trains } from './state';

function isPicked(sel: Selection, kind: string, id: string): boolean {
  if (!sel || sel.kind !== kind) return false;
  if (sel.kind === 'roster') return sel.entryId === id;
  if (sel.kind === 'train') return sel.trainId === id;
  if (sel.kind === 'profile') return sel.profileId === id;
  return false;
}

/** The muted second line of a roster row. Inventory fields still to be
 *  decided (plan §6) go here as they are added to the Train Roster. */
function rosterRowDetail(entry: RosterEntry): string {
  return [entry.model && entry.model !== entryName(entry) ? entry.model : '', entry.consist_length_cm != null ? formatNumber(entry.consist_length_cm, 'cm') : '']
    .filter(Boolean)
    .join(' · ');
}

function TrainChip({ train }: { train: TrainSummary | null }) {
  if (!train) return <span class="chip chip-muted" title="Not on the layout">not placed</span>;
  const calibrating = train.status === 'CALIBRATING';
  return (
    <span class={`chip ${calibrating ? 'chip-info' : ''}`} title={`On the layout as ${train.id}, ${train.status}`}>
      {train.name} · {train.status}
    </span>
  );
}

export function LocomotiveList() {
  const sel = selection.value;
  const entries = sortedRoster(roster.value);
  const loose = trainsNotInRoster(trains.value, roster.value);
  const models = modelProfiles(profiles.value);
  const pick = (next: Selection) => {
    selection.value = next;
  };

  return (
    <aside class="nloco-list" aria-label="Locomotives">
      <div class="nloco-head">
        <h2>Locomotives</h2>
        <span class="chip">{entries.length}</span>
      </div>
      <div class="nloco-list-body">
        {entries.length === 0 ? (
          <div class="empty-state-action">
            <div>No locomotives yet. Add the ones you own.</div>
            <button type="button" class="btn btn-accent btn-sm" onClick={() => pick({ kind: 'new' })}>
              Add locomotive
            </button>
          </div>
        ) : (
          <ul class="nloco-rows">
            {entries.map((entry) => {
              const train = placedTrainFor(entry, trains.value);
              const detail = rosterRowDetail(entry);
              return (
                <li key={entry.entry_id}>
                  <button
                    type="button"
                    class={isPicked(sel, 'roster', entry.entry_id) ? 'nloco-row row-selected' : 'nloco-row'}
                    aria-pressed={isPicked(sel, 'roster', entry.entry_id)}
                    onClick={() => pick({ kind: 'roster', entryId: entry.entry_id })}
                  >
                    <span class="nloco-row-name">{entryName(entry)}</span>
                    <TrainChip train={train} />
                    {detail && <span class="nloco-row-detail">{detail}</span>}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
        {entries.length > 0 && (
          <button
            type="button"
            class={sel?.kind === 'new' ? 'nloco-add row-selected' : 'nloco-add'}
            onClick={() => pick({ kind: 'new' })}
          >
            <i class="ti ti-plus" aria-hidden="true" /> Add locomotive
          </button>
        )}

        {loose.length > 0 && (
          <>
            <div class="nloco-group-head" title="Placed trains that were not put on the layout from a roster entry">
              On the layout, not in the roster
            </div>
            <ul class="nloco-rows">
              {loose.map((train) => (
                <li key={train.id}>
                  <button
                    type="button"
                    class={isPicked(sel, 'train', train.id) ? 'nloco-row row-selected' : 'nloco-row'}
                    aria-pressed={isPicked(sel, 'train', train.id)}
                    onClick={() => pick({ kind: 'train', trainId: train.id })}
                  >
                    <span class="nloco-row-name font-mono">{train.name}</span>
                    <TrainChip train={train} />
                    {train.speedProfileId && <span class="nloco-row-detail font-mono">{train.speedProfileId}</span>}
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}

        <div class="nloco-group-head">Profiles (models)</div>
        <ul class="nloco-rows">
          {models.map((p) => (
            <li key={p.profile_id}>
              <button
                type="button"
                class={isPicked(sel, 'profile', p.profile_id) ? 'nloco-row row-selected' : 'nloco-row'}
                aria-pressed={isPicked(sel, 'profile', p.profile_id)}
                onClick={() => pick({ kind: 'profile', profileId: p.profile_id })}
              >
                <span class="nloco-row-name font-mono">{p.profile_id}</span>
                {p.calibrated_at ? (
                  <span class="chip chip-success" title={`Measured ${p.calibrated_at}`}>measured</span>
                ) : (
                  <span class="chip chip-muted" title="No speed calibration has written this curve">not measured</span>
                )}
                {(p.model || p.name) && <span class="nloco-row-detail">{p.model || p.name}</span>}
              </button>
            </li>
          ))}
        </ul>
      </div>
    </aside>
  );
}
