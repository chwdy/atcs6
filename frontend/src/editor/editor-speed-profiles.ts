// editor-speed-profiles.ts — Speed Profile Editor Handler.
//
// A standalone manager for `SpeedProfile` records (speed_profile.py): a list
// panel plus a per-profile curve editor. The profile's one curve, voltage →
// speed, is edited two ways at once — a draggable canvas plot and a numeric
// point list — both routed through one auto-saving commit path
// (DESIGN_LANGUAGE.md §4 "Two views of one value"). A converter section turns
// a voltage into a speed and back, and a power percent into a speed and back.
//
// Owned by the Editor (`new SpeedProfileEditorHandler(this)`).

import { showToast } from '../lib/toast';
import { escapeAttr, showConfirmModal, showFormModal } from '../lib/helpers';
import { stateManager } from '../lib/state-manager';
import { trainDisplayStatus } from '../lib/train-status';
import { changedProfileFields } from '../lib/profile-edits';
import {
  type CalibrationRunView,
  calibrationControls,
  calibrationTrainChoices,
  compareAxisTop,
  crossingRows,
  speedPassProgress,
  measuredCurve,
  pointRows,
  readinessApplies,
  speedPassEndedMessage,
  trainChoiceLabel,
  trainStoredValue,
  viewForOpenRun,
  volts,
} from '../lib/calibration-panel';
import type { EditorHost } from './types';
import {
  type Curve,
  type CurvePoint,
  lightingVoltageProblem,
  powerPercentToSpeed,
  powerPercentToVoltage,
  speedToPowerPercent,
  speedToVoltage,
  startingSpeed,
  startingVoltage,
  topSpeed,
  voltageToSpeed,
} from '../lib/speed-profile-curve';

interface SpeedProfileData {
  profile_id: string;
  name: string;
  // The locomotive model this profile describes, matched against
  // RosterEntry.model so one measured curve serves every locomotive of a model.
  model: string;
  // ISO stamp of the speed-calibration commit that wrote the curve this profile
  // runs on; null when none did (a seeded or hand-drawn curve).
  calibrated_at: string | null;
  // [track_voltage, speed_kmh] points, voltage ascending. The first point is
  // the starting voltage and starting speed; the last sits at max_voltage and
  // carries the top speed.
  voltage_speed_curve: CurvePoint[];
  // The voltage a standing, powered train is held at so its lamps light; null
  // when the locomotive has none (it then stands at 0 V).
  lighting_voltage: number | null;
  max_voltage: number;
  acceleration_rate: number;
  deceleration_rate: number;
  emergency_brake_rate: number;
  friction_rate: number;
  operational_decel_ratio: number;
  air_resistance_enabled: boolean;
  drag_coefficient: number;
  reference_speed_kmh: number;
  // Per-train notch response: multipliers keyed by handle ("P1".."P5" /
  // "B1".."B8"). Power notches scale acceleration; brake notches scale the
  // operational decel rate. Mirrors SpeedProfile.power/brake_multipliers.
  power_multipliers: Record<string, number>;
  brake_multipliers: Record<string, number>;
}

// Standard notch tables (match enums.POWER_MULTIPLIERS / BRAKE_MULTIPLIERS).
// Used as the render order and as the fallback when a profile omits a notch.
const POWER_NOTCHES: ReadonlyArray<[string, number]> = [
  ['P1', 0.2], ['P2', 0.4], ['P3', 0.6], ['P4', 0.8], ['P5', 1.0],
];
const BRAKE_NOTCHES: ReadonlyArray<[string, number]> = [
  ['B1', 0.2], ['B2', 0.4], ['B3', 0.55], ['B4', 0.7],
  ['B5', 0.85], ['B6', 1.0], ['B7', 1.3], ['B8', 1.7],
];
const DEFAULT_POWER_MULT: Record<string, number> = Object.fromEntries(POWER_NOTCHES);
const DEFAULT_BRAKE_MULT: Record<string, number> = Object.fromEntries(BRAKE_NOTCHES);

/** A plot's axes: x is track voltage from 0 to `xMax`, y is speed. */
interface PlotAxis {
  xMax: number;
  yMin: number;
  yMax: number;
}

/** A voltage marked on a plot's voltage axis, with a short label. */
interface VoltageMark {
  x: number;
  label: string;
}

interface CurvePlotOptions {
  canvas: HTMLCanvasElement;
  color: string;
  onChange: (points: CurvePoint[], dragging: boolean) => void;
  /** Left-click on the line: insert a point in segment `segIndex` at plot-space (x, y). */
  onInsert: (segIndex: number, x: number, y: number) => void;
  /** Right-click an interior handle: remove point `index` (endpoints are never reported). */
  onRemove: (index: number) => void;
}

/** What the calibration group holds while the profile editor is open. */
interface CalibrationGroupState {
  /** The group's element; once it leaves the page the operator has left the profile. */
  root: HTMLElement;
  profileId: string;
  /** Every profile, to read an instance profile's own values. */
  profiles: any[];
  /** Profile ids a train named that the list did not hold, already re-read for. */
  profilesReadFor: Set<string>;
  view: CalibrationRunView;
  /** The last readiness check: `unmet` is null until the first reply. */
  ready: boolean;
  unmet: string[] | null;
  readinessError: string | null;
  /** The open speed pass or held result, as the last poll returned it. */
  speedRun: any | null;
  measuredBlockId: string;
  stretchText: string;
  stretchResolved: boolean;
  /** How the last speed pass ended, shown in the setup. */
  message: string;
  /** The pass ended on its own (EB, a power-off, a session stop, a
   *  measurement fault), not by the operator's Stop: shown as a warning. */
  messageWarn: boolean;
  timer: number | null;
  /** A request is on its way; the refresh tick skips rather than piles up. */
  busy: boolean;
  compare: CurveComparePlot | null;
  /** Bumped when another train is named, so a late reply for the old one is dropped. */
  generation: number;
}

/** What the calibration group shows, as the panel's host reads it. */
export interface CalibrationGroupSummary {
  profileId: string;
  /** The train the locomotive row names, or null. */
  trainId: string | null;
  view: CalibrationRunView;
  /** The last readiness check: `unmet` is null until the first reply. */
  ready: boolean;
  unmet: string[] | null;
  /** The named train runs on a lighting voltage. */
  lightingVoltageSet: boolean;
  /** A speed pass was committed for the named train since the panel was
   *  opened on it. */
  speedCommitted: boolean;
}

/**
 * An element of its own that the panel draws into: the Locomotives screen
 * (frontend/src/next/locomotives/). Without a host the panel draws through
 * Editor.showSecondarySidebar, as every other panel does.
 */
export interface SpeedProfilePanelHost {
  el: HTMLElement;
  /** The profile list was asked for; the host shows its own list instead. */
  showList(): void;
  /** The panel now shows this profile. */
  profileShown(profileId: string): void;
  /** The profile on screen was deleted, which closed the panel. */
  profileDeleted(profileId: string): void;
  /** The calibration group's named train or run changed. */
  calibrationChanged(summary: CalibrationGroupSummary | null): void;
}

/** How often the calibration group re-reads the readiness check and the open pass. */
const CALIBRATION_REFRESH_MS = 1000;

const EPS_X = 0.05; // min voltage gap between adjacent points (keeps voltage strictly rising)
const EPS_Y = 0.02; // min speed gap between the starting speed and the top speed
const HIT_R = 9; // px hit radius for grabbing a point handle
const ADD_HIT_R = 6; // px distance from the line that still reads as "click the line to add a point"
const PAD = { left: 40, right: 14, top: 14, bottom: 26 };

/** Round for display without trailing-zero noise. */
function fmt(n: number, digits = 2): string {
  return String(Number(n.toFixed(digits)));
}

/**
 * Size a canvas's backing store to device pixels (crisp on retina) so drawing
 * stays in CSS px. False while the canvas is not laid out yet.
 */
function fitCanvasToDevice(canvas: HTMLCanvasElement, ctx: CanvasRenderingContext2D): boolean {
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return false;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.scale(dpr, dpr);
  return true;
}

/** The gap between voltage gridlines: 1, 2 or 5 V (times a power of ten),
 *  so the axis carries at most about eight of them. */
function voltageTickStep(xMax: number): number {
  const raw = Math.max(xMax, 1) / 8;
  const base = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10]) if (base * m >= raw) return base * m;
  return base * 10;
}

/** The gridlines, tick labels and axes every voltage/speed plot shares, and
 *  the voltages marked on the voltage axis. */
function drawPlotFrame(
  ctx: CanvasRenderingContext2D,
  rect: DOMRect,
  axis: PlotAxis,
  xToPx: (x: number) => number,
  yToPx: (y: number) => number,
  marks: VoltageMark[] = []
): void {
  const css = getComputedStyle(document.documentElement);
  const border = css.getPropertyValue('--border-color').trim() || '#333';
  const textMuted = css.getPropertyValue('--text-secondary').trim() || '#999';

  const x0 = PAD.left;
  const y0 = rect.height - PAD.bottom;
  const x1 = rect.width - PAD.right;
  const y1 = PAD.top;

  // gridlines + ticks
  ctx.font = '9px ui-monospace, monospace';
  ctx.fillStyle = textMuted;
  ctx.strokeStyle = border;
  ctx.lineWidth = 1;
  // X gridlines (track voltage, 0..xMax)
  const step = voltageTickStep(axis.xMax);
  for (let gx = 0; gx <= axis.xMax + 1e-9; gx += step) {
    const px = xToPx(gx);
    ctx.globalAlpha = 0.4;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(px, y1);
    ctx.lineTo(px, y0);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    ctx.textAlign = 'center';
    ctx.fillText(fmt(gx, 1), px, y0 + 12);
  }
  // Y gridlines (axis range, 4 divisions)
  const { yMin, yMax } = axis;
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const yv = yMin + ((yMax - yMin) * i) / 4;
    const py = yToPx(yv);
    ctx.globalAlpha = 0.4;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(x0, py);
    ctx.lineTo(x1, py);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    ctx.fillText(fmt(yv, 1), x0 - 5, py + 3);
  }

  // axes (solid)
  ctx.beginPath();
  ctx.moveTo(x0, y1);
  ctx.lineTo(x0, y0);
  ctx.lineTo(x1, y0);
  ctx.stroke();

  // Marked voltages: a dashed line up from the voltage axis, labelled at the
  // top of the plot. The marks sit at low voltages, where the curve is near
  // the bottom, so labels at the top stay clear of the curve's first point and
  // of the tick labels below.
  const accent = css.getPropertyValue('--warning-color').trim() || '#f5a524';
  marks.forEach((m, i) => {
    const px = xToPx(m.x);
    ctx.strokeStyle = accent;
    ctx.fillStyle = accent;
    ctx.globalAlpha = 0.8;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(px, y0);
    ctx.lineTo(px, y1);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;
    ctx.textAlign = 'left';
    ctx.fillText(`${m.label} ${fmt(m.x)} V`, px + 3, y1 + 10 + i * 11);
  });
  ctx.strokeStyle = border;
}

/**
 * The profile's voltage → speed curve as one editable plot over a `<canvas>`.
 * It works in plot space `[voltage, speed]` and reports edits back through
 * `onChange` (drag a handle), `onInsert` (left-click the line to add a point),
 * and `onRemove` (right-click a point between the two ends to delete it). The
 * handler owns the commit.
 *
 * What a drag may do keeps the curve one a save accepts (design_new.md
 * "SpeedProfile", `voltage_speed_curve`):
 *   - voltage stays strictly rising from point to point; the first point (the
 *     starting voltage) stays above 0 V, and the last point stays at
 *     `max_voltage`;
 *   - speed never falls as voltage rises (a flat stretch is allowed); the
 *     first point's speed stays above 0, and the top speed stays above the
 *     starting speed.
 */
class CurvePlot {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private color: string;
  private onChange: (points: CurvePoint[], dragging: boolean) => void;
  private onInsert: (segIndex: number, x: number, y: number) => void;
  private onRemove: (index: number) => void;
  private points: CurvePoint[] = [];
  private axis: PlotAxis = { xMax: 12, yMin: 0, yMax: 1 };
  private marks: VoltageMark[] = [];
  private dragIndex: number | null = null;
  private hoverIndex: number | null = null;
  private editable = true;
  private ro: ResizeObserver | null = null;
  private onDown: (e: MouseEvent) => void;
  private onMove: (e: MouseEvent) => void;
  private onUp: (e: MouseEvent) => void;
  private onLeave: (e: MouseEvent) => void;
  private onContext: (e: MouseEvent) => void;

  constructor(opts: CurvePlotOptions) {
    this.canvas = opts.canvas;
    this.ctx = opts.canvas.getContext('2d')!;
    this.color = opts.color;
    this.onChange = opts.onChange;
    this.onInsert = opts.onInsert;
    this.onRemove = opts.onRemove;

    this.onDown = (e) => this.handleDown(e);
    this.onMove = (e) => this.handleMove(e);
    this.onUp = () => this.handleUp();
    this.onLeave = () => {
      this.hoverIndex = null;
      if (this.dragIndex === null) this.draw();
    };
    this.onContext = (e) => this.handleContext(e);
    this.canvas.addEventListener('mousedown', this.onDown);
    window.addEventListener('mousemove', this.onMove);
    window.addEventListener('mouseup', this.onUp);
    this.canvas.addEventListener('mouseleave', this.onLeave);
    this.canvas.addEventListener('contextmenu', this.onContext);

    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(this.canvas);
  }

  /** Replace the plotted points, axis and marked voltages, then redraw at
   *  device resolution. */
  setData(points: CurvePoint[], axis: PlotAxis, marks: VoltageMark[] = []): void {
    this.points = points.map((p) => [p[0], p[1]] as CurvePoint);
    this.axis = axis;
    this.marks = marks;
    this.resize();
  }

  /** Turn editing off, for a curve the profile on screen does not own. */
  setEditable(editable: boolean): void {
    this.editable = editable;
    this.dragIndex = null;
    this.hoverIndex = null;
    this.canvas.style.cursor = editable ? 'default' : 'not-allowed';
    this.draw();
  }

  destroy(): void {
    this.canvas.removeEventListener('mousedown', this.onDown);
    window.removeEventListener('mousemove', this.onMove);
    window.removeEventListener('mouseup', this.onUp);
    this.canvas.removeEventListener('mouseleave', this.onLeave);
    this.canvas.removeEventListener('contextmenu', this.onContext);
    this.ro?.disconnect();
    this.ro = null;
  }

  // --- geometry ---
  private plotW(rect: DOMRect): number {
    return rect.width - PAD.left - PAD.right;
  }
  private plotH(rect: DOMRect): number {
    return rect.height - PAD.top - PAD.bottom;
  }
  private xToPx(x: number, rect: DOMRect): number {
    return PAD.left + (x / (this.axis.xMax || 1)) * this.plotW(rect);
  }
  private yToPx(y: number, rect: DOMRect): number {
    const { yMin, yMax } = this.axis;
    const span = yMax - yMin || 1;
    return PAD.top + (1 - (y - yMin) / span) * this.plotH(rect);
  }
  private pxToX(px: number, rect: DOMRect): number {
    return ((px - PAD.left) / this.plotW(rect)) * this.axis.xMax;
  }
  private pxToY(py: number, rect: DOMRect): number {
    const { yMin, yMax } = this.axis;
    return yMin + (1 - (py - PAD.top) / this.plotH(rect)) * (yMax - yMin);
  }

  /** Size the backing store to device pixels (crisp on retina), draw in CSS px. */
  resize(): void {
    if (!fitCanvasToDevice(this.canvas, this.ctx)) {
      // Not laid out yet — retry next frame.
      requestAnimationFrame(() => this.resize());
      return;
    }
    this.draw();
  }

  private draw(): void {
    const ctx = this.ctx;
    const rect = this.canvas.getBoundingClientRect();
    if (rect.width < 2) return;
    ctx.clearRect(0, 0, rect.width, rect.height);

    drawPlotFrame(
      ctx,
      rect,
      this.axis,
      (x) => this.xToPx(x, rect),
      (y) => this.yToPx(y, rect),
      this.marks
    );

    if (this.points.length === 0) return;

    // polyline — the affordance reads brighter than the chrome (§4)
    ctx.strokeStyle = this.color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    this.points.forEach((p, i) => {
      const px = this.xToPx(p[0], rect);
      const py = this.yToPx(p[1], rect);
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();

    // handles
    this.points.forEach((p, i) => {
      const px = this.xToPx(p[0], rect);
      const py = this.yToPx(p[1], rect);
      const isEnd = i === 0 || i === this.points.length - 1;
      const active = i === this.dragIndex || i === this.hoverIndex;
      ctx.beginPath();
      ctx.arc(px, py, active ? 6.5 : 5, 0, Math.PI * 2);
      ctx.fillStyle = this.color;
      ctx.fill();
      if (isEnd) {
        ctx.lineWidth = 2;
        ctx.strokeStyle = '#fff';
        ctx.stroke();
      }
      if (active) {
        ctx.beginPath();
        ctx.arc(px, py, 10, 0, Math.PI * 2);
        ctx.strokeStyle = this.color;
        ctx.globalAlpha = 0.4;
        ctx.stroke();
        ctx.globalAlpha = 1;
      }
    });
  }

  private localPoint(e: MouseEvent): { px: number; py: number; rect: DOMRect } {
    const rect = this.canvas.getBoundingClientRect();
    return { px: e.clientX - rect.left, py: e.clientY - rect.top, rect };
  }

  private handleAt(px: number, py: number, rect: DOMRect): number | null {
    let best: number | null = null;
    let bestD = HIT_R * HIT_R;
    this.points.forEach((p, i) => {
      const dx = this.xToPx(p[0], rect) - px;
      const dy = this.yToPx(p[1], rect) - py;
      const d = dx * dx + dy * dy;
      if (d <= bestD) {
        bestD = d;
        best = i;
      }
    });
    return best;
  }

  /**
   * Nearest polyline segment within ADD_HIT_R of (px, py), or null. Returns the
   * segment's start index plus the click in plot space, for inserting a point.
   */
  private segmentAt(
    px: number,
    py: number,
    rect: DOMRect,
  ): { index: number; x: number; y: number } | null {
    let best: { index: number; x: number; y: number } | null = null;
    let bestD = ADD_HIT_R * ADD_HIT_R;
    for (let i = 0; i < this.points.length - 1; i++) {
      const ax = this.xToPx(this.points[i][0], rect);
      const ay = this.yToPx(this.points[i][1], rect);
      const bx = this.xToPx(this.points[i + 1][0], rect);
      const by = this.yToPx(this.points[i + 1][1], rect);
      const vx = bx - ax;
      const vy = by - ay;
      const len2 = vx * vx + vy * vy || 1;
      let t = ((px - ax) * vx + (py - ay) * vy) / len2;
      t = Math.max(0, Math.min(1, t));
      const cx = ax + t * vx;
      const cy = ay + t * vy;
      const d = (px - cx) * (px - cx) + (py - cy) * (py - cy);
      if (d <= bestD) {
        bestD = d;
        best = { index: i, x: this.pxToX(px, rect), y: this.pxToY(py, rect) };
      }
    }
    return best;
  }

  private handleDown(e: MouseEvent): void {
    if (e.button !== 0 || !this.editable) return; // left button only; right-click is handled by contextmenu
    const { px, py, rect } = this.localPoint(e);
    const idx = this.handleAt(px, py, rect);
    if (idx !== null) {
      e.preventDefault();
      this.dragIndex = idx;
      this.draw();
      return;
    }
    // Not on a handle — a left-click on the line adds a point there.
    const seg = this.segmentAt(px, py, rect);
    if (seg) {
      e.preventDefault();
      this.onInsert(seg.index, seg.x, seg.y);
    }
  }

  private handleMove(e: MouseEvent): void {
    if (!this.editable) return;
    const { px, py, rect } = this.localPoint(e);
    if (this.dragIndex === null) {
      const idx = this.handleAt(px, py, rect);
      if (idx !== null) this.canvas.style.cursor = 'grab';
      else this.canvas.style.cursor = this.segmentAt(px, py, rect) ? 'copy' : 'default';
      if (idx !== this.hoverIndex) {
        this.hoverIndex = idx;
        this.draw();
      }
      return;
    }
    this.canvas.style.cursor = 'grabbing';
    this.points[this.dragIndex] = clampCurvePoint(
      this.points,
      this.dragIndex,
      this.pxToX(px, rect),
      this.pxToY(py, rect),
      this.axis.yMax
    );
    this.draw();
    this.onChange(this.points, true);
  }

  private handleUp(): void {
    if (this.dragIndex === null) return;
    this.dragIndex = null;
    this.canvas.style.cursor = 'grab';
    this.draw();
    this.onChange(this.points, false);
  }

  /** Right-click a point between the two ends to remove it; the two ends stay. */
  private handleContext(e: MouseEvent): void {
    e.preventDefault(); // keep the browser context menu off the plot
    if (!this.editable) return;
    const { px, py, rect } = this.localPoint(e);
    const idx = this.handleAt(px, py, rect);
    if (idx === null || idx === 0 || idx === this.points.length - 1) return;
    this.hoverIndex = null;
    this.dragIndex = null;
    this.onRemove(idx);
  }
}

/**
 * Where point `i` of `points` may go when dragged toward (x, y): the rules in
 * CurvePlot's comment. The last point's voltage stays where it is
 * (`max_voltage`).
 */
function clampCurvePoint(
  points: CurvePoint[],
  i: number,
  x: number,
  y: number,
  yMax: number
): CurvePoint {
  const last = points.length - 1;
  let nx = points[i][0];
  if (i !== last) {
    const lo = i === 0 ? EPS_X : points[i - 1][0] + EPS_X;
    const hi = points[i + 1][0] - EPS_X;
    nx = Math.min(hi, Math.max(lo, x));
  }
  let lo = i === 0 ? EPS_Y : points[i - 1][1];
  let hi = i === last ? yMax : points[i + 1][1];
  // The top speed stays above the starting speed.
  if (i === last) lo = Math.max(lo, points[0][1] + EPS_Y);
  if (i === 0 && last > 0) hi = Math.min(hi, points[last][1] - EPS_Y);
  const ny = Math.min(hi, Math.max(lo, y));
  return [nx, ny];
}

/**
 * A read-only plot of the measured voltage/speed points over the stored curve,
 * for a speed pass in progress and for a held result (design_new.md "Speed
 * calibration", The panel). The stored curve is drawn dashed and dim and the
 * measured one solid with a dot per point, so the two read apart without
 * relying on colour. The starting voltage and the lighting voltage are marked
 * on the voltage axis.
 */
class CurveComparePlot {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private stored: Curve = [];
  private measured: Curve = [];
  private axis: PlotAxis = { xMax: 12, yMin: 0, yMax: 1 };
  private marks: VoltageMark[] = [];
  private ro: ResizeObserver | null;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d')!;
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(canvas);
  }

  setData(stored: Curve, measured: Curve, axis: PlotAxis, marks: VoltageMark[] = []): void {
    this.stored = stored;
    this.measured = measured;
    this.axis = axis;
    this.marks = marks;
    this.resize();
  }

  destroy(): void {
    this.ro?.disconnect();
    this.ro = null;
  }

  private resize(): void {
    if (!this.ro) return;
    if (!fitCanvasToDevice(this.canvas, this.ctx)) {
      requestAnimationFrame(() => this.resize());
      return;
    }
    this.draw();
  }

  private draw(): void {
    const ctx = this.ctx;
    const rect = this.canvas.getBoundingClientRect();
    if (rect.width < 2) return;
    ctx.clearRect(0, 0, rect.width, rect.height);
    const w = rect.width - PAD.left - PAD.right;
    const h = rect.height - PAD.top - PAD.bottom;
    const span = this.axis.yMax - this.axis.yMin || 1;
    const xToPx = (x: number) => PAD.left + (x / (this.axis.xMax || 1)) * w;
    const yToPx = (y: number) => PAD.top + (1 - (y - this.axis.yMin) / span) * h;
    drawPlotFrame(ctx, rect, this.axis, xToPx, yToPx, this.marks);

    const css = getComputedStyle(document.documentElement);
    const line = (points: Curve) => {
      ctx.beginPath();
      points.forEach(([x, y], i) => {
        if (i === 0) ctx.moveTo(xToPx(x), yToPx(y));
        else ctx.lineTo(xToPx(x), yToPx(y));
      });
      ctx.stroke();
    };

    ctx.strokeStyle = css.getPropertyValue('--text-secondary').trim() || '#999';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    line(this.stored);
    ctx.setLineDash([]);

    const measured = css.getPropertyValue('--success-color').trim() || '#4ade80';
    ctx.strokeStyle = measured;
    ctx.fillStyle = measured;
    ctx.lineWidth = 2;
    line(this.measured);
    for (const [x, y] of this.measured) {
      ctx.beginPath();
      ctx.arc(xToPx(x), yToPx(y), 3, 0, Math.PI * 2);
      ctx.fill();
    }
  }
}

export class SpeedProfileEditorHandler {
  editor: EditorHost;
  private _profile: SpeedProfileData | null = null;
  private _lastSaved: SpeedProfileData | null = null;
  /** The model profile the open profile derives from, or null on a model
   *  profile; and which instance fields the open instance carries itself. */
  private _derivedFrom: string | null = null;
  private _ownFields: string[] = [];
  private _plot: CurvePlot | null = null;
  private _saveTimer: ReturnType<typeof setTimeout> | null = null;
  private _host: SpeedProfilePanelHost | null = null;

  constructor(editor: EditorHost) {
    this.editor = editor;
  }

  get api(): any {
    return this.editor.api;
  }

  /** Draw the panel into `host` from now on instead of the secondary sidebar. */
  setHost(host: SpeedProfilePanelHost | null): void {
    this._host = host;
  }

  // === Manager list ===

  async showProfilesPanel(): Promise<void> {
    if (this._host) {
      this._host.showList();
      return;
    }
    this._teardownCalibration();
    this._teardownPlots();
    this.editor._currentEditorType = 'speed-profiles-list';
    this.editor._currentEditorId = null;

    let profiles: any[] = [];
    try {
      profiles = await this.api.getSpeedProfiles();
    } catch (err: any) {
      console.error('[SpeedProfileEditorHandler] Failed to load profiles:', err);
      showToast('Failed to load speed profiles: ' + err.message, 'error', 5000);
    }

    let content = `
            <div class="sp-list-panel">
                <div class="routes-toolbar mb-16">
                    <button id="sp-new-btn" class="btn btn-primary btn-sm flex-1">+ New Profile</button>
                </div>`;
    if (!profiles.length) {
      content += '<div class="empty-state">No speed profiles</div>';
    } else {
      content += '<div class="sp-list">';
      for (const p of profiles) content += this._renderProfileRow(p);
      content += '</div>';
    }
    content += '</div>';

    this.editor.showSecondarySidebar('Speed Profiles', content);
    setTimeout(() => this._bindListHandlers(), 0);
  }

  private _renderProfileRow(p: any): string {
    const id = p.profile_id;
    const isDefault = id === 'default';
    const top = topSpeed(p.voltage_speed_curve || []);
    const pts = p.voltage_speed_curve?.length || 0;
    return `
            <div class="rr-card accent-info mb-8" data-profile-id="${id}">
                <div class="rr-card-head">
                    <div class="rr-card-title-col">
                        <div class="rr-card-train font-mono">${id}</div>
                    </div>
                    <div class="route-row-actions">
                        <button class="rr-flat-btn sp-edit-btn" data-profile-id="${id}">Edit</button>
                        <button class="rr-flat-btn rr-flat-btn-danger sp-delete-btn" data-profile-id="${id}" ${isDefault ? 'disabled title="The default profile cannot be deleted"' : ''}>Delete</button>
                    </div>
                </div>
                <div class="chip-row">
                    <span class="chip">${fmt(top, 0)} km/h top</span>
                    <span class="chip">${pts} pts</span>
                </div>
                <div class="rr-card-foot">${p.name || ''}</div>
            </div>`;
  }

  private _bindListHandlers(): void {
    const newBtn = document.getElementById('sp-new-btn');
    if (newBtn) newBtn.onclick = () => this._createProfile();

    document.querySelectorAll<HTMLButtonElement>('.sp-edit-btn').forEach((btn) => {
      btn.onclick = () => this.showProfileEditor(btn.dataset.profileId || '');
    });
    document.querySelectorAll<HTMLButtonElement>('.sp-delete-btn').forEach((btn) => {
      if (btn.disabled) return;
      btn.onclick = () => this._deleteProfile(btn.dataset.profileId || '');
    });
  }

  private async _createProfile(): Promise<void> {
    const values = await showFormModal({
      title: 'New Speed Profile',
      fields: [
        { name: 'profile_id', label: 'Profile ID', required: true, placeholder: 'e.g. shinkansen_n700' },
        { name: 'name', label: 'Name', required: true, placeholder: 'Display name' },
      ],
      confirmText: 'Create',
    });
    if (!values) return;
    try {
      const created = await this.api.createSpeedProfile({
        profile_id: String(values.profile_id).trim(),
        name: String(values.name).trim(),
      });
      showToast('Speed profile created', 'success', 2000);
      await this.showProfileEditor(created.profile_id);
    } catch (err: any) {
      showToast('Failed to create profile: ' + err.message, 'error', 5000);
    }
  }

  private async _deleteProfile(id: string): Promise<void> {
    const confirmed = await showConfirmModal({
      title: 'Delete Speed Profile',
      message: `Delete speed profile "${id}"?`,
      detail: 'Profiles assigned to a train, and the default profile, cannot be deleted.',
      confirmText: 'Delete',
      confirmVariant: 'danger',
    });
    if (!confirmed) return;
    try {
      await this.api.deleteSpeedProfile(id);
      showToast('Speed profile deleted', 'success', 2000);
      if (this._host) {
        this.close();
        this._host.profileDeleted(id);
      } else {
        await this.showProfilesPanel();
      }
    } catch (err: any) {
      showToast(err.message || 'Failed to delete profile', 'error', 5000);
    }
  }

  // === Per-profile editor ===

  /**
   * Show one profile. `options.trainId`, when given, is the train the
   * calibration group's locomotive row names once the panel is open (kept
   * only while the row lists it); otherwise the row keeps the train it named.
   */
  async showProfileEditor(profileId: string, options: { trainId?: string | null } = {}): Promise<void> {
    this._teardownCalibration();
    this._teardownPlots();
    if ('trainId' in options && options.trainId !== this._calTrainId) {
      this._calTrainId = options.trainId ?? null;
      this._speedCommittedFor = null;
    }
    // A hosted panel is not one of the secondary sidebar's editors.
    if (!this._host) {
      this.editor._currentEditorType = 'speed-profile';
      this.editor._currentEditorId = profileId;
    }

    // The panel shows the resolved profile, so a locomotive's own profile
    // shows what it runs on (design_new.md "SpeedProfile", Editing a profile
    // by hand).
    let data: any;
    try {
      data = await this.api.getSpeedProfile(profileId, { resolved: true });
      if (!data || data.error) {
        showToast('Speed profile not found', 'error', 5000);
        return;
      }
    } catch (err: any) {
      showToast('Failed to load speed profile: ' + err.message, 'error', 5000);
      return;
    }

    this._applyView(data);

    if (this._host) {
      this._host.el.innerHTML = this._editorHtml(this._profile!);
      this._host.profileShown(profileId);
    } else {
      this.editor.showSecondarySidebar(profileId, this._editorHtml(this._profile!));
      // Wider canvas room — same affordance the route editor uses.
      document.getElementById('secondary-sidebar')?.classList.add('route-editor-wide');
    }
    setTimeout(() => this._initEditor(), 0);
  }

  /** Take a resolved view from the server as the profile on screen. */
  private _applyView(view: any): void {
    this._derivedFrom = view.derived_from || null;
    this._ownFields = Array.isArray(view.own_fields) ? view.own_fields : [];
    this._profile = this._normalize(view);
    this._lastSaved = this._clone(this._profile);
  }

  /** A locomotive's own profile: only its name, lighting voltage and
   *  voltage/speed curve are edited here; every other value belongs to its
   *  model profile. */
  private _isInstance(): boolean {
    return this._derivedFrom !== null;
  }

  /** The note an instance profile opens with: what it carries itself and
   *  where its other values are edited. */
  private _instanceNoteHtml(): string {
    if (!this._derivedFrom) return '';
    const model = escapeAttr(this._derivedFrom);
    const curve = this._ownFields.includes('voltage_speed_curve')
      ? 'This locomotive has its own voltage → speed curve, so a change to the model curve does not reach it.'
      : `Its voltage → speed curve comes from <span class="font-mono">${model}</span>; editing it here gives this locomotive its own curve.`;
    const lighting = this._ownFields.includes('lighting_voltage')
      ? 'It has its own lighting voltage.'
      : `Its lighting voltage comes from <span class="font-mono">${model}</span> until you set one here.`;
    return `
                <div class="info-box text-xs mb-12 sp-instance-note">
                    This is one locomotive's own profile. ${curve} ${lighting}
                    Every other value comes from model profile <span class="font-mono">${model}</span> and is edited there.
                    <button type="button" class="rr-flat-btn" id="sp-open-model" data-profile-id="${model}">Edit ${model} →</button>
                </div>`;
  }

  private _normalize(d: any): SpeedProfileData {
    const curve: CurvePoint[] = (d.voltage_speed_curve || []).map((p: number[]) => [p[0], p[1]] as CurvePoint);
    curve.sort((a, b) => a[0] - b[0]);
    return {
      profile_id: d.profile_id,
      name: d.name || '',
      model: d.model || '',
      calibrated_at: d.calibrated_at || null,
      voltage_speed_curve: curve,
      max_voltage:
        typeof d.max_voltage === 'number' ? d.max_voltage : curve.length ? curve[curve.length - 1][0] : 12,
      lighting_voltage: typeof d.lighting_voltage === 'number' ? d.lighting_voltage : null,
      acceleration_rate: d.acceleration_rate ?? 10,
      deceleration_rate: d.deceleration_rate ?? 15,
      emergency_brake_rate: d.emergency_brake_rate ?? 30,
      friction_rate: d.friction_rate ?? 0.5,
      operational_decel_ratio: d.operational_decel_ratio ?? 0.8,
      air_resistance_enabled: d.air_resistance_enabled ?? true,
      drag_coefficient: d.drag_coefficient ?? 0.001,
      reference_speed_kmh: d.reference_speed_kmh ?? 100,
      // Merge the profile's notch tables over the standard table so a missing
      // notch falls back rather than reading as undefined.
      power_multipliers: { ...DEFAULT_POWER_MULT, ...(d.power_multipliers || {}) },
      brake_multipliers: { ...DEFAULT_BRAKE_MULT, ...(d.brake_multipliers || {}) },
    };
  }

  private _clone(p: SpeedProfileData): SpeedProfileData {
    return JSON.parse(JSON.stringify(p));
  }

  /** The curve's points, copied so a plot edit never writes the profile directly. */
  private _curvePoints(): CurvePoint[] {
    return (this._profile!.voltage_speed_curve || []).map((pt) => [pt[0], pt[1]] as CurvePoint);
  }
  private _axis(): PlotAxis {
    const p = this._profile!;
    const top = topSpeed(p.voltage_speed_curve || []);
    return { xMax: p.max_voltage, yMin: 0, yMax: Math.max(30, Math.ceil(top * 1.25)) };
  }
  /** The starting voltage and, when set, the lighting voltage, marked on the
   *  voltage axis. */
  private _marks(curve: Curve, lighting: number | null): VoltageMark[] {
    const marks: VoltageMark[] = [];
    if (curve.length) marks.push({ x: startingVoltage(curve), label: 'starting' });
    if (lighting != null) marks.push({ x: lighting, label: 'lighting' });
    return marks;
  }
  /** Redraw the plot from the profile on screen. */
  private _redrawPlot(): void {
    const p = this._profile;
    if (!p) return;
    this._plot?.setData(this._curvePoints(), this._axis(), this._marks(p.voltage_speed_curve, p.lighting_voltage));
  }

  /** One of the profile's own values, beside the label it belongs to. A value
   *  a locomotive's own profile reads from its model profile is plain text,
   *  not a disabled input: it is edited on the model profile. */
  private _scalarRowHtml(
    id: string,
    label: string,
    value: string,
    opts: { editable: boolean; unit?: string; attrs?: string; placeholder?: string }
  ): string {
    const unit = opts.unit ? `<span class="sp-scalar-unit">${opts.unit}</span>` : '';
    const field = opts.editable
      ? `<input id="${id}" class="input-small-field sp-scalar-input" value="${escapeAttr(value)}" ${opts.attrs || ''}${
          opts.placeholder ? ` placeholder="${escapeAttr(opts.placeholder)}"` : ''
        }>`
      : value
        ? `<span class="sp-scalar-value">${escapeAttr(value)}</span>`
        : '<span class="sp-scalar-value is-empty">not set</span>';
    return `
                    <div class="sp-scalar-row">
                        <label class="input-small-label"${opts.editable ? ` for="${id}"` : ''}>${label}</label>
                        ${field}${unit}
                    </div>`;
  }

  private _editorHtml(p: SpeedProfileData): string {
    const ro = this._isInstance();
    return `
            <div class="sp-editor">
                ${this._instanceNoteHtml()}
                <div class="control-section sp-model-section">
                    ${this._scalarRowHtml('sp-model', 'Model', p.model || '', {
                      editable: !ro,
                      attrs: 'type="text"',
                      placeholder: 'e.g. Kato EF66',
                    })}
                </div>

                <div class="control-section">
                    <div class="control-section-header">Voltage → speed curve</div>
                    ${this._scalarRowHtml('sp-maxv', 'Max voltage', fmt(p.max_voltage), {
                      editable: !ro,
                      unit: 'V',
                      attrs: 'type="number" min="0" step="0.1"',
                    })}
                    <div class="sp-scalar-row">
                        <span class="input-small-label">Starting voltage</span>
                        <span class="sp-scalar-value" id="sp-start-v"></span><span class="sp-scalar-unit">V</span>
                    </div>
                    <div class="sp-scalar-row">
                        <span class="input-small-label">Starting speed</span>
                        <span class="sp-scalar-value" id="sp-start-s"></span><span class="sp-scalar-unit">km/h</span>
                    </div>
                    <div class="sp-scalar-row">
                        <span class="input-small-label">Top speed</span>
                        <span class="sp-scalar-value" id="sp-top-s"></span><span class="sp-scalar-unit">km/h</span>
                    </div>
                    <div class="text-muted text-xs mb-8">X = track voltage, Y = speed (km/h). The first point is the starting voltage: the lowest voltage at which the locomotive moves steadily, and its speed there. The last point sits at the max voltage and gives the top speed. Click the line to add a point · drag to move · right-click a point to remove.</div>
                    <div class="sp-plot-block">
                        <div class="sp-point-list" id="sp-curve-list"></div>
                        <canvas class="sp-canvas" id="sp-curve-canvas"></canvas>
                    </div>
                </div>

                <div class="control-section">
                    <div class="control-section-header">Converter</div>
                    <div class="sp-converter">
                        <div class="sp-converter-row">
                            <label>Voltage</label>
                            <input type="number" id="cv-vs-v" class="property-input" step="0.1">
                            <span class="sp-conv-sep">↔</span>
                            <input type="number" id="cv-vs-s" class="property-input" step="1">
                            <label>Speed</label>
                        </div>
                        <div class="sp-converter-row">
                            <label>Power %</label>
                            <input type="number" id="cv-ps-p" class="property-input" step="1">
                            <span class="sp-conv-sep">↔</span>
                            <input type="number" id="cv-ps-s" class="property-input" step="1">
                            <label>Speed</label>
                        </div>
                        <div class="text-muted text-xs" id="cv-ps-v"></div>
                    </div>
                </div>

${this._calibrationGroupHtml(p)}

                <details class="tc-disclosure">
                    <summary>Motion &amp; drag</summary>
                    <div class="tc-disclosure-body">
                    ${this._rateRow('Acceleration (km/h/s)', 'sp-accel', p.acceleration_rate, ro)}
                    ${this._rateRow('Deceleration (km/h/s)', 'sp-decel', p.deceleration_rate, ro)}
                    ${this._rateRow('Emergency brake (km/h/s)', 'sp-eb', p.emergency_brake_rate, ro)}
                    ${this._rateRow('Friction (km/h/s)', 'sp-friction', p.friction_rate, ro)}
                    ${this._rateRow('Operational decel ratio', 'sp-opdecel', p.operational_decel_ratio, ro)}
                    ${this._rateRow('Drag coefficient', 'sp-drag', p.drag_coefficient, ro)}
                    ${this._rateRow('Reference speed (km/h)', 'sp-refspeed', p.reference_speed_kmh, ro)}
                    </div>
                </details>

                <details class="tc-disclosure">
                    <summary>Notch response</summary>
                    <div class="tc-disclosure-body">
                    <div class="input-small-row mb-8"><label class="input-small-label">Power notches (× acceleration)</label></div>
                    ${POWER_NOTCHES.map(([h]) => this._notchRow(h, `sp-pm-${h}`, p.power_multipliers[h], ro)).join('')}
                    <div class="input-small-row mb-8"><label class="input-small-label">Brake notches (× operational decel)</label></div>
                    ${BRAKE_NOTCHES.map(([h]) => this._notchRow(h, `sp-bm-${h}`, p.brake_multipliers[h], ro)).join('')}
                    </div>
                </details>

                ${
                  p.profile_id === 'default'
                    ? ''
                    : `<div class="flex mt-16"><button id="sp-delete-btn" class="btn btn-danger btn-sm">Delete profile</button></div>`
                }
            </div>`;
  }

  /** `readOnly`: the field belongs to the model profile of the instance on screen. */
  private _rateRow(label: string, id: string, value: number, readOnly = false): string {
    return `
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="${id}">${label}</label>
                        <input type="number" id="${id}" class="input-small-field input-small-field-compact" value="${fmt(value, 4)}" step="0.1"${readOnly ? ' disabled' : ''}>
                    </div>`;
  }

  private _notchRow(label: string, id: string, value: number, readOnly = false): string {
    return `
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="${id}">${label}</label>
                        <input type="number" id="${id}" class="input-small-field input-small-field-compact" value="${fmt(value ?? 0, 2)}" min="0" step="0.05"${readOnly ? ' disabled' : ''}>
                    </div>`;
  }

  private _initEditor(): void {
    if (!this._profile) return;

    const canvas = document.getElementById('sp-curve-canvas') as HTMLCanvasElement | null;
    if (canvas) {
      // Both a model profile and a locomotive's own profile may edit the curve.
      this._plot = new CurvePlot({
        canvas,
        color: getComputedStyle(document.documentElement).getPropertyValue('--success-color').trim() || '#4ade80',
        onChange: (pts, dragging) => this._applyPlot(pts, dragging),
        onInsert: (seg, x, y) => this._insertPlotPoint(seg, x, y),
        onRemove: (i) => this._removePoint(i),
      });
      this._redrawPlot();
    }

    this._rebuildPointList();
    this._bindScalars();
    this._bindLightingField();
    this._bindConverter();
    this._recomputeConverter();

    const del = document.getElementById('sp-delete-btn');
    if (del) del.onclick = () => this._deleteProfile(this._profile!.profile_id);

    const openModel = document.getElementById('sp-open-model') as HTMLButtonElement | null;
    if (openModel) openModel.onclick = () => void this.showProfileEditor(openModel.dataset.profileId || '');

    const model = document.getElementById('sp-model') as HTMLInputElement | null;
    if (model)
      model.onchange = () => {
        this._profile!.model = model.value.trim();
        this._commit();
      };

    this._syncValues();
    void this._initCalibration();
  }

  // === Calibration group ===
  //
  // design_new.md "Calibrating a locomotive" and "Speed calibration" (The
  // panel). The group's locomotive row names the train the speed pass drives;
  // the pass never picks a train for itself. The readiness line under it names
  // every opening condition the named train does not yet meet, read from the
  // server's readiness check. The lighting-voltage field sits in the group too:
  // the operator types the value and the profile saves it like any other
  // field. The speed pass opens in place under its Calibrate speed control. A
  // pass the group shows ends when the operator names another train, leaves
  // the profile, or closes the panel.

  /** The train the locomotive row names. Kept when the panel shows a profile
   *  again (after a Save, or coming back from the list) while the train is
   *  still listed; closing the panel forgets it. */
  private _calTrainId: string | null = null;
  private _cal: CalibrationGroupState | null = null;
  /** The named train, once its speed pass was committed; the host marks that
   *  step done. */
  private _speedCommittedFor: string | null = null;

  /** Tell the host what the group shows now. */
  private _notifyCalibration(): void {
    const host = this._host;
    if (!host) return;
    const cal = this._cal;
    const trainId = this._calTrainId;
    host.calibrationChanged(
      cal
        ? {
            profileId: cal.profileId,
            trainId,
            view: cal.view,
            ready: cal.ready,
            unmet: cal.unmet,
            lightingVoltageSet: !!trainId && this._trainLightingVoltage() != null,
            speedCommitted: !!trainId && this._speedCommittedFor === trainId,
          }
        : null
    );
  }

  private _calibrationGroupHtml(p: SpeedProfileData): string {
    return `
                <div class="control-section" id="sp-cal-group">
                    <div class="control-section-header">Calibration</div>
                    <div class="input-small-row sp-cal-train-row">
                        <label class="input-small-label input-small-label-inline" for="sp-cal-train">Locomotive</label>
                        <select id="sp-cal-train" class="input-small-field" title="The placed train the speed pass drives. Naming another train ends a pass that is open."></select>
                    </div>
                    <div class="sp-cal-readiness" id="sp-cal-readiness"></div>
                    <div id="sp-lighting-row">
                        <div class="input-small-row sp-cal-run-head">
                            <label class="input-small-label input-small-label-inline" for="sp-lighting">Lighting voltage</label>
                            <input type="number" id="sp-lighting" class="input-small-field input-small-field-compact" min="0" step="0.05" value="${p.lighting_voltage == null ? '' : fmt(p.lighting_voltage)}" placeholder="none">
                            <span class="sp-scalar-unit">V</span>
                        </div>
                        <div class="text-muted text-xs mb-8" id="sp-lighting-hint"></div>
                    </div>
                    <div id="sp-speed-row"></div>
                </div>`;
  }

  private async _initCalibration(): Promise<void> {
    const root = document.getElementById('sp-cal-group');
    if (!root || !this._profile) return;
    const cal: CalibrationGroupState = {
      root,
      profileId: this._profile.profile_id,
      profiles: [],
      profilesReadFor: new Set(),
      view: 'none',
      ready: false,
      unmet: null,
      readinessError: null,
      speedRun: null,
      measuredBlockId: '',
      stretchText: '',
      stretchResolved: false,
      message: '',
      messageWarn: false,
      timer: null,
      busy: false,
      compare: null,
      generation: 0,
    };
    this._cal = cal;
    try {
      cal.profiles = (await this.api.getSpeedProfiles()) || [];
    } catch (err: any) {
      showToast('Failed to load speed profiles: ' + (err.message || err), 'error', 5000);
    }
    if (this._cal !== cal) return;

    const select = root.querySelector('#sp-cal-train') as HTMLSelectElement;
    select.onchange = () => void this._nameTrain(select.value || null);

    // The pick survives a reload of the panel (after a Save) only while the
    // train is still one the row lists.
    const keep = this._calTrainId;
    this._calTrainId = null;
    if (keep && this._calChoices().some((t) => t.id === keep)) {
      this._calTrainId = keep;
      await this._pickUpOpenRuns(cal, keep);
    }
    if (this._cal !== cal) return;
    this._renderCalibration();
    cal.timer = window.setInterval(() => void this._calTick(), CALIBRATION_REFRESH_MS);
    void this._refreshReadiness();
  }

  /** The trains the row lists, from the train list every network refresh
   *  brings; the picker re-reads it on each refresh tick. */
  private _calChoices(): any[] {
    const cal = this._cal;
    if (!cal || !this._profile) return [];
    return calibrationTrainChoices(stateManager.getAll('trains'), cal.profiles, cal.profileId);
  }

  private _namedTrain(): any | null {
    if (!this._calTrainId) return null;
    return this._calChoices().find((t) => t.id === this._calTrainId) || null;
  }

  private _trainStatus(trainId: string): string | null {
    const controller = stateManager.getController(trainId);
    if (!controller) return null;
    return trainDisplayStatus(controller, stateManager.getOperatorStatus(trainId));
  }

  /** Name a train in the locomotive row. Naming another train ends the run
   *  the group is showing for the previous one (design_new.md "What ends a
   *  calibration run"). */
  private async _nameTrain(trainId: string | null): Promise<void> {
    const cal = this._cal;
    if (!cal || trainId === this._calTrainId) return;
    void this._endShownRun(cal);
    this._calTrainId = trainId;
    this._speedCommittedFor = null;
    cal.generation += 1;
    cal.view = cal.view === 'speed-setup' ? 'speed-setup' : 'none';
    cal.speedRun = null;
    cal.ready = false;
    cal.unmet = null;
    cal.readinessError = null;
    cal.message = '';
    if (trainId) await this._pickUpOpenRuns(cal, trainId);
    if (this._cal !== cal) return;
    this._renderCalibration();
    void this._refreshReadiness();
  }

  /**
   * A pass already open for the named train — opened before a page reload,
   * say — or a finished pass waiting for a commit is shown so the operator
   * can end it, rather than left to block a new pass with nothing on screen.
   */
  private async _pickUpOpenRuns(cal: CalibrationGroupState, trainId: string): Promise<void> {
    const generation = cal.generation;
    const speedRun = await this.api.getCalibration(trainId).catch(() => null);
    if (this._cal !== cal || cal.generation !== generation) return;
    const view = viewForOpenRun(speedRun);
    if (view === 'none') return;
    cal.view = view;
    cal.speedRun = speedRun;
    cal.measuredBlockId = String(speedRun.measured_block_id || '');
    this._highlightStretch(cal.measuredBlockId, speedRun.outer_block_ids || []);
  }

  /** End whatever pass the group is showing, writing nothing. Resolves once
   *  the server has ended it. */
  private _endShownRun(cal: CalibrationGroupState): Promise<void> {
    const trainId = this._calTrainId;
    if (!trainId) return Promise.resolve();
    let ended: Promise<unknown> = Promise.resolve();
    if (cal.view === 'speed-running' || cal.view === 'speed-held') {
      // One call ends an open pass and discards a held result alike.
      ended = this.api.cancelCalibration(trainId);
    }
    if (cal.view !== 'speed-setup') this._clearStretchHighlight();
    cal.speedRun = null;
    return ended.then(
      () => {},
      () => {}
    );
  }

  /** Leaving the profile or closing the panel ends the shown pass. */
  private _teardownCalibration(): void {
    const cal = this._cal;
    if (!cal) return;
    if (cal.timer !== null) window.clearInterval(cal.timer);
    cal.timer = null;
    void this._endShownRun(cal);
    this._clearStretchHighlight();
    cal.compare?.destroy();
    cal.compare = null;
    this._cal = null;
    this._notifyCalibration();
  }

  private async _calTick(): Promise<void> {
    const cal = this._cal;
    if (!cal) return;
    // The panel's content was replaced (another inspector opened in its
    // place): the operator left the profile.
    if (!cal.root.isConnected) {
      this._teardownCalibration();
      return;
    }
    this._refreshTrainPicker();
    if (!this._calTrainId || cal.busy) return;
    if (cal.view === 'speed-running') await this._pollSpeedPass(cal);
    else await this._refreshReadiness();
  }

  private _renderCalibration(): void {
    this._refreshTrainPicker(true);
    this._renderReadiness();
    this._renderLightingHint();
    this._renderSpeedRow();
    this._notifyCalibration();
  }

  /** Fill the picker, or refresh its rows' states in place. Options are only
   *  rebuilt when the set of trains changes, and never while it is open. */
  private _refreshTrainPicker(force = false): void {
    const cal = this._cal;
    const select = cal?.root.querySelector('#sp-cal-train') as HTMLSelectElement | null;
    if (!cal || !select) return;
    this._rereadProfilesForNewTrains(cal);
    const choices = this._calChoices();
    const named = this._calTrainId;
    const ids = choices.map((t) => t.id).join('\n');
    if (force || (select.dataset.ids !== ids && document.activeElement !== select)) {
      const placeholder = choices.length
        ? '<option value="">Choose a locomotive…</option>'
        : '<option value="">No placed train uses this profile</option>';
      // A named train that left the list (its profile changed, say) stays
      // named until the operator picks another, so its run is not dropped
      // behind the operator's back. A deleted train is forgotten once the
      // readiness check no longer finds it.
      const missing = named && !choices.some((t) => t.id === named);
      select.innerHTML =
        placeholder +
        (missing ? `<option value="${escapeAttr(named)}">${escapeAttr(named)}</option>` : '') +
        choices
          .map((t) => `<option value="${escapeAttr(t.id)}"></option>`)
          .join('');
      select.dataset.ids = ids;
      select.disabled = !choices.length && !named;
      select.value = named || '';
    }
    for (const option of Array.from(select.options)) {
      const train = choices.find((t) => t.id === option.value);
      if (!train) continue;
      const label = trainChoiceLabel(train, this._trainStatus(train.id));
      if (option.textContent !== label) option.textContent = label;
    }
  }

  /**
   * A train placed while the panel is open names a new instance profile the
   * list read at open does not hold, so the row could not tell it derives from
   * the profile on screen. Re-read the list once for each such id.
   */
  private _rereadProfilesForNewTrains(cal: CalibrationGroupState): void {
    const known = new Set(cal.profiles.map((p: any) => p.profile_id));
    const unseen = stateManager
      .getAll('trains')
      .map((t: any) => t.speed_profile_id)
      .filter((id: any) => id && !known.has(id) && !cal.profilesReadFor.has(id));
    if (!unseen.length) return;
    unseen.forEach((id: string) => cal.profilesReadFor.add(id));
    this.api
      .getSpeedProfiles()
      .then((profiles: any[]) => {
        if (this._cal !== cal) return;
        cal.profiles = profiles || [];
        this._refreshTrainPicker();
      })
      .catch(() => {});
  }

  private async _refreshReadiness(): Promise<void> {
    const cal = this._cal;
    const trainId = this._calTrainId;
    if (!cal || !trainId || !readinessApplies(cal.view)) {
      this._renderReadiness();
      this._notifyCalibration();
      return;
    }
    const generation = cal.generation;
    const view = cal.view;
    cal.busy = true;
    try {
      const block = view === 'speed-setup' ? cal.measuredBlockId : '';
      const r = await this.api.getCalibrationReadiness(trainId, block);
      if (this._cal !== cal || cal.generation !== generation || cal.view !== view) return;
      cal.ready = !!r.ready;
      cal.unmet = r.unmet || [];
      cal.readinessError = null;
    } catch (err: any) {
      if (this._cal !== cal || cal.generation !== generation) return;
      if (err.status === 404) {
        // The train is gone (deleted, or the layout was cleared or replaced).
        // Its pass went with it, so there is nothing to end: stop naming it.
        this._forgetNamedTrain(cal);
        return;
      }
      cal.ready = false;
      cal.unmet = null;
      cal.readinessError = err.message || 'Cannot check the train';
    } finally {
      cal.busy = false;
    }
    this._renderReadiness();
    this._updateCalibrationControls();
    this._notifyCalibration();
  }

  private _forgetNamedTrain(cal: CalibrationGroupState): void {
    this._calTrainId = null;
    cal.generation += 1;
    cal.view = cal.view === 'speed-setup' ? 'speed-setup' : 'none';
    cal.speedRun = null;
    cal.ready = false;
    cal.unmet = null;
    cal.readinessError = null;
    this._renderCalibration();
  }

  private _renderReadiness(): void {
    const cal = this._cal;
    const el = cal?.root.querySelector('#sp-cal-readiness') as HTMLElement | null;
    if (!cal || !el) return;
    el.classList.remove('is-warn');
    if (!this._calTrainId) {
      el.textContent = this._calChoices().length
        ? 'Choose the locomotive the speed pass drives.'
        : 'Place a train that uses this profile — calibration drives the locomotive.';
      return;
    }
    if (!readinessApplies(cal.view)) {
      el.textContent = '';
      return;
    }
    if (cal.readinessError) {
      el.classList.add('is-warn');
      el.textContent = cal.readinessError;
      return;
    }
    if (cal.unmet === null) return; // first check still on its way
    if (cal.ready) {
      el.textContent =
        'Ready. Start powers the train on if it is off and selects manual.';
      return;
    }
    el.classList.add('is-warn');
    el.innerHTML =
      'Not ready:<ul>' + cal.unmet.map((u) => `<li>${escapeAttr(u)}</li>`).join('') + '</ul>';
  }

  private _controls(): ReturnType<typeof calibrationControls> {
    const cal = this._cal!;
    return calibrationControls({
      trainNamed: !!this._calTrainId,
      ready: cal.ready,
      view: cal.view,
      stretchResolved: cal.stretchResolved,
    });
  }

  /** Enable / disable the group's buttons without redrawing the rows. */
  private _updateCalibrationControls(): void {
    const cal = this._cal;
    if (!cal) return;
    const c = this._controls();
    const set = (sel: string, enabled: boolean) => {
      const btn = cal.root.querySelector(sel) as HTMLButtonElement | null;
      if (btn) btn.disabled = !enabled;
    };
    set('#sp-speed-calibrate', c.speedCalibrate);
    set('#sp-cal-start', c.speedStart);
  }

  // --- lighting-voltage field ---
  //
  // design_new.md "Calibrating a locomotive", step 2, and "SpeedProfile",
  // `lighting_voltage`. The operator types the voltage a standing, powered
  // train is held at so its lamps light; an empty field means the locomotive
  // has none and stands at 0 V. It edits the profile on screen and saves like
  // any other field. A save refuses a value below 0 V or at or above the
  // starting voltage, so the field says that before sending.

  /** The lighting voltage the named train runs on: its own profile's when it
   *  carries one, the profile on screen's otherwise. */
  private _trainLightingVoltage(): number | null {
    const cal = this._cal;
    if (!cal || !this._profile) return null;
    const v = trainStoredValue(this._namedTrain(), cal.profiles, this._profile, 'lighting_voltage');
    return v == null ? null : Number(v);
  }

  private _bindLightingField(): void {
    const el = document.getElementById('sp-lighting') as HTMLInputElement | null;
    if (!el) return;
    el.onchange = () => {
      if (!this._profile) return;
      const text = el.value.trim();
      const value = text === '' ? null : Number(text);
      const problem = lightingVoltageProblem(value, this._profile.voltage_speed_curve);
      if (problem) {
        this._renderLightingHint(problem);
        return;
      }
      this._profile.lighting_voltage = value;
      this._renderLightingHint();
      this._redrawPlot();
      this._commitDebounced(0);
    };
    this._renderLightingHint();
  }

  /** The line under the lighting-voltage field: why a typed value cannot be
   *  saved, or what the field means, and the value the named train runs on
   *  when its own profile sets a different one. */
  private _renderLightingHint(typed?: string): void {
    const hint = document.getElementById('sp-lighting-hint');
    if (!hint || !this._profile) return;
    // A curve edit can move the starting voltage down to the stored lighting
    // voltage, which a save refuses too.
    const problem =
      typed ?? lightingVoltageProblem(this._profile.lighting_voltage, this._profile.voltage_speed_curve);
    hint.classList.toggle('text-warning', !!problem);
    hint.classList.toggle('text-muted', !problem);
    if (problem) {
      hint.textContent = typed ? `Not saved: ${problem}.` : `A save is refused while ${problem}.`;
      return;
    }
    const start = startingVoltage(this._profile.voltage_speed_curve);
    const empty = this._isInstance()
      ? 'Empty reads the model profile’s value.'
      : 'Empty means none: a standing, powered train gets 0 V.';
    let text =
      `For a locomotive whose lamps light before it moves. It must stay below the starting voltage (${fmt(start)} V). ${empty}`;
    const train = this._namedTrain();
    const own = this._trainLightingVoltage();
    if (train && own !== this._profile.lighting_voltage) {
      text += ` ${trainChoiceLabel(train, null)} runs on ${own == null ? 'none' : `${fmt(own)} V`}, set on its own profile.`;
    }
    hint.textContent = text;
  }

  // --- speed pass ---
  //
  // design_new.md "Speed calibration". The pass times the locomotive across
  // one operator-named measured block and turns it around at each end itself,
  // so a plain length of track measures a locomotive with no loop to lap. It
  // writes nothing until the operator picks a commit target: Save to this
  // locomotive writes the measurement onto this loco's instance profile alone,
  // Promote to model writes it onto the model profile and clears this loco's
  // own curve, so every locomotive of that model with no curve of its own runs
  // on it (design_new.md "What commit writes").

  private _renderSpeedRow(): void {
    const cal = this._cal;
    const row = cal?.root.querySelector('#sp-speed-row') as HTMLElement | null;
    if (!cal || !row || !this._profile) return;
    cal.compare?.destroy();
    cal.compare = null;
    const p = this._profile;
    const stamp = p.calibrated_at
      ? `<span class="sp-cal-stamp">Measured ${escapeAttr(String(p.calibrated_at).replace('T', ' ').replace('+00:00', ' UTC'))}</span>`
      : '<span class="sp-cal-stamp" title="No speed calibration has written this curve.">Not measured</span>';
    const head = `
            <div class="input-small-row sp-cal-run-head">
                <label class="input-small-label input-small-label-inline">Voltage → speed curve</label>
                ${stamp}
                ${
                  cal.view === 'none'
                    ? `<button id="sp-speed-calibrate" class="btn btn-outline btn-sm" title="Measure this locomotive by running it back and forth across a block">Calibrate speed</button>`
                    : ''
                }
            </div>`;

    let body = '';
    if (cal.view === 'speed-setup') {
      const options = this._blockChoices()
        .map(
          (c) =>
            `<option value="${escapeAttr(c.id)}" ${c.id === cal.measuredBlockId ? 'selected' : ''}>${escapeAttr(c.label)}</option>`
        )
        .join('');
      body = `
            <div class="sp-cal-run-body">
                <div class="text-muted text-xs mb-8">The train runs back and forth across the measured block. The blocks either side are its run-up and its stopping room.</div>
                <div class="input-small-row mb-8">
                    <label class="input-small-label input-small-label-inline" for="sp-cal-block">Measured block</label>
                    <select id="sp-cal-block" class="input-small-field">
                        <option value="">Choose a block…</option>
                        ${options}
                    </select>
                    <button id="sp-cal-pick" class="btn btn-outline btn-sm" title="Pick on map (click the block on the layout; Esc cancels)"><i class="ti ti-map-pin"></i></button>
                </div>
                <div class="text-muted text-xs mb-8" id="sp-cal-stretch">${escapeAttr(cal.stretchText || 'Choose a block to see the stretch.')}</div>
                ${
                  cal.message
                    ? cal.messageWarn
                      ? `<div class="banner banner-warn sp-cal-message" role="status">${escapeAttr(cal.message)}</div>`
                      : `<div class="text-xs mb-8">${escapeAttr(cal.message)}</div>`
                    : ''
                }
                <div class="sp-cal-actions">
                    <button id="sp-cal-cancel" class="btn btn-outline btn-sm">Cancel</button>
                    <button id="sp-cal-start" class="btn btn-accent btn-sm" title="Take control of the train and run the pass">Start</button>
                </div>
            </div>`;
    } else if (cal.view === 'speed-running' || cal.view === 'speed-held') {
      const held = cal.view === 'speed-held';
      const modelName = (p.model || '').trim();
      body = `
            <div class="sp-cal-run-body">
                <div class="text-xs mb-8" id="sp-cal-progress"></div>
                <div class="sp-plot-block">
                    <div class="sp-point-list" id="sp-cal-points"></div>
                    <div>
                        <canvas class="sp-canvas sp-compare-canvas" id="sp-cal-compare"></canvas>
                        <div class="sp-compare-legend">
                            <span><i class="sp-legend-line sp-legend-measured"></i>Measured</span>
                            <span><i class="sp-legend-line sp-legend-stored"></i>Stored</span>
                        </div>
                    </div>
                </div>
                <div class="sp-crossing-list" id="sp-cal-crossings"></div>
                ${
                  held
                    ? `<div class="text-muted text-xs mb-8">Save to this locomotive changes only this locomotive. Promote to model writes the curve to ${modelName ? escapeAttr(modelName) : 'the model'}: every locomotive of that model without its own measured curve runs on it, this one included. A locomotive saved with its own curve keeps it.</div>
                <div class="sp-cal-actions">
                    <button id="sp-cal-discard" class="btn btn-outline btn-sm" title="Throw the measurement away; nothing is written">Discard</button>
                    <button id="sp-cal-model" class="btn btn-outline btn-sm">Promote to model</button>
                    <button id="sp-cal-locomotive" class="btn btn-accent btn-sm">Save to this locomotive</button>
                </div>`
                    : `<div class="sp-cal-actions">
                    <button id="sp-cal-stop" class="btn btn-outline btn-sm" title="End the pass; nothing is written">Stop</button>
                </div>`
                }
            </div>`;
    }
    row.innerHTML = head + body;

    const open = row.querySelector('#sp-speed-calibrate') as HTMLButtonElement | null;
    if (open) open.onclick = () => this._openSpeedSetup();
    if (cal.view === 'speed-setup') {
      const blockEl = row.querySelector('#sp-cal-block') as HTMLSelectElement;
      blockEl.onchange = () => void this._resolveStretch(blockEl.value.trim());
      (row.querySelector('#sp-cal-pick') as HTMLButtonElement).onclick = () =>
        this.editor.enterMode(this._buildMeasuredBlockPickMode());
      (row.querySelector('#sp-cal-cancel') as HTMLButtonElement).onclick = () =>
        this._closeSpeedSetup();
      (row.querySelector('#sp-cal-start') as HTMLButtonElement).onclick = () =>
        void this._startSpeedPass();
    } else if (cal.view === 'speed-running') {
      (row.querySelector('#sp-cal-stop') as HTMLButtonElement).onclick = () =>
        void this._stopSpeedPass();
    } else if (cal.view === 'speed-held') {
      (row.querySelector('#sp-cal-locomotive') as HTMLButtonElement).onclick = () =>
        void this._commitSpeedResult('locomotive');
      (row.querySelector('#sp-cal-model') as HTMLButtonElement).onclick = () =>
        void this._commitSpeedResult('model');
      (row.querySelector('#sp-cal-discard') as HTMLButtonElement).onclick = () =>
        void this._discardSpeedResult();
    }
    const canvas = row.querySelector('#sp-cal-compare') as HTMLCanvasElement | null;
    if (canvas) {
      cal.compare = new CurveComparePlot(canvas);
      this._paintSpeedPass();
    }
    this._updateCalibrationControls();
  }

  /** The progress line, the measured points, and the measured curve drawn
   *  against the curve the train runs on now. */
  private _paintSpeedPass(): void {
    const cal = this._cal;
    const run = cal?.speedRun;
    if (!cal || !run || !this._profile) return;
    const progress = cal.root.querySelector('#sp-cal-progress');
    if (progress) {
      progress.textContent = speedPassProgress(run, cal.view === 'speed-held');
    }
    const measured = measuredCurve(run.samples || []);
    const list = cal.root.querySelector('#sp-cal-points');
    if (list) {
      // Every finished step by voltage: a step that failed gave no point, and
      // the lowest step that passed is the starting voltage.
      list.innerHTML =
        `<div class="sp-point-head sp-point-head-readonly"><span>V</span><span>km/h</span></div>` +
        pointRows(run)
          .map((r) => {
            const speed = r.speed == null ? 'failed' : fmt(r.speed, 1);
            const cls = r.speed == null ? ' sp-point-failed' : r.starting ? ' sp-point-starting' : '';
            const title = r.speed == null
              ? 'This step failed and gave no point'
              : r.starting
                ? 'The starting voltage: the lowest step that passed'
                : '';
            return `<div class="sp-point-readonly${cls}"${title ? ` title="${title}"` : ''}><span>${volts(r.voltage)}</span><span>${speed}</span></div>`;
          })
          .join('');
    }
    const crossings = cal.root.querySelector('#sp-cal-crossings');
    if (crossings) {
      const rows = crossingRows(run);
      crossings.innerHTML = rows.length
        ? `<div class="sp-crossing-head"><span>V</span><span>Toward</span><span>s</span><span>km/h</span></div>` +
          rows
            .map(
              (r) =>
                `<div class="sp-crossing-row${r.used ? '' : ' sp-crossing-unused'}${r.firstOfStep ? ' sp-crossing-step' : ''}">` +
                `<span>${r.firstOfStep ? volts(r.voltage) : ''}</span>` +
                `<span>${escapeAttr(r.toward)}</span>` +
                `<span>${r.seconds}</span><span>${r.speed}</span>` +
                (r.note ? `<span class="sp-crossing-note">${escapeAttr(r.note)}</span>` : '') +
                `</div>`
            )
            .join('')
        : '';
    }
    const stored: Curve =
      trainStoredValue(this._namedTrain(), cal.profiles, this._profile, 'voltage_speed_curve') ||
      [];
    const xMax = Number(run.max_voltage) || this._profile.max_voltage;
    cal.compare?.setData(
      stored,
      measured,
      { xMax, yMin: 0, yMax: compareAxisTop(stored, measured) },
      this._marks(measured, this._trainLightingVoltage())
    );
  }

  private _openSpeedSetup(): void {
    const cal = this._cal;
    if (!cal || cal.view !== 'none') return;
    cal.view = 'speed-setup';
    cal.message = '';
    cal.ready = false;
    cal.unmet = null;
    this._renderCalibration();
    if (cal.measuredBlockId) void this._resolveStretch(cal.measuredBlockId);
    else void this._refreshReadiness();
  }

  private _closeSpeedSetup(): void {
    const cal = this._cal;
    if (!cal || cal.view !== 'speed-setup') return;
    cal.view = 'none';
    cal.message = '';
    cal.unmet = null;
    this._clearStretchHighlight();
    this._renderCalibration();
    void this._refreshReadiness();
  }

  /**
   * Resolve the stretch before Start, so the operator sees which two blocks
   * the points currently lead to rather than finding out by watching the
   * locomotive run into one. The same three blocks light up on the layout, so
   * "which stretch is this?" is answered by looking at the map.
   */
  private async _resolveStretch(blockId: string): Promise<void> {
    const cal = this._cal;
    if (!cal) return;
    cal.measuredBlockId = blockId;
    cal.stretchResolved = false;
    cal.stretchText = blockId ? 'Resolving the stretch…' : '';
    cal.unmet = null;
    this._updateCalibrationControls();
    this._clearStretchHighlight();
    let outer: string[] = [];
    if (blockId) {
      try {
        const s = await this.api.getCalibrationStretch(this._calTrainId || '_', blockId);
        if (this._cal !== cal || cal.measuredBlockId !== blockId) return;
        outer = s.outer_block_ids || [];
        const names: Record<string, string> = s.block_names || {};
        const shown = outer.map((id: string) => names[id] || id);
        cal.stretchText =
          `Run-up / stopping room: ${shown.join(' ↔ ')} · measured length ${s.length_cm} cm`;
        cal.stretchResolved = true;
      } catch (err: any) {
        if (this._cal !== cal || cal.measuredBlockId !== blockId) return;
        // The measured block still lights up on a rejection: seeing where it
        // sits is how the operator works out which end has no room.
        cal.stretchText = err.message || 'Cannot use that block';
      }
      this._highlightStretch(blockId, outer);
    }
    const stretch = cal.root.querySelector('#sp-cal-stretch');
    if (stretch) stretch.textContent = cal.stretchText || 'Choose a block to see the stretch.';
    this._updateCalibrationControls();
    void this._refreshReadiness();
  }

  /** Light the measured block and its two outer blocks on the layout. */
  private _highlightStretch(blockId: string, outer: string[]): void {
    const lit = this._blockTiles(blockId);
    for (const b of outer) this._blockTiles(b).forEach((k) => lit.add(k));
    this.editor.highlighter.show({ keys: lit }, { channel: 'reachable' });
    this.editor.renderer?.requestRedraw();
  }

  private _clearStretchHighlight(): void {
    this.editor.highlighter?.clear('reachable');
    this.editor.renderer?.requestRedraw();
  }

  // Start opens the pass; the server takes control of the train itself —
  // powering it up if off and selecting manual — once every opening condition
  // holds (design_new.md "Speed calibration", Opening a run takes control of
  // the train).
  private async _startSpeedPass(): Promise<void> {
    const cal = this._cal;
    const trainId = this._calTrainId;
    if (!cal || !trainId || cal.view !== 'speed-setup') return;
    const generation = cal.generation;
    const start = cal.root.querySelector('#sp-cal-start') as HTMLButtonElement | null;
    if (start) start.disabled = true;
    let run: any;
    try {
      run = await this.api.startCalibration(trainId, { measured_block_id: cal.measuredBlockId });
    } catch (err: any) {
      showToast(err.message || 'Cannot start calibration', 'error', 6000);
      void this._refreshReadiness();
      return;
    }
    if (this._cal !== cal || cal.generation !== generation) {
      void this.api.cancelCalibration(trainId).catch(() => {});
      return;
    }
    cal.view = 'speed-running';
    cal.message = '';
    cal.speedRun = run;
    this._renderCalibration();
  }

  /** Poll the open pass: its progress, then the held result once every step
   *  is measured, or the setup again when the pass ended. */
  private async _pollSpeedPass(cal: CalibrationGroupState): Promise<void> {
    const trainId = this._calTrainId!;
    const generation = cal.generation;
    cal.busy = true;
    let run: any;
    try {
      run = await this.api.getCalibration(trainId);
    } catch {
      return;
    } finally {
      cal.busy = false;
    }
    if (this._cal !== cal || cal.generation !== generation || cal.view !== 'speed-running') return;
    if (!run.open) {
      // EB, a power-off, a session stop, or a measurement fault ended it
      // (design_new.md "What ends a calibration run"). The ending's reason
      // went out on the command-result stream; the status reply carries the
      // latest one.
      cal.view = 'speed-setup';
      cal.speedRun = null;
      cal.message = speedPassEndedMessage(run.last_result);
      cal.messageWarn = true;
      this._renderCalibration();
      void this._refreshReadiness();
      return;
    }
    cal.speedRun = run;
    if (run.phase === 'measured') {
      cal.view = 'speed-held';
      this._renderCalibration();
      return;
    }
    this._paintSpeedPass();
  }

  // Stop ends the pass through `SpeedCalibration.abandon(...)`: it cuts the
  // drive, tears the leg route down, and writes nothing.
  private async _stopSpeedPass(): Promise<void> {
    const cal = this._cal;
    if (!cal || cal.view !== 'speed-running') return;
    const ended = this._endShownRun(cal);
    cal.view = 'speed-setup';
    cal.message = 'Stopped. Nothing was written.';
    cal.messageWarn = false;
    this._renderCalibration();
    await ended;
    void this._resolveStretch(cal.measuredBlockId);
  }

  private async _discardSpeedResult(): Promise<void> {
    const cal = this._cal;
    if (!cal || cal.view !== 'speed-held') return;
    const ended = this._endShownRun(cal);
    cal.view = 'none';
    this._renderCalibration();
    await ended;
    void this._refreshReadiness();
  }

  /** Write the held result. Nothing is written until the operator picks one
   *  of the two targets. */
  private async _commitSpeedResult(target: 'locomotive' | 'model'): Promise<void> {
    const cal = this._cal;
    const trainId = this._calTrainId;
    if (!cal || !trainId || cal.view !== 'speed-held') return;
    let result: any;
    try {
      result = await this.api.commitCalibration(trainId, target);
    } catch (err: any) {
      showToast(err.message || 'Commit failed', 'error', 6000);
      return;
    }
    if (this._cal !== cal) return;
    // The commit ended the pass, so there is nothing left to end.
    cal.view = 'none';
    cal.speedRun = null;
    this._speedCommittedFor = trainId;
    this._clearStretchHighlight();
    showToast(
      target === 'model' ? 'Promoted to the model profile' : 'Saved to this locomotive',
      'success',
      4000
    );
    // Promote to model wrote a different profile (the model's) and cleared
    // this loco's own curve, so open the profile that was actually written
    // rather than the one on screen.
    await this.showProfileEditor(result?.profile?.profile_id || this._profile!.profile_id);
  }

  /** The "x,y" tiles of a block, from the editor's cached track list. */
  private _blockTiles(blockId: string): Set<string> {
    const keys = new Set<string>();
    for (const t of this.editor.cachedNetwork?.tracks || []) {
      if (t.block_section_id === blockId) keys.add(`${t.x},${t.y}`);
    }
    return keys;
  }

  /** Every block on the layout, as menu rows for the measured-block picker. */
  private _blockChoices(): Array<{ id: string; label: string }> {
    const blocks = Object.values(this.editor.cachedNetwork?.blocks || {}) as any[];
    return blocks
      .map((b) => {
        const id = String(b.block_id);
        const name = String(b.name || '').trim();
        const len = Number(b.length_cm) || 0;
        return {
          id,
          label:
            (name && name !== id ? `${name} — ${id}` : id) +
            (len > 0 ? ` (${len} cm)` : ' (no length)'),
        };
      })
      .sort((a, b) => a.label.localeCompare(b.label));
  }

  /**
   * Layout-click mode for the measured block: the next click on a block names
   * it, exactly as if it had been chosen from the menu. Esc through the mode
   * manager leaves the choice as it was.
   */
  private _buildMeasuredBlockPickMode(): any {
    const setStatus = (text: string) => {
      const status = this.editor.$id?.('status-state');
      if (status) status.textContent = text;
    };
    let picked: string | null = null;
    return {
      name: 'calibration-block-pick',
      toolName: 'inspect',
      cancelMessage: 'Measured block pick cancelled',
      onEnter: () => setStatus('Calibration: click the block to measure. Esc to cancel.'),
      onClick: async ({ gridPos }: any) => {
        const track = (this.editor.cachedNetwork?.tracks || []).find(
          (t: any) => t.x === gridPos.x && t.y === gridPos.y
        );
        if (!track?.block_section_id) {
          showToast('That tile is not in a block — click a track inside one', 'info', 2500);
          return true;
        }
        picked = String(track.block_section_id);
        await this.editor.exitMode();
        return true;
      },
      onExit: () => {
        setStatus('Ready');
        const cal = this._cal;
        if (!picked || !cal || cal.view !== 'speed-setup') return;
        const blockEl = cal.root.querySelector('#sp-cal-block') as HTMLSelectElement | null;
        // A block the cached network did not carry when the menu was built
        // still has to be selectable — add the row rather than drop the pick.
        if (blockEl && !Array.from(blockEl.options).some((o) => o.value === picked)) {
          const opt = document.createElement('option');
          opt.value = picked;
          opt.textContent = picked;
          blockEl.appendChild(opt);
        }
        if (blockEl) blockEl.value = picked;
        void this._resolveStretch(picked);
      },
    };
  }

  // --- plot edits → working profile ---
  private _applyPlot(plotPts: CurvePoint[], dragging: boolean): void {
    this._profile!.voltage_speed_curve = plotPts
      .map((pt) => [pt[0], pt[1]] as CurvePoint)
      .sort((a, b) => a[0] - b[0]);
    this._syncValues();
    if (!dragging) {
      this._redrawPlot();
      this._commitDebounced(0);
    }
  }

  // --- numeric point list ---
  private _rebuildPointList(): void {
    const container = document.getElementById('sp-curve-list');
    if (!container || !this._profile) return;
    const pts = this._curvePoints();
    let html = `<div class="sp-point-head"><span>V</span><span>km/h</span><span></span></div>`;
    pts.forEach((pt, i) => {
      const isEnd = i === 0 || i === pts.length - 1;
      // The last point's voltage is max_voltage, set in its own field.
      const lockX = i === pts.length - 1;
      html += `
                <div class="sp-point-row" data-i="${i}">
                    <input type="number" class="property-input sp-px" data-i="${i}" value="${fmt(pt[0])}" step="0.1" ${lockX ? 'disabled title="The max voltage"' : ''}>
                    <input type="number" class="property-input sp-py" data-i="${i}" value="${fmt(pt[1], 1)}" step="1">
                    <button class="rr-flat-btn sp-pt-del" data-i="${i}" ${isEnd ? 'disabled title="The first and last points cannot be removed"' : ''}>✕</button>
                </div>`;
    });
    html += `<button class="rr-flat-btn sp-pt-add">+ add point</button>`;
    container.innerHTML = html;

    container.querySelectorAll<HTMLInputElement>('.sp-px, .sp-py').forEach((el) => {
      el.oninput = () => this._onPointInput();
    });
    container.querySelectorAll<HTMLButtonElement>('.sp-pt-del').forEach((btn) => {
      if (btn.disabled) return;
      btn.onclick = () => this._removePoint(Number(btn.dataset.i));
    });
    const addBtn = container.querySelector<HTMLButtonElement>('.sp-pt-add');
    if (addBtn) addBtn.onclick = () => this._addPoint();
  }

  /** A typed point. The save refuses a curve whose voltages do not rise or
   *  whose speed falls, and puts the last saved curve back. */
  private _onPointInput(): void {
    const container = document.getElementById('sp-curve-list');
    if (!container || !this._profile) return;
    const count = container.querySelectorAll('.sp-point-row').length;
    const plotPts: CurvePoint[] = [];
    for (let i = 0; i < count; i++) {
      const xEl = container.querySelector<HTMLInputElement>(`.sp-px[data-i="${i}"]`);
      const yEl = container.querySelector<HTMLInputElement>(`.sp-py[data-i="${i}"]`);
      const x = parseFloat(xEl?.value ?? '');
      const y = parseFloat(yEl?.value ?? '');
      if (!Number.isFinite(x) || !Number.isFinite(y)) return; // wait for valid input
      plotPts.push([x, y]);
    }
    this._applyPlot(plotPts, false);
  }

  private _addPoint(): void {
    if (!this._profile) return;
    const pts = this._curvePoints();
    // Insert at the midpoint of the widest voltage gap, speed on the line between.
    let gapAt = 0;
    let gap = -1;
    for (let i = 0; i < pts.length - 1; i++) {
      const g = pts[i + 1][0] - pts[i][0];
      if (g > gap) {
        gap = g;
        gapAt = i;
      }
    }
    if (gap < 2 * EPS_X) {
      showToast('No room to add another point here', 'warning', 2500);
      return;
    }
    const a = pts[gapAt];
    const b = pts[gapAt + 1];
    pts.splice(gapAt + 1, 0, [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]);
    this._applyPlot(pts, false);
    this._rebuildPointList();
  }

  /** Insert a point from a click on the plot line: segment `segIndex`, at plot-space (x, y). */
  private _insertPlotPoint(segIndex: number, x: number, y: number): void {
    if (!this._profile) return;
    const pts = this._curvePoints();
    const a = pts[segIndex];
    const b = pts[segIndex + 1];
    if (!a || !b) return;
    // Keep the new point strictly inside the segment's voltages, and its speed
    // between the two ends', so voltage rises and speed never falls.
    const loX = a[0] + EPS_X;
    const hiX = b[0] - EPS_X;
    if (hiX <= loX) {
      showToast('No room to add another point here', 'warning', 2500);
      return;
    }
    const nx = Math.min(hiX, Math.max(loX, x));
    const ny = Math.min(b[1], Math.max(a[1], y));
    pts.splice(segIndex + 1, 0, [nx, ny]);
    this._applyPlot(pts, false);
    this._rebuildPointList();
  }

  private _removePoint(index: number): void {
    if (!this._profile) return;
    const pts = this._curvePoints();
    if (index <= 0 || index >= pts.length - 1) return; // never the first or last point
    pts.splice(index, 1);
    this._applyPlot(pts, false);
    this._rebuildPointList();
  }

  // --- scalar fields ---
  private _bindScalars(): void {
    const maxv = document.getElementById('sp-maxv') as HTMLInputElement | null;

    // The curve's last point sits at max_voltage, so moving max_voltage moves
    // that point with it; both go in one save.
    if (maxv)
      maxv.oninput = () => this._onScalar(maxv, (v) => {
        const curve = this._profile!.voltage_speed_curve;
        if (curve.length >= 2 && v <= curve[curve.length - 2][0]) return; // above the point before it
        if (curve.length) curve[curve.length - 1][0] = v;
        this._profile!.max_voltage = v;
        this._redrawPlot();
        this._rebuildPointList();
      });

    const rates: Array<[string, keyof SpeedProfileData]> = [
      ['sp-accel', 'acceleration_rate'],
      ['sp-decel', 'deceleration_rate'],
      ['sp-eb', 'emergency_brake_rate'],
      ['sp-friction', 'friction_rate'],
      ['sp-opdecel', 'operational_decel_ratio'],
      ['sp-drag', 'drag_coefficient'],
      ['sp-refspeed', 'reference_speed_kmh'],
    ];
    rates.forEach(([id, key]) => {
      const el = document.getElementById(id) as HTMLInputElement | null;
      if (el) el.oninput = () => this._onScalar(el, (v) => ((this._profile as any)[key] = v));
    });

    // Per-train notch tables.
    POWER_NOTCHES.forEach(([h]) => {
      const el = document.getElementById(`sp-pm-${h}`) as HTMLInputElement | null;
      if (el) el.oninput = () => this._onScalar(el, (v) => { this._profile!.power_multipliers[h] = v; });
    });
    BRAKE_NOTCHES.forEach(([h]) => {
      const el = document.getElementById(`sp-bm-${h}`) as HTMLInputElement | null;
      if (el) el.oninput = () => this._onScalar(el, (v) => { this._profile!.brake_multipliers[h] = v; });
    });
  }

  private _onScalar(el: HTMLInputElement, apply: (v: number) => void): void {
    const v = parseFloat(el.value);
    if (!Number.isFinite(v)) return;
    apply(v);
    this._syncValues();
    this._commitDebounced(250);
  }

  // --- converter ---
  //
  // design_new.md "SpeedProfile": voltage and speed are read off the curve;
  // power percent is a straight line in speed from the starting speed (just
  // above 0%) to the top speed (100%), and 0% is stopped. A power percent's
  // voltage goes through its speed.
  private _bindConverter(): void {
    const bind = (id: string, compute: (val: number) => void) => {
      const el = document.getElementById(id) as HTMLInputElement | null;
      if (el)
        el.oninput = () => {
          const v = parseFloat(el.value);
          if (Number.isFinite(v)) compute(v);
        };
    };
    const curve = () => this._profile!.voltage_speed_curve;
    const set = (id: string, val: number, digits = 2) => {
      const el = document.getElementById(id) as HTMLInputElement | null;
      if (el && document.activeElement !== el) el.value = fmt(val, digits);
    };
    bind('cv-vs-v', (v) => set('cv-vs-s', voltageToSpeed(curve(), v), 1));
    bind('cv-vs-s', (s) =>
      set('cv-vs-v', speedToVoltage(curve(), s, this._profile!.max_voltage, this._profile!.lighting_voltage), 2)
    );
    bind('cv-ps-p', (pct) => {
      set('cv-ps-s', powerPercentToSpeed(curve(), pct), 1);
      this._showPowerVoltage(pct);
    });
    bind('cv-ps-s', (s) => {
      const pct = speedToPowerPercent(curve(), s);
      set('cv-ps-p', pct, 1);
      this._showPowerVoltage(pct);
    });
  }

  /** The line under the power row: the voltage a power percent drives. */
  private _showPowerVoltage(pct: number): void {
    const el = document.getElementById('cv-ps-v');
    const p = this._profile;
    if (!el || !p) return;
    const v = powerPercentToVoltage(p.voltage_speed_curve, pct, p.max_voltage, p.lighting_voltage);
    el.textContent = `${fmt(pct, 1)}% drives ${fmt(v)} V on the rails.`;
  }

  /** Seed the converter with a representative operating point (50% power). */
  private _recomputeConverter(): void {
    const p = this._profile;
    if (!p) return;
    const curve = p.voltage_speed_curve;
    const set = (id: string, val: number, digits = 2) => {
      const el = document.getElementById(id) as HTMLInputElement | null;
      if (el && document.activeElement !== el) el.value = fmt(val, digits);
    };
    const halfV = powerPercentToVoltage(curve, 50, p.max_voltage, p.lighting_voltage);
    set('cv-vs-v', halfV, 2);
    set('cv-vs-s', voltageToSpeed(curve, halfV), 1);
    set('cv-ps-p', 50, 0);
    set('cv-ps-s', powerPercentToSpeed(curve, 50), 1);
    this._showPowerVoltage(50);
  }

  /** Refresh point-list inputs, the curve's read-only values, the
   *  lighting-voltage field and the converter from the working profile
   *  without rebuilding DOM. Skips whatever input has focus. */
  private _syncValues(): void {
    const p = this._profile;
    if (!p) return;
    const setVal = (el: Element | null, val: string) => {
      const input = el as HTMLInputElement | null;
      if (input && document.activeElement !== input) input.value = val;
    };
    const list = document.getElementById('sp-curve-list');
    if (list) {
      p.voltage_speed_curve.forEach((pt, i) => {
        setVal(list.querySelector(`.sp-px[data-i="${i}"]`), fmt(pt[0]));
        setVal(list.querySelector(`.sp-py[data-i="${i}"]`), fmt(pt[1], 1));
      });
    }
    setVal(document.getElementById('sp-maxv'), fmt(p.max_voltage));
    setVal(document.getElementById('sp-lighting'), p.lighting_voltage == null ? '' : fmt(p.lighting_voltage));
    const text = (id: string, val: string) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };
    const curve = p.voltage_speed_curve;
    text('sp-start-v', fmt(startingVoltage(curve)));
    text('sp-start-s', fmt(startingSpeed(curve), 1));
    text('sp-top-s', fmt(topSpeed(curve), 1));
    this._renderLightingHint();
    this._recomputeConverter();
  }

  // --- commit / auto-save ---
  private _commitDebounced(ms: number): void {
    if (this._saveTimer) clearTimeout(this._saveTimer);
    this._saveTimer = setTimeout(() => this._commit(), ms);
  }

  private async _commit(): Promise<void> {
    if (!this._profile) return;
    // A save writes only the fields the operator changed, so a locomotive's
    // own profile goes on reading everything else from its model profile
    // (design_new.md "SpeedProfile", Editing a profile by hand).
    const edits = changedProfileFields(
      this._lastSaved as unknown as Record<string, unknown>,
      this._profile as unknown as Record<string, unknown>,
      this._isInstance()
    );
    if (!Object.keys(edits).length) return;
    try {
      await this.api.updateSpeedProfile(this._profile.profile_id, edits);
      const view = await this.api.getSpeedProfile(this._profile.profile_id, { resolved: true });
      const hadOwn = this._ownFields.join(',');
      this._applyView(view);
      // The first hand edit of an instance's curve or lighting voltage changes
      // what the note says.
      if (this._isInstance() && hadOwn !== this._ownFields.join(',')) {
        const note = document.querySelector('.sp-instance-note');
        if (note) note.outerHTML = this._instanceNoteHtml();
        const openModel = document.getElementById('sp-open-model') as HTMLButtonElement | null;
        if (openModel) openModel.onclick = () => void this.showProfileEditor(openModel.dataset.profileId || '');
      }
      this._redrawPlot();
      this._syncValues();
      this._notifyCalibration();
    } catch (err: any) {
      console.error('[SpeedProfileEditorHandler] save failed:', err);
      showToast('Failed to save profile: ' + (err.message || err), 'error', 5000);
      // Revert optimistic edits to the last server-confirmed state.
      if (this._lastSaved) this._profile = this._clone(this._lastSaved);
      this._rebuildPointList();
      this._redrawPlot();
      this._syncValues();
    }
  }

  private _teardownPlots(): void {
    if (this._saveTimer) {
      clearTimeout(this._saveTimer);
      this._saveTimer = null;
    }
    this._plot?.destroy();
    this._plot = null;
  }

  /** The secondary sidebar calls this when it closes or swaps the panel out.
   *  A hosted panel is not in the sidebar, so only its host closes it. */
  _cleanup(): void {
    if (this._host) return;
    this.close();
  }

  /** Closing the panel ends the pass the calibration group shows, and
   *  forgets the named train. */
  close(): void {
    this._teardownCalibration();
    this._calTrainId = null;
    this._speedCommittedFor = null;
    this._teardownPlots();
    this._profile = null;
    this._lastSaved = null;
    if (this._host) this._host.el.innerHTML = '';
  }
}
