/**
 * The Locomotives screen's left column (plan §6): the trains on the layout,
 * then the speed-profile library — the Default profile and every model with
 * its sets (design_new.md "SpeedProfile", Models, sets and instance
 * profiles). Picking a train or a set shows its Speed Profile panel; picking a
 * model or the Default shows its card.
 *
 * Rows show names, never ids: a train by its one name, a set by its model and
 * set name (design_new.md "Train Entity", A locomotive has one name;
 * "SpeedProfile", Profile and model ids are numbers).
 */
import {
  measuredAt,
  placedTrains,
  setsOf,
  sortedModels,
  trainSource,
  trainsFrom,
  type ModelSummary,
  type ProfileSummary,
  type Selection,
  type TrainSummary,
} from './model';
import { models, profiles, selection, trains } from './state';

function isPicked(sel: Selection, next: Selection): boolean {
  if (!sel || !next || sel.kind !== next.kind) return false;
  if (sel.kind === 'train' && next.kind === 'train') return sel.trainId === next.trainId;
  if (sel.kind === 'set' && next.kind === 'set') return sel.profileId === next.profileId;
  if (sel.kind === 'model' && next.kind === 'model') return sel.modelId === next.modelId;
  return sel.kind === 'default';
}

/** Whether the curve a profile runs on was measured. */
function MeasuredChip({ at }: { at: string | null }) {
  return at ? (
    <span class="chip chip-success" title={`Measured ${at}`}>
      measured
    </span>
  ) : (
    <span class="chip chip-muted" title="No speed calibration has written this curve">
      not measured
    </span>
  );
}

function StatusChip({ train }: { train: TrainSummary }) {
  const calibrating = train.status === 'CALIBRATING';
  return (
    <span class={`chip ${calibrating ? 'chip-info' : ''}`} title={`On the layout, ${train.status}`}>
      {train.status}
    </span>
  );
}

function Row({
  pick,
  sel,
  next,
  name,
  muted,
  indent,
  chips,
  detail,
}: {
  pick: (s: Selection) => void;
  sel: Selection;
  next: Selection;
  name: string;
  muted?: boolean;
  indent?: boolean;
  chips?: preact.ComponentChildren;
  detail?: string;
}) {
  const picked = isPicked(sel, next);
  const cls = ['nloco-row', picked ? 'row-selected' : '', indent ? 'nloco-row-indent' : ''].filter(Boolean).join(' ');
  return (
    <li>
      <button type="button" class={cls} aria-pressed={picked} onClick={() => pick(next)}>
        <span class={muted ? 'nloco-row-name nloco-muted' : 'nloco-row-name'}>{name}</span>
        {chips && <span class="nloco-row-chips">{chips}</span>}
        {detail && <span class="nloco-row-detail">{detail}</span>}
      </button>
    </li>
  );
}

function usedBy(names: TrainSummary[]): string {
  return names.length ? `Used by ${names.map((t) => t.name).join(', ')}` : 'Used by no train';
}

function ModelRows({
  model,
  sel,
  pick,
  all,
}: {
  model: ModelSummary;
  sel: Selection;
  pick: (s: Selection) => void;
  all: ProfileSummary[];
}) {
  const sets = setsOf(model.model_id, all);
  return (
    <>
      <Row
        pick={pick}
        sel={sel}
        next={{ kind: 'model', modelId: model.model_id }}
        name={model.name}
        detail={sets.length === 1 ? '1 set' : `${sets.length} sets`}
      />
      {sets.map((s) => (
        <Row
          key={s.profile_id}
          pick={pick}
          sel={sel}
          next={{ kind: 'set', profileId: s.profile_id }}
          name={`Set ${s.set_name || '—'}`}
          indent
          chips={<MeasuredChip at={s.calibrated_at} />}
          detail={usedBy(trainsFrom(s.profile_id, trains.value, all))}
        />
      ))}
    </>
  );
}

export function LocomotiveList() {
  const sel = selection.value;
  const onLayout = placedTrains(trains.value);
  const all = profiles.value;
  const pick = (next: Selection) => {
    selection.value = next;
  };

  return (
    <aside class="nloco-list" aria-label="Locomotives">
      <div class="nloco-head">
        <h2>Trains</h2>
        <span class="chip">{onLayout.length}</span>
      </div>
      <div class="nloco-list-body">
        {onLayout.length === 0 ? (
          <div class="empty-state">No trains on the layout. Place one in Operate.</div>
        ) : (
          <ul class="nloco-rows">
            {onLayout.map((t) => (
              <Row
                key={t.id}
                pick={pick}
                sel={sel}
                next={{ kind: 'train', trainId: t.id }}
                name={t.name}
                chips={
                  <>
                    <StatusChip train={t} />
                    <MeasuredChip at={measuredAt(t.speedProfileId, all)} />
                  </>
                }
                detail={trainSource(t, all, models.value)}
              />
            ))}
          </ul>
        )}

        <div class="nloco-group-head" title="Saved profiles: the Default, and each model's sets">
          Models
        </div>
        <ul class="nloco-rows">
          <Row
            pick={pick}
            sel={sel}
            next={{ kind: 'default' }}
            name="Default"
            detail={usedBy(trainsFrom(0, trains.value, all))}
          />
          {sortedModels(models.value).map((m) => (
            <ModelRows key={m.model_id} model={m} sel={sel} pick={pick} all={all} />
          ))}
        </ul>
        {models.value.length === 0 && (
          <div class="nloco-muted nloco-form-note">
            No models yet. A model and its first set are made when you Promote a finished speed pass.
          </div>
        )}
      </div>
    </aside>
  );
}
