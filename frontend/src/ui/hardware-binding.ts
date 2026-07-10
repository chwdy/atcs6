/**
 * Per-entity Hardware section for the block and switch editors.
 *
 * Renders a small "Hardware" section — a port dropdown per binding slot plus a
 * Test button — and wires it: a dropdown change PUTs one binding
 * (`/api/hardware/binding`), and Test pulses/reads that port. Shown only when
 * real hardware is enabled and a chip of the slot's role is configured, so a
 * pure-simulation layout never sees it. Shared by both editors to keep the
 * port-selection logic in one place.
 */
import { api } from '../lib/api';
import { flashField } from '../lib/helpers';
import { showToast } from '../lib/toast';

interface Binding { chip_id: string; port: number; }
interface CurrentBinding extends Binding {
  baseline_ma?: number | null;
  threshold_ma?: number | null;
}
interface HwChip {
  chip_id: string; kind: string; role?: string; port_count?: number; label?: string;
  port_kind?: string;
}
export interface HwConfig {
  enabled: boolean;
  chips: HwChip[];
  block_track: Record<string, Binding>;
  block_occupancy: Record<string, Binding>;
  block_occupancy_current: Record<string, CurrentBinding>;
  switch_output: Record<string, Binding>;
  [k: string]: any;
}
export interface HwSlot {
  map: 'block_track' | 'block_occupancy' | 'block_occupancy_current' | 'switch_output';
  role: string;
  label: string;
  /** When set, only chips whose port_kind matches fill this slot (the two
   * occupancy sources share the occupancy role but split by port kind). */
  portKind?: string;
  /** Current-sense calibration slot: renders channel + baseline/threshold + Read mA. */
  current?: boolean;
}

export const BLOCK_HW_SLOTS: HwSlot[] = [
  { map: 'block_occupancy', role: 'occupancy_input', label: 'Occupancy (idle)', portKind: 'digital_in' },
  {
    map: 'block_occupancy_current', role: 'occupancy_input', label: 'Occupancy (driving)',
    portKind: 'current_sense', current: true,
  },
  { map: 'block_track', role: 'track_output', label: 'Track power' },
];
export const SWITCH_HW_SLOTS: HwSlot[] = [
  { map: 'switch_output', role: 'switch_output', label: 'Throw output' },
];

const SECTION_ID = 'hw-bind-section';

function esc(s: any): string {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' } as any)[c]);
}

/** Fetch the current hardware config, or null on failure (editors degrade to no section). */
export async function fetchHardwareConfig(): Promise<HwConfig | null> {
  try {
    const res = await api.getHardwareConfig();
    return (res && res.config) || null;
  } catch {
    return null;
  }
}

/** Chips that can fill a slot: right role, and right port_kind when the slot pins one. */
function slotChips(cfg: HwConfig, slot: HwSlot): HwChip[] {
  return cfg.chips.filter(
    (c) => c.role === slot.role && (!slot.portKind || c.port_kind === slot.portKind)
  );
}

/** Section HTML, or '' when hardware is off or no chip fills any slot. */
export function hardwareSectionHtml(cfg: HwConfig | null, logicalId: string, slots: HwSlot[]): string {
  if (!cfg || !cfg.enabled) return '';
  const usable = slots.filter((s) => slotChips(cfg, s).length > 0);
  if (!usable.length) return '';
  const rows = usable
    .map((s) => (s.current ? currentRowHtml(cfg, logicalId, s) : portRowHtml(cfg, logicalId, s)))
    .join('');
  return `<div class="control-section control-section-divider mt-12 pt-12" id="${SECTION_ID}">
      <div class="control-section-header">Hardware</div>
      ${rows}
      <p class="text-muted text-xs">Pick a port, then Test to confirm the wire responds.</p>
    </div>`;
}

function channelOptions(chips: HwChip[], current: Binding | undefined, currentSense: boolean): string {
  const opts = ['<option value="">— none —</option>'];
  for (const chip of chips) {
    for (let p = 0; p < (chip.port_count || 0); p++) {
      const val = `${chip.chip_id}::${p}`;
      const sel = current && current.chip_id === chip.chip_id && current.port === p ? ' selected' : '';
      const portLabel = currentSense ? `CH${p + 1}` : `P${p}`;
      opts.push(`<option value="${esc(val)}"${sel}>${esc(chip.label || chip.kind)} · ${portLabel}</option>`);
    }
  }
  return opts.join('');
}

function portRowHtml(cfg: HwConfig, logicalId: string, slot: HwSlot): string {
  const current = (cfg[slot.map] as Record<string, Binding>)[logicalId];
  const opts = channelOptions(slotChips(cfg, slot), current, false);
  return `<div class="input-small-row mb-8">
      <label class="input-small-label input-small-label-inline">${esc(slot.label)}</label>
      <select class="input-small-field hw-bind-select" data-hw-map="${slot.map}">${opts}</select>
      <button type="button" class="btn btn-secondary btn-sm hw-bind-test" data-hw-map="${slot.map}"${current ? '' : ' disabled'}>Test</button>
    </div>`;
}

/** Current-sense calibration row: INA channel + baseline/threshold mA + live read. */
function currentRowHtml(cfg: HwConfig, logicalId: string, slot: HwSlot): string {
  const current = (cfg[slot.map] as Record<string, CurrentBinding>)[logicalId];
  const opts = channelOptions(slotChips(cfg, slot), current, true);
  const baseline = current && current.baseline_ma != null ? current.baseline_ma : '';
  const threshold = current && current.threshold_ma != null ? current.threshold_ma : '';
  return `<div class="input-small-row mb-8">
      <label class="input-small-label input-small-label-inline">${esc(slot.label)}</label>
      <select class="input-small-field hw-cur-select">${opts}</select>
      <button type="button" class="btn btn-secondary btn-sm hw-cur-read"${current ? '' : ' disabled'}>Read mA</button>
    </div>
    <div class="input-small-row mb-8 hw-cur-cal"${current ? '' : ' hidden'}>
      <label class="input-small-label input-small-label-inline">Baseline / threshold mA</label>
      <input type="number" step="0.1" min="0" class="input-small-field hw-cur-baseline" placeholder="0" value="${esc(baseline)}">
      <input type="number" step="0.1" min="0" class="input-small-field hw-cur-threshold" placeholder="15" value="${esc(threshold)}">
    </div>`;
}

/** Wire the section's dropdowns (PUT binding) + Test buttons (port-test). Call after render. */
export function bindHardwareSection(cfg: HwConfig | null, logicalId: string): void {
  const section = document.getElementById(SECTION_ID);
  if (!section || !cfg) return;

  section.querySelectorAll<HTMLSelectElement>('.hw-bind-select').forEach((sel) => {
    sel.addEventListener('change', async () => {
      const map = sel.dataset.hwMap!;
      const testBtn = section.querySelector<HTMLButtonElement>(`.hw-bind-test[data-hw-map="${map}"]`);
      try {
        if (!sel.value) {
          await api.updateHardwareBinding(map, logicalId, null, null);
          delete (cfg as any)[map][logicalId];
        } else {
          const [chipId, port] = sel.value.split('::');
          await api.updateHardwareBinding(map, logicalId, chipId, Number(port));
          (cfg as any)[map][logicalId] = { chip_id: chipId, port: Number(port) };
        }
        if (testBtn) testBtn.disabled = !sel.value;
        flashField(sel, 'ok');
      } catch (e: any) {
        flashField(sel, 'err');
        showToast(e?.message || 'Could not save binding.', 'error', 4000);
      }
    });
  });

  section.querySelectorAll<HTMLButtonElement>('.hw-bind-test').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const map = btn.dataset.hwMap!;
      const sel = section.querySelector<HTMLSelectElement>(`.hw-bind-select[data-hw-map="${map}"]`);
      if (!sel?.value) return;
      const [chipId, port] = sel.value.split('::');
      const action = map === 'block_occupancy' ? 'read_input' : 'pulse_output';
      btn.disabled = true;
      try {
        const res = await api.runHardwarePortTest(cfg, chipId, Number(port), action);
        if (!res?.ok) {
          showToast(res?.error || 'Port test failed.', 'error', 3500);
        } else if (action === 'read_input') {
          showToast(`Port ${port}: ${res.value ? 'current present' : 'no current'}`, 'info', 2500);
        } else {
          showToast(`Pulsed port ${port}.`, 'success', 2000);
        }
      } catch (e: any) {
        showToast(e?.message || 'Port test failed.', 'error', 3500);
      } finally {
        btn.disabled = false;
      }
    });
  });

  // Current-sense calibration slot (INA3221): channel + baseline/threshold + live read.
  const curSel = section.querySelector<HTMLSelectElement>('.hw-cur-select');
  if (curSel) {
    const map = 'block_occupancy_current';
    const calRow = section.querySelector<HTMLElement>('.hw-cur-cal');
    const baseInput = section.querySelector<HTMLInputElement>('.hw-cur-baseline');
    const threshInput = section.querySelector<HTMLInputElement>('.hw-cur-threshold');
    const readBtn = section.querySelector<HTMLButtonElement>('.hw-cur-read');

    const saveCurrent = async (flashTarget: HTMLElement) => {
      try {
        if (!curSel.value) {
          await api.updateHardwareBinding(map, logicalId, null, null);
          delete (cfg as any)[map][logicalId];
        } else {
          const [chipId, port] = curSel.value.split('::');
          const baseline_ma = baseInput && baseInput.value !== '' ? Number(baseInput.value) : null;
          const threshold_ma = threshInput && threshInput.value !== '' ? Number(threshInput.value) : null;
          await api.updateHardwareBinding(map, logicalId, chipId, Number(port), {
            baseline_ma,
            threshold_ma,
          });
          (cfg as any)[map][logicalId] = { chip_id: chipId, port: Number(port), baseline_ma, threshold_ma };
        }
        if (calRow) calRow.hidden = !curSel.value;
        if (readBtn) readBtn.disabled = !curSel.value;
        flashField(flashTarget, 'ok');
      } catch (e: any) {
        flashField(flashTarget, 'err');
        showToast(e?.message || 'Could not save current binding.', 'error', 4000);
      }
    };

    curSel.addEventListener('change', () => saveCurrent(curSel));
    baseInput?.addEventListener('change', () => { if (curSel.value) saveCurrent(baseInput); });
    threshInput?.addEventListener('change', () => { if (curSel.value) saveCurrent(threshInput); });

    readBtn?.addEventListener('click', async () => {
      if (!curSel.value) return;
      const [chipId, port] = curSel.value.split('::');
      readBtn.disabled = true;
      try {
        const res = await api.runHardwarePortTest(cfg, chipId, Number(port), 'read_input');
        if (!res?.ok) {
          showToast(res?.error || 'Read failed.', 'error', 3500);
        } else {
          const ma = typeof res.current_ma === 'number' ? res.current_ma.toFixed(1) : '?';
          const bus = typeof res.bus_v === 'number' ? res.bus_v.toFixed(1) : '?';
          showToast(`CH${Number(port) + 1}: ${ma} mA (bus ${bus} V)`, 'info', 3000);
        }
      } catch (e: any) {
        showToast(e?.message || 'Read failed.', 'error', 3500);
      } finally {
        readBtn.disabled = false;
      }
    });
  }
}
