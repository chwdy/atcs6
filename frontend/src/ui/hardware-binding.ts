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
interface HwChip {
  chip_id: string; kind: string; role?: string; port_count?: number; label?: string;
}
export interface HwConfig {
  enabled: boolean;
  chips: HwChip[];
  block_track: Record<string, Binding>;
  block_occupancy: Record<string, Binding>;
  switch_output: Record<string, Binding>;
  [k: string]: any;
}
export interface HwSlot {
  map: 'block_track' | 'block_occupancy' | 'switch_output';
  role: string;
  label: string;
}

export const BLOCK_HW_SLOTS: HwSlot[] = [
  { map: 'block_occupancy', role: 'occupancy_input', label: 'Occupancy' },
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

/** Section HTML, or '' when hardware is off or no chip of any slot's role exists. */
export function hardwareSectionHtml(cfg: HwConfig | null, logicalId: string, slots: HwSlot[]): string {
  if (!cfg || !cfg.enabled) return '';
  const usable = slots.filter((s) => cfg.chips.some((c) => c.role === s.role));
  if (!usable.length) return '';
  const rows = usable.map((s) => portRowHtml(cfg, logicalId, s)).join('');
  return `<div class="control-section control-section-divider mt-12 pt-12" id="${SECTION_ID}">
      <div class="control-section-header">Hardware</div>
      ${rows}
      <p class="text-muted text-xs">Pick a port, then Test to confirm the wire responds.</p>
    </div>`;
}

function portRowHtml(cfg: HwConfig, logicalId: string, slot: HwSlot): string {
  const chips = cfg.chips.filter((c) => c.role === slot.role);
  const current = (cfg[slot.map] as Record<string, Binding>)[logicalId];
  const opts = ['<option value="">— none —</option>'];
  for (const chip of chips) {
    for (let p = 0; p < (chip.port_count || 0); p++) {
      const val = `${chip.chip_id}::${p}`;
      const sel =
        current && current.chip_id === chip.chip_id && current.port === p ? ' selected' : '';
      opts.push(`<option value="${esc(val)}"${sel}>${esc(chip.label || chip.kind)} · P${p}</option>`);
    }
  }
  return `<div class="input-small-row mb-8">
      <label class="input-small-label input-small-label-inline">${esc(slot.label)}</label>
      <select class="input-small-field hw-bind-select" data-hw-map="${slot.map}">${opts.join('')}</select>
      <button type="button" class="btn btn-secondary btn-sm hw-bind-test" data-hw-map="${slot.map}"${current ? '' : ' disabled'}>Test</button>
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
}
