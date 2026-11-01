/**
 * The Train Monitor readings shown above the Diagnostics tab's groups: the
 * ones the old Train Monitor window was the only place to see (plan §13.10).
 * Speed in km/h and cm/s, distance to the next stop, the block's speed
 * limit, the speed limit reason, and a button that opens the raw look-ahead
 * JSON.
 *
 * It reads `GET /api/trains/<id>/lookahead` at the tab's refresh rate, and
 * the train's speed from the shared network snapshot. The numbers are worked
 * out the way panels/train-monitor.ts works them out.
 */
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';

import { api } from '../../lib/api';
import { stateManager } from '../../lib/state-manager';
import { singleFlight, useRatePoll } from './use-rate-poll';

/** Opens a JSON viewer on `data` (the Editor's `showFullJSONViewer`). */
export type ShowJson = (data: unknown, title: string) => void;

/** The model scale the server reports before the first answer. */
const DEFAULT_SCALE = 160;

/** cm/s in one km/h. */
const CMS_PER_KMH = 100000 / 3600;

export interface ReadoutRow {
  label: string;
  value: string;
}

/** A distance the way the Train Monitor writes it: cm below a metre, m above. */
export function formatDistance(cm: number): string {
  return cm < 100 ? `${cm.toFixed(2)} cm` : `${(cm / 100).toFixed(2)} m`;
}

/**
 * The readings for one train. `controller` is its TrainController from the
 * network snapshot; `payload` is the look-ahead answer, or null before the
 * first one. A reading with nothing to report shows a dash, so the rows stay
 * in place while the train runs.
 */
export function readoutRows(controller: any, payload: any, scale: number): ReadoutRow[] {
  const kmh = Number(controller?.current_speed) || 0;
  const cms = (kmh * CMS_PER_KMH) / (scale > 0 ? scale : DEFAULT_SCALE);
  const lookahead = payload?.lookahead ?? null;
  const clearance = Number(lookahead?.clearance_cm) || 0;
  const margin = Number(lookahead?.safety_margin_cm ?? 25);
  const stopCm = Math.max(0, Number(lookahead?.target_stop_cm ?? clearance - margin) || 0);
  const blockLimit = Number(payload?.current_block_speed_limit);
  const reason = payload?.speed_limit_reason ?? controller?.speed_limit_reason ?? null;
  return [
    { label: 'Speed', value: `${Math.round(kmh)} km/h · ${cms.toFixed(1)} cm/s` },
    { label: 'Next stop', value: payload && stopCm > 0 ? formatDistance(stopCm) : '—' },
    { label: 'Block limit', value: blockLimit > 0 ? `${Math.round(blockLimit)} km/h` : '—' },
    { label: 'Speed limit reason', value: reason ? String(reason) : '—' },
  ];
}

export function TrainReadout({ trainId, hz, showJson }: { trainId: string; hz: number; showJson?: ShowJson }) {
  const [payload, setPayload] = useState<any>(null);
  const [scale, setScale] = useState(DEFAULT_SCALE);
  const current = useRef(trainId);
  current.current = trainId;

  useEffect(() => {
    let live = true;
    api
      .getScale()
      .then((data: any) => {
        if (live && Number(data?.scale) > 0) setScale(Number(data.scale));
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => setPayload(null), [trainId]);

  const read = useMemo(
    () =>
      singleFlight(async () => {
        const id = current.current;
        try {
          const next = await api.getTrainLookahead(id);
          if (id === current.current) setPayload(next ?? null);
        } catch {
          // The Diagnostics groups below report a failed read; these
          // readings keep their last values.
        }
      }),
    []
  );
  useRatePoll(read, hz);

  const rows = readoutRows(stateManager.getController(trainId), payload, scale);
  return (
    <div class="nlp-readout" aria-label="Train Monitor readings">
      {rows.map((row) => (
        <div class="nlp-readout-item" key={row.label}>
          <span class="nlp-readout-label">{row.label}</span>
          <span class="nlp-readout-value">{row.value}</span>
        </div>
      ))}
      <button
        type="button"
        class="btn btn-sm nlp-readout-json"
        title="Open the raw look-ahead answer for this train"
        disabled={!payload || !showJson}
        onClick={() => showJson?.(payload, `Look-ahead: ${trainId}`)}
      >
        Look-ahead JSON
      </button>
    </div>
  );
}
