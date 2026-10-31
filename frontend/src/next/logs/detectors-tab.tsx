/**
 * The Logs panel's Detectors tab: a live read-back of what the occupancy
 * detectors see, one row per detected block. It replaces today's Raw
 * Occupancy window (panels/occupancy-monitor.ts) under ?ui=next.
 *
 * It reads `GET /api/hardware/occupancy/raw`, which reports recorded state and
 * sends nothing on the bus, so reading it fast cannot disturb the detectors it
 * reports on. Each row shows the read before debouncing beside the confirmed
 * verdict, which detector answered, what it measured, and the drive the block
 * is getting. The rows are built by the pure `buildRows` in
 * panels/occupancy-monitor.ts; the styles are that panel's `.occ-*` classes
 * (features/occupancy-monitor.css).
 */
import { useEffect, useMemo, useRef, useState } from 'preact/hooks';

import { api } from '../../lib/api';
import {
  FILTER_OPTIONS,
  FLASH_MS,
  REFRESH_OPTIONS,
  buildRows,
  changedBlocks,
  describeLink,
  type OccupancyFilter,
  type OccupancyRow,
  type RawOccupancySnapshot,
} from '../../panels/occupancy-monitor';
import { readPrefs, writePrefs } from './state';
import { singleFlight, useRatePoll } from './use-rate-poll';

const DEFAULT_HZ = 4;

function savedHz(): number {
  const hz = Number(readPrefs().detectors?.refreshHz);
  return REFRESH_OPTIONS.some((o) => o.hz === hz) ? hz : DEFAULT_HZ;
}

function savedFilter(): OccupancyFilter {
  const filter = readPrefs().detectors?.filter;
  return FILTER_OPTIONS.some((o) => o.value === filter) ? (filter as OccupancyFilter) : 'all';
}

export function DetectorsTab() {
  const [hz, setHz] = useState<number>(savedHz);
  const [filter, setFilter] = useState<OccupancyFilter>(savedFilter);
  const [snapshot, setSnapshot] = useState<RawOccupancySnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Blocks whose confirmed verdict just flipped: the event this tab exists
  // to catch, so their rows flash for a moment.
  const [flashing, setFlashing] = useState<ReadonlySet<string>>(new Set());
  // How many times each block's verdict has flipped. It is part of the row's
  // key, so a block that flips again while still flashing gets a new row and
  // the flash plays again.
  const flips = useRef(new Map<string, number>());
  const last = useRef<RawOccupancySnapshot | null>(null);
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (flashTimer.current) clearTimeout(flashTimer.current);
    },
    []
  );

  const read = useMemo(
    () =>
      singleFlight(async () => {
        try {
          const next = (await api.getRawOccupancy()) as RawOccupancySnapshot;
          const changed = changedBlocks(last.current, next);
          last.current = next;
          setSnapshot(next);
          setError(null);
          if (changed.size) {
            for (const id of changed) flips.current.set(id, (flips.current.get(id) ?? 0) + 1);
            setFlashing((prev) => new Set([...prev, ...changed]));
            if (flashTimer.current) clearTimeout(flashTimer.current);
            flashTimer.current = setTimeout(() => {
              flashTimer.current = null;
              setFlashing(new Set());
            }, FLASH_MS);
          }
        } catch (err) {
          setError((err as Error)?.message || String(err));
        }
      }),
    []
  );

  useRatePoll(read, hz);

  const changeRate = (value: string) => {
    const next = Number(value);
    const rate = Number.isFinite(next) ? next : DEFAULT_HZ;
    setHz(rate);
    writePrefs({ detectors: { refreshHz: rate } });
  };

  const changeFilter = (value: OccupancyFilter) => {
    setFilter(value);
    writePrefs({ detectors: { filter: value } });
  };

  const link = describeLink(snapshot?.link);
  const total = Object.keys(snapshot?.blocks || {}).length;

  return (
    <div class="nlp-pane nlp-detectors">
      <div class="occ-toolbar">
        <label class="occ-field">
          Refresh
          <select value={String(hz)} onChange={(e) => changeRate(e.currentTarget.value)}>
            {REFRESH_OPTIONS.map((o) => (
              <option key={o.hz} value={String(o.hz)}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label class="occ-field">
          Show
          <select value={filter} onChange={(e) => changeFilter(e.currentTarget.value as OccupancyFilter)}>
            {FILTER_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <button class="occ-icon-btn" type="button" title="Read now" aria-label="Read now" onClick={() => void read()}>
          ↻
        </button>
      </div>
      {/* The USB link strip shows only when there is a link to report, so in
          Simulation it is absent rather than a permanent row saying nothing. */}
      {link && <div class={`occ-link occ-link-${link.tone}`}>{link.text}</div>}
      <div class="occ-body">
        <DetectorRows
          snapshot={snapshot}
          error={error}
          filter={filter}
          flashing={flashing}
          flips={flips.current}
          total={total}
        />
      </div>
      <div class="occ-footer">
        {snapshot &&
          [
            `read #${snapshot.read_count ?? 0}`,
            `on ≥ ${snapshot.on_threshold ?? '?'}`,
            `off ≥ ${snapshot.off_threshold ?? '?'}`,
            `${total} block${total === 1 ? '' : 's'}`,
          ].join(' · ')}
      </div>
    </div>
  );
}

function DetectorRows(props: {
  snapshot: RawOccupancySnapshot | null;
  error: string | null;
  filter: OccupancyFilter;
  flashing: ReadonlySet<string>;
  flips: ReadonlyMap<string, number>;
  total: number;
}) {
  const { snapshot, error, filter, flashing, flips, total } = props;
  if (error) return <div class="empty-state empty-state-error">{error}</div>;
  if (!snapshot) return <div class="empty-state">Reading…</div>;
  const rows = buildRows(snapshot, filter);
  if (!rows.length) {
    return (
      <div class="empty-state">
        {total
          ? 'No block matches this filter.'
          : 'No block is being detected. Bind occupancy inputs in Hardware Setup.'}
      </div>
    );
  }
  return (
    <>
      <div class="occ-head">
        <span>Block</span>
        <span>Source</span>
        <span>Reading</span>
        <span>Drive</span>
        <span class="occ-col-state" title="Raw read · Confirmed verdict">
          Raw · Conf
        </span>
      </div>
      {rows.map((row) => (
        <DetectorRow
          key={`${row.blockId}#${flips.get(row.blockId) ?? 0}`}
          row={row}
          flashing={flashing.has(row.blockId)}
        />
      ))}
    </>
  );
}

function DetectorRow({ row, flashing }: { row: OccupancyRow; flashing: boolean }) {
  const classes = ['occ-row'];
  if (row.settling) classes.push('occ-row-settling');
  if (flashing) classes.push('occ-row-flash');
  return (
    <div class={classes.join(' ')}>
      <span class="occ-block">{row.blockId}</span>
      <span class="occ-source">
        {row.source && <span class={`chip chip-${row.sourceTone}`}>{row.source}</span>}
      </span>
      {/* The threshold and baseline never change from read to read, so they
          sit in the tooltip instead of a sub-line on every row. */}
      <span class="occ-reading" title={row.readingDetail || undefined}>
        {row.reading || <span class="occ-dash">—</span>}
      </span>
      <span class="occ-drive">
        {row.drive}
        {row.zone && <span class="occ-zone">{row.zone}</span>}
      </span>
      <span class="occ-state">
        <i class={`occ-dot${row.raw ? ' occ-dot-on' : ''}`} title={`Raw read: ${row.raw ? 'present' : 'clear'}`} />
        <i
          class={`occ-dot occ-dot-verdict${row.confirmed ? ' occ-dot-on' : ''}`}
          title={`Confirmed verdict: ${row.confirmed ? 'occupied' : 'clear'}`}
        />
      </span>
      {row.note && <div class="occ-sub">{row.note}</div>}
      {row.debounce && (
        <div class="occ-debounce">
          <span class="occ-debounce-label">
            {row.debounce.label} {row.debounce.count}/{row.debounce.total}
          </span>
          <span class="occ-debounce-track">
            <span class="occ-debounce-fill" style={{ width: `${Math.round(row.debounce.fraction * 100)}%` }} />
          </span>
        </div>
      )}
    </div>
  );
}
