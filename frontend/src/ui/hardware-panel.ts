/**
 * Hardware Setup modal (#hardware-modal).
 *
 * This screen commissions the *chips* on the control board and nothing else:
 * where each chip is (which bus + address / latch line), what it is, how many
 * ports it has, and whether those ports respond. It does not bind ports to the
 * layout — blocks and switches are wired to ports in their own editor panels
 * (see hardware-binding.ts), so the config the panel writes carries only the
 * controller and the chip list and leaves the binding maps untouched.
 *
 * The flow, top to bottom:
 *   0. Wiring overview     — a collapsible diagram built from the configured
 *      chips (buildWiringDiagramSvg): one lane per chip showing its bus,
 *      address/latch, ports, and reference support wiring, for bench-checking.
 *   1. Connect the bridge  — pick the controller, Detect it (scans the I2C bus).
 *   2. Your chips, by bus  — I2C / SPI / GPIO groups, each chip ordered by its
 *      address or latch line. Prove each chip: outputs Sweep, inputs Read live.
 *   3. What your layout is wired to — the tally of blocks and switches actually
 *      bound to those chips, and what breaks for the ones that are not. Read
 *      only: the binding itself is done in the per-entity editors.
 *   4. Go live (footer)    — one readiness checklist gates the Enable switch;
 *      enabling applies on the next session build, so a save offers a restart.
 *
 * setupHardwarePanel() wires the delegated handlers once; refreshHardwarePanel()
 * loads the catalog + persisted config and renders — call it each time the modal
 * opens.
 */
import { api } from '../lib/api';
import { showToast } from '../lib/toast';
import { startRestartReconnectFlow } from './restart-flow';

const BUS_ORDER = ['i2c', 'spi', 'gpio'] as const;
const BUS_LABEL: Record<string, string> = { i2c: 'I²C', spi: 'SPI', gpio: 'GPIO' };
const BUS_ORDERED_BY: Record<string, string> = {
  i2c: 'ordered by address', spi: 'ordered by latch line', gpio: 'ordered by pin',
};

interface CatalogChip {
  kind: string; label: string; buses: string[]; role: string; direction: string;
  ports_per_unit: number; lines_per_unit: number; cascadable: boolean; port_kind: string;
  default_i2c_address: number | null; i2c_addresses: number[]; port_help: string;
}
interface Catalog {
  controllers: { kind: string; label: string; needs_device_port: boolean }[];
  chips: CatalogChip[];
  spi_select_lines: string[];
  gpio_lines: string[];
}
interface ChipCfg {
  chip_id: string; kind: string; bus: string;
  i2c_address: number | null; chain_length: number; spi_select: string | null; label: string;
  // Shift-in chips only: the detector feeding this chain pulls its line LOW when
  // current flows (a comparator with no inverter stage). The hardware layer
  // flips those bits, so nothing above it sees a polarity.
  active_low: boolean;
}
interface HwState {
  enabled: boolean;
  develop: boolean;
  controller: { kind: string; device_port: string | null };
  chips: ChipCfg[];
  // Drive threshold for the hybrid occupancy detector: a block commanded above
  // this magnitude is read by its current channel, at or below it by its '165
  // detector. Layout-specific, so it is configured rather than fixed.
  idleDeadbandV: number;
}
// How sure we are that a configured chip is really on the board.
//   alive    — an I²C address that answered the last scan, or a shift register
//              the user has run a port test on.
//   noresp   — an I²C address that did not answer a scan that did run.
//   untested — no scan has run yet, or a shift register nobody has tested.
type ChipState = 'alive' | 'noresp' | 'untested';

// Develop mode (fake hardware) — the running session's mode info and the raw
// binding maps the "Fake inputs" section pokes. See devRows() for the row model.
interface SessionInfo { operation_mode: string; develop_active: boolean; }
interface DevBindings {
  block_occupancy: Record<string, any>;
  block_occupancy_current: Record<string, any>;
}
interface DevReadback {
  develop_active?: boolean;
  blocks?: Record<string, { occupancy_bit: boolean | null; current_ma: number | null; volts: number | null; occupied: boolean | null }>;
  switches?: Record<string, any>;
}
export interface DevRow {
  blockId: string;
  hasDigital: boolean;
  hasCurrent: boolean;
  occupancyBit: boolean | null;
  currentMa: number | null;
  volts: number | null;
  occupied: boolean | null;
}

// Layout coverage — which blocks / switches in the loaded layout are wired at
// all. Distinct from chip readiness above: a board can be perfectly proven and
// still have half the layout bound to nothing. See coverageRows().
interface CoverageSplit { bound: string[]; unbound: string[]; }
export interface Coverage {
  enabled: boolean;
  blocks: { total: number; occupancy: CoverageSplit; track: CoverageSplit };
  switches: { total: number; bound: string[]; unbound: string[] };
  stale: Record<string, string[]>;
  complete: boolean;
}
export interface CoverageRow {
  key: string;
  label: string;
  bound: number;
  total: number;
  missing: string[];
  /** What goes wrong for the entities in `missing`, once hardware is live. */
  consequence: string;
}

// Mirrors hardware_config.DEFAULT_IDLE_DEADBAND_V / MAX_IDLE_DEADBAND_V. The
// server re-validates; these only keep the field from sending a value it will
// reject.
const DEFAULT_IDLE_DEADBAND_V = 0.25;
const MAX_IDLE_DEADBAND_V = 5.0;

const DEFAULT_STATE: HwState = {
  enabled: false,
  develop: false,
  controller: { kind: 'ch347', device_port: null },
  chips: [],
  idleDeadbandV: DEFAULT_IDLE_DEADBAND_V,
};

let state: HwState = clone(DEFAULT_STATE);
let catalog: Catalog | null = null;
// The last bus scan: every I²C address that answered, as lowercase hex.
let i2cScan: string[] = [];
let scanned = false;
// Shift registers (SPI / GPIO) cannot be scanned for — they have no
// acknowledgement and no ID register, so a '595 or '165 reads exactly the same
// unplugged as wired. The user running its port test is the only proof there
// is, so the chips that have been tested this session are recorded here.
let manualProven = new Set<string>();
let controllerReachable = false;
let savedSinceEdit = false;
let adding = false;

// Develop mode: the running session's mode info, the binding maps the Fake-input
// section pokes, its latest read-back, and the 1 s poll while the modal is open.
let sessionInfo: SessionInfo | null = null;
let devBindings: DevBindings = { block_occupancy: {}, block_occupancy_current: {} };
let devReadback: DevReadback | null = null;
let devPollTimer: any = null;

// Layout coverage, refreshed with the config each time the modal opens.
let coverage: Coverage | null = null;

// Commissioning — a held device for the live input view and the output sweep.
// Only one activity runs at a time; it is stopped on modal close / refresh.
let commissionActive = false;
let activity: { chipId: string; kind: 'sweep' | 'live'; port: number; values: number[] } | null = null;
let activityTimer: any = null;
let sweepAbort = false;

function clone<T>(v: T): T { return JSON.parse(JSON.stringify(v)); }
function esc(s: any): string {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' } as any)[c]);
}
function el<T extends HTMLElement = HTMLElement>(id: string): T | null {
  return document.getElementById(id) as T | null;
}
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// --- develop-mode fake-input rows (pure; unit-tested) ----------------------
// The Fake-inputs section lists every block with an occupancy detector and/or a
// current-sense binding, joined to the latest read-back from the fake device.
export function devRows(bindings: DevBindings, readback: DevReadback | null): DevRow[] {
  const digital = bindings.block_occupancy || {};
  const current = bindings.block_occupancy_current || {};
  const ids = Array.from(new Set([...Object.keys(digital), ...Object.keys(current)])).sort();
  const blocks = readback?.blocks || {};
  return ids.map((blockId) => {
    const rb = blocks[blockId] || {};
    return {
      blockId,
      hasDigital: blockId in digital,
      hasCurrent: blockId in current,
      occupancyBit: rb.occupancy_bit ?? null,
      currentMa: rb.current_ma ?? null,
      volts: rb.volts ?? null,
      occupied: rb.occupied ?? null,
    };
  });
}

// --- layout coverage rows (pure; unit-tested) ------------------------------
// One row per kind of binding a layout entity needs, each carrying what goes
// wrong for the entities that lack it. The consequences are not decoration:
// half-wired is a state the config validator passes cleanly, and each of these
// then shows up as the app quietly not working rather than as a warning.
export function coverageRows(cov: Coverage | null): CoverageRow[] {
  if (!cov) return [];
  return [
    {
      key: 'occupancy',
      label: 'Block detectors',
      bound: cov.blocks.occupancy.bound.length,
      total: cov.blocks.total,
      missing: cov.blocks.occupancy.unbound,
      consequence: 'read permanently clear — no train is ever detected entering them',
    },
    {
      key: 'track',
      label: 'Block track power',
      bound: cov.blocks.track.bound.length,
      total: cov.blocks.total,
      missing: cov.blocks.track.unbound,
      consequence: 'cannot be energized, so a train stops on reaching them',
    },
    {
      key: 'switches',
      label: 'Switch coils',
      bound: cov.switches.bound.length,
      total: cov.switches.total,
      missing: cov.switches.unbound,
      consequence: 'cannot be thrown, so no route can align them',
    },
  ];
}

/** Bindings naming entities the layout no longer has, flattened to one list. */
export function staleBindings(cov: Coverage | null): string[] {
  if (!cov) return [];
  const seen = new Set<string>();
  for (const ids of Object.values(cov.stale || {})) ids.forEach((id) => seen.add(id));
  return Array.from(seen).sort();
}

// --- catalog-derived chip facts --------------------------------------------
function catalogChip(kind: string): CatalogChip | undefined {
  return catalog?.chips.find((c) => c.kind === kind);
}
function chipPorts(chip: ChipCfg): number {
  const ck = catalogChip(chip.kind);
  if (!ck) return 0;
  return ck.ports_per_unit * (ck.cascadable ? Math.max(1, chip.chain_length) : 1);
}
// The individually-addressable lines a chip exposes — what the port grid below
// shows and what a click on one of those pills drives. This is NOT the port
// count: one PCA9685 track port is a *pair* of PWM channels (an H-bridge's two
// inputs), so the chip carries 8 ports but 16 channels, and the grid addresses
// the channels. Mirrors `lines_for_chip` in hardware/catalog.py.
function chipLines(chip: ChipCfg): number {
  const ck = catalogChip(chip.kind);
  if (!ck) return 0;
  return ck.lines_per_unit * (ck.cascadable ? Math.max(1, chip.chain_length) : 1);
}
// What one line is called on this chip. Matches the port picker's naming
// (port-picker.ts lineLabel) so a channel has one name across the whole app.
function lineLabel(chip: ChipCfg, line: number): string {
  const kind = catalogChip(chip.kind)?.port_kind;
  if (kind === 'bipolar_pair') return `ch${line}`;
  if (kind === 'current_sense') return `CH${line + 1}`;
  return `P${line}`;
}
function chipDirection(chip: ChipCfg): string { return catalogChip(chip.kind)?.direction || 'output'; }
function isCurrentSense(chip: ChipCfg): boolean { return catalogChip(chip.kind)?.port_kind === 'current_sense'; }
function chipLabel(chip: ChipCfg): string { return catalogChip(chip.kind)?.label || chip.kind; }
function latchLines(bus: string): string[] {
  if (bus === 'spi') return catalog?.spi_select_lines || [];
  if (bus === 'gpio') return catalog?.gpio_lines || [];
  return [];
}
function hexAddr(addr: number): string { return '0x' + addr.toString(16).padStart(2, '0'); }
// "0x41" / "65" -> 65. NaN when there is nothing to read.
export function parseAddr(raw: string): number {
  const v = String(raw ?? '').trim();
  if (!v) return NaN;
  return v.toLowerCase().startsWith('0x') ? parseInt(v, 16) : parseInt(v, 10);
}
// I²C 7-bit peripheral range — mirrors `_I2C_MIN`/`_I2C_MAX` in
// hardware_config.py, which is what actually accepts or rejects a saved
// address. 0x00-0x02 and 0x78-0x7f are reserved.
const I2C_MIN = 0x03, I2C_MAX = 0x77;

// What an I²C chip's address dropdown offers, in groups.
//
// Once a scan has run the bus is the authority: the addresses that actually
// answered are the whole list, because an address nothing answers on is an
// address no chip is at. The chip kind's strap range is NOT a ceiling on them —
// a breakout board can carry address pins the bare part does not, or ship
// strapped outside the datasheet's nominal window, so a current-sense chip
// answering at 0x45 must be selectable even though its A0 pin nominally spans
// 0x40..0x43.
//
// With no scan there is nothing measured to list, so the strap range leads (it
// is the good guess) and the rest of the legal range follows behind it, so the
// strap range never becomes a cap on a board that sits outside it.
//
// The configured address is always present, so a saved value never silently
// drops out of its own dropdown.
export type AddrGroup = { label: string; addrs: number[] };
export function addressGroups(
  catalogAddrs: number[], scannedAddrs: number[], current: number | null,
): AddrGroup[] {
  const uniq = (xs: number[]) => Array.from(new Set(xs)).sort((a, b) => a - b);
  if (scannedAddrs.length) {
    const found = uniq(scannedAddrs);
    const groups: AddrGroup[] = [{ label: 'Answering on the bus', addrs: found }];
    if (current != null && !found.includes(current)) {
      groups.push({ label: 'Configured, not answering', addrs: [current] });
    }
    return groups;
  }
  const straps = uniq(catalogAddrs);
  const strapSet = new Set(straps);
  const rest: number[] = [];
  for (let a = I2C_MIN; a <= I2C_MAX; a++) if (!strapSet.has(a)) rest.push(a);
  const groups: AddrGroup[] = [];
  if (straps.length) groups.push({ label: 'Typical straps for this chip', addrs: straps });
  groups.push({ label: straps.length ? 'Any other I²C address' : 'I²C address', addrs: rest });
  return groups;
}
// Module-state wrapper over `addressGroups` for one configured chip.
function addrGroupsFor(kind: string, current: number | null): AddrGroup[] {
  const catalogAddrs = catalogChip(kind)?.i2c_addresses || [];
  return addressGroups(catalogAddrs, detectedAddrs(), current);
}
// The last scan's addresses as numbers. Empty until a scan has actually run —
// "no scan yet" and "scan found nothing" both mean there is no measurement.
function detectedAddrs(): number[] {
  if (!scanned) return [];
  return i2cScan.map(parseAddr).filter((n) => Number.isFinite(n));
}
function addrGroupsHtml(groups: AddrGroup[], selected: number | null): string {
  return groups.map((g) => {
    const opts = g.addrs.map((a) =>
      `<option value="${esc(hexAddr(a))}"${a === selected ? ' selected' : ''}>` +
      `${esc(hexAddr(a))}${onBus(a) ? ' ●' : ''}</option>`).join('');
    return `<optgroup label="${esc(g.label)}">${opts}</optgroup>`;
  }).join('');
}
function coordText(chip: ChipCfg): string {
  if (chip.bus === 'i2c') return chip.i2c_address != null ? hexAddr(chip.i2c_address) : '';
  return (chip.spi_select || '').toUpperCase();
}
// Is this address answering on the bus right now (per the last scan)?
function onBus(addr: number | null): boolean {
  return addr != null && i2cScan.includes(hexAddr(addr));
}
// How sure we are this chip is really there. An I²C chip answers a scan or it
// does not; a shift register cannot be scanned for at all, so the user's own
// port test is what proves it (see `manualProven`).
function chipState(chip: ChipCfg): ChipState {
  if (chip.bus !== 'i2c') return manualProven.has(chip.chip_id) ? 'alive' : 'untested';
  if (!scanned || chip.i2c_address == null) return 'untested';
  return onBus(chip.i2c_address) ? 'alive' : 'noresp';
}
function coordSort(chip: ChipCfg): number {
  if (chip.bus === 'i2c') return chip.i2c_address ?? 9999;
  return latchLines(chip.bus).indexOf(chip.spi_select || '');
}

// --- wiring diagram (driven by the configured chips) -----------------------
// One wiring lane per chip in `state.chips`: the chip drawn with its real bus,
// address/latch line, and port count, plus the reference support wiring for
// that chip kind (voltages, resistors, DRV8871/LM339/shunts/…), all fanning
// out from the controller on the left. It reads `state`, so it is rebuilt on
// every render() and updates live as the user edits the chip list below.
//
// The low-level primitives below take absolute SVG coordinates; each per-kind
// fragment (wdFrag*) lays itself out around a lane's vertical centre `mid`.
function wdPath(pts: Array<[number, number]>, label?: string, arrow?: boolean): string {
  const d = pts.map((pt, i) => `${i === 0 ? 'M' : 'L'} ${pt[0]} ${pt[1]}`).join(' ');
  const line = `<path d="${d}" class="hw-wd-wire"${arrow ? ' marker-end="url(#hwWdArrow)"' : ''}/>`;
  if (!label) return line;
  const mid = Math.max(0, Math.floor((pts.length - 1) / 2));
  const [x1, y1] = pts[mid];
  const [x2, y2] = pts[mid + 1] || pts[mid];
  const horiz = y1 === y2;
  const tx = (x1 + x2) / 2, ty = (y1 + y2) / 2;
  const text = horiz
    ? `<text x="${tx}" y="${ty - 5}" text-anchor="middle" class="hw-wd-wlabel">${esc(label)}</text>`
    : `<text x="${tx + 6}" y="${ty + 3}" text-anchor="start" class="hw-wd-wlabel">${esc(label)}</text>`;
  return line + text;
}
function wdBox(x: number, y: number, w: number, h: number, cls: string, name: string, sub?: string): string {
  const cx = x + w / 2;
  const nameY = sub ? y + h / 2 - 2 : y + h / 2 + 4;
  const subText = sub
    ? `<text x="${cx}" y="${y + h / 2 + 14}" text-anchor="middle" class="hw-wd-sub">${esc(sub)}</text>` : '';
  return `<g class="hw-wd-node ${cls}">
    <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="5"/>
    <text x="${cx}" y="${nameY}" text-anchor="middle" class="hw-wd-name">${esc(name)}</text>
    ${subText}
  </g>`;
}
function wdPill(x: number, y: number, w: number, h: number, text: string): string {
  return `<g class="hw-wd-rail">
    <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${h / 2}"/>
    <text x="${x + w / 2}" y="${y + h / 2 + 4}" text-anchor="middle">${esc(text)}</text>
  </g>`;
}
// `side` puts the value label left of the body instead of right, for risers
// close enough to the canvas edge that a right-hand label would overflow.
function wdResistorV(x: number, y: number, len: number, label: string, side: 1 | -1 = 1): string {
  const rw = 10, rh = 22, ry = y + (len - rh) / 2;
  const lx = side === 1 ? x + rw / 2 + 5 : x - rw / 2 - 5;
  return `<line x1="${x}" y1="${y}" x2="${x}" y2="${ry}" class="hw-wd-wire"/>
    <rect x="${x - rw / 2}" y="${ry}" width="${rw}" height="${rh}" class="hw-wd-r"/>
    <line x1="${x}" y1="${ry + rh}" x2="${x}" y2="${y + len}" class="hw-wd-wire"/>
    <text x="${lx}" y="${ry + rh / 2 + 3}" text-anchor="${side === 1 ? 'start' : 'end'}" class="hw-wd-wlabel">${esc(label)}</text>`;
}
function wdResistorH(x: number, y: number, len: number, label: string): string {
  const rw = 22, rh = 10, rx = x + (len - rw) / 2;
  return `<line x1="${x}" y1="${y}" x2="${rx}" y2="${y}" class="hw-wd-wire"/>
    <rect x="${rx}" y="${y - rh / 2}" width="${rw}" height="${rh}" class="hw-wd-r"/>
    <line x1="${rx + rw}" y1="${y}" x2="${x + len}" y2="${y}" class="hw-wd-wire"/>
    <text x="${x + len / 2}" y="${y - rh / 2 - 5}" text-anchor="middle" class="hw-wd-wlabel">${esc(label)}</text>`;
}
// `dir` is the direction current passes: 1 points the triangle +x, -1 points it
// -x, for a chain drawn right-to-left (a supply on the right feeding a node on
// the left). Anode is the flat base, cathode the barred tip.
function wdDiodeH(x: number, y: number, len: number, label: string, dir: 1 | -1 = 1): string {
  const dw = 16, dx = x + (len - dw) / 2;
  const base = dir === 1 ? dx : dx + dw;
  const tip = dir === 1 ? dx + dw - 3 : dx + 3;
  return `<line x1="${x}" y1="${y}" x2="${dx}" y2="${y}" class="hw-wd-wire"/>
    <polygon points="${base},${y - 6} ${base},${y + 6} ${tip},${y}" class="hw-wd-d"/>
    <line x1="${tip}" y1="${y - 7}" x2="${tip}" y2="${y + 7}" class="hw-wd-d-bar"/>
    <line x1="${dx + dw}" y1="${y}" x2="${x + len}" y2="${y}" class="hw-wd-wire"/>
    <text x="${x + len / 2}" y="${y + 17}" text-anchor="middle" class="hw-wd-wlabel">${esc(label)}</text>`;
}

// A circled path number, keyed to the path list printed under the same lane.
function wdPathNum(x: number, y: number, n: number): string {
  return `<g class="hw-wd-pnum">
    <circle cx="${x}" cy="${y}" r="7.5"/>
    <text x="${x}" y="${y + 3.5}" text-anchor="middle">${n}</text>
  </g>`;
}
function wdGround(x: number, y: number): string {
  return `<line x1="${x}" y1="${y}" x2="${x}" y2="${y + 7}" class="hw-wd-wire"/>
    <line x1="${x - 9}" y1="${y + 7}" x2="${x + 9}" y2="${y + 7}" class="hw-wd-gnd"/>
    <line x1="${x - 5.5}" y1="${y + 11}" x2="${x + 5.5}" y2="${y + 11}" class="hw-wd-gnd"/>
    <line x1="${x - 2}" y1="${y + 15}" x2="${x + 2}" y2="${y + 15}" class="hw-wd-gnd"/>`;
}

// A configured chip drawn as a bus-coloured box: kind, its real coordinate
// (I²C address or latch line) and port count, a live health dot mirroring the
// self-test state, and its label if set.
function wdChipBox(x: number, y: number, w: number, h: number, chip: ChipCfg): string {
  const busCls = chip.bus === 'spi' ? 'role-spi' : chip.bus === 'gpio' ? 'role-gpio' : 'role-i2c';
  const coord = coordText(chip) || (chip.bus === 'i2c' ? 'no addr' : 'no line');
  const dir = chipDirection(chip) === 'output' ? 'out' : 'in';
  const dot = chipState(chip);
  const cx = x + w / 2;
  const label = chip.label
    ? `<text x="${cx}" y="${y + 50}" text-anchor="middle" class="hw-wd-sub">${esc(chip.label)}</text>` : '';
  return `<g class="hw-wd-node ${busCls}">
    <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="5"/>
    <circle cx="${x + 12}" cy="${y + 14}" r="4" class="hw-wd-dot ${dot}"/>
    <text x="${cx}" y="${y + 21}" text-anchor="middle" class="hw-wd-name">${esc(chip.kind)}</text>
    <text x="${cx}" y="${y + 36}" text-anchor="middle" class="hw-wd-sub">${esc(coord)} · ${chipPorts(chip)} ${dir}</text>
    ${label}
  </g>`;
}

// --- per-kind support-wiring fragments (drawn to the right of the chip box) -
// Each lays out around the lane centre `mid`; the chip box's right edge is at
// x=346 (CHIP_X + CHIP_W). Values mirror the reference circuit the user gave.
//
// A fragment may also publish `anchors`: named points where a net leaves or
// enters its lane. Two lanes naming the same net get a tie drawn between them
// (WD_TIES), which is how the diagram shows that separate lanes are one circuit.
type WdAnchors = Record<string, [number, number]>;
interface WdFrag { svg: string; anchors: WdAnchors; }

const WD_W = 800;   // canvas width every fragment lays out against

// The H-bridge box sits at the same x in the track and current-sense lanes, so
// the +12V tie between them is a straight drop and reads as one supply net.
const WD_HB_X = 528, WD_HB_W = 104, WD_HB_CX = WD_HB_X + WD_HB_W / 2;

function wdFragTrack(mid: number): WdFrag {   // pca9685 -> DRV8871 -> rails
  const s: string[] = [];
  s.push(wdPath([[346, mid], [WD_HB_X, mid]], 'PWM · IN1/IN2', true));
  s.push(`<text x="${WD_HB_CX + 9}" y="${mid - 25}" text-anchor="start" class="hw-wd-wlabel">VM +12V</text>`);
  s.push(wdBox(WD_HB_X, mid - 20, WD_HB_W, 40, '', 'DRV8871', 'H-bridge'));
  // Rail A is drawn lower so its underside is free for the tie down to the
  // detector lane, which reads Rail A. Stacking it above Rail B would make that
  // tie leave a Rail B edge and land on a Rail A box — a connection that is not
  // there. Which output feeds which rail is carried by the OUT1 / OUT2 labels.
  s.push(wdPath([[632, mid - 11], [666, mid - 11]], 'OUT2'));
  s.push(wdBox(666, mid - 24, 62, 24, '', 'Rail B'));
  s.push(wdPath([[632, mid + 11], [666, mid + 11]], 'OUT1'));
  s.push(wdBox(666, mid + 2, 62, 24, '', 'Rail A'));
  s.push(wdPath([[728, mid - 12], [752, mid - 12]], undefined, true));
  s.push(`<text x="756" y="${mid - 9}" text-anchor="start" class="hw-wd-wlabel">to track</text>`);
  s.push(wdPath([[728, mid + 12], [752, mid + 12]], undefined, true));
  s.push(`<text x="756" y="${mid + 15}" text-anchor="start" class="hw-wd-wlabel">to track</text>`);
  return { svg: s.join(''), anchors: { vm_in: [WD_HB_CX, mid - 20], rails_out: [697, mid + 26] } };
}
// ina3221 / ina219: the chip sits high-side on the +12V feed, one channel per
// block, each channel measuring its own shunt just before that block's DRV8871
// supply pin. Channel count is the only thing that separates the two kinds —
// three for the ina3221, one for the ina219 — so both render through here.
function wdFragCurrentSense(chip: ChipCfg, mid: number): WdFrag {
  const s: string[] = [];
  const n = chipPorts(chip);
  const rowA = mid - 26, rowB = mid + 20;

  // What the chip contributes: one sense channel per block.
  s.push(wdPath([[346, rowA], [372, rowA]], undefined, true));
  const cellW = 38, gap = 4;
  let x = 372;
  for (let i = 0; i < n; i++) {
    s.push(`<g class="hw-wd-bit"><rect x="${x}" y="${rowA - 11}" width="${cellW}" height="22" rx="3"/>` +
      `<text x="${x + cellW / 2}" y="${rowA + 4}" text-anchor="middle">CH${i + 1}</text></g>`);
    x += cellW + gap;
  }
  s.push(`<text x="${x + 4}" y="${rowA + 4}" text-anchor="start" class="hw-wd-wlabel">` +
    `${n === 1 ? 'one block' : `${n} blocks`}</text>`);
  // The destructive-mistake warning rides this row, not the shunt row below:
  // the +12V tie drops through the shunt row on its way to the H-bridge lane.
  s.push(`<text x="${WD_W - 8}" y="${rowA + 4}" text-anchor="end" class="hw-wd-caption-inline">` +
    `never in series with Rail A / Rail B</text>`);

  // One channel expanded: +12V in, across the shunt, out to that block's
  // H-bridge supply pin. The H-bridge — not the sense chip — drives the rails.
  s.push(wdPill(372, rowB - 9, 44, 18, '+12V'));
  s.push(wdPath([[416, rowB], [434, rowB]]));
  s.push(wdResistorH(434, rowB, 56, 'R shunt'));
  s.push(wdPath([[490, rowB], [WD_HB_X, rowB]], 'VM', true));
  s.push(wdBox(WD_HB_X, rowB - 13, WD_HB_W, 26, '', 'DRV8871'));
  s.push(wdPath([[632, rowB], [664, rowB]], undefined, true));
  s.push(`<text x="668" y="${rowB + 3}" text-anchor="start" class="hw-wd-wlabel">to rails</text>`);
  s.push(wdPath([[434, rowB], [434, rowB + 13]]));
  s.push(`<text x="434" y="${rowB + 24}" text-anchor="middle" class="hw-wd-wlabel">IN+</text>`);
  s.push(wdPath([[490, rowB], [490, rowB + 13]]));
  s.push(`<text x="490" y="${rowB + 24}" text-anchor="middle" class="hw-wd-wlabel">IN−</text>`);
  s.push(`<text x="372" y="${rowB + 36}" text-anchor="start" class="hw-wd-caption-inline">` +
    `one channel shown</text>`);
  return { svg: s.join(''), anchors: { vm_out: [WD_HB_CX, rowB + 13] } };
}
function wdFragSwitch(chip: ChipCfg, mid: number): WdFrag {   // hc595 shift-out bits
  const s: string[] = [];
  const n = chipPorts(chip);
  const shown = Math.min(n, 8);
  s.push(wdPath([[346, mid], [372, mid]], undefined, true));
  const cellW = 30, gap = 3;
  let x = 372;
  for (let i = 0; i < shown; i++) {
    s.push(`<g class="hw-wd-bit"><rect x="${x}" y="${mid - 11}" width="${cellW}" height="22" rx="3"/>` +
      `<text x="${x + cellW / 2}" y="${mid + 4}" text-anchor="middle">Q${i}</text></g>`);
    x += cellW + gap;
  }
  if (n > 8) { s.push(`<text x="${x + 2}" y="${mid + 4}" text-anchor="start" class="hw-wd-wlabel">+${n - 8}</text>`); x += 26; }
  s.push(wdPath([[x + 2, mid], [x + 38, mid]], undefined, true));
  s.push(`<text x="${x + 42}" y="${mid - 4}" text-anchor="start" class="hw-wd-wlabel">to switch</text>`);
  s.push(`<text x="${x + 42}" y="${mid + 9}" text-anchor="start" class="hw-wd-wlabel">drivers</text>`);
  return { svg: s.join(''), anchors: {} };
}
// hc165 <- LM339 <- Rail A / Rail B. The detector is four current paths off one
// +5V rail; each is numbered here and listed again under the lane, so a bench
// check can follow them one at a time.
function wdFragOccupancy(mid: number): WdFrag {
  const s: string[] = [];
  const V5 = mid - 82;                       // bottom edge of the +5V rail
  const PIN_P = mid - 16, PIN_N = mid + 16;  // LM339 input pins
  const NODE = 430;                          // pull-up / OUT / HC165 bit junction

  s.push(wdPill(392, mid - 100, 400, 18, '+5V'));

  // The comparator. Its output is open-collector, so the path-4 pull-up is what
  // actually drives the HC165 input high.
  s.push(`<g class="hw-wd-node">
    <rect x="500" y="${mid - 26}" width="104" height="52" rx="5"/>
    <text x="552" y="${mid - 4}" text-anchor="middle" class="hw-wd-name">LM339</text>
    <text x="552" y="${mid + 10}" text-anchor="middle" class="hw-wd-sub">VCC +12V</text>
    <text x="598" y="${PIN_P + 3}" text-anchor="end" class="hw-wd-pin">+</text>
    <text x="598" y="${PIN_N + 3}" text-anchor="end" class="hw-wd-pin">−</text>
  </g>`);

  // 4 — +5V through the pull-up to the open-collector output, then to the bit.
  s.push(wdResistorV(NODE, V5, 82, '10k'));
  s.push(`<circle cx="${NODE}" cy="${mid}" r="2.5" class="hw-wd-junction"/>`);
  s.push(wdPath([[500, mid], [NODE, mid]], 'OUT'));
  s.push(wdPath([[NODE, mid], [346, mid]], 'bit', true));
  s.push(wdPathNum(414, mid - 41, 4));

  // 2 — +5V through the dropper and diode onto Rail A, which the + input reads.
  s.push(wdPath([[604, PIN_P], [622, PIN_P]]));
  s.push(wdBox(622, PIN_P - 19, 58, 38, '', 'Rail A', 'OUT1'));
  s.push(wdDiodeH(680, PIN_P, 30, 'D1', -1));
  s.push(wdResistorH(710, PIN_P, 38, '47k'));
  s.push(wdPath([[748, PIN_P], [748, V5]]));
  s.push(wdPathNum(695, mid - 34, 2));

  // 3 — the fixed threshold the + input is compared against: an even divider,
  // so the − input sits at half the +5V rail.
  s.push(wdPath([[604, PIN_N], [786, PIN_N]]));
  s.push(`<circle cx="786" cy="${PIN_N}" r="2.5" class="hw-wd-junction"/>`);
  s.push(wdResistorV(786, V5, 98, '10k', -1));
  s.push(wdResistorV(786, PIN_N, 40, '10k', -1));
  s.push(wdGround(786, PIN_N + 40));
  s.push(wdPathNum(752, PIN_N, 3));   // on the − wire, clear of the D1 label above

  // 1 — the bleed resistor that pulls Rail B down, so an empty block sits at a
  // known level instead of floating.
  s.push(wdBox(622, mid + 34, 58, 38, '', 'Rail B', 'OUT2'));
  s.push(wdResistorV(651, mid + 72, 28, '10k', -1));
  s.push(wdGround(651, mid + 100));
  s.push(wdPathNum(676, mid + 86, 1));

  const cap = (y: number, t: string) =>
    `<text x="346" y="${y}" text-anchor="start" class="hw-wd-caption-inline">${t}</text>`;
  s.push(cap(mid + 128, '① Rail B → 10k → GND     ② +5V → 47k → D1 → Rail A → LM339 +'));
  s.push(cap(mid + 138, '③ +5V → 10k → LM339 − → 10k → GND     ④ +5V → 10k → LM339 OUT → HC165 bit'));
  s.push(cap(mid + 148, 'Rail A / Rail B are this block’s H-bridge outputs — one detector per block.'));
  return { svg: s.join(''), anchors: { rails_in: [651, PIN_P - 19] } };
}
function wdFragment(chip: ChipCfg, mid: number): WdFrag {
  switch (chip.kind) {
    case 'pca9685': return wdFragTrack(mid);
    case 'ina3221':
    case 'ina219': return wdFragCurrentSense(chip, mid);
    case 'hc595': return wdFragSwitch(chip, mid);
    case 'hc165': return wdFragOccupancy(mid);
    default: return { svg: '', anchors: {} };
  }
}

// Stack lanes so the track-power circuit reads top to bottom the way the current
// flows — measured, then driven, then read back — and the cross-lane ties below
// join lanes that sit next to each other. Switches are an independent subsystem,
// so they go last. Within a kind, order by coordinate so identical chips group.
const WD_KIND_ORDER = ['ina3221', 'ina219', 'pca9685', 'hc165', 'hc595'];
// A lane is centred on its chip box, so its height must cover twice the
// fragment's deepest side. The detector lane is the tall one: its four paths
// reach ~150 below centre, most of it the path list printed under the schematic.
const WD_LANE_H: Record<string, number> = {
  pca9685: 116, ina3221: 128, ina219: 128, hc595: 110, hc165: 304,
};
const WD_DEFAULT_LANE_H = 118;

function wdOrderedChips(): ChipCfg[] {
  const rank = (k: string) => { const i = WD_KIND_ORDER.indexOf(k); return i < 0 ? WD_KIND_ORDER.length : i; };
  return [...state.chips].sort((a, b) => rank(a.kind) - rank(b.kind) || coordSort(a) - coordSort(b));
}

// One physical net shared by two lanes. A tie is drawn only when both ends are
// present, so removing a chip removes the claim that the net is wired — the
// picture never asserts a connection the configured board does not have.
const WD_TIES: { from: string; to: string; label: string }[] = [
  { from: 'vm_out', to: 'vm_in', label: 'one +12V net — shunt, then VM' },
  { from: 'rails_out', to: 'rails_in', label: 'Rail A (Rail B pairs alike)' },
];

// Ties are drawn before the lanes, so one that passes a supply rail or a chip
// box is hidden behind it — the standard reading for a wire crossing without
// joining. Each tie runs from the lowest lane publishing `from` that still sits
// above the lane publishing `to`, which is the nearest end of the shared net.
function wdTies(laneAnchors: WdAnchors[]): string {
  const out: string[] = [];
  for (const tie of WD_TIES) {
    const toIdx = laneAnchors.findIndex((a) => a[tie.to]);
    if (toIdx < 0) continue;
    let fromIdx = -1;
    for (let i = 0; i < toIdx; i++) if (laneAnchors[i][tie.from]) fromIdx = i;
    if (fromIdx < 0) continue;

    const [x1, y1] = laneAnchors[fromIdx][tie.from];
    const [x2, y2] = laneAnchors[toIdx][tie.to];
    const endY = y2 - 3;                       // stop short so the arrowhead clears the box
    const jogY = Math.round((y1 + endY) / 2);
    const pts: Array<[number, number]> = x1 === x2
      ? [[x1, y1], [x2, endY]]
      : [[x1, y1], [x1, jogY], [x2, jogY], [x2, endY]];
    const d = pts.map((pt, i) => `${i === 0 ? 'M' : 'L'} ${pt[0]} ${pt[1]}`).join(' ');
    out.push(`<path d="${d}" class="hw-wd-tie" marker-end="url(#hwWdTieArrow)"/>`);
    // Label to the right of the tie, unless that would run off the canvas — the
    // 8.5px mono face is ~5.1px per character, so the width is predictable.
    const right = Math.max(x1, x2) + 9;
    const fits = right + tie.label.length * 5.1 <= WD_W - 8;
    out.push(`<text x="${fits ? right : Math.min(x1, x2) - 9}" y="${jogY - 4}" ` +
      `text-anchor="${fits ? 'start' : 'end'}" class="hw-wd-tielabel">${esc(tie.label)}</text>`);
  }
  return out.join('');
}

function buildWiringDiagramSvg(): string {
  const chips = wdOrderedChips();
  if (!chips.length) {
    return `<div class="hw-wd-empty">Add a chip below and its wiring appears here — the voltages, resistors, and support parts it connects to — so you can check it against your board before going live.</div>`;
  }
  const W = WD_W, PAD = 14;
  const SPINE_X = 16, SPINE_W = 116, SPINE_R = SPINE_X + SPINE_W;   // right edge = 132
  const CHIP_X = 196, CHIP_W = 150, CHIP_H = 58;

  const heights = chips.map((c) => WD_LANE_H[c.kind] ?? WD_DEFAULT_LANE_H);
  let y = PAD;
  const tops = heights.map((h) => { const t = y; y += h; return t; });
  const totalH = y + PAD;

  const p: string[] = [];
  p.push(`<defs>
    <marker id="hwWdArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0,0 L8,4 L0,8 Z" class="hw-wd-arrowhead"/>
    </marker>
    <marker id="hwWdTieArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0,0 L8,4 L0,8 Z" class="hw-wd-tie-arrowhead"/>
    </marker>
  </defs>`);

  // Controller spine — one bridge every chip fans out from. Turns green once
  // Detect has proven it reachable (mirrors the connection banner below).
  const ctrl = catalog?.controllers.find((c) => c.kind === state.controller.kind) || catalog?.controllers[0];
  const parts = (ctrl?.label || 'CH347 · USB-HID bridge').split('·');
  const ctrlName = (parts[0] || 'CH347').trim();
  const ctrlSub = (parts[1] || '').trim();
  const sTop = PAD, sBot = totalH - PAD, sMid = (sTop + sBot) / 2, sCx = SPINE_X + SPINE_W / 2;
  p.push(`<g class="hw-wd-node role-hub${controllerReachable ? ' ok' : ''}">
    <rect x="${SPINE_X}" y="${sTop}" width="${SPINE_W}" height="${sBot - sTop}" rx="6"/>
    <text x="${sCx}" y="${sTop + 18}" text-anchor="middle" class="hw-wd-zone">BRIDGE</text>
    <text x="${sCx}" y="${sMid - 2}" text-anchor="middle" class="hw-wd-name">${esc(ctrlName)}</text>
    <text x="${sCx}" y="${sMid + 13}" text-anchor="middle" class="hw-wd-sub">${esc(ctrlSub)}</text>
  </g>`);

  // Build every lane first so the ties can be laid out from the anchors, then
  // paint ties underneath the lanes (see wdTies for why the order matters).
  const lanes = chips.map((chip, i) => {
    const mid = tops[i] + heights[i] / 2;
    const frag = wdFragment(chip, mid);
    return {
      svg: wdPath([[SPINE_R, mid], [CHIP_X, mid]], BUS_LABEL[chip.bus] || chip.bus)
        + wdChipBox(CHIP_X, mid - CHIP_H / 2, CHIP_W, CHIP_H, chip)
        + frag.svg,
      anchors: frag.anchors,
    };
  });

  p.push(wdTies(lanes.map((l) => l.anchors)));
  lanes.forEach((lane, i) => {
    p.push(lane.svg);
    if (i < chips.length - 1) {
      const sepY = tops[i] + heights[i];
      p.push(`<line x1="${CHIP_X - 8}" y1="${sepY}" x2="${W - 8}" y2="${sepY}" class="hw-wd-sep"/>`);
    }
  });

  return `<svg viewBox="0 0 ${W} ${totalH}" class="hw-wd-svg" role="img" aria-label="Wiring diagram of your configured chips">${p.join('')}</svg>`;
}

// The <details> open/closed state must survive the many render() calls this
// panel makes (detect, self-test, sweep polling, ...), which each rebuild
// #hw-body; `wiringOpen` (set by the toggle listener in setup) carries it.
let wiringOpen = false;

function wiringDiagramHtml(): string {
  return `<details class="tc-disclosure hw-wiring"${wiringOpen ? ' open' : ''}>
    <summary>Wiring overview</summary>
    <div class="tc-disclosure-body">
      <p class="hw-wd-caption">Your configured chips and how each one wires up — the voltages, resistors, and support parts it connects to — so you can double-check against your board. Box colour matches the bus badges below (I²C blue, SPI purple, GPIO amber); it updates as you edit the list.</p>
      ${buildWiringDiagramSvg()}
    </div>
  </details>`;
}

// --- setup (once) ----------------------------------------------------------
export function setupHardwarePanel(): void {
  const modal = el('hardware-modal');
  const bodyEl = el('hw-body');
  const gateEl = el('hw-gate');
  if (!modal || !bodyEl || !gateEl) {
    console.warn('[App] Hardware panel elements not found');
    return;
  }

  const root = modal;
  root.addEventListener('click', (e) => void onClick(e));
  root.addEventListener('change', (e) => onChange(e));
  root.addEventListener('input', (e) => onInput(e));
  // `toggle` does not bubble, so this only reaches `root` via the capture
  // phase — needed because render() rebuilds #hw-body (and its <details>)
  // from scratch on every interaction, which would otherwise re-close the
  // wiring reference the instant the user did anything else in the panel.
  root.addEventListener('toggle', (e) => {
    const t = e.target as HTMLElement;
    if (t.classList?.contains('hw-wiring')) wiringOpen = (t as HTMLDetailsElement).open;
  }, true);

  // Stop any live/sweep activity (and release the device) when the modal closes
  // via any path — close button, backdrop, or Escape all drop the `active` class.
  new MutationObserver(() => {
    if (!modal.classList.contains('active')) void stopCommissioning();
  }).observe(modal, { attributes: true, attributeFilter: ['class'] });
}

function onEdit(): void { savedSinceEdit = false; }

// --- refresh (each open) ---------------------------------------------------
export async function refreshHardwarePanel(): Promise<void> {
  await stopCommissioning();
  savedSinceEdit = false;
  adding = false;
  i2cScan = [];
  scanned = false;
  manualProven = new Set();
  controllerReachable = false;
  sessionInfo = null;
  devReadback = null;
  coverage = null;
  devBindings = { block_occupancy: {}, block_occupancy_current: {} };
  try {
    const [cat, cfg, cov] = await Promise.all([
      api.getHardwareCatalog(),
      api.getHardwareConfig(),
      // Coverage is advisory, so it must never be the reason the panel fails to
      // open: a failure here leaves the section unrendered and nothing else.
      api.getHardwareCoverage().catch(() => null),
    ]);
    catalog = cat as Catalog;
    coverage = (cov as Coverage) || null;
    const raw = cfg?.config || {};
    state = normalizeConfig(raw);
    sessionInfo = cfg?.session || null;
    // Capture the raw binding maps before normalizeConfig strips them — the
    // Fake-inputs section pokes blocks by their occupancy / current bindings.
    devBindings = {
      block_occupancy: raw.block_occupancy || {},
      block_occupancy_current: raw.block_occupancy_current || {},
    };
  } catch (e) {
    console.error('[App] Failed to load hardware setup:', e);
    showToast('Failed to load hardware setup.', 'error');
    state = clone(DEFAULT_STATE);
  }
  render();
  startDevPoll();
}

function normalizeConfig(cfg: any): HwState {
  return {
    enabled: !!cfg.enabled,
    develop: !!cfg.develop,
    controller: { kind: cfg.controller?.kind || 'ch347', device_port: cfg.controller?.device_port ?? null },
    chips: (cfg.chips || []).map((c: any): ChipCfg => ({
      chip_id: String(c.chip_id),
      kind: String(c.kind),
      bus: String(c.bus || catalogChip(String(c.kind))?.buses?.[0] || 'i2c'),
      i2c_address: c.i2c_address ?? null,
      chain_length: Number(c.chain_length) || 1,
      spi_select: c.spi_select ?? null,
      label: c.label || '',
      active_low: !!c.active_low,
    })),
    idleDeadbandV: Number.isFinite(Number(cfg.idle_deadband_v))
      ? Number(cfg.idle_deadband_v)
      : DEFAULT_IDLE_DEADBAND_V,
  };
}

// --- payload: controller + chips only (binding maps stay untouched) --------
function payload(): any {
  return {
    enabled: state.enabled,
    develop: state.develop,
    controller: { kind: state.controller.kind, device_port: state.controller.device_port },
    chips: state.chips.map((c) => ({
      chip_id: c.chip_id, kind: c.kind, bus: c.bus, i2c_address: c.i2c_address,
      chain_length: c.chain_length, spi_select: c.spi_select, label: c.label,
      active_low: c.active_low,
    })),
    idle_deadband_v: state.idleDeadbandV,
  };
}

// --- readiness -------------------------------------------------------------
interface Check { key: string; ok: boolean; label: string; }
function readiness(): Check[] {
  const checks: Check[] = [];
  checks.push({ key: 'bridge', ok: controllerReachable, label: controllerReachable ? 'Bridge connected' : 'Connect the bridge' });

  const noCoord = state.chips.filter((c) => (c.bus === 'i2c' ? c.i2c_address == null : !c.spi_select));
  checks.push({
    key: 'coord', ok: state.chips.length > 0 && noCoord.length === 0,
    label: noCoord.length ? `Set ${noCoord.length} address/latch` : state.chips.length ? 'Addresses & latches set' : 'Add a chip',
  });

  const unproven = state.chips.filter((c) => chipState(c) !== 'alive');
  checks.push({
    key: 'test', ok: state.chips.length > 0 && unproven.length === 0,
    label: unproven.length ? `${unproven.length} chip${unproven.length > 1 ? 's' : ''} unproven` : 'Every chip proven',
  });
  return checks;
}
const isReady = (): boolean => state.chips.length > 0 && readiness().every((c) => c.ok);

// --- render ----------------------------------------------------------------
function render(): void {
  const body = el('hw-body');
  const gate = el('hw-gate');
  if (!body || !gate || !catalog) return;
  body.innerHTML = wiringDiagramHtml() + controllerStageHtml() + chipsStageHtml()
    + coverageStageHtml() + devSectionHtml();
  gate.innerHTML = gateHtml();
}

// --- layout coverage section ------------------------------------------------
// Stage 3. The two stages above are about the board: which chips exist and
// whether they answer. This one is about the layout: how much of it is actually
// bound to those chips. A board can be perfectly proven with half the blocks
// bound to nothing, and saving is no help — the config validator checks the
// bindings that exist and has nothing to say about the ones nobody made.
//
// Reported, never enforced. Wiring a layout up an entity at a time is how it
// gets wired, so this names the gap and leaves the judgement to the operator.
const COVERAGE_NAME_LIMIT = 8;

function coverageStageHtml(): string {
  if (!coverage) return '';
  if (!coverage.blocks.total && !coverage.switches.total) return '';
  const rows = coverageRows(coverage);
  const stale = staleBindings(coverage);
  const done = coverage.complete;
  const rowsHtml = rows.map(coverageRowHtml).join('');
  const staleHtml = stale.length
    ? `<div class="hw-coverage-row warn">
        <span class="hw-coverage-label">Bindings with no entity</span>
        <span class="hw-coverage-count">${stale.length}</span>
        <span class="hw-coverage-note">saved against blocks or switches this layout no longer has — dead configuration, and it makes the counts above read better than they are: ${coverageNames(stale)}</span>
      </div>`
    : '';
  return `<section class="hw-stage${done ? ' done' : ''}">
    <div class="hw-stage-head"><span class="hw-stage-num">${done ? '✓' : '3'}</span>
      <span class="hw-stage-title"><i class="ti ti-plug-connected" aria-hidden="true"></i> What your layout is wired to</span></div>
    <p class="hw-stage-desc">Blocks and switches are bound to ports in their own editor panels, not here — this is the tally. Anything unwired below keeps working in simulation and silently stops working once hardware is live.</p>
    <div class="hw-indent"><div class="hw-coverage">${rowsHtml}${staleHtml}</div></div>
  </section>`;
}

function coverageRowHtml(row: CoverageRow): string {
  if (!row.total) return '';
  const ok = row.missing.length === 0;
  return `<div class="hw-coverage-row ${ok ? 'ok' : 'warn'}" data-key="${row.key}">
      <span class="hw-coverage-label">
        <i class="ti ti-${ok ? 'check' : 'alert-triangle'}" aria-hidden="true"></i> ${esc(row.label)}</span>
      <span class="hw-coverage-count">${row.bound} of ${row.total}</span>
      <span class="hw-coverage-note">${
        ok ? 'all wired' : `${esc(coverageNames(row.missing))} ${esc(row.consequence)}`
      }</span>
    </div>`;
}

/** Name the unwired entities, capped — a long list is a summary, not a census. */
function coverageNames(ids: string[]): string {
  const shown = ids.slice(0, COVERAGE_NAME_LIMIT).join(', ');
  const rest = ids.length - COVERAGE_NAME_LIMIT;
  return rest > 0 ? `${shown} and ${rest} more` : shown;
}

// --- develop-mode "Fake inputs" section ------------------------------------
// Rendered only when the RUNNING session is on the fake device
// (`session.develop_active`): a row per bound block with an Occupied toggle, a
// current-mA field, and the live read-back the 1 s poll refreshes.
function devSectionHtml(): string {
  if (!sessionInfo?.develop_active) return '';
  const head = `<div class="hw-stage-head"><span class="hw-stage-num"><i class="ti ti-flask" aria-hidden="true"></i></span>
      <span class="hw-stage-title">Fake inputs</span></div>
    <p class="hw-stage-desc">This session runs on the in-memory fake device. Poke a block's occupancy detector or current channel and watch the real occupancy pipeline react. Current decides only while a block is driven; an idle block reads its digital bit.</p>`;
  const rows = devRows(devBindings, devReadback);
  if (!rows.length) {
    return `<section class="hw-dev-section">${head}
      <p class="hw-dev-empty">No block has an occupancy detector or current-sense binding yet. Bind one in a block's editor to poke it here.</p></section>`;
  }
  const rowHtml = rows.map((r) => {
    const occ = r.hasDigital
      ? `<button type="button" class="btn hw-btn-sm ${r.occupancyBit ? 'btn-accent' : 'btn-secondary'}" data-hw="dev-occ" data-id="${esc(r.blockId)}">${r.occupancyBit ? 'Occupied' : 'Clear'}</button>`
      : `<span class="hw-dev-na">—</span>`;
    const cur = r.hasCurrent
      ? `<span class="hw-dev-cur"><input type="number" class="setting-input-compact hw-dev-cur-in" data-hw="dev-current-in" data-id="${esc(r.blockId)}" value="${r.currentMa ?? 0}" step="1" /><span class="hw-dev-unit">mA</span>
          <button type="button" class="btn hw-btn-sm btn-secondary" data-hw="dev-current" data-id="${esc(r.blockId)}">Set</button></span>`
      : `<span class="hw-dev-na">—</span>`;
    const bit = r.occupancyBit == null ? '—' : (r.occupancyBit ? '1' : '0');
    const ma = r.currentMa == null ? '—' : `${r.currentMa} mA`;
    const volts = r.volts == null ? '—' : `${r.volts} V`;
    const verdict = r.occupied == null ? '—' : (r.occupied ? 'occupied' : 'clear');
    return `<div class="hw-dev-row">
      <span class="hw-dev-block">${esc(r.blockId)}</span>
      <span class="hw-dev-ctl">${occ}</span>
      <span class="hw-dev-ctl">${cur}</span>
      <span class="hw-dev-read">bit ${bit} · ${ma} · ${volts} · <strong class="${r.occupied ? 'occ' : ''}">${verdict}</strong></span>
    </div>`;
  }).join('');
  return `<section class="hw-dev-section">${head}
    <div class="hw-dev-rows">
      <div class="hw-dev-row hw-dev-head"><span>Block</span><span>Occupancy</span><span>Current</span><span>Reads back</span></div>
      ${rowHtml}
    </div></section>`;
}

function controllerStageHtml(): string {
  const ctrl = catalog!.controllers.find((c) => c.kind === state.controller.kind) || catalog!.controllers[0];
  const portRow = ctrl?.needs_device_port
    ? `<div class="hw-row"><span class="hw-lbl">Port</span>
        <input type="text" class="setting-input-compact" data-hw="device-port"
          placeholder="auto (USB VID/PID)" value="${esc(state.controller.device_port ?? '')}" spellcheck="false" /></div>`
    : '';
  // Every address that answered, each saying whether a configured chip claims
  // it. An unclaimed address is the useful half: a chip strapped somewhere the
  // user did not expect shows up here instead of as silence.
  const claimed = new Set(state.chips
    .filter((c) => c.bus === 'i2c' && c.i2c_address != null)
    .map((c) => hexAddr(c.i2c_address!)));
  const addrChips = i2cScan.map((a) => {
    const mine = claimed.has(a);
    return `<span class="hw-addr-chip${mine ? ' claimed' : ' unclaimed'}" title="${mine ? 'A configured chip sits here' : 'Answering, but no configured chip uses this address'}">${esc(a)}</span>`;
  }).join('');
  const found = i2cScan.length
    ? `${i2cScan.length} device${i2cScan.length === 1 ? '' : 's'} answering on the I²C bus.`
    : 'Nothing answering on the I²C bus.';
  const conn = controllerReachable
    ? `<div class="hw-conn ok"><span class="hw-conn-dot"></span>
        <span><strong>Connected.</strong> ${found}</span>
        <span class="hw-scan-chips">${addrChips}</span></div>`
    : `<div class="hw-conn"><span class="hw-conn-dot"></span>
        <span>Not connected — running in <strong>simulation</strong>. Detect to talk to the board.</span></div>`;
  const done = controllerReachable ? ' done' : '';
  // One button, two jobs: it opens the bridge the first time and re-reads the
  // bus after that. It never re-opens chips or changes the configuration, so it
  // is safe to press whenever the board has been re-plugged or re-strapped.
  const scanBtn = controllerReachable
    ? `<button type="button" class="btn btn-secondary hw-btn-sm" data-hw="detect" title="Read the I²C bus again — pick up a chip that was just plugged in or re-strapped"><i class="ti ti-refresh" aria-hidden="true"></i> Rescan bus</button>`
    : `<button type="button" class="btn btn-accent hw-btn-sm" data-hw="detect"><i class="ti ti-plug-connected" aria-hidden="true"></i> Detect controller</button>`;
  return `<section class="hw-stage${done}">
    <div class="hw-stage-head"><span class="hw-stage-num">${controllerReachable ? '✓' : '1'}</span>
      <span class="hw-stage-title">Connect the bridge</span></div>
    <p class="hw-stage-desc">The USB board every message travels through. Detect reads every I²C address that answers; rescan after plugging a chip in or changing its address straps.</p>
    <div class="hw-row hw-indent">
      <span class="hw-lbl">Bridge</span>
      <select class="setting-select" data-hw="controller-kind">
        ${catalog!.controllers.map((c) => `<option value="${esc(c.kind)}"${c.kind === state.controller.kind ? ' selected' : ''}>${esc(c.label)}</option>`).join('')}
      </select>
      ${scanBtn}
    </div>
    ${portRow ? `<div class="hw-indent">${portRow}</div>` : ''}
    <div class="hw-indent">${conn}</div>
  </section>`;
}

function chipsStageHtml(): string {
  const proven = state.chips.length > 0 && readiness()[1].ok && readiness()[2].ok;
  const groups = BUS_ORDER.map(busGroupHtml).join('');
  return `<section class="hw-stage${proven ? ' done' : ''}">
    <div class="hw-stage-head"><span class="hw-stage-num">${proven ? '✓' : '2'}</span>
      <span class="hw-stage-title"><i class="ti ti-cpu" aria-hidden="true"></i> Your chips, by bus</span></div>
    <p class="hw-stage-desc">Every chip on the board — grouped by the bus it lives on, ordered by its address or latch line. An I²C chip proves itself by answering the bus scan; a shift register answers nothing, so run its test and watch the board.</p>
    <div class="hw-indent">${groups}${addFormHtml()}</div>
  </section>`;
}

function busGroupHtml(bus: string): string {
  const chips = state.chips.filter((c) => c.bus === bus).sort((a, b) => coordSort(a) - coordSort(b));
  const cards = chips.length ? chips.map(chipCardHtml).join('') : `<div class="hw-bus-empty">No chips on this bus yet.</div>`;
  return `<div class="hw-bus" data-bus="${bus}">
    <div class="hw-bus-head"><span class="hw-bus-badge hw-bus-${bus}">${BUS_LABEL[bus]}</span>
      <span class="hw-bus-order">${BUS_ORDERED_BY[bus]}</span></div>
    ${cards}</div>`;
}

function chipCardHtml(chip: ChipCfg): string {
  const ck = catalogChip(chip.kind);
  const dir = chipDirection(chip);
  const stateCls = chipState(chip);
  const badge = stateBadgeHtml(chip, stateCls);
  const dirBadge = `<span class="hw-dir ${dir === 'output' ? 'out' : 'in'}">${dir === 'output' ? 'OUTPUT' : 'INPUT'}</span>`;
  const chained = ck?.cascadable ? ` (${Math.max(1, chip.chain_length)} chip${chip.chain_length > 1 ? 's' : ''} chained)` : '';
  const on = activity?.chipId === chip.chip_id;
  const testBtn = dir === 'output'
    ? `<button type="button" class="hw-chip-action${on ? ' on' : ''}" data-hw="sweep" data-id="${esc(chip.chip_id)}"><i class="ti ti-${on ? 'player-stop' : 'player-play'}" aria-hidden="true"></i> ${on ? 'Stop' : 'Sweep'}</button>`
    : `<button type="button" class="hw-chip-action${on ? ' on' : ''}" data-hw="live" data-id="${esc(chip.chip_id)}"><i class="ti ti-${on ? 'player-stop' : 'activity'}" aria-hidden="true"></i> ${on ? 'Stop' : 'Read live'}</button>`;
  return `<div class="hw-chip ${stateCls}" data-chip="${esc(chip.chip_id)}">
    <div class="hw-chip-top">
      ${coordCellHtml(chip)}
      <div class="hw-chip-id">
        <span class="hw-chip-name">${esc(chip.kind)} ${dirBadge}</span>
        <span class="hw-chip-meta">${esc(chipLabel(chip))} · ${chipPorts(chip)} ports${chained}</span>
      </div>
      <div class="hw-chip-actions">
        ${badge}
        ${testBtn}
        <button type="button" class="hw-chip-remove" data-hw="remove" data-id="${esc(chip.chip_id)}" title="Remove chip" aria-label="Remove chip">&times;</button>
      </div>
    </div>
    ${chip.bus !== 'i2c' && ck?.cascadable ? portCountRowHtml(chip) : ''}
    ${ck?.port_kind === 'digital_in' ? polarityRowHtml(chip) : ''}
    ${portsHtml(chip)}
  </div>`;
}

// What the card says about whether the chip is really there, in the terms that
// bus can actually prove. An I²C chip either answered the scan or it did not.
// A shift register answers nothing, so its badge reports the only fact there
// is — whether the user has run its test.
function stateBadgeHtml(chip: ChipCfg, st: ChipState): string {
  if (chip.bus !== 'i2c') {
    return st === 'alive'
      ? `<span class="hw-state-badge alive" title="You ran this chip's test. A shift register cannot be detected any other way — it has no address and no reply."><i class="ti ti-check" aria-hidden="true"></i> Tested by you</span>`
      : `<span class="hw-state-badge untested" title="A shift register cannot be detected: it has no address and sends no reply, so it reads the same unplugged as wired. Run the test and watch the board.">Test to prove</span>`;
  }
  if (st === 'alive') {
    return `<span class="hw-state-badge alive" title="This address answered the last bus scan"><i class="ti ti-check" aria-hidden="true"></i> On the bus</span>`;
  }
  if (st === 'noresp') {
    return `<span class="hw-state-badge noresp" title="The scan ran and nothing answered at this address — check the wiring and the address straps"><i class="ti ti-alert-triangle" aria-hidden="true"></i> Not on the bus</span>`;
  }
  return `<span class="hw-state-badge untested">Not scanned yet</span>`;
}

// Detector polarity for a shift-in chain. A comparator with no inverter stage
// (an LM339 straight into the '165) pulls its line LOW when a train draws
// current, so the raw bit is backwards. The hardware layer flips it, so this is
// purely a statement about the wiring — nothing above the interface sees it.
function polarityRowHtml(chip: ChipCfg): string {
  return `<div class="hw-polarity">
    <span class="hw-lbl">Detector</span>
    <label class="hw-polarity-opt">
      <input type="radio" name="pol-${esc(chip.chip_id)}" data-hw="polarity" data-id="${esc(chip.chip_id)}"
        value="high"${chip.active_low ? '' : ' checked'} />
      <span>Active&nbsp;high <em>1 = occupied</em></span>
    </label>
    <label class="hw-polarity-opt">
      <input type="radio" name="pol-${esc(chip.chip_id)}" data-hw="polarity" data-id="${esc(chip.chip_id)}"
        value="low"${chip.active_low ? ' checked' : ''} />
      <span>Active&nbsp;low <em>0 = occupied</em></span>
    </label>
    <span class="hw-hint-inline">LM339 with no inverter is active low</span>
  </div>`;
}

function coordCellHtml(chip: ChipCfg): string {
  if (chip.bus === 'i2c') {
    // After a scan the list is what the bus answered, so picking the address the
    // chip is actually at is reading the list, not guessing; before one it is
    // the kind's straps first and the rest of the legal range behind them.
    const opts = addrGroupsHtml(addrGroupsFor(chip.kind, chip.i2c_address), chip.i2c_address);
    const title = detectedAddrs().length
      ? 'I²C address — every address answering the last bus scan; ● marks one answering now'
      : 'I²C address — detect the bus to list what is really there';
    return `<span class="hw-coord"><select class="hw-coord-select" data-hw="addr" data-id="${esc(chip.chip_id)}" title="${title}">
      ${opts}
    </select></span>`;
  }
  const lines = latchLines(chip.bus);
  return `<span class="hw-coord"><select class="hw-coord-select" data-hw="latch" data-id="${esc(chip.chip_id)}" title="Latch line">
    ${lines.map((l) => `<option value="${esc(l)}"${l === chip.spi_select ? ' selected' : ''}>${esc(l.toUpperCase())}</option>`).join('')}
  </select></span>`;
}

function portCountRowHtml(chip: ChipCfg): string {
  const ck = catalogChip(chip.kind)!;
  return `<div class="hw-portcount">
    <span class="hw-lbl">Ports</span>
    <input type="number" class="hw-portcount-input" data-hw="ports" data-id="${esc(chip.chip_id)}"
      min="${ck.ports_per_unit}" step="${ck.ports_per_unit}" value="${chipPorts(chip)}" />
    <span class="hw-hint-inline">${ck.ports_per_unit} per chip</span>
  </div>`;
}

// The chip's lines, one pill each. This grid is at *line* granularity, not port
// granularity: clicking a pill drives that one hardware line, so a PCA9685 shows
// all 16 PWM channels rather than the 8 H-bridge pairs those channels make up.
// A block's two rails can sit on any two channels, so every channel has to be
// individually pulseable to check its wiring.
function portsHtml(chip: ChipCfg): string {
  const ck = catalogChip(chip.kind);
  const n = chipLines(chip);
  const dir = chipDirection(chip);
  const current = isCurrentSense(chip);
  let legend: string;
  if (ck?.port_kind === 'bipolar_pair') legend = `${n} PWM channels · ${chipPorts(chip)} bipolar ports, ±12V`;
  else if (ck?.port_kind === 'digital_out') legend = `${n} output bits · 0/1`;
  else if (ck?.port_kind === 'digital_in') legend = `${n} input bits`;
  else legend = `${n} current channels · mA`;
  const hint = dir === 'output'
    ? (ck?.port_kind === 'bipolar_pair' ? 'click a channel to drive it 1 s' : 'click a port to drive it 1 s')
    : 'Read live to watch these';

  const act = activity?.chipId === chip.chip_id ? activity : null;
  const pills: string[] = [];
  for (let p = 0; p < n; p++) {
    const name = lineLabel(chip, p);
    let cls = 'hw-port', txt = name, extra = '';
    if (act?.kind === 'sweep' && act.port === p) cls += ' sweep';
    if (act?.kind === 'live') {
      const v = act.values[p];
      if (current) { cls += ' wide'; txt = v != null ? Math.round(v) + 'mA' : name; if (v > 30) cls += ' live'; }
      else if (v) cls += ' live';
    }
    if (dir === 'output') {
      pills.push(`<button type="button" class="${cls}${extra}" data-hw="pulse" data-id="${esc(chip.chip_id)}" data-port="${p}" title="${name} — click to drive high for 1 s">${txt}</button>`);
    } else {
      pills.push(`<span class="${cls}${extra}" title="${name}">${txt}</span>`);
    }
  }
  return `<div class="hw-ports">
    <div class="hw-ports-legend"><span>${legend}</span><span class="hw-test-hint">· ${hint}</span></div>
    <div class="hw-port-grid">${pills.join('')}</div>
  </div>`;
}

// --- add-chip form ---------------------------------------------------------
function addFormHtml(): string {
  if (!adding) {
    return `<button type="button" class="btn btn-secondary hw-add-open" data-hw="add-open"><i class="ti ti-plus" aria-hidden="true"></i> Add a chip</button>`;
  }
  const kinds = catalog!.chips;
  const typeOpts = kinds.map((k) => `<option value="${esc(k.kind)}">${esc(k.label)}</option>`).join('');
  return `<div class="hw-add-form" id="hw-add-form">
    <div class="hw-add-fld"><span class="hw-lbl">Chip</span>
      <select class="setting-select" id="hw-af-type">${typeOpts}</select></div>
    <div class="hw-add-fld"><span class="hw-lbl">Bus</span>
      <select class="setting-select" id="hw-af-bus"></select></div>
    <div class="hw-add-fld"><span class="hw-lbl" id="hw-af-coord-lbl">Address</span>
      <span id="hw-af-coord"></span></div>
    <div class="hw-add-fld"><span class="hw-lbl">Ports</span>
      <input type="number" class="hw-portcount-input" id="hw-af-ports" min="1" value="8" /></div>
    <div class="hw-add-fld hw-add-actions">
      <button type="button" class="btn btn-accent hw-btn-sm" data-hw="add-commit">Add chip</button>
      <button type="button" class="btn btn-secondary hw-btn-sm" data-hw="add-cancel">Cancel</button>
    </div>
  </div>`;
}

function syncAddForm(): void {
  const typeSel = el<HTMLSelectElement>('hw-af-type');
  const busSel = el<HTMLSelectElement>('hw-af-bus');
  const coordLbl = el('hw-af-coord-lbl');
  const coord = el('hw-af-coord');
  const ports = el<HTMLInputElement>('hw-af-ports');
  if (!typeSel || !busSel || !coordLbl || !coord || !ports) return;
  const ck = catalogChip(typeSel.value);
  if (!ck) return;
  busSel.innerHTML = ck.buses.map((b) => `<option value="${esc(b)}">${BUS_LABEL[b]}</option>`).join('');
  ports.value = String(ck.ports_per_unit);
  ports.step = String(ck.ports_per_unit);
  ports.disabled = !ck.cascadable;
  syncAddCoord();

  function syncAddCoord(): void {
    const bus = busSel!.value;
    if (bus === 'i2c') {
      coordLbl!.textContent = 'Address';
      // Same rule as a configured chip's dropdown: the bus wins over the strap
      // range once it has been read. The pre-selection is the first answering
      // address no configured chip has claimed — the one a chip just wired in
      // is most likely sitting on — falling back to the kind's default.
      const groups = addrGroupsFor(typeSel!.value, null);
      const addrs = groups.flatMap((g) => g.addrs);
      const claimed = new Set(state.chips.map((c) => c.i2c_address).filter((a) => a != null));
      const pick = addrs.find((a) => onBus(a) && !claimed.has(a))
        ?? (addrs.includes(ck!.default_i2c_address ?? -1) ? ck!.default_i2c_address : addrs[0]);
      coord!.innerHTML = `<select class="setting-select hw-coord-select" id="hw-af-coord-val">
        ${addrGroupsHtml(groups, pick ?? null)}
      </select>`;
    } else {
      coordLbl!.textContent = 'Latch line';
      const lines = latchLines(bus);
      coord!.innerHTML = `<select class="setting-select" id="hw-af-coord-val">${lines.map((l) => `<option value="${esc(l)}">${esc(l.toUpperCase())}</option>`).join('')}</select>`;
    }
  }
  typeSel.onchange = syncAddForm;
  busSel.onchange = syncAddCoord;
}

function commitAdd(): void {
  const typeSel = el<HTMLSelectElement>('hw-af-type');
  const busSel = el<HTMLSelectElement>('hw-af-bus');
  const portsEl = el<HTMLInputElement>('hw-af-ports');
  const coordVal = el<HTMLInputElement>('hw-af-coord-val');
  if (!typeSel || !busSel || !portsEl) return;
  const ck = catalogChip(typeSel.value);
  if (!ck) return;
  const bus = busSel.value;
  const chip: ChipCfg = {
    chip_id: nextChipId(typeSel.value), kind: typeSel.value, bus,
    i2c_address: null, chain_length: 1, spi_select: null, label: '',
    active_low: false,
  };
  if (bus === 'i2c') {
    const addr = parseAddr(coordVal?.value || '');
    if (!Number.isFinite(addr)) { showToast('Enter an I²C address (e.g. 0x40).', 'error'); return; }
    chip.i2c_address = addr;
  } else {
    chip.spi_select = coordVal?.value || latchLines(bus)[0];
  }
  if (ck.cascadable) {
    const ports = Math.max(ck.ports_per_unit, parseInt(portsEl.value, 10) || ck.ports_per_unit);
    chip.chain_length = Math.max(1, Math.round(ports / ck.ports_per_unit));
  }
  state.chips.push(chip);
  adding = false;
  onEdit();
  render();
  showToast('Chip added.', 'success', 1800);
}

function nextChipId(kind: string): string {
  const existing = new Set(state.chips.map((c) => c.chip_id));
  let n = 1;
  while (existing.has(`${kind}-${n}`)) n++;
  return `${kind}-${n}`;
}

// --- footer gate -----------------------------------------------------------
function gateHtml(): string {
  const checks = readiness();
  const ready = isReady();
  const pills = checks.map((c) =>
    `<span class="hw-check ${c.ok ? 'ok' : 'warn'}"${c.ok ? '' : ` data-hw="jump" data-key="${c.key}"`}>` +
    `<i class="ti ti-${c.ok ? 'check' : 'alert-triangle'}" aria-hidden="true"></i> ${esc(c.label)}</span>`).join('');
  const effEnabled = state.enabled && ready;
  // One constant label, so the TOGGLE POSITION is what carries on/off. Naming
  // the off state in the label instead ("Simulation only") inverted its meaning:
  // a switch sitting off under those words reads as "simulation-only is off, so
  // hardware must be on" — the opposite of the truth. The sub-line names the
  // current consequence and the next step.
  const enableTitle = 'Use real hardware';
  const enableSub = !ready
    ? 'simulation only — clear the amber steps first'
    : effEnabled
      ? 'applies after restart'
      : 'off — simulation only';
  const restart = savedSinceEdit
    ? `<span class="hw-restart-note">Applies after a restart.</span>
       <button type="button" class="btn btn-primary" data-hw="restart">Restart now</button>`
    : `<button type="button" class="btn ${effEnabled ? 'btn-accent' : 'btn-secondary'}" data-hw="save">Save</button>`;
  const devActive = !!sessionInfo?.develop_active;
  // Truth badge: this session is really running on the fake device.
  const badge = devActive
    ? `<span class="hw-develop-badge" title="This session is running on the in-memory fake device — no USB hardware.">
        <i class="ti ti-flask" aria-hidden="true"></i> DEVELOP · fake hardware</span>`
    : (state.develop ? `<span class="hw-develop-hint">develop saved — restart to apply</span>` : '');
  // The develop toggle is NOT gated by the readiness checklist: its purpose is
  // hardware-free work, so it can be flipped and saved before any chip responds.
  const devToggle = `<label class="hw-develop ${state.develop ? 'on' : ''}" data-hw="develop"
      title="Run the real-mode stack against an in-memory fake device — build and test with no hardware attached. Applies after restart.">
      <span class="hw-toggle"></span>
      <span class="hw-develop-text"><span class="hw-develop-label">Develop mode</span>
        <span class="hw-develop-sub">fake hardware</span></span>
    </label>`;
  // Which occupancy source answers for a block depends on whether it is being
  // driven, and this is where that line sits. Not gated by the readiness
  // checklist — it is a number, not a chip, and costs nothing to set early.
  const deadband = `<label class="hw-deadband" title="A block commanded above this voltage is read by its current-sense channel; at or below it, by its '165 detector.">
      <span class="hw-deadband-text"><span class="hw-deadband-label">Idle threshold</span>
        <span class="hw-deadband-sub">below this, a block counts as parked</span></span>
      <span class="hw-deadband-field">
        <input type="number" data-hw="idle-deadband" class="property-input"
          min="0" max="${MAX_IDLE_DEADBAND_V - 0.01}" step="0.05"
          value="${state.idleDeadbandV}" aria-label="Idle threshold in volts">
        <span class="hw-deadband-unit">V</span>
      </span>
    </label>`;
  return `<div class="hw-checklist">${pills}${badge}</div>
    <div class="hw-gate-actions">
      ${deadband}
      ${devToggle}
      <label class="hw-enable ${effEnabled ? 'on' : ''} ${ready ? '' : 'locked'}" data-hw="enable"
        title="On, the running simulation reads your detectors and drives your track and points. Off, it runs entirely on the simulated drivers and never touches the rig.">
        <span class="hw-enable-text"><span class="hw-enable-label">${enableTitle}</span>
          <span class="hw-enable-sub">${enableSub}</span></span>
        <span class="hw-toggle"></span>
      </label>
      ${restart}
    </div>`;
}

// --- event handlers --------------------------------------------------------
async function onClick(e: Event): Promise<void> {
  const target = (e.target as HTMLElement).closest<HTMLElement>('[data-hw]');
  if (!target) return;
  const act = target.dataset.hw;
  const id = target.dataset.id || '';
  switch (act) {
    case 'detect': return void scanBus();
    case 'remove': return removeChip(id);
    case 'sweep': return void toggleSweep(id);
    case 'live': return void toggleLive(id);
    case 'pulse': return void pulsePort(id, Number(target.dataset.port));
    case 'add-open': adding = true; render(); syncAddForm(); return;
    case 'add-cancel': adding = false; render(); return;
    case 'add-commit': return commitAdd();
    case 'enable': return toggleEnable();
    case 'develop': return toggleDevelop();
    case 'dev-occ': return void toggleDevOccupancy(id);
    case 'dev-current': return void setDevCurrent(id);
    case 'save': return void save();
    case 'restart': return void restart();
    case 'jump': return jumpTo(target.dataset.key || '');
  }
}

function onChange(e: Event): void {
  const t = (e.target as HTMLElement).closest<HTMLElement>('[data-hw]');
  if (!t) return;
  const id = t.dataset.id || '';
  if (t.dataset.hw === 'controller-kind') { state.controller.kind = (t as HTMLSelectElement).value; onEdit(); render(); }
  else if (t.dataset.hw === 'latch') { setChip(id, (c) => { c.spi_select = (t as HTMLSelectElement).value; }); }
  else if (t.dataset.hw === 'addr') { setAddress(id, (t as HTMLInputElement).value); }
  else if (t.dataset.hw === 'ports') { setPorts(id, (t as HTMLInputElement).value); }
  else if (t.dataset.hw === 'polarity') {
    setChip(id, (c) => { c.active_low = (t as HTMLInputElement).value === 'low'; });
  }
}

function onInput(e: Event): void {
  const t = (e.target as HTMLElement).closest<HTMLElement>('[data-hw="device-port"]');
  if (t) { state.controller.device_port = (t as HTMLInputElement).value.trim() || null; onEdit(); }
  const dead = (e.target as HTMLElement).closest<HTMLElement>('[data-hw="idle-deadband"]');
  if (dead) { state.idleDeadbandV = parseDeadband((dead as HTMLInputElement).value); onEdit(); }
}

// Blank / unparseable reads as the default rather than 0, because 0 would mean
// "any commanded voltage at all counts as driven" — a silent behavior change
// from an empty box. Clamped to the range the server accepts.
export function parseDeadband(raw: string): number {
  const text = String(raw).trim();
  // Number('') is 0, not NaN, so an empty box has to be caught before the parse
  // or clearing the field would quietly mean "no deadband at all".
  if (!text) return DEFAULT_IDLE_DEADBAND_V;
  const n = Number(text);
  if (!Number.isFinite(n)) return DEFAULT_IDLE_DEADBAND_V;
  return Math.min(Math.max(n, 0), MAX_IDLE_DEADBAND_V - 0.01);
}

function setChip(id: string, mut: (c: ChipCfg) => void): void {
  const chip = state.chips.find((c) => c.chip_id === id);
  if (!chip) return;
  mut(chip);
  // Re-strapping or re-wiring a chip retires the proof the user gathered for
  // the old settings; the badge drops back to unproven until it is re-earned.
  manualProven.delete(id);
  onEdit();
  render();
}
function setAddress(id: string, raw: string): void {
  const addr = parseAddr(raw);
  setChip(id, (c) => { c.i2c_address = Number.isFinite(addr) ? addr : null; });
}
function setPorts(id: string, raw: string): void {
  const chip = state.chips.find((c) => c.chip_id === id);
  const ck = chip && catalogChip(chip.kind);
  if (!chip || !ck) return;
  const ports = Math.max(ck.ports_per_unit, parseInt(raw, 10) || ck.ports_per_unit);
  setChip(id, (c) => { c.chain_length = Math.max(1, Math.round(ports / ck.ports_per_unit)); });
}

function removeChip(id: string): void {
  if (activity?.chipId === id) void stopCommissioning();
  state.chips = state.chips.filter((c) => c.chip_id !== id);
  manualProven.delete(id);
  onEdit();
  render();
}

function toggleEnable(): void {
  if (!isReady()) { showToast('Finish the amber steps to enable hardware.', 'warning', 3600); return; }
  state.enabled = !state.enabled;
  onEdit();
  render();
}

function toggleDevelop(): void {
  state.develop = !state.develop;
  // Flipping the toggle changes which device a scan talks to, so whatever the
  // last scan proved was proof about the OTHER device: fake addresses would go
  // on reading as "Connected" after switching to real, and vice versa. Retire
  // it the same way re-strapping a chip does (see `setChip`) — the bridge drops
  // back to unproven until Detect is pressed against the device now selected.
  controllerReachable = false;
  scanned = false;
  i2cScan = [];
  manualProven = new Set();
  onEdit();
  render();
  // Detect follows the toggle immediately — the scan is run against the config
  // being edited, so no Save is needed to change which device answers. Saving
  // (and restarting) is what moves the running SESSION onto the fake.
  showToast(
    state.develop
      ? 'Develop mode on — Detect now runs against the fake device.'
      : 'Develop mode off — Detect now talks to the real board.',
    'info', 4000);
}

// --- develop-mode fake-input poking ----------------------------------------
async function toggleDevOccupancy(id: string): Promise<void> {
  const cur = devReadback?.blocks?.[id]?.occupancy_bit ?? false;
  try {
    await api.setHwDebugOccupancy(id, !cur);
    await pollDev();
  } catch (e: any) {
    showToast(e?.message || 'Failed to set occupancy.', 'error', 3000);
  }
}

async function setDevCurrent(id: string): Promise<void> {
  const input = Array.from(document.querySelectorAll<HTMLInputElement>('[data-hw="dev-current-in"]'))
    .find((i) => i.dataset.id === id);
  const ma = Number(input?.value) || 0;
  try {
    await api.setHwDebugCurrent(id, ma);
    await pollDev();
  } catch (e: any) {
    showToast(e?.message || 'Failed to set current.', 'error', 3000);
  }
}

function startDevPoll(): void {
  if (devPollTimer || !sessionInfo?.develop_active) return;
  void pollDev();
  devPollTimer = setInterval(() => void pollDev(), 1000);
}

async function pollDev(): Promise<void> {
  if (!sessionInfo?.develop_active) return;
  try {
    const res = await api.getHwDebugState();
    if (res?.develop_active) {
      devReadback = res;
      // Don't clobber a current-mA field the user is editing.
      const active = document.activeElement as HTMLElement | null;
      if (active?.getAttribute?.('data-hw') !== 'dev-current-in') render();
    }
  } catch { /* transient */ }
}

function jumpTo(key: string): void {
  const sel = key === 'bridge' ? '.hw-stage' : '.hw-bus';
  el('hw-body')?.querySelector(sel)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// --- detect / rescan -------------------------------------------------------
// Read the bus and repaint. This is the panel's only automatic proof, and it
// asks the bus rather than the configuration: it re-reads which addresses are
// answering right now, so a chip that was just plugged in, unplugged, or
// re-strapped shows its new truth without touching the chip list.
async function scanBus(): Promise<void> {
  const btn = document.querySelector<HTMLButtonElement>('[data-hw="detect"]');
  if (btn) btn.disabled = true;
  try {
    const res = await api.scanHardwareBus(payload());
    controllerReachable = !!res?.ok && !!res?.controller?.reachable;
    i2cScan = res?.controller?.i2c_scan || [];
    scanned = controllerReachable;
    render();
    if (!controllerReachable) {
      showToast(res?.error ? `Controller not reachable: ${res.error}` : 'Controller not reachable.', 'error', 4000);
      return;
    }
    const i2cChips = state.chips.filter((c) => c.bus === 'i2c');
    const found = i2cChips.filter((c) => onBus(c.i2c_address)).length;
    showToast(
      i2cChips.length
        ? `Bus scanned — ${found}/${i2cChips.length} configured I²C chips answered.`
        : `Bus scanned — ${i2cScan.length} device${i2cScan.length === 1 ? '' : 's'} answering.`,
      'success', 2500);
  } catch (e: any) {
    showToast(e?.message || 'Scan failed.', 'error', 4000);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Drive one line for a measurable moment — the "does this wire respond" check.
// `line` is the raw hardware line the grid pill stands for (a PWM channel, a
// shift-out bit), which is exactly what run_port_test drives.
//
// The server holds the line ~1s so a meter can read it, which is long enough
// that a second click lands mid-pulse; `pulsing` drops those rather than let two
// tests interleave writes on one chip.
let pulsing = false;

async function pulsePort(id: string, line: number): Promise<void> {
  if (activity || pulsing) return;   // don't pulse while a sweep/live session (or another pulse) holds the device
  const chip = state.chips.find((c) => c.chip_id === id);
  pulsing = true;
  try {
    const res = await api.runHardwarePortTest(payload(), id, line, 'pulse_output');
    if (!res?.ok) { showToast(res?.error || 'Port test failed.', 'error', 3500); return; }
    markProven(id);
    render();
    const held = typeof res.pulse_s === 'number' ? ` for ${res.pulse_s.toFixed(1)} s` : '';
    showToast(`Drove ${chip ? lineLabel(chip, line) : line} high${held}.`, 'success', 1600);
  } catch (e: any) {
    showToast(e?.message || 'Port test failed.', 'error', 3500);
  } finally {
    pulsing = false;
  }
}

// --- commissioning: sweep (output) + read-live (input) ---------------------
async function ensureCommission(): Promise<boolean> {
  if (commissionActive) return true;
  try {
    const res = await api.commissionOpen(payload());
    if (!res?.ok) { showToast(res?.error ? `Controller unavailable: ${res.error}` : 'Controller unavailable.', 'error', 4000); return false; }
  } catch (e: any) {
    showToast(e?.message || 'Controller unavailable.', 'error', 4000);
    return false;
  }
  commissionActive = true;
  return true;
}

async function stopCommissioning(): Promise<void> {
  sweepAbort = true;
  if (activityTimer) { clearInterval(activityTimer); activityTimer = null; }
  if (devPollTimer) { clearInterval(devPollTimer); devPollTimer = null; }
  activity = null;
  if (commissionActive) {
    commissionActive = false;
    try { await api.commissionClose(); } catch { /* best effort */ }
  }
}

async function toggleSweep(id: string): Promise<void> {
  if (activity?.chipId === id) { await stopCommissioning(); render(); return; }
  await stopCommissioning();
  if (!(await ensureCommission())) return;
  activity = { chipId: id, kind: 'sweep', port: 0, values: [] };
  sweepAbort = false;
  markProven(id);
  render();
  void runSweep(id);
}

// A shift register is proven by its owner watching it work, so running its test
// is what earns its badge — nothing on the bus can report it (see chipState).
// I²C chips answer a scan, so their badge is not this to give.
function markProven(id: string): void {
  const chip = state.chips.find((c) => c.chip_id === id);
  if (chip && chip.bus !== 'i2c') manualProven.add(id);
}

async function runSweep(id: string): Promise<void> {
  const chip = state.chips.find((c) => c.chip_id === id);
  if (!chip) return;
  // Walk lines, not ports — a PCA9685 sweep visits each of its 16 PWM channels
  // in turn, which is what the grid shows and what the user watches on the board.
  const count = chipLines(chip);
  const duties = chip.bus === 'i2c' ? [0.3, 0.6, 1.0] : [1.0];   // PWM steps vs on/off
  // A shift-out bit may be wired to a solenoid point-motor coil, which burns out
  // if held energized, so the marquee pulses those bits server-side for the
  // switch-coil width instead of driving them for the whole visual step.
  const isShiftOut = chip.kind === 'hc595';
  let pass = 0;
  while (!sweepAbort && activity?.chipId === id) {
    const duty = duties[pass % duties.length];
    pass++;
    for (let p = 0; p < count && !sweepAbort && activity?.chipId === id; p++) {
      try {
        if (isShiftOut) await api.commissionPulse(id, p);
        else await api.commissionOutput(id, p, duty);
      } catch { /* ignore */ }
      if (activity) { activity.port = p; render(); }
      await sleep(450);
      if (!isShiftOut) {
        try { await api.commissionOutput(id, p, 0); } catch { /* ignore */ }
      }
    }
  }
}

async function toggleLive(id: string): Promise<void> {
  if (activity?.chipId === id) { await stopCommissioning(); render(); return; }
  await stopCommissioning();
  if (!(await ensureCommission())) return;
  const chip = state.chips.find((c) => c.chip_id === id);
  const current = chip ? isCurrentSense(chip) : false;
  activity = { chipId: id, kind: 'live', port: -1, values: [] };
  markProven(id);
  render();
  const poll = async () => {
    if (activity?.chipId !== id) return;
    try {
      if (current) {
        const res = await api.commissionCurrents(id);
        if (res?.ok && activity) { activity.values = (res.currents || []).map((p: any) => Number(p.current_ma) || 0); render(); }
      } else {
        const res = await api.commissionInputs(id);
        if (res?.ok && activity) { activity.values = (res.ports || []).map((p: any) => (p.value ? 1 : 0)); render(); }
      }
    } catch { /* transient */ }
  };
  await poll();
  activityTimer = setInterval(poll, 500);
}

// --- save / restart --------------------------------------------------------
async function save(): Promise<void> {
  const btn = document.querySelector<HTMLButtonElement>('[data-hw="save"]');
  if (btn) btn.disabled = true;
  try {
    if (state.enabled && !isReady()) { state.enabled = false; render(); showToast('Hardware stayed off — finish the checklist.', 'warning', 4000); return; }
    await api.setHardwareConfig(payload());
    savedSinceEdit = true;
    render();
    showToast('Hardware setup saved — restart to apply.', 'success', 2500);
  } catch (e: any) {
    showToast(e?.message || 'Failed to save hardware setup.', 'error', 4000);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function restart(): Promise<void> {
  try { await api.restartServer(); } catch (e) {
    console.error('[App] Hardware restart request failed:', e);
    showToast('Could not reach server to restart.', 'error', 4000);
    return;
  }
  startRestartReconnectFlow();
}
