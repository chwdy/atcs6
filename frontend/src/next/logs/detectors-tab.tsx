/**
 * The Logs screen's Detectors tab: a live read-back of what the occupancy
 * detectors see, one row per detected block. It replaced the old Raw
 * Occupancy window; its helpers are in panels/occupancy-monitor.ts. A click
 * on a block on the canvas marks its row and scrolls it into view
 * (`pickedBlockId` in ./state.ts).
 *
 * It reads `GET /api/hardware/occupancy/raw`, which reports recorded state and
 * sends nothing on the bus, so reading it fast cannot disturb the detectors it
 * reports on. Each row shows the read before debouncing beside the confirmed
 * verdict, which detector answered, what it measured, and the drive the block
 * is getting. The rows are built by the pure `buildRows` in
 * panels/occupancy-monitor.ts; the styles are that panel's `.occ-*` classes
 * (features/occupancy-monitor.css).
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'preact/hooks';

import { api } from '../../lib/api';
import { stateManager } from '../../lib/state-manager';
import {
  FILTER_OPTIONS,
  FLASH_MS,
  RAIL_SAMPLES,
  REFRESH_OPTIONS,
  buildRows,
  changedBlocks,
  describeLink,
  summarizeRailReadings,
  type OccupancyFilter,
  type OccupancyRow,
  type RawOccupancySnapshot,
} from '../../panels/occupancy-monitor';
import { pickedBlockId, readPrefs, writePrefs } from './state';
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

  // Rail voltage: what each block's current sensor measures on the rails. Each
  // reading is a bus transaction, so it runs only when the button is pressed,
  // never on the refresh above.
  const [rail, setRail] = useState<Record<string, Array<number | null>>>({});
  const [railBusy, setRailBusy] = useState(false);
  const readRail = async () => {
    const ids = Object.keys(last.current?.blocks || {}).sort();
    if (!ids.length || railBusy) return;
    setRailBusy(true);
    const next: Record<string, Array<number | null>> = {};
    try {
      for (const id of ids) {
        const samples: Array<number | null> = [];
        for (let i = 0; i < RAIL_SAMPLES; i++) {
          let volts: number | null = null;
          try {
            const res: any = await api.getBlockVoltage(id);
            volts = typeof res?.voltage === 'number' ? res.voltage : null;
          } catch {
            volts = null;
          }
          samples.push(volts);
          // No sensor, or a failed read: reading again would only repeat it
          // (and its warning in the event log).
          if (volts === null) break;
        }
        next[id] = samples;
      }
      setRail(next);
    } finally {
      setRailBusy(false);
    }
  };

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
        <button
          class="btn btn-secondary btn-sm occ-rail-btn"
          type="button"
          disabled={railBusy || !total}
          title={`Read the voltage on each block's rails from its current sensor, ${RAIL_SAMPLES} times, and show the average`}
          onClick={() => void readRail()}
        >
          {railBusy ? 'Reading…' : 'Read rail voltage'}
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
          rail={rail}
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
  rail: Record<string, Array<number | null>>;
}) {
  const { snapshot, error, filter, flashing, flips, total, rail } = props;
  const picked = pickedBlockId.value;
  if (error) return <div class="empty-state empty-state-error">{error}</div>;
  if (!snapshot) return <div class="empty-state">Reading…</div>;
  const rows = buildRows(snapshot, filter);
  // The block picked on the canvas may have no detector, or this filter may
  // leave it out; say so instead of marking nothing.
  const pickedMissing = picked && !rows.some((row) => row.blockId === picked);
  const missingNote = pickedMissing && (
    <div class="occ-picked-note">
      {snapshot.blocks?.[picked]
        ? `${picked} is hidden by the Show filter.`
        : `${picked} has no detector.`}
    </div>
  );
  if (!rows.length) {
    return (
      <>
        {missingNote}
        <div class="empty-state">
          {total
            ? 'No block matches this filter.'
            : 'No block is being detected. Bind occupancy inputs in Hardware Setup.'}
        </div>
      </>
    );
  }
  return (
    <>
      {missingNote}
      <div class="occ-head">
        <span>Block</span>
        <span>Source</span>
        <span>Reading</span>
        <span>Drive</span>
        <span title="The voltage measured on the rails by the block's current sensor. Press Read rail voltage to read it.">Rail</span>
        <span class="occ-col-state" title="Raw read · Confirmed verdict">
          Raw · Conf
        </span>
      </div>
      {rows.map((row) => (
        <DetectorRow
          key={`${row.blockId}#${flips.get(row.blockId) ?? 0}`}
          row={row}
          flashing={flashing.has(row.blockId)}
          picked={row.blockId === picked}
          rail={rail[row.blockId]}
        />
      ))}
    </>
  );
}

function DetectorRow({
  row,
  flashing,
  picked,
  rail,
}: {
  row: OccupancyRow;
  flashing: boolean;
  picked: boolean;
  /** This block's rail voltage readings, or undefined before the first press. */
  rail?: Array<number | null>;
}) {
  const railCell = rail ? summarizeRailReadings(rail) : null;
  const el = useRef<HTMLDivElement>(null);
  // Scroll the picked row into view when it is picked, not on every read.
  useLayoutEffect(() => {
    if (picked) el.current?.scrollIntoView?.({ block: 'nearest' });
  }, [picked]);
  const classes = ['occ-row'];
  if (row.settling) classes.push('occ-row-settling');
  if (flashing) classes.push('occ-row-flash');
  if (picked) classes.push('row-selected');
  // The block's name ("Main", "Left") is what an operator recognises on the
  // layout; the id goes under it so a row still matches the event log.
  const name = String((stateManager.get('blocks', row.blockId) as any)?.name || '').trim();
  return (
    <div class={classes.join(' ')} ref={el}>
      <span class="occ-block" title={row.blockId}>
        {name || row.blockId}
        {name && <span class="occ-block-id">{row.blockId}</span>}
      </span>
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
      <span class="occ-rail" title={railCell?.detail}>
        {railCell ? railCell.text : <span class="occ-dash">—</span>}
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
