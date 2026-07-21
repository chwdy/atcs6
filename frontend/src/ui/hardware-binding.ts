/**
 * Per-entity Hardware section for the block and switch editors.
 *
 * A block renders one row per binding slot — a port dropdown plus a Test button
 * — and a dropdown change PUTs that one binding (`/api/hardware/binding`).
 *
 * A switch renders its own shape, because a solenoid point motor takes two
 * output bits (one coil per side) rather than a level on one, plus an optional
 * standby bit for drivers that need it. Its test alternates the two coils once a
 * second and lights the one it just fired.
 *
 * Both are shown only when real hardware is enabled and a chip of the right role
 * is configured, so a pure-simulation layout never sees them. Both live here to
 * keep port selection in one place.
 */
import { api } from '../lib/api';
import { flashField } from '../lib/helpers';
import { showToast } from '../lib/toast';

interface Binding { chip_id: string; port: number; }
interface CurrentBinding extends Binding {
  baseline_ma?: number | null;
  threshold_ma?: number | null;
}
/** A switch's throw wiring: one coil bit per side, plus the optional standby bit
 * a motor driver (TB6612) needs held high while a coil is driven. */
interface SwitchBinding {
  chip_id: string;
  port_main: number;
  port_diverge: number;
  port_standby?: number | null;
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
  switch_output: Record<string, SwitchBinding>;
  [k: string]: any;
}
export interface HwSlot {
  map: 'block_track' | 'block_occupancy' | 'block_occupancy_current';
  role: string;
  label: string;
  /** When set, only chips whose port_kind matches fill this slot (the two
   * occupancy sources share the occupancy role but split by port kind). */
  portKind?: string;
  /** Current-sense calibration slot: renders channel + baseline/threshold mA. */
  current?: boolean;
  /** When set, the row gets a Test button running this port-test action. The
   * occupancy slots have none: they share one test that reads both at once. */
  test?: 'pulse_output' | 'drive_sequence';
  /** Label each port with the H-bridge channel pair it drives (the two rails). */
  rails?: boolean;
}

export const BLOCK_HW_SLOTS: HwSlot[] = [
  { map: 'block_occupancy', role: 'occupancy_input', label: 'Occupancy (idle)', portKind: 'digital_in' },
  {
    map: 'block_occupancy_current', role: 'occupancy_input', label: 'Occupancy (driving)',
    portKind: 'current_sense', current: true,
  },
  { map: 'block_track', role: 'track_output', label: 'Track power', test: 'drive_sequence', rails: true },
];
const SECTION_ID = 'hw-bind-section';
const SWITCH_SECTION_ID = 'hw-switch-section';
/** How long a coil row stays lit after its pulse. The pulse itself is 10 ms —
 * far too short to see — so the highlight is stretched to a readable flash. */
const COIL_FLASH_MS = 260;
/** Gap between the two throws of the alternating bench test. */
const THROW_TEST_INTERVAL_MS = 1000;

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
  const rowHtml = (s: HwSlot) =>
    s.current ? currentRowHtml(cfg, logicalId, s) : portRowHtml(cfg, logicalId, s);
  // The two occupancy sources share one test, so they are grouped together with
  // it before the remaining slots.
  const occupancy = usable.filter((s) => s.role === 'occupancy_input');
  const rest = usable.filter((s) => s.role !== 'occupancy_input');
  const rows = [
    ...occupancy.map(rowHtml),
    occupancy.length ? occupancyTestHtml() : '',
    ...rest.map(rowHtml),
  ].join('');
  return `<div class="control-section control-section-divider mt-12 pt-12" id="${SECTION_ID}">
      <div class="control-section-header">Hardware</div>
      ${rows}
      <p class="text-muted text-xs">Pick a port, then Test to confirm the wire responds.</p>
    </div>`;
}

function channelOptions(
  chips: HwChip[],
  current: Binding | undefined,
  currentSense: boolean,
  rails = false
): string {
  const opts = ['<option value="">— none —</option>'];
  for (const chip of chips) {
    for (let p = 0; p < (chip.port_count || 0); p++) {
      const val = `${chip.chip_id}::${p}`;
      const sel = current && current.chip_id === chip.chip_id && current.port === p ? ' selected' : '';
      // A track port is an H-bridge input pair, so name the two channels it
      // drives: those are the wires that reach the block's two rails.
      const portLabel = currentSense
        ? `CH${p + 1}`
        : rails
          ? `P${p} · rails ch${2 * p}/ch${2 * p + 1}`
          : `P${p}`;
      opts.push(`<option value="${esc(val)}"${sel}>${esc(chip.label || chip.kind)} · ${portLabel}</option>`);
    }
  }
  return opts.join('');
}

function portRowHtml(cfg: HwConfig, logicalId: string, slot: HwSlot): string {
  const current = (cfg[slot.map] as Record<string, Binding>)[logicalId];
  const opts = channelOptions(slotChips(cfg, slot), current, false, slot.rails);
  const test = slot.test
    ? `<button type="button" class="btn btn-secondary btn-sm hw-bind-test" data-hw-map="${slot.map}" data-hw-action="${slot.test}"${current ? '' : ' disabled'}>Test</button>`
    : '';
  return `<div class="input-small-row mb-8">
      <label class="input-small-label input-small-label-inline">${esc(slot.label)}</label>
      <select class="input-small-field hw-bind-select" data-hw-map="${slot.map}">${opts}</select>
      ${test}
    </div>`;
}

/** Current-sense calibration row: sensor channel + the baseline/threshold mA the
 * reading is judged against. Its live reading comes from the occupancy test. */
function currentRowHtml(cfg: HwConfig, logicalId: string, slot: HwSlot): string {
  const current = (cfg[slot.map] as Record<string, CurrentBinding>)[logicalId];
  const opts = channelOptions(slotChips(cfg, slot), current, true);
  const baseline = current && current.baseline_ma != null ? current.baseline_ma : '';
  const threshold = current && current.threshold_ma != null ? current.threshold_ma : '';
  return `<div class="input-small-row mb-8">
      <label class="input-small-label input-small-label-inline">${esc(slot.label)}</label>
      <select class="input-small-field hw-cur-select">${opts}</select>
    </div>
    <div class="input-small-row mb-8 hw-cur-cal"${current ? '' : ' hidden'}>
      <label class="input-small-label input-small-label-inline">Baseline / threshold mA</label>
      <input type="number" step="0.1" min="0" class="input-small-field hw-cur-baseline" placeholder="0" value="${esc(baseline)}">
      <input type="number" step="0.1" min="0" class="input-small-field hw-cur-threshold" placeholder="15" value="${esc(threshold)}">
    </div>`;
}

// ---------------------------------------------------------------------------
// Block occupancy test — read both detectors, show which one decides
// ---------------------------------------------------------------------------
// A block's occupancy comes from one of two detectors depending on whether the
// block is being driven: the '165 bit while it is idle, the current-sense
// channel while it is powered. One button reads both and reports the verdict the
// runtime would take, so the user can see each detector's raw state next to the
// answer it produces. Nothing is driven — the block is read as it stands.

function occupancyTestHtml(): string {
  return `<div class="input-small-row mb-8">
      <button type="button" class="btn btn-secondary btn-sm hw-occ-test">Test occupancy</button>
      <span class="text-muted text-xs hw-occ-status"></span>
    </div>
    <div class="hw-occ-result" hidden></div>`;
}

function fmt(value: any, digits = 1): string {
  return typeof value === 'number' ? value.toFixed(digits) : '?';
}

function occRow(key: string, value: string, deciding: boolean): string {
  const pill = deciding ? '<span class="hw-occ-pill">deciding</span>' : '';
  return `<div class="hw-occ-row${deciding ? ' is-source' : ''}">
      <span class="hw-occ-key">${esc(key)}</span>
      <span class="hw-occ-val">${esc(value)}${pill ? ' ' : ''}</span>${pill}
    </div>`;
}

/** Render the occupancy-test report: drive state, each detector, the verdict. */
export function occupancyResultHtml(res: any): string {
  const track = res.track || {};
  const trackText = !track.bound
    ? 'no port bound — read as idle'
    : track.error
      ? `could not read (${track.error})`
      : `${fmt(track.volts)} V · ${res.driven ? 'driving' : 'idle'}`;

  const digital = res.digital || {};
  const digitalText = !digital.bound
    ? 'not bound'
    : digital.error
      ? `could not read (${digital.error})`
      : `P${digital.port} · ${digital.occupied ? 'occupied' : 'clear'}`;

  const current = res.current || {};
  const currentText = !current.bound
    ? 'not bound'
    : current.error
      ? `could not read (${current.error})`
      : `CH${current.port + 1} · ${fmt(current.current_ma)} mA `
        + `(baseline ${fmt(current.baseline_ma)}, trips at ±${fmt(current.threshold_ma)}) · `
        + `${current.occupied ? 'occupied' : 'clear'}`;

  const verdict =
    res.source === 'none'
      ? 'No detector bound to this block'
      : `${res.occupied ? 'Occupied' : 'Clear'} — the block is `
        + `${res.driven ? 'driving' : 'idle'}, so the `
        + `${res.source === 'current' ? 'driving' : 'idle'} detector decides`;

  return `${occRow('Track power', trackText, false)}
    ${occRow("Idle detector ('165)", digitalText, res.source === 'digital')}
    ${occRow('Driving detector (current)', currentText, res.source === 'current')}
    <div class="hw-occ-verdict ${res.source === 'none' ? 'is-unbound' : res.occupied ? 'is-occupied' : 'is-clear'}">
      ${esc(verdict)}
    </div>`;
}

// ---------------------------------------------------------------------------
// Switch throw wiring — two coils, an optional standby bit, and a bench test
// ---------------------------------------------------------------------------
// A solenoid point motor has one coil per side, so a switch binds two output
// bits rather than holding a level on one. Both coils rest low between throws;
// the alignment is held by the points themselves. `port_standby` is the bit
// wired to a motor driver's standby/enable pin, which a TB6612 needs high before
// either coil does anything.

type CoilSide = 'main' | 'diverge' | 'standby';

const SIDE_LABEL: Record<CoilSide, string> = {
  main: 'Main coil',
  diverge: 'Diverge coil',
  standby: 'Standby bit',
};

/** The chips that can drive a switch coil. */
function switchChips(cfg: HwConfig): HwChip[] {
  return cfg.chips.filter((c) => c.role === 'switch_output');
}

function coilRowHtml(side: CoilSide, chips: HwChip[], current: Binding | undefined): string {
  const opts = channelOptions(chips, current, false);
  return `<div class="input-small-row mb-8 hw-coil-row" data-side="${side}">
      <label class="input-small-label input-small-label-inline">${esc(SIDE_LABEL[side])}</label>
      <select class="input-small-field hw-sw-select" data-side="${side}">${opts}</select>
      <span class="hw-coil-dot" title="Lights when this bit is energized"></span>
    </div>`;
}

/** Switch Hardware section, or '' when hardware is off or no switch chip exists. */
export function switchHardwareSectionHtml(cfg: HwConfig | null, switchId: string): string {
  if (!cfg || !cfg.enabled) return '';
  const chips = switchChips(cfg);
  if (!chips.length) return '';
  const b = cfg.switch_output[switchId];
  const port = (p: number | null | undefined): Binding | undefined =>
    b && p != null ? { chip_id: b.chip_id, port: p } : undefined;
  const standbyOn = !!(b && b.port_standby != null);
  const bound = !!b;
  return `<div class="control-section control-section-divider mt-12 pt-12" id="${SWITCH_SECTION_ID}">
      <div class="control-section-header">Hardware</div>
      ${coilRowHtml('main', chips, port(b?.port_main))}
      ${coilRowHtml('diverge', chips, port(b?.port_diverge))}
      <label class="checkbox-wrapper mb-8">
        <input type="checkbox" class="checkbox-input hw-sw-standby-on"${standbyOn ? ' checked' : ''}>
        <span class="checkbox-custom"></span>
        <span class="checkbox-label">Motor driver needs a standby pin (TB6612)</span>
      </label>
      <div class="hw-sw-standby-row"${standbyOn ? '' : ' hidden'}>
        ${coilRowHtml('standby', chips, port(b?.port_standby))}
      </div>
      <div class="input-small-row mb-8">
        <button type="button" class="btn btn-secondary btn-sm hw-sw-test"${bound ? '' : ' disabled'}>Test throw</button>
        <span class="text-muted text-xs hw-sw-test-status"></span>
      </div>
      <p class="text-muted text-xs">Pick a coil bit per side — a switch is wired once both are set.
        Test throws it main / diverge once a second and lights the coil it just fired.
        Each throw energizes the coil for 10&nbsp;ms only, so the motor cannot burn out.</p>
    </div>`;
}

/** The running bench test, if any. Module-level so a panel re-render or a switch
 * to another entity stops the previous one instead of leaving a timer driving
 * hardware nobody is watching. */
let throwTest: { timer: number; stop: () => void } | null = null;

function stopThrowTest(): void {
  if (!throwTest) return;
  const running = throwTest;
  throwTest = null;
  window.clearInterval(running.timer);
  running.stop();
}

/** Wire the switch Hardware section: coil dropdowns (PUT binding), the standby
 * toggle, and the alternating throw test. Call after render. */
export function bindSwitchHardwareSection(cfg: HwConfig | null, switchId: string): void {
  stopThrowTest();   // a re-render replaced the section a running test was driving
  const section = document.getElementById(SWITCH_SECTION_ID);
  if (!section || !cfg) return;

  const selects = new Map<CoilSide, HTMLSelectElement>();
  section.querySelectorAll<HTMLSelectElement>('.hw-sw-select').forEach((sel) => {
    selects.set(sel.dataset.side as CoilSide, sel);
  });
  const standbyToggle = section.querySelector<HTMLInputElement>('.hw-sw-standby-on');
  const standbyRow = section.querySelector<HTMLElement>('.hw-sw-standby-row');
  const testBtn = section.querySelector<HTMLButtonElement>('.hw-sw-test');
  const testStatus = section.querySelector<HTMLElement>('.hw-sw-test-status');

  /** Split a `chipId::port` option value, or null when the row is unset. */
  const picked = (side: CoilSide): { chipId: string; port: number } | null => {
    const raw = selects.get(side)?.value;
    if (!raw) return null;
    const [chipId, port] = raw.split('::');
    return { chipId, port: Number(port) };
  };

  const standbyEnabled = () => !!standbyToggle?.checked;

  /**
   * Persist the whole binding. A switch is only wired when both coils are
   * picked, so a half-filled section clears the binding rather than storing a
   * switch the driver could not throw. All three bits must sit on one chip —
   * they are one motor driver's inputs — so a cross-chip pick is refused here
   * with a message instead of as a server validation error.
   */
  const save = async (flashTarget: HTMLElement) => {
    const main = picked('main');
    const diverge = picked('diverge');
    const standby = standbyEnabled() ? picked('standby') : null;
    try {
      if (!main || !diverge) {
        await api.updateSwitchBinding(switchId, null, null, null, null);
        delete cfg.switch_output[switchId];
      } else {
        if (main.chipId !== diverge.chipId || (standby && standby.chipId !== main.chipId)) {
          throw new Error('Both coils and the standby bit must be on the same chip.');
        }
        const standbyPort = standby ? standby.port : null;
        await api.updateSwitchBinding(
          switchId, main.chipId, main.port, diverge.port, standbyPort
        );
        cfg.switch_output[switchId] = {
          chip_id: main.chipId,
          port_main: main.port,
          port_diverge: diverge.port,
          port_standby: standbyPort,
        };
      }
      if (testBtn) testBtn.disabled = !cfg.switch_output[switchId];
      flashField(flashTarget, 'ok');
    } catch (e: any) {
      flashField(flashTarget, 'err');
      showToast(e?.message || 'Could not save switch wiring.', 'error', 4000);
    }
  };

  selects.forEach((sel) => sel.addEventListener('change', () => save(sel)));

  standbyToggle?.addEventListener('change', () => {
    if (standbyRow) standbyRow.hidden = !standbyToggle.checked;
    save(standbyToggle);
  });

  // --- the alternating bench test ---------------------------------------
  const rowFor = (side: CoilSide) =>
    section.querySelector<HTMLElement>(`.hw-coil-row[data-side="${side}"]`);

  /** Light the coil that just fired (and the standby bit, which rises with it).
   * The 10 ms pulse is invisible, so the class lingers for a readable flash. */
  const lightCoil = (side: CoilSide, withStandby: boolean) => {
    section.querySelectorAll('.hw-coil-row').forEach((r) => r.classList.remove('is-energized'));
    rowFor(side)?.classList.add('is-energized');
    if (withStandby) rowFor('standby')?.classList.add('is-energized');
    window.setTimeout(() => {
      section.querySelectorAll('.hw-coil-row').forEach((r) => r.classList.remove('is-energized'));
    }, COIL_FLASH_MS);
  };

  const setTestUi = (running: boolean, note = '') => {
    if (testBtn) testBtn.textContent = running ? 'Stop test' : 'Test throw';
    if (testStatus) testStatus.textContent = note;
    if (!running) {
      section.querySelectorAll('.hw-coil-row').forEach((r) => {
        r.classList.remove('is-energized', 'is-thrown');
      });
    }
  };

  testBtn?.addEventListener('click', async () => {
    if (throwTest) { stopThrowTest(); setTestUi(false); return; }
    const b = cfg.switch_output[switchId];
    if (!b) return;

    testBtn.disabled = true;
    try {
      const res = await api.commissionOpen(cfg);
      if (!res?.ok) {
        // The raw driver error ("expected bytes, NoneType found") means nothing
        // to someone at the bench, so it trails a sentence that does.
        showToast(`Could not reach the controller — is it plugged in? (${res?.error || 'no detail'})`, 'error', 5000);
        return;
      }
    } catch (e: any) {
      showToast(`Could not reach the controller — is it plugged in? (${e?.message || 'no detail'})`, 'error', 5000);
      return;
    } finally {
      testBtn.disabled = false;
    }

    let side: CoilSide = 'main';
    const throwOnce = async () => {
      // The panel can be torn down under a running timer (the editor re-renders
      // on switch updates); stop rather than keep driving a detached section.
      if (!section.isConnected) { stopThrowTest(); return; }
      const port = side === 'main' ? b.port_main : b.port_diverge;
      // The other coil is the motor driver's other input: it is held low for the
      // pulse so the two are never driven together (see set_throw's write order).
      const offPort = side === 'main' ? b.port_diverge : b.port_main;
      const standbyPort = b.port_standby ?? null;
      rowFor('main')?.classList.toggle('is-thrown', side === 'main');
      rowFor('diverge')?.classList.toggle('is-thrown', side === 'diverge');
      lightCoil(side, standbyPort != null);
      if (testStatus) testStatus.textContent = `throwing ${side}`;
      try {
        const res = await api.commissionPulse(b.chip_id, port, standbyPort, offPort);
        if (!res?.ok) {
          showToast(res?.error || 'Throw failed.', 'error', 3500);
          stopThrowTest();
          setTestUi(false);
          return;
        }
      } catch (e: any) {
        showToast(e?.message || 'Throw failed.', 'error', 3500);
        stopThrowTest();
        setTestUi(false);
        return;
      }
      side = side === 'main' ? 'diverge' : 'main';
    };

    throwTest = {
      timer: window.setInterval(throwOnce, THROW_TEST_INTERVAL_MS),
      stop: () => { void api.commissionClose().catch(() => { /* best effort */ }); },
    };
    setTestUi(true, 'throwing main');
    void throwOnce();
  });
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
      const action = (btn.dataset.hwAction || 'pulse_output') as
        'pulse_output' | 'drive_sequence';
      const sel = section.querySelector<HTMLSelectElement>(`.hw-bind-select[data-hw-map="${map}"]`);
      if (!sel?.value) return;
      const [chipId, port] = sel.value.split('::');
      const label = btn.textContent;
      btn.disabled = true;
      // The direction sweep drives the track for three seconds, so the button
      // says so rather than looking hung.
      if (action === 'drive_sequence') btn.textContent = 'Running…';
      try {
        const res = await api.runHardwarePortTest(cfg, chipId, Number(port), action);
        if (!res?.ok) {
          showToast(res?.error || 'Port test failed.', 'error', 3500);
        } else if (action === 'drive_sequence') {
          showToast(
            `Swept port ${port}: ${fmt(res.volts, 0)} V one way, off, then the other way. `
            + 'A loco on this block should have run forward, stopped, then reversed.',
            'success',
            5000
          );
        } else {
          showToast(`Pulsed port ${port}.`, 'success', 2000);
        }
      } catch (e: any) {
        showToast(e?.message || 'Port test failed.', 'error', 3500);
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    });
  });

  // One test for both occupancy detectors: read each, show which one the drive
  // state makes authoritative, and give the verdict that follows.
  const occBtn = section.querySelector<HTMLButtonElement>('.hw-occ-test');
  occBtn?.addEventListener('click', async () => {
    const panel = section.querySelector<HTMLElement>('.hw-occ-result');
    const status = section.querySelector<HTMLElement>('.hw-occ-status');
    occBtn.disabled = true;
    if (status) status.textContent = 'reading…';
    try {
      const res = await api.runHardwareOccupancyTest(cfg, logicalId);
      if (!res?.ok) {
        if (panel) panel.hidden = true;
        showToast(res?.error || 'Could not read the detectors.', 'error', 4000);
        return;
      }
      if (panel) {
        panel.innerHTML = occupancyResultHtml(res);
        panel.hidden = false;
      }
    } catch (e: any) {
      showToast(e?.message || 'Could not read the detectors.', 'error', 4000);
    } finally {
      occBtn.disabled = false;
      if (status) status.textContent = '';
    }
  });

  // Current-sense calibration slot: channel + the baseline/threshold it is
  // judged against. Its live reading comes from the occupancy test above.
  const curSel = section.querySelector<HTMLSelectElement>('.hw-cur-select');
  if (curSel) {
    const map = 'block_occupancy_current';
    const calRow = section.querySelector<HTMLElement>('.hw-cur-cal');
    const baseInput = section.querySelector<HTMLInputElement>('.hw-cur-baseline');
    const threshInput = section.querySelector<HTMLInputElement>('.hw-cur-threshold');

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
        flashField(flashTarget, 'ok');
      } catch (e: any) {
        flashField(flashTarget, 'err');
        showToast(e?.message || 'Could not save current binding.', 'error', 4000);
      }
    };

    curSel.addEventListener('change', () => saveCurrent(curSel));
    baseInput?.addEventListener('change', () => { if (curSel.value) saveCurrent(baseInput); });
    threshInput?.addEventListener('change', () => { if (curSel.value) saveCurrent(threshInput); });
  }
}
