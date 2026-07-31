/**
 * Port picker — a popover for choosing which hardware line(s) an entity is wired
 * to, shared by every per-entity Hardware binding slot.
 *
 * A dropdown could not express the real wiring: it listed pre-paired track
 * "ports" (channels 2p / 2p+1), so a block whose two rails sit on non-adjacent
 * PWM channels was unbindable. This picker addresses the chip's raw *lines*
 * instead — a PWM channel, a shift-register bit, a current-sense channel — and
 * fills a set of named slots (a track's forward/reverse rails, a switch's two
 * coils), so each pick is independent and labelled by the role it plays.
 *
 * Opened with `pickPorts(...)`, which resolves to the picked lines when the user
 * saves, or `null` when they cancel. The caller persists them; the picker itself
 * writes no configuration. Double-clicking an output line drives it high on the
 * real controller for a second — long enough to read on a meter — so a wire can
 * be identified before it is bound.
 */
import { api } from '../lib/api';
import { showToast } from '../lib/toast';

/** One named target the picker must fill (a rail, a coil, a detector). */
export interface PickerSlot {
  key: string;
  label: string;
  /** Shown under the label while this slot is the active one. */
  hint?: string;
}

/** A chosen hardware line: which chip, which raw line on it. */
export interface PickedLine {
  chip_id: string;
  line: number;
}

interface PickerChip {
  chip_id: string;
  kind: string;
  role?: string;
  port_kind?: string;
  label?: string;
  line_count?: number;
  port_count?: number;
  bus?: string;
  i2c_address?: number | null;
  spi_select?: string | null;
}

export interface PickPortsOptions {
  /** The hardware config (chips + current bindings). */
  cfg: { chips: PickerChip[]; [k: string]: any };
  /** Element the popover is positioned against. */
  anchor: HTMLElement;
  /** Only chips of this interface role are offered. */
  role: string;
  /** When set, only chips whose port_kind matches are offered. */
  portKind?: string;
  /** Output lines can be pulse-tested by double-click; input lines cannot. */
  direction: 'input' | 'output';
  /** The named targets to fill, in order. */
  slots: PickerSlot[];
  /** Lines already chosen (re-opening an existing binding). */
  initial?: Record<string, PickedLine>;
  /** `chipId::line` -> a label for whatever already claims it, so the picker can
   * grey it out instead of letting the server reject the save. */
  taken?: Record<string, string>;
  title?: string;
}

const POPOVER_ID = 'hw-port-picker';

function esc(s: any): string {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' } as any)[c]);
}

/** How many individually-addressable lines a chip exposes. The backend sends
 * `line_count`; fall back to `port_count` for a chip payload without it. */
function lineCount(chip: PickerChip): number {
  return chip.line_count ?? chip.port_count ?? 0;
}

/** What one line is called on this chip — a PWM channel, an output/input bit, or
 * a 1-based current-sense channel. */
export function lineLabel(chip: PickerChip, line: number): string {
  if (chip.port_kind === 'bipolar_pair') return `ch${line}`;
  if (chip.port_kind === 'current_sense') return `CH${line + 1}`;
  return `P${line}`;
}

/** Where a chip sits on its bus — an I²C address or an SPI latch line. Empty
 * when the chip has neither, which is the one case a name cannot be pinned to a
 * physical part. */
function chipAddress(chip: PickerChip): string {
  if (chip.i2c_address != null) {
    return `0x${Number(chip.i2c_address).toString(16).padStart(2, '0')}`;
  }
  return (chip.spi_select || '').toUpperCase();
}

/** Where a chip sits, for the group header (mirrors the Hardware tab). */
function chipCoordinate(chip: PickerChip): string {
  return chipAddress(chip) || chip.bus || '';
}

/**
 * A chip's name for a one-line reference, e.g. "ina219 0x41".
 *
 * The address is part of the name rather than a detail beside it: a rig with two
 * of the same part reads "ina219" twice over, and the address is the only thing
 * that says which one — the same designator that is on the chip's row in
 * Hardware Setup. The bus is deliberately left out of this form: every chip on a
 * bus shares it, so it tells two of them apart no better than the kind does.
 */
export function chipDisplayName(chip: PickerChip): string {
  const name = chip.label || chip.kind;
  const addr = chipAddress(chip);
  return addr ? `${name} ${addr}` : name;
}

const key = (chipId: string, line: number) => `${chipId}::${line}`;

/**
 * Open the picker over `anchor`. Resolves to the filled slots on save, or null
 * if the user cancels (Escape, the Cancel button, or a click outside).
 */
export function pickPorts(opts: PickPortsOptions): Promise<Record<string, PickedLine> | null> {
  const chips = opts.cfg.chips.filter(
    (c) => c.role === opts.role && (!opts.portKind || c.port_kind === opts.portKind)
  );
  const picked: Record<string, PickedLine> = { ...(opts.initial || {}) };
  // Start on the first slot that still needs a line, so re-opening a complete
  // binding lands on slot 1 for editing rather than on nothing.
  let activeSlot = opts.slots.find((s) => !picked[s.key])?.key || opts.slots[0].key;

  return new Promise((resolve) => {
    document.getElementById(POPOVER_ID)?.remove();
    const el = document.createElement('div');
    el.id = POPOVER_ID;
    el.className = 'pp-popover';
    document.body.appendChild(el);

    let settled = false;
    const close = (result: Record<string, PickedLine> | null) => {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey, true);
      document.removeEventListener('mousedown', onOutside, true);
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
      el.remove();
      resolve(result);
    };

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.stopPropagation(); close(null); }
    };
    const onOutside = (e: MouseEvent) => {
      if (!el.contains(e.target as Node)) close(null);
    };

    /** Keep the popover beside the anchor and inside the viewport. */
    function place() {
      const r = opts.anchor.getBoundingClientRect();
      const w = el.offsetWidth || 360;
      const h = el.offsetHeight || 320;
      const margin = 12;
      const gap = 8;
      // Prefer below the row; flip above when there is not enough room.
      let top = r.bottom + gap;
      if (top + h > window.innerHeight - margin) {
        top = Math.max(margin, r.top - h - gap);
      }
      // Right-align to the row, then clamp.
      let left = Math.min(r.right - w, window.innerWidth - w - margin);
      left = Math.max(margin, left);
      el.style.left = `${Math.round(left)}px`;
      el.style.top = `${Math.round(top)}px`;
    }

    function slotsHtml(): string {
      return opts.slots
        .map((s) => {
          const p = picked[s.key];
          const chip = p && chips.find((c) => c.chip_id === p.chip_id);
          const value = p && chip
            ? `${chipDisplayName(chip)} · ${lineLabel(chip, p.line)}`
            : 'not set';
          const cls = [
            'pp-slot',
            s.key === activeSlot ? 'is-active' : '',
            p ? 'is-filled' : '',
          ].filter(Boolean).join(' ');
          return `<button type="button" class="${cls}" data-slot="${esc(s.key)}">
              <span class="pp-slot-label">${esc(s.label)}</span>
              <span class="pp-slot-value">${esc(value)}</span>
            </button>`;
        })
        .join('');
    }

    function gridHtml(): string {
      if (!chips.length) {
        return `<p class="pp-empty">No chip of this kind is configured. Add one in Hardware&nbsp;Setup first.</p>`;
      }
      // A line already used by one of this picker's own slots, so it can be
      // shown as taken-by-me rather than free.
      const mine = new Map<string, string>();
      for (const s of opts.slots) {
        const p = picked[s.key];
        if (p) mine.set(key(p.chip_id, p.line), s.label);
      }
      return chips
        .map((chip) => {
          const n = lineCount(chip);
          const cells = Array.from({ length: n }, (_, line) => {
            const k = key(chip.chip_id, line);
            const byMe = mine.get(k);
            const byOther = !byMe && opts.taken ? opts.taken[k] : undefined;
            const cls = [
              'pp-line',
              byMe ? 'is-picked' : '',
              byOther ? 'is-taken' : '',
            ].filter(Boolean).join(' ');
            const title = byMe
              ? `${lineLabel(chip, line)} — ${byMe}`
              : byOther
                ? `${lineLabel(chip, line)} — already used by ${byOther}`
                : opts.direction === 'output'
                  ? `${lineLabel(chip, line)} — click to assign, double-click to pulse for 1 s`
                  : `${lineLabel(chip, line)} — click to assign`;
            return `<button type="button" class="${cls}" data-chip="${esc(chip.chip_id)}"
                data-line="${line}" title="${esc(title)}">${esc(lineLabel(chip, line))}</button>`;
          }).join('');
          const coord = chipCoordinate(chip);
          // An unlabelled chip is named by its kind, so repeating the kind in the
          // meta line says nothing — the coordinate alone is what identifies it.
          const name = chip.label || chip.kind;
          const meta = [name === chip.kind ? '' : chip.kind, coord].filter(Boolean).join(' · ');
          return `<div class="pp-chip">
              <div class="pp-chip-head">
                <span class="pp-chip-name">${esc(name)}</span>
                <span class="pp-chip-meta">${esc(meta)}</span>
              </div>
              <div class="pp-grid">${cells}</div>
            </div>`;
        })
        .join('');
    }

    function render() {
      const complete = opts.slots.every((s) => picked[s.key]);
      const active = opts.slots.find((s) => s.key === activeSlot);
      const hint = !chips.length
        ? ''
        : active
          ? `Pick the line for <strong>${esc(active.label)}</strong>.`
          : '';
      el.innerHTML = `
        <div class="pp-head">
          <span class="pp-title">${esc(opts.title || 'Choose ports')}</span>
        </div>
        <div class="pp-slots">${slotsHtml()}</div>
        <div class="pp-body">${gridHtml()}</div>
        <p class="pp-hint">${hint}${
          chips.length && opts.direction === 'output'
            ? ' Double-click a line to drive it high for 1 s — long enough to read on a meter.'
            : ''
        }</p>
        <div class="pp-foot">
          <button type="button" class="btn btn-secondary btn-sm" data-pp="cancel">Cancel</button>
          <button type="button" class="btn btn-accent btn-sm" data-pp="save"${complete ? '' : ' disabled'}>Save</button>
        </div>`;
      place();
    }

    // --- interaction ------------------------------------------------------
    el.addEventListener('click', (e) => {
      const target = e.target as HTMLElement;

      const slotBtn = target.closest<HTMLElement>('.pp-slot');
      if (slotBtn) { activeSlot = slotBtn.dataset.slot!; render(); return; }

      const cell = target.closest<HTMLElement>('.pp-line');
      if (cell) {
        const chipId = cell.dataset.chip!;
        const line = Number(cell.dataset.line);
        if (cell.classList.contains('is-taken')) {
          showToast(opts.taken?.[key(chipId, line)]
            ? `That line is already used by ${opts.taken[key(chipId, line)]}.`
            : 'That line is already in use.', 'warning', 3000);
          return;
        }
        // One line cannot serve two slots (both rails on one channel would short
        // the bridge), so assigning a line clears it from any other slot.
        for (const s of opts.slots) {
          const p = picked[s.key];
          if (p && p.chip_id === chipId && p.line === line) delete picked[s.key];
        }
        picked[activeSlot] = { chip_id: chipId, line };
        const next = opts.slots.find((s) => !picked[s.key]);
        if (next) activeSlot = next.key;
        render();
        return;
      }

      const action = target.closest<HTMLElement>('[data-pp]')?.dataset.pp;
      if (action === 'cancel') { close(null); return; }
      if (action === 'save') {
        if (opts.slots.every((s) => picked[s.key])) close({ ...picked });
      }
    });

    // Double-click an output line to pulse it — the "which wire is this" check.
    el.addEventListener('dblclick', async (e) => {
      if (opts.direction !== 'output') return;
      const cell = (e.target as HTMLElement).closest<HTMLElement>('.pp-line');
      if (!cell) return;
      const chipId = cell.dataset.chip!;
      const line = Number(cell.dataset.line);
      cell.classList.add('is-pulsing');
      try {
        const res = await api.runHardwarePortTest(opts.cfg, chipId, line, 'pulse_output');
        if (!res?.ok) showToast(res?.error || 'Could not pulse that line.', 'error', 3500);
      } catch (err: any) {
        showToast(err?.message || 'Could not pulse that line.', 'error', 3500);
      } finally {
        cell.classList.remove('is-pulsing');
      }
    });

    render();
    // Registered after the opening click has finished, so it does not
    // immediately close the popover it just opened.
    setTimeout(() => {
      document.addEventListener('keydown', onKey, true);
      document.addEventListener('mousedown', onOutside, true);
    }, 0);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
  });
}
