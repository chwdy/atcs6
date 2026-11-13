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
 * Both are shown only when a chip of the right role is configured, so a
 * pure-simulation layout never sees them. Both live here to
 * keep port selection in one place.
 */
import { api } from '../lib/api';
import { flashField } from '../lib/helpers';
import { showToast } from '../lib/toast';
import { setupToolsAllowed, SETUP_TOOL_HINT, type SessionStateView } from '../lib/session-controls';
import { chipDisplayName, pickPorts } from './port-picker';

interface Binding { chip_id: string; port: number; }
/** A block's track wiring: the two PWM channels driving its rails, picked
 * independently so they need not be an adjacent pair. */
/** `reversed` is the rail-polarity flag stored with the wiring: the two rails
 * are wired the opposite way round, so the driver swaps the channels as it
 * resolves the binding (design_new.md "Power", Rail polarity lives on the rail
 * binding). */
interface TrackBinding { chip_id: string; forward: number; reverse: number; reversed?: boolean; }
/** `driven_threshold_ma` judges the channel while its block is driven and
 * `threshold_ma` in the lighting zone; unset, the driven zone uses
 * `threshold_ma` (design_new.md "Occupancy source follows drive state"). */
interface CurrentBinding extends Binding {
  baseline_ma?: number | null;
  threshold_ma?: number | null;
  driven_threshold_ma?: number | null;
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
  /** Bus coordinate — carried so a row names *which* of two identical chips it
   * binds (see `chipDisplayName`), not just the kind. */
  bus?: string; i2c_address?: number | null; spi_select?: string | null;
}
export interface HwConfig {
  operation_environment?: string;
  environment_active?: boolean;
  /** Saved setup changes the running session was not built with. Covers only
   * what activation captures — a coil port or a rail-polarity flag is read per
   * write, so editing one never raises this. */
  awaits_activation?: boolean;
  /** The session state (design_new.md "Session state"); the track Test is a
   * Setup tool, so it runs only in Setup. */
  session_state?: SessionStateView | null;
  chips: HwChip[];
  block_track: Record<string, TrackBinding>;
  block_occupancy: Record<string, Binding>;
  block_occupancy_current: Record<string, CurrentBinding>;
  switch_output: Record<string, SwitchBinding>;
  /** switch_id -> block_id whose feed powers that switch's rails. */
  switch_track_source?: Record<string, string>;
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
  /** Track slot: two independently-picked channels, chosen in the port picker
   * rather than a dropdown (a dropdown could only offer fixed channel pairs). */
  track?: boolean;
}

export const BLOCK_HW_SLOTS: HwSlot[] = [
  { map: 'block_occupancy', role: 'occupancy_input', label: 'Occupancy (idle)', portKind: 'digital_in' },
  {
    map: 'block_occupancy_current', role: 'occupancy_input', label: 'Occupancy (driving)',
    portKind: 'current_sense', current: true,
  },
  { map: 'block_track', role: 'track_output', label: 'Track power', track: true },
];

/** The two rails a block's track binding names, in picker-slot form. */
const TRACK_SLOTS = [
  { key: 'forward', label: 'Rail A (forward)' },
  { key: 'reverse', label: 'Rail B (reverse)' },
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
    if (!res?.config) return null;
    return {
      ...res.config,
      operation_environment: res.session?.operation_environment,
      environment_active: !!res.session?.environment_active,
      awaits_activation: !!res.session?.awaits_activation,
      session_state: res.session?.session_state ?? null,
    };
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

/** Section HTML, or '' when no chip fills any slot.
 *
 * Gated on chips existing, not on the selected Run Environment: binding a
 * port to a block is bench configuration you do before the rig is powered or
 * even attached, so it must not wait on going live. A pure-simulation layout has
 * no chips, so it still sees nothing. */
export function hardwareSectionHtml(cfg: HwConfig | null, logicalId: string, slots: HwSlot[]): string {
  if (!cfg) return '';
  const usable = slots.filter((s) => slotChips(cfg, s).length > 0);
  if (!usable.length) return '';
  const rowHtml = (s: HwSlot) =>
    s.track ? trackRowHtml(cfg, logicalId, s)
      : s.current ? currentRowHtml(cfg, logicalId, s)
        : portRowHtml(cfg, logicalId, s);
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
      ${activationNoteHtml(cfg)}
    </div>`;
}

/** The one-line note about how this section's edits reach the rig — which one
 * applies depends on whether a hardware session is running.
 *
 * Not running: wiring is still worth doing, and the bench tests still drive the
 * ports, so the note says so rather than reading as "nothing here works".
 * Running: the note is the standing reminder that saved setup changes are not
 * in the session yet. It renders hidden and is revealed by `noteBindingSaved`,
 * because the edit that makes it true happens while the operator is looking at
 * this panel and a toast alone is missable. */
function activationNoteHtml(cfg: HwConfig): string {
  if (!cfg.environment_active) {
    const test = setupToolsAllowed(cfg.session_state) ? 'Test works' : 'Test works in Setup';
    return `<p class="text-warning text-xs mt-4">This Hardware Setup is not active. Wiring saves now and ${test}; activate Virtual Hardware or Real Hardware from Run Environment to use it.</p>`;
  }
  return `<p class="text-warning text-xs mt-4 hw-pending-note"${cfg.awaits_activation ? '' : ' hidden'}>Saved setup changes are not in the running session — activate the environment again from Run Environment to apply them.</p>`;
}

/** Report a saved binding that the running session captured at activation and
 * therefore is NOT using yet: toast it, and reveal the standing note so the
 * state survives the toast. */
function noteBindingSaved(cfg: HwConfig, what: string): void {
  if (!cfg.environment_active) return;
  cfg.awaits_activation = true;
  document
    .querySelectorAll<HTMLElement>('.hw-pending-note')
    .forEach((note) => { note.hidden = false; });
  showToast(`${what} saved — activate the environment again to apply.`, 'info', 4000);
}

/** Report a saved binding the drivers read per write, which the running session
 * is therefore already using. Its own note because the opposite message —
 * "activate again to apply" — is what made re-picking a coil port look like it
 * did nothing. */
function noteBindingLive(cfg: HwConfig, what: string): void {
  if (!cfg.environment_active) return;
  showToast(`${what} saved — the next throw uses it.`, 'info', 3000);
}

function channelOptions(
  chips: HwChip[],
  current: Binding | undefined,
  currentSense: boolean
): string {
  const opts = ['<option value="">— none —</option>'];
  for (const chip of chips) {
    for (let p = 0; p < (chip.port_count || 0); p++) {
      const val = `${chip.chip_id}::${p}`;
      const sel = current && current.chip_id === chip.chip_id && current.port === p ? ' selected' : '';
      const portLabel = currentSense ? `CH${p + 1}` : `P${p}`;
      opts.push(`<option value="${esc(val)}"${sel}>${esc(chipDisplayName(chip))} · ${portLabel}</option>`);
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
    </div>`;
}

/**
 * Channels already driving some *other* block, keyed `chipId::line`. The picker
 * greys these out, so a double-booked channel is caught while choosing rather
 * than as a rejected save.
 */
function trackChannelsTakenByOthers(cfg: HwConfig, blockId: string): Record<string, string> {
  const taken: Record<string, string> = {};
  for (const [otherId, b] of Object.entries(cfg.block_track)) {
    if (otherId === blockId) continue;
    taken[`${b.chip_id}::${b.forward}`] = otherId;
    taken[`${b.chip_id}::${b.reverse}`] = otherId;
  }
  return taken;
}

/** How a block's bound rails read back, e.g. "Main loop · ch3 → ch10". */
export function trackBindingSummary(cfg: HwConfig, blockId: string): string {
  const b = cfg.block_track[blockId];
  if (!b) return 'not wired';
  const chip = cfg.chips.find((c) => c.chip_id === b.chip_id);
  const name = chip ? chipDisplayName(chip) : b.chip_id;
  return `${name} · ch${b.forward} → ch${b.reverse}`;
}

/**
 * Track row: the two rails are two independently-chosen PWM channels, so this
 * opens the port picker rather than offering a dropdown of fixed channel pairs.
 * Test sweeps the block through both polarities so a loco visibly runs.
 */
function trackRowHtml(cfg: HwConfig, blockId: string, slot: HwSlot): string {
  const bound = !!cfg.block_track[blockId];
  // The track direction sweep drives the block, so it is a Setup tool.
  const inSetup = setupToolsAllowed(cfg.session_state);
  const testAttrs = !bound ? ' disabled' : inSetup ? '' : ` disabled title="${esc(SETUP_TOOL_HINT)}"`;
  return `<div class="input-small-row mb-8">
      <label class="input-small-label input-small-label-inline">${esc(slot.label)}</label>
      <button type="button" class="input-small-field text-left hw-track-pick">${esc(trackBindingSummary(cfg, blockId))}</button>
      <button type="button" class="btn btn-secondary btn-sm hw-track-test"${testAttrs}>Test</button>
    </div>`;
}

/** Current-sense calibration row: sensor channel + the baseline/threshold mA the
 * reading is judged against. Its live reading comes from the occupancy test. */
function currentRowHtml(cfg: HwConfig, logicalId: string, slot: HwSlot): string {
  const current = (cfg[slot.map] as Record<string, CurrentBinding>)[logicalId];
  const opts = channelOptions(slotChips(cfg, slot), current, true);
  const baseline = current && current.baseline_ma != null ? current.baseline_ma : '';
  const threshold = current && current.threshold_ma != null ? current.threshold_ma : '';
  const driven = current && current.driven_threshold_ma != null ? current.driven_threshold_ma : '';
  return `<div class="input-small-row mb-8">
      <label class="input-small-label input-small-label-inline">${esc(slot.label)}</label>
      <select class="input-small-field hw-cur-select">${opts}</select>
    </div>
    <div class="input-small-row mb-8 hw-cur-cal"${current ? '' : ' hidden'}>
      <label class="input-small-label input-small-label-inline">Baseline / threshold mA</label>
      <input type="number" step="0.1" min="0" class="input-small-field hw-cur-baseline" placeholder="0" value="${esc(baseline)}">
      <input type="number" step="0.1" min="0" class="input-small-field hw-cur-threshold" placeholder="15" value="${esc(threshold)}">
    </div>
    <div class="input-small-row mb-8 hw-cur-cal"${current ? '' : ' hidden'}>
      <label class="input-small-label input-small-label-inline" title="Threshold used while the block is driven. Empty uses the threshold above.">Driven threshold mA</label>
      <input type="number" step="0.1" min="0" class="input-small-field hw-cur-driven" placeholder="same" value="${esc(driven)}">
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
  // Three drive zones, not two: "lit" is the band where a stopped train is
  // powered for its lights but draws nothing a current sensor can read.
  const zoneLabel =
    res.zone === 'driven' ? 'driving' : res.zone === 'lighting' ? 'lit, not driving' : 'idle';
  const trackText = !track.bound
    ? 'no port bound — read as idle'
    : track.error
      ? `could not read (${track.error})`
      : `${fmt(track.volts)} V · ${zoneLabel}`;

  const digital = res.digital || {};
  // The pin level and the conclusion are two different facts once a polarity is
  // set, so both are shown — a bare "occupied" against a pin reading 0 is what
  // makes a reversed detector look like a broken one.
  const flipped = !!digital.active_low;
  const digitalText = !digital.bound
    ? 'not bound'
    : digital.error
      ? `could not read (${digital.error})`
      : `P${digital.port} · reads ${digital.value ? '1' : '0'}`
        + `${flipped ? ' (reversed)' : ''} · ${digital.occupied ? 'occupied' : 'clear'}`;

  const current = res.current || {};
  const currentText = !current.bound
    ? 'not bound'
    : current.error
      ? `could not read (${current.error})`
      : `CH${current.port + 1} · ${fmt(current.current_ma)} mA `
        + `(baseline ${fmt(current.baseline_ma)}, trips at ±${fmt(current.threshold_ma)}) · `
        + `${current.occupied ? 'occupied' : 'clear'}`;

  // The lighting zone is the band between the idle deadband and
  // the current-sense floor. The same detector decides there as when driving —
  // the '165's rail is the one being pulsed, so its bit reports the duty cycle
  // rather than the train — but a CLEAR takes a run of reads rather than one, so
  // the verdict has to say so. A "Clear" the runtime would not have acted on
  // yet would otherwise read as a wiring fault.
  const decider = res.source === 'current' ? 'driving' : 'idle';
  const verdict =
    res.source === 'none'
      ? 'No detector bound to this block'
      : res.held_zone
        ? `${res.occupied ? 'Occupied' : 'Clear'} — the block is `
          + `lit but not driving (under ${fmt(res.min_drive_v)} V), so the `
          + `${decider} detector decides, and a clear only releases the block `
          + `after it reads clear for a run of reads`
        : `${res.occupied ? 'Occupied' : 'Clear'} — the block is `
          + `${res.driven ? 'driving' : 'idle'}, so the ${decider} detector decides`;

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

/** The feed-block row: which block's voltage this switch's rails carry.
 *
 * A switch has no track channel of its own, so this names a block rather than a
 * chip port. The options come from `block_track` because every layout block
 * must appear there before a hardware environment may activate — so those keys
 * are the layout's blocks in any setup complete enough to go live. */
function feedRowHtml(cfg: HwConfig, switchId: string): string {
  const current = cfg.switch_track_source?.[switchId] ?? '';
  const blockIds = Object.keys(cfg.block_track).sort();
  // Keep a stored value selectable even if its block lost its track binding,
  // so opening the panel cannot silently drop the wiring on the next save.
  if (current && !blockIds.includes(current)) blockIds.push(current);
  const opts = [`<option value="">—</option>`]
    .concat(
      blockIds.map(
        (id) =>
          `<option value="${esc(id)}"${id === current ? ' selected' : ''}>${esc(id)}</option>`
      )
    )
    .join('');
  return `<div class="input-small-row mb-8">
      <label class="input-small-label input-small-label-inline">Rail feed</label>
      <select class="input-small-field hw-sw-feed">${opts}</select>
    </div>`;
}

/** Switch Hardware section, or '' when no switch chip exists.
 *
 * Gated on a switch-output chip existing, not on the selected Run Environment
 * — coil wiring is bench configuration, the same reasoning as
 * `hardwareSectionHtml`. */
export function switchHardwareSectionHtml(cfg: HwConfig | null, switchId: string): string {
  if (!cfg) return '';
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
      ${feedRowHtml(cfg, switchId)}
      <div class="input-small-row mb-8">
        <button type="button" class="btn btn-secondary btn-sm hw-sw-test"${bound ? '' : ' disabled'}>Test throw</button>
        <span class="text-muted text-xs hw-sw-test-status"></span>
      </div>
      <p class="text-muted text-xs">Rail feed is the block at this switch's main entrance —
        its rails carry that block's track voltage, so it needs no channel of its own.
        Pick a coil bit per side — a switch is wired once both are set.
        Test throws it main / diverge once a second and lights the coil it just fired,
        moving this switch's position with it so the points and the app stay in step.
        Each throw energizes the coil for 10&nbsp;ms only, so the motor cannot burn out.</p>
      ${activationNoteHtml(cfg)}
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
  const feedSelect = section.querySelector<HTMLSelectElement>('.hw-sw-feed');
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
      noteBindingLive(cfg, 'Switch wiring');
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

  /** The rail feed saves on its own — it names a block, not a coil port, so it
   * shares none of the both-coils-or-nothing rules above. */
  feedSelect?.addEventListener('change', async () => {
    const blockId = feedSelect.value || null;
    try {
      await api.updateSwitchTrackSource(switchId, blockId);
      if (!cfg.switch_track_source) cfg.switch_track_source = {};
      if (blockId) cfg.switch_track_source[switchId] = blockId;
      else delete cfg.switch_track_source[switchId];
      flashField(feedSelect, 'ok');
      noteBindingLive(cfg, 'Switch rail feed');
    } catch (e: any) {
      flashField(feedSelect, 'err');
      showToast(e?.message || 'Could not save the rail feed.', 'error', 4000);
    }
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

    let side: CoilSide = 'main';
    const throwOnce = async () => {
      // The panel can be torn down under a running timer (the editor re-renders
      // on switch updates); stop rather than keep driving a detached section.
      if (!section.isConnected) { stopThrowTest(); return; }
      rowFor('main')?.classList.toggle('is-thrown', side === 'main');
      rowFor('diverge')?.classList.toggle('is-thrown', side === 'diverge');
      lightCoil(side, b.port_standby != null);
      if (testStatus) testStatus.textContent = `throwing ${side}`;
      try {
        // A commanded throw, not a bare coil pulse: this switch's own position
        // follows the side being tested, so the points and the app still agree
        // when the test stops. The server chooses which coil driver to use —
        // the running session's, or the commissioning one — and opens nothing
        // here, so a live session no longer looks like an unreachable bridge.
        const res = await api.switchThrowTest(cfg, switchId, side === 'main' ? 0 : 1);
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
      // The server may have opened a commissioning session to pulse the coils;
      // closing is harmless when it did not, and releases the controller when
      // it did.
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
        noteBindingSaved(cfg, 'Binding');
      } catch (e: any) {
        flashField(sel, 'err');
        showToast(e?.message || 'Could not save binding.', 'error', 4000);
      }
    });
  });

  // --- track power: pick the two rail channels, then sweep them -------------
  const pickBtn = section.querySelector<HTMLButtonElement>('.hw-track-pick');
  const trackTestBtn = section.querySelector<HTMLButtonElement>('.hw-track-test');

  pickBtn?.addEventListener('click', async () => {
    const bound = cfg.block_track[logicalId];
    const picked = await pickPorts({
      cfg,
      anchor: pickBtn,
      role: 'track_output',
      direction: 'output',
      title: 'Track power — pick the two rails',
      slots: TRACK_SLOTS,
      initial: bound
        ? {
            forward: { chip_id: bound.chip_id, line: bound.forward },
            reverse: { chip_id: bound.chip_id, line: bound.reverse },
          }
        : undefined,
      // Channels other blocks already drive, so this block cannot steal one.
      taken: trackChannelsTakenByOthers(cfg, logicalId),
    });
    if (!picked) return;                       // cancelled — leave the binding be
    const { forward, reverse } = picked;
    if (forward.chip_id !== reverse.chip_id) {
      showToast('Both rails must be on the same PWM chip.', 'error', 4000);
      return;
    }
    try {
      await api.updateTrackBinding(logicalId, forward.chip_id, forward.line, reverse.line);
      cfg.block_track[logicalId] = {
        chip_id: forward.chip_id, forward: forward.line, reverse: reverse.line,
        // A channel edit keeps the stored polarity flag (the server does the
        // same when `reversed` is omitted from the PUT).
        reversed: cfg.block_track[logicalId]?.reversed === true,
      };
      pickBtn.textContent = trackBindingSummary(cfg, logicalId);
      if (trackTestBtn) trackTestBtn.disabled = !setupToolsAllowed(cfg.session_state);
      flashField(pickBtn, 'ok');
      noteBindingSaved(cfg, 'Track wiring');
    } catch (e: any) {
      flashField(pickBtn, 'err');
      showToast(e?.message || 'Could not save the track wiring.', 'error', 4000);
    }
  });

  trackTestBtn?.addEventListener('click', async () => {
    const label = trackTestBtn.textContent;
    trackTestBtn.disabled = true;
    // The sweep drives the track for about three seconds, so the button says so
    // rather than looking hung.
    trackTestBtn.textContent = 'Running…';
    try {
      const res = await api.runHardwareTrackTest(cfg, logicalId);
      if (!res?.ok) {
        showToast(res?.error || 'Track test failed.', 'error', 3500);
      } else {
        showToast(
          `Swept this block: ${fmt(res.volts, 0)} V one way, off, then the other way. `
          + 'A loco on it should have run forward, stopped, then reversed.',
          'success',
          5000
        );
      }
    } catch (e: any) {
      showToast(e?.message || 'Track test failed.', 'error', 3500);
    } finally {
      trackTestBtn.disabled = false;
      trackTestBtn.textContent = label;
    }
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
    const calRows = section.querySelectorAll<HTMLElement>('.hw-cur-cal');
    const baseInput = section.querySelector<HTMLInputElement>('.hw-cur-baseline');
    const threshInput = section.querySelector<HTMLInputElement>('.hw-cur-threshold');
    const drivenInput = section.querySelector<HTMLInputElement>('.hw-cur-driven');

    const saveCurrent = async (flashTarget: HTMLElement) => {
      try {
        if (!curSel.value) {
          await api.updateHardwareBinding(map, logicalId, null, null);
          delete (cfg as any)[map][logicalId];
        } else {
          const [chipId, port] = curSel.value.split('::');
          const baseline_ma = baseInput && baseInput.value !== '' ? Number(baseInput.value) : null;
          const threshold_ma = threshInput && threshInput.value !== '' ? Number(threshInput.value) : null;
          const driven_threshold_ma =
            drivenInput && drivenInput.value !== '' ? Number(drivenInput.value) : null;
          await api.updateHardwareBinding(map, logicalId, chipId, Number(port), {
            baseline_ma,
            threshold_ma,
            driven_threshold_ma,
          });
          (cfg as any)[map][logicalId] = {
            chip_id: chipId, port: Number(port), baseline_ma, threshold_ma, driven_threshold_ma,
          };
        }
        calRows.forEach((row) => { row.hidden = !curSel.value; });
        flashField(flashTarget, 'ok');
        noteBindingSaved(cfg, 'Current-sense binding');
      } catch (e: any) {
        flashField(flashTarget, 'err');
        showToast(e?.message || 'Could not save current binding.', 'error', 4000);
      }
    };

    curSel.addEventListener('change', () => saveCurrent(curSel));
    baseInput?.addEventListener('change', () => { if (curSel.value) saveCurrent(baseInput); });
    threshInput?.addEventListener('change', () => { if (curSel.value) saveCurrent(threshInput); });
    drivenInput?.addEventListener('change', () => { if (curSel.value) saveCurrent(drivenInput); });
  }
}
