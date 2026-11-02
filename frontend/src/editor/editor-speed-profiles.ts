// editor-speed-profiles.ts — Speed Profile Editor Handler.
//
// A standalone manager for `SpeedProfile` records (speed_profile.py): a list
// panel plus a per-profile curve editor. Each calibration curve is edited two
// ways at once — a draggable canvas plot and a numeric point list — both routed
// through one auto-saving commit path (DESIGN_LANGUAGE.md §4 "Two views of one
// value"). A converter section does y=f(x) and x=f^-1(y) for all three
// relations (voltage↔power, power↔speed, voltage↔speed).
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
  measuredCurve,
  readinessApplies,
  readinessKind,
  speedPassEndedMessage,
  trainChoiceLabel,
  trainStoredValue,
  viewForOpenRuns,
} from '../lib/calibration-panel';
import type { EditorHost } from './types';
import {
  type Curve,
  type CurvePoint,
  reanchorVoltagePowerCurve,
  voltageToPower,
  powerToVoltage,
  powerPercentToSpeed,
  speedToPower,
  voltageToSpeedTwoStage,
  speedToVoltageTwoStage,
  topSpeed,
  maxVoltage,
  lightingVoltage,
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
  power_speed_curve: CurvePoint[];
  voltage_power_curve: CurvePoint[];
  lighting_voltage: number;
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

interface PlotAxis {
  yMin: number;
  yMax: number;
}

interface CurvePlotOptions {
  canvas: HTMLCanvasElement;
  color: string;
  /** Lock the left endpoint in Y too (plot-2 origin (0,0)). X of both endpoints is always locked. */
  lockLeftY: boolean;
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
  /** The open lighting run's candidate and ceiling. */
  lighting: { candidate: number; ceiling: number } | null;
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
  /** Step presses run one after another, so a quick double press counts twice. */
  stepChain: Promise<void>;
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
  /** A lighting run was saved, or a speed pass committed, for the named
   *  train since the panel was opened on it. */
  lightingSaved: boolean;
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

/** How often the calibration group re-reads the readiness check and the open run. */
const CALIBRATION_REFRESH_MS = 1000;

const EPS_X = 0.5; // min power gap between adjacent points (keeps X strictly ascending)
const EPS_Y = 0.02; // min gap to keep Y strictly ascending (invertibility)
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

/** The gridlines, tick labels and axes every power-axis plot shares. */
function drawPlotFrame(
  ctx: CanvasRenderingContext2D,
  rect: DOMRect,
  axis: PlotAxis,
  xToPx: (x: number) => number,
  yToPx: (y: number) => number
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
  // X gridlines (power 0..100 every 25)
  for (let gx = 0; gx <= 100; gx += 25) {
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
    ctx.fillText(String(gx), px, y0 + 12);
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
}

/**
 * One editable piecewise-linear plot over a `<canvas>`. Axis-agnostic: it works
 * in plot space `[x, y]` (x = power 0..100 here) and reports edits back through
 * `onChange` (drag a handle), `onInsert` (left-click the line to add a point),
 * and `onRemove` (right-click an interior point to delete it — the two endpoints
 * are locked). The handler owns translation to/from backend curve storage and the
 * commit. Both X and Y are kept strictly ascending so the curve stays invertible
 * (the backend's reverse lookups assume monotonicity).
 */
class CurvePlot {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private color: string;
  private lockLeftY: boolean;
  private onChange: (points: CurvePoint[], dragging: boolean) => void;
  private onInsert: (segIndex: number, x: number, y: number) => void;
  private onRemove: (index: number) => void;
  private points: CurvePoint[] = [];
  private axis: PlotAxis = { yMin: 0, yMax: 1 };
  private xMin = 0;
  private xMax = 100;
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
    this.lockLeftY = opts.lockLeftY;
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

  /** Replace the plotted points and axis, then redraw at device resolution. */
  setData(points: CurvePoint[], axis: PlotAxis): void {
    this.points = points.map((p) => [p[0], p[1]] as CurvePoint);
    this.axis = axis;
    this.resize();
  }

  /** Turn editing off while the plot shows a preview rather than the stored
   *  curve, so a drag cannot save the preview. */
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
    return PAD.left + ((x - this.xMin) / (this.xMax - this.xMin)) * this.plotW(rect);
  }
  private yToPx(y: number, rect: DOMRect): number {
    const { yMin, yMax } = this.axis;
    const span = yMax - yMin || 1;
    return PAD.top + (1 - (y - yMin) / span) * this.plotH(rect);
  }
  private pxToX(px: number, rect: DOMRect): number {
    return this.xMin + ((px - PAD.left) / this.plotW(rect)) * (this.xMax - this.xMin);
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
      (y) => this.yToPx(y, rect)
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
    const i = this.dragIndex;
    const last = this.points.length - 1;
    const isLeft = i === 0;
    const isRight = i === last;

    // X: endpoints locked; interior strictly between neighbours.
    let nx = this.points[i][0];
    if (!isLeft && !isRight) {
      const lo = this.points[i - 1][0] + EPS_X;
      const hi = this.points[i + 1][0] - EPS_X;
      nx = Math.min(hi, Math.max(lo, this.pxToX(px, rect)));
    }

    // Y: clamp to axis, then keep strictly ascending vs neighbours.
    let ny = this.points[i][1];
    if (!(isLeft && this.lockLeftY)) {
      ny = Math.min(this.axis.yMax, Math.max(this.axis.yMin, this.pxToY(py, rect)));
      const lo = isLeft ? this.axis.yMin : this.points[i - 1][1] + EPS_Y;
      const hi = isRight ? this.axis.yMax : this.points[i + 1][1] - EPS_Y;
      ny = Math.min(hi, Math.max(lo, ny));
    }

    this.points[i] = [nx, ny];
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

  /** Right-click an interior handle to remove it; the two endpoints are locked. */
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
 * A read-only plot of the measured power/speed curve over the stored one, for
 * a speed pass in progress and for a held result (design_new.md "Speed
 * calibration", The panel). The stored curve is drawn dashed and dim and the
 * measured one solid with a dot per point, so the two read apart without
 * relying on colour.
 */
class CurveComparePlot {
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private stored: Curve = [];
  private measured: Curve = [];
  private axis: PlotAxis = { yMin: 0, yMax: 1 };
  private ro: ResizeObserver | null;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d')!;
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(canvas);
  }

  setData(stored: Curve, measured: Curve, axis: PlotAxis): void {
    this.stored = stored;
    this.measured = measured;
    this.axis = axis;
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
    const xToPx = (x: number) => PAD.left + (x / 100) * w;
    const yToPx = (y: number) => PAD.top + (1 - (y - this.axis.yMin) / span) * h;
    drawPlotFrame(ctx, rect, this.axis, xToPx, yToPx);

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
  private _vpPlot: CurvePlot | null = null;
  private _psPlot: CurvePlot | null = null;
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
    const top = topSpeed(p.power_speed_curve || []);
    const pts =
      (p.voltage_power_curve?.length || 0) + (p.power_speed_curve?.length || 0);
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
      this._lightingSavedFor = null;
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

  /** A locomotive's own profile: only its power/speed curve is edited here;
   *  every other value belongs to its model profile. */
  private _isInstance(): boolean {
    return this._derivedFrom !== null;
  }

  /** The note an instance profile opens with: what it carries itself and
   *  where its other values are edited. */
  private _instanceNoteHtml(): string {
    if (!this._derivedFrom) return '';
    const model = escapeAttr(this._derivedFrom);
    const curve = this._ownFields.includes('power_speed_curve')
      ? 'This locomotive has its own power ↔ speed curve, so a change to the model curve does not reach it.'
      : `Its power ↔ speed curve comes from <span class="font-mono">${model}</span>; editing it here gives this locomotive its own curve.`;
    return `
                <div class="info-box text-xs mb-12 sp-instance-note">
                    This is one locomotive's own profile. ${curve}
                    Every other value comes from model profile <span class="font-mono">${model}</span> and is edited there.
                    <button type="button" class="rr-flat-btn" id="sp-open-model" data-profile-id="${model}">Edit ${model} →</button>
                </div>`;
  }

  private _normalize(d: any): SpeedProfileData {
    const vp: CurvePoint[] = (d.voltage_power_curve || []).map((p: number[]) => [p[0], p[1]] as CurvePoint);
    const ps: CurvePoint[] = (d.power_speed_curve || []).map((p: number[]) => [p[0], p[1]] as CurvePoint);
    return {
      profile_id: d.profile_id,
      name: d.name || '',
      model: d.model || '',
      calibrated_at: d.calibrated_at || null,
      voltage_power_curve: vp,
      power_speed_curve: ps,
      // max_voltage may be absent on older GETs — fall back to the curve top.
      max_voltage: typeof d.max_voltage === 'number' ? d.max_voltage : maxVoltage(vp, 12),
      // lighting_voltage mirrors the 0%-power point of the curve.
      lighting_voltage:
        typeof d.lighting_voltage === 'number' ? d.lighting_voltage : lightingVoltage(vp, 1.5),
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

  // Plot-space points (x = power 0..100). Plot-1 maps [v,p]→[p,v]; plot-2 uses [p,s].
  private _vpPlotPoints(): CurvePoint[] {
    return (this._profile!.voltage_power_curve || []).map((pt) => [pt[1], pt[0]] as CurvePoint);
  }
  private _psPlotPoints(): CurvePoint[] {
    return (this._profile!.power_speed_curve || []).map((pt) => [pt[0], pt[1]] as CurvePoint);
  }
  private _vpAxis(): PlotAxis {
    return { yMin: 0, yMax: Math.max(16, Math.ceil(this._profile!.max_voltage + 3)) };
  }
  private _psAxis(): PlotAxis {
    const top = topSpeed(this._profile!.power_speed_curve || []);
    return { yMin: 0, yMax: Math.max(30, Math.ceil(top * 1.25)) };
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
                    <div class="control-section-header">Voltage ↔ Power</div>
                    ${this._scalarRowHtml('sp-maxv', 'Max voltage (100% power)', fmt(p.max_voltage), {
                      editable: !ro,
                      unit: 'V',
                      attrs: 'type="number" min="0" step="0.1"',
                    })}
                    <div class="text-muted text-xs mb-8">X = power %, Y = track voltage. Click the line to add a point · drag to move · right-click a point to remove. The 0% point is the lighting voltage; its Calibrate in the calibration group is the one way to move it.</div>
                    <div class="sp-plot-block">
                        <div class="sp-point-list" id="sp-vp-list"></div>
                        <canvas class="sp-canvas" id="sp-vp-canvas"></canvas>
                    </div>
                </div>

                <div class="control-section">
                    <div class="control-section-header">Power ↔ Speed</div>
                    ${this._scalarRowHtml('sp-topspeed', 'Top speed (100% power)', fmt(topSpeed(p.power_speed_curve)), {
                      editable: true,
                      unit: 'km/h',
                      attrs: 'type="number" min="0" step="1"',
                    })}
                    <div class="text-muted text-xs mb-8">X = power %, Y = speed (km/h). Click the line to add a point · drag to move · right-click to remove. Left point is pinned at the origin.</div>
                    <div class="sp-plot-block">
                        <div class="sp-point-list" id="sp-ps-list"></div>
                        <canvas class="sp-canvas" id="sp-ps-canvas"></canvas>
                    </div>
                </div>

                <div class="control-section">
                    <div class="control-section-header">Converter</div>
                    <div class="sp-converter">
                        <div class="sp-converter-row">
                            <label>Voltage</label>
                            <input type="number" id="cv-vp-v" class="property-input" step="0.1">
                            <span class="sp-conv-sep">↔</span>
                            <input type="number" id="cv-vp-p" class="property-input" step="1">
                            <label>Power %</label>
                        </div>
                        <div class="sp-converter-row">
                            <label>Power %</label>
                            <input type="number" id="cv-ps-p" class="property-input" step="1">
                            <span class="sp-conv-sep">↔</span>
                            <input type="number" id="cv-ps-s" class="property-input" step="1">
                            <label>Speed</label>
                        </div>
                        <div class="sp-converter-row">
                            <label>Voltage</label>
                            <input type="number" id="cv-vs-v" class="property-input" step="0.1">
                            <span class="sp-conv-sep">↔</span>
                            <input type="number" id="cv-vs-s" class="property-input" step="1">
                            <label>Speed</label>
                        </div>
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

    const vpCanvas = document.getElementById('sp-vp-canvas') as HTMLCanvasElement | null;
    const psCanvas = document.getElementById('sp-ps-canvas') as HTMLCanvasElement | null;
    if (vpCanvas) {
      this._vpPlot = new CurvePlot({
        canvas: vpCanvas,
        color: getComputedStyle(document.documentElement).getPropertyValue('--info-color').trim() || '#4aa3ff',
        // The 0% point is the lighting voltage. design_new.md "Lighting-voltage
        // calibration", The panel: the field is read-only and its step controls
        // are the one way to move it, so the plot does not drag it either.
        lockLeftY: true,
        onChange: (pts, dragging) => this._applyVpPlot(pts, dragging),
        onInsert: (seg, x, y) => this._insertPlotPoint('vp', seg, x, y),
        onRemove: (i) => this._removePoint('vp', i),
      });
      this._vpPlot.setData(this._vpPlotPoints(), this._vpAxis());
      // An instance profile stores no voltage/power curve; it is its model's.
      if (this._isInstance()) this._vpPlot.setEditable(false);
    }
    if (psCanvas) {
      this._psPlot = new CurvePlot({
        canvas: psCanvas,
        color: getComputedStyle(document.documentElement).getPropertyValue('--success-color').trim() || '#4ade80',
        lockLeftY: true,
        onChange: (pts, dragging) => this._applyPsPlot(pts, dragging),
        onInsert: (seg, x, y) => this._insertPlotPoint('ps', seg, x, y),
        onRemove: (i) => this._removePoint('ps', i),
      });
      this._psPlot.setData(this._psPlotPoints(), this._psAxis());
    }

    this._rebuildPointList('vp');
    this._rebuildPointList('ps');
    this._bindScalars();
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

    void this._initCalibration();
  }

  // === Calibration group ===
  //
  // design_new.md "Calibrating a locomotive", "Speed calibration" (The panel)
  // and "Lighting-voltage calibration" (The panel). The group's locomotive row
  // names the train both runs drive; neither run picks a train for itself. The
  // readiness line under it names every opening condition the named train does
  // not yet meet, read from the server's readiness check. Both runs open in
  // place: the lighting run on the lighting-voltage row, the speed pass under
  // its Calibrate speed control. A run the group shows ends when the operator
  // names another train, leaves the profile, or closes the panel.

  /** The train the locomotive row names. Kept when the panel shows a profile
   *  again (after a Save, or coming back from the list) while the train is
   *  still listed; closing the panel forgets it. */
  private _calTrainId: string | null = null;
  private _cal: CalibrationGroupState | null = null;
  /** The named train, once its lighting run was saved or its speed pass
   *  committed; the host marks those steps done. */
  private _lightingSavedFor: string | null = null;
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
            lightingSaved: !!trainId && this._lightingSavedFor === trainId,
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
                        <select id="sp-cal-train" class="input-small-field" title="The placed train both calibration runs drive. Naming another train ends a run that is open."></select>
                    </div>
                    <div class="sp-cal-readiness" id="sp-cal-readiness"></div>
                    <div id="sp-lighting-row"></div>
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
      lighting: null,
      speedRun: null,
      measuredBlockId: '',
      stretchText: '',
      stretchResolved: false,
      message: '',
      messageWarn: false,
      timer: null,
      busy: false,
      stepChain: Promise.resolve(),
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
    this._lightingSavedFor = null;
    this._speedCommittedFor = null;
    cal.generation += 1;
    cal.view = cal.view === 'speed-setup' ? 'speed-setup' : 'none';
    cal.lighting = null;
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
   * A run already open for the named train — opened before a page reload,
   * say — or a finished pass waiting for a commit is shown so the operator
   * can end it, rather than left to block a new run with nothing on screen.
   */
  private async _pickUpOpenRuns(cal: CalibrationGroupState, trainId: string): Promise<void> {
    const generation = cal.generation;
    const [speedRun, lightingRun] = await Promise.all([
      this.api.getCalibration(trainId).catch(() => null),
      this.api.getLightingCalibration(trainId).catch(() => null),
    ]);
    if (this._cal !== cal || cal.generation !== generation) return;
    const view = viewForOpenRuns(speedRun, lightingRun);
    if (view === 'none') return;
    cal.view = view;
    if (view === 'lighting') {
      cal.lighting = {
        candidate: Number(lightingRun.candidate_voltage) || 0,
        ceiling: Number(lightingRun.ceiling_voltage) || 0,
      };
    } else {
      cal.speedRun = speedRun;
      cal.measuredBlockId = String(speedRun.measured_block_id || '');
      this._highlightStretch(cal.measuredBlockId, speedRun.outer_block_ids || []);
    }
  }

  /** End whatever run the group is showing, writing nothing. Resolves once
   *  the server has ended it. */
  private _endShownRun(cal: CalibrationGroupState): Promise<void> {
    const trainId = this._calTrainId;
    if (!trainId) return Promise.resolve();
    let ended: Promise<unknown> = Promise.resolve();
    if (cal.view === 'lighting') {
      ended = this.api.cancelLightingCalibration(trainId);
      this._previewLightingAnchor(null);
    } else if (cal.view === 'speed-running' || cal.view === 'speed-held') {
      // One call ends an open pass and discards a held result alike.
      ended = this.api.cancelCalibration(trainId);
    }
    if (cal.view !== 'speed-setup') this._clearStretchHighlight();
    cal.lighting = null;
    cal.speedRun = null;
    return ended.then(
      () => {},
      () => {}
    );
  }

  /** Leaving the profile or closing the panel ends the shown run. */
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
    else if (cal.view === 'lighting') await this._pollLightingRun(cal);
    else await this._refreshReadiness();
  }

  private _renderCalibration(): void {
    this._refreshTrainPicker(true);
    this._renderReadiness();
    this._renderLightingRow();
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
      const r = await this.api.getCalibrationReadiness(trainId, readinessKind(view), block);
      if (this._cal !== cal || cal.generation !== generation || cal.view !== view) return;
      cal.ready = !!r.ready;
      cal.unmet = r.unmet || [];
      cal.readinessError = null;
    } catch (err: any) {
      if (this._cal !== cal || cal.generation !== generation) return;
      if (err.status === 404) {
        // The train is gone (deleted, or the layout was cleared or replaced).
        // Its runs went with it, so there is nothing to end: stop naming it.
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
    if (cal.view === 'lighting') this._previewLightingAnchor(null);
    this._calTrainId = null;
    cal.generation += 1;
    cal.view = cal.view === 'speed-setup' ? 'speed-setup' : 'none';
    cal.lighting = null;
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
        ? 'Choose the locomotive both runs drive.'
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
        'Ready. Opening a run powers the train on if it is off and selects manual.';
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
    set('#sp-lighting-calibrate', c.lightingCalibrate);
    set('#sp-speed-calibrate', c.speedCalibrate);
    set('#sp-cal-start', c.speedStart);
  }

  // --- lighting-voltage row ---
  //
  // design_new.md "Lighting-voltage calibration", The panel. The row is the
  // run's panel: Calibrate opens it in place, and the row then holds the
  // candidate, a step down and a step up, Save and Revert. Nothing on the rig
  // can tell whether the locomotive moved, so the operator's eye is the
  // measurement: step up until it creeps, step back down one, and save. The
  // voltage/power curve above follows the candidate, so the operator sees what
  // Save will write before writing it.

  private _renderLightingRow(): void {
    const cal = this._cal;
    const row = cal?.root.querySelector('#sp-lighting-row') as HTMLElement | null;
    if (!cal || !row || !this._profile) return;
    const train = this._namedTrain();
    // The field is read-only: it shows the value the named train runs on,
    // and the step controls are the one way to move it.
    const stored = Number(
      trainStoredValue(train, cal.profiles, this._profile, 'lighting_voltage')
    );
    const head = `
            <div class="input-small-row sp-cal-run-head">
                <label class="input-small-label input-small-label-inline">Lighting voltage (0% power)</label>
                <span class="sp-cal-value" id="sp-lighting" title="Read-only: moved by Calibrate">${fmt(stored)}<small>V</small></span>
                ${
                  cal.view === 'lighting'
                    ? ''
                    : `<button id="sp-lighting-calibrate" class="btn btn-outline btn-sm" title="Put a test voltage on the rails under this locomotive and step it until the locomotive just creeps">Calibrate</button>`
                }
            </div>`;
    if (cal.view !== 'lighting' || !cal.lighting) {
      row.innerHTML = head;
      const open = row.querySelector('#sp-lighting-calibrate') as HTMLButtonElement | null;
      if (open) open.onclick = () => void this._openLightingRun();
      this._updateCalibrationControls();
      return;
    }
    row.innerHTML = `${head}
            <div class="sp-cal-run-body" id="sp-lighting-run" tabindex="-1">
                <div class="sp-lighting-stepper">
                    <button class="btn btn-outline sp-lighting-step" data-dir="-1" aria-label="Step down" title="Step down (↓)">−</button>
                    <div class="sp-lighting-readout"><span id="sp-lighting-value"></span><small>V</small></div>
                    <button class="btn btn-outline sp-lighting-step" data-dir="1" aria-label="Step up" title="Step up (↑)">+</button>
                </div>
                <div class="text-muted text-xs mb-8">Step up until the locomotive just creeps, then step back down one and save.</div>
                <div class="sp-cal-actions">
                    <button id="sp-lighting-revert" class="btn btn-outline btn-sm" title="End the run and keep the stored value (Esc)">Revert</button>
                    <button id="sp-lighting-save" class="btn btn-accent btn-sm" title="Write the candidate to this locomotive's lighting voltage">Save</button>
                </div>
            </div>`;
    const body = row.querySelector('#sp-lighting-run') as HTMLElement;
    row.querySelectorAll<HTMLButtonElement>('.sp-lighting-step').forEach((btn) => {
      btn.onclick = () => void this._stepLightingRun(Number(btn.dataset.dir));
    });
    (row.querySelector('#sp-lighting-revert') as HTMLButtonElement).onclick = () =>
      void this._revertLightingRun();
    (row.querySelector('#sp-lighting-save') as HTMLButtonElement).onclick = () =>
      void this._saveLightingRun();
    // The arrow keys step while the row has focus; Esc reverts.
    body.addEventListener('keydown', (e: KeyboardEvent) => {
      const action =
        e.key === 'ArrowUp' ? () => this._stepLightingRun(1)
        : e.key === 'ArrowDown' ? () => this._stepLightingRun(-1)
        : e.key === 'Escape' ? () => this._revertLightingRun()
        : null;
      if (!action) return;
      e.preventDefault();
      e.stopPropagation();
      void action();
    });
    this._paintLightingRun();
  }

  private _paintLightingRun(): void {
    const cal = this._cal;
    if (!cal?.lighting) return;
    const { candidate, ceiling } = cal.lighting;
    const value = cal.root.querySelector('#sp-lighting-value');
    if (value) value.textContent = candidate.toFixed(2);
    // The step controls stop at 0 and at the ceiling, so the row never shows
    // a value Save would refuse.
    const [down, up] = Array.from(
      cal.root.querySelectorAll<HTMLButtonElement>('.sp-lighting-step')
    );
    if (down) down.disabled = candidate <= 0;
    if (up) up.disabled = candidate >= ceiling;
    this._previewLightingAnchor(candidate);
  }

  /** Draw the voltage/power curve with its 0% point at the candidate, or put
   *  the stored curve back (`null`). The stored profile is not touched. */
  private _previewLightingAnchor(candidate: number | null): void {
    if (!this._profile || !this._vpPlot) return;
    const vp = this._profile.voltage_power_curve;
    const curve =
      candidate === null || !vp.length
        ? vp
        : reanchorVoltagePowerCurve(vp, candidate, this._profile.max_voltage);
    const plotPts = curve.map((pt) => [pt[1], pt[0]] as CurvePoint);
    this._vpPlot.setData(plotPts, this._vpAxis());
    // While they show the candidate, both views of plot 1 are a preview: a
    // drag or a typed point would save the candidate as the curve, so both
    // lock until the run ends.
    const previewing = candidate !== null;
    this._vpPlot.setEditable(!previewing && !this._isInstance());
    this._rebuildPointList('vp', previewing ? plotPts : undefined);
    if (previewing) {
      document
        .querySelectorAll<HTMLInputElement | HTMLButtonElement>('#sp-vp-list input, #sp-vp-list button')
        .forEach((el) => (el.disabled = true));
    }
  }

  private async _openLightingRun(): Promise<void> {
    const cal = this._cal;
    const trainId = this._calTrainId;
    if (!cal || !trainId) return;
    const generation = cal.generation;
    let run: any;
    try {
      run = await this.api.startLightingCalibration(trainId);
    } catch (err: any) {
      showToast(err.message || 'Cannot start lighting calibration', 'error', 6000);
      void this._refreshReadiness();
      return;
    }
    if (this._cal !== cal || cal.generation !== generation) {
      void this.api.cancelLightingCalibration(trainId).catch(() => {});
      return;
    }
    cal.view = 'lighting';
    cal.message = '';
    cal.lighting = {
      candidate: Number(run.candidate_voltage) || 0,
      ceiling: Number(run.ceiling_voltage) || 0,
    };
    this._renderCalibration();
    (cal.root.querySelector('#sp-lighting-run') as HTMLElement | null)?.focus();
  }

  private _stepLightingRun(dir: number): Promise<void> {
    const cal = this._cal;
    if (!cal || cal.view !== 'lighting') return Promise.resolve();
    cal.stepChain = cal.stepChain.then(() => this._sendLightingStep(cal, dir));
    return cal.stepChain;
  }

  private async _sendLightingStep(cal: CalibrationGroupState, dir: number): Promise<void> {
    const trainId = this._calTrainId;
    if (this._cal !== cal || !trainId || cal.view !== 'lighting') return;
    try {
      const next = await this.api.stepLightingCalibration(trainId, dir);
      if (this._cal !== cal || !cal.lighting) return;
      cal.lighting = {
        candidate: Number(next.candidate_voltage),
        ceiling: Number(next.ceiling_voltage),
      };
      this._paintLightingRun();
    } catch (err: any) {
      showToast(err.message || 'Step failed', 'error', 5000);
    }
  }

  private async _revertLightingRun(): Promise<void> {
    const cal = this._cal;
    if (!cal || cal.view !== 'lighting') return;
    // A step still on its way would otherwise reach a run that has ended.
    await cal.stepChain;
    if (this._cal !== cal || cal.view !== 'lighting') return;
    const ended = this._endShownRun(cal);
    cal.view = 'none';
    this._renderCalibration();
    await ended;
    void this._refreshReadiness();
  }

  private async _saveLightingRun(): Promise<void> {
    const cal = this._cal;
    const trainId = this._calTrainId;
    if (!cal || !trainId || cal.view !== 'lighting') return;
    // Save writes the candidate the operator last saw, so let every step
    // pressed before it land first.
    await cal.stepChain;
    if (this._cal !== cal || cal.view !== 'lighting') return;
    let result: any;
    try {
      result = await this.api.saveLightingCalibration(trainId);
    } catch (err: any) {
      showToast(err.message || 'Failed to save lighting voltage', 'error', 5000);
      return;
    }
    if (this._cal !== cal) return;
    // The run ended with the Save, so there is nothing left to end.
    cal.view = 'none';
    cal.lighting = null;
    this._lightingSavedFor = trainId;
    const written = this._namedTrain()?.speed_profile_id || this._profile!.profile_id;
    showToast(
      `Lighting voltage saved at ${Number(result.lighting_voltage).toFixed(2)} V on ${written}`,
      'success',
      3000
    );
    // The commit moved the 0%-power anchor of the curve this train runs on,
    // so reload rather than patch the field.
    await this.showProfileEditor(this._profile!.profile_id);
  }

  /** A lighting run can end without the operator (EB, a power-off, the
   *  session leaving Live); drop the stepper when it has. */
  private async _pollLightingRun(cal: CalibrationGroupState): Promise<void> {
    const trainId = this._calTrainId!;
    const generation = cal.generation;
    cal.busy = true;
    const run = await this.api.getLightingCalibration(trainId).catch(() => null);
    cal.busy = false;
    if (!run || this._cal !== cal || cal.generation !== generation) return;
    if (cal.view !== 'lighting' || run.open) return;
    this._previewLightingAnchor(null);
    cal.view = 'none';
    cal.lighting = null;
    showToast('The lighting run ended. Nothing was written.', 'info', 4000);
    this._renderCalibration();
    void this._refreshReadiness();
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
                <label class="input-small-label input-small-label-inline">Power → speed curve</label>
                ${stamp}
                ${
                  cal.view === 'none' || cal.view === 'lighting'
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
      progress.textContent =
        cal.view === 'speed-held'
          ? `Measured every step: ${run.samples.length} points across ${run.measured_block_id}.`
          : `Measuring ${run.current_power_percent}% — step ${run.step_index + 1} of ` +
            `${run.step_count}, crossing ` +
            `${Math.min(run.crossings_done + 1, run.crossings_target)} of ${run.crossings_target}`;
    }
    const measured = measuredCurve(run.samples || []);
    const list = cal.root.querySelector('#sp-cal-points');
    if (list) {
      list.innerHTML =
        `<div class="sp-point-head sp-point-head-readonly"><span>P%</span><span>km/h</span></div>` +
        measured
          .slice(1)
          .map(
            ([pw, s]) =>
              `<div class="sp-point-readonly"><span>${fmt(pw, 0)}</span><span>${fmt(s, 1)}</span></div>`
          )
          .join('');
    }
    const crossings = cal.root.querySelector('#sp-cal-crossings');
    if (crossings) {
      const rows = crossingRows(run);
      crossings.innerHTML = rows.length
        ? `<div class="sp-crossing-head"><span>P%</span><span>Toward</span><span>s</span><span>km/h</span></div>` +
          rows
            .map(
              (r) =>
                `<div class="sp-crossing-row${r.used ? '' : ' sp-crossing-unused'}${r.firstOfStep ? ' sp-crossing-step' : ''}">` +
                `<span>${r.firstOfStep ? fmt(r.power, 0) : ''}</span>` +
                `<span>${escapeAttr(r.toward)}</span>` +
                `<span>${r.seconds}</span><span>${r.speed}</span>` +
                (r.note ? `<span class="sp-crossing-note">${escapeAttr(r.note)}</span>` : '') +
                `</div>`
            )
            .join('')
        : '';
    }
    const stored: Curve =
      trainStoredValue(this._namedTrain(), cal.profiles, this._profile, 'power_speed_curve') ||
      [];
    cal.compare?.setData(stored, measured, { yMin: 0, yMax: compareAxisTop(stored, measured) });
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
        cal.stretchText =
          `Run-up / stopping room: ${outer.join(' ↔ ')} · measured length ${s.length_cm} cm`;
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

  // Stop ends the pass through `SpeedCalibration.abandon(...)`: it commands 0%
  // power, tears the leg route down, and writes nothing.
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
  private _applyVpPlot(plotPts: CurvePoint[], dragging: boolean): void {
    // plot space [power, voltage] → storage [voltage, power], voltage-ascending.
    const storage = plotPts
      .map((pt) => [pt[1], pt[0]] as CurvePoint)
      .sort((a, b) => a[0] - b[0]);
    this._profile!.voltage_power_curve = storage;
    // The 0%-power point is the read-only lighting voltage: the plot locks it
    // and the server keeps the stored value, so the plot never writes it
    // (design_new.md "Lighting-voltage calibration", The panel).
    this._profile!.max_voltage = storage[storage.length - 1][0];
    this._syncValues();
    if (!dragging) this._commitDebounced(0);
  }

  private _applyPsPlot(plotPts: CurvePoint[], dragging: boolean): void {
    const storage = plotPts.map((pt) => [pt[0], pt[1]] as CurvePoint).sort((a, b) => a[0] - b[0]);
    this._profile!.power_speed_curve = storage;
    this._syncValues();
    if (!dragging) this._commitDebounced(0);
  }

  // --- numeric point list ---
  /** `preview` draws points that are not the stored curve (the lighting
   *  run's candidate anchor); the caller locks the list while it does. */
  private _rebuildPointList(which: 'vp' | 'ps', preview?: CurvePoint[]): void {
    const container = document.getElementById(which === 'vp' ? 'sp-vp-list' : 'sp-ps-list');
    if (!container || !this._profile) return;
    const isVp = which === 'vp';
    const plotPts = preview ?? (isVp ? this._vpPlotPoints() : this._psPlotPoints());
    const yLabel = isVp ? 'V' : 'km/h';
    let html = `<div class="sp-point-head"><span>P%</span><span>${yLabel}</span><span></span></div>`;
    plotPts.forEach((pt, i) => {
      const isEnd = i === 0 || i === plotPts.length - 1;
      const lockX = isEnd; // endpoints' power is fixed at 0 / 100
      // Plot 1: the 0%-power voltage is the read-only lighting voltage.
      // Plot 2: the origin is pinned at (0, 0).
      const lockY = i === 0;
      html += `
                <div class="sp-point-row" data-i="${i}">
                    <input type="number" class="property-input sp-px" data-which="${which}" data-i="${i}" value="${fmt(pt[0])}" step="1" ${lockX ? 'disabled' : ''}>
                    <input type="number" class="property-input sp-py" data-which="${which}" data-i="${i}" value="${fmt(pt[1])}" step="${isVp ? '0.1' : '1'}" ${lockY ? 'disabled' : ''}>
                    <button class="rr-flat-btn sp-pt-del" data-which="${which}" data-i="${i}" ${isEnd ? 'disabled title="Endpoints cannot be removed"' : ''}>✕</button>
                </div>`;
    });
    html += `<button class="rr-flat-btn sp-pt-add" data-which="${which}">+ add point</button>`;
    container.innerHTML = html;
    // An instance profile's voltage/power curve is its model's: read-only here.
    if (isVp && this._isInstance()) {
      container.querySelectorAll<HTMLInputElement | HTMLButtonElement>('input, button').forEach((el) => {
        el.disabled = true;
      });
      return;
    }

    container.querySelectorAll<HTMLInputElement>('.sp-px, .sp-py').forEach((el) => {
      el.oninput = () => this._onPointInput(which);
    });
    container.querySelectorAll<HTMLButtonElement>('.sp-pt-del').forEach((btn) => {
      if (btn.disabled) return;
      btn.onclick = () => this._removePoint(which, Number(btn.dataset.i));
    });
    const addBtn = container.querySelector<HTMLButtonElement>('.sp-pt-add');
    if (addBtn) addBtn.onclick = () => this._addPoint(which);
  }

  private _onPointInput(which: 'vp' | 'ps'): void {
    const container = document.getElementById(which === 'vp' ? 'sp-vp-list' : 'sp-ps-list');
    if (!container || !this._profile) return;
    const isVp = which === 'vp';
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
    if (isVp) this._applyVpPlot(plotPts, false);
    else this._applyPsPlot(plotPts, false);
    // redraw the plot to match the typed values
    (isVp ? this._vpPlot : this._psPlot)?.setData(
      isVp ? this._vpPlotPoints() : this._psPlotPoints(),
      isVp ? this._vpAxis() : this._psAxis(),
    );
  }

  private _addPoint(which: 'vp' | 'ps'): void {
    if (!this._profile) return;
    const isVp = which === 'vp';
    const pts = isVp ? this._vpPlotPoints() : this._psPlotPoints();
    // Insert at the midpoint of the widest power gap, y on the line between.
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
    const nx = (a[0] + b[0]) / 2;
    const ny = (a[1] + b[1]) / 2;
    pts.splice(gapAt + 1, 0, [nx, ny]);
    if (isVp) this._applyVpPlot(pts, false);
    else this._applyPsPlot(pts, false);
    this._rebuildPointList(which);
    (isVp ? this._vpPlot : this._psPlot)?.setData(pts, isVp ? this._vpAxis() : this._psAxis());
  }

  /** Insert a point from a click on the plot line: segment `segIndex`, at plot-space (x, y). */
  private _insertPlotPoint(which: 'vp' | 'ps', segIndex: number, x: number, y: number): void {
    if (!this._profile) return;
    const isVp = which === 'vp';
    const pts = isVp ? this._vpPlotPoints() : this._psPlotPoints();
    const a = pts[segIndex];
    const b = pts[segIndex + 1];
    if (!a || !b) return;
    // Keep the new point strictly inside the segment so X and Y stay ascending.
    const loX = a[0] + EPS_X;
    const hiX = b[0] - EPS_X;
    const loY = a[1] + EPS_Y;
    const hiY = b[1] - EPS_Y;
    if (hiX <= loX || hiY <= loY) {
      showToast('No room to add another point here', 'warning', 2500);
      return;
    }
    const nx = Math.min(hiX, Math.max(loX, x));
    const ny = Math.min(hiY, Math.max(loY, y));
    pts.splice(segIndex + 1, 0, [nx, ny]);
    if (isVp) this._applyVpPlot(pts, false);
    else this._applyPsPlot(pts, false);
    this._rebuildPointList(which);
    (isVp ? this._vpPlot : this._psPlot)?.setData(pts, isVp ? this._vpAxis() : this._psAxis());
  }

  private _removePoint(which: 'vp' | 'ps', index: number): void {
    if (!this._profile) return;
    const isVp = which === 'vp';
    const pts = isVp ? this._vpPlotPoints() : this._psPlotPoints();
    if (index <= 0 || index >= pts.length - 1) return; // never the endpoints
    pts.splice(index, 1);
    if (isVp) this._applyVpPlot(pts, false);
    else this._applyPsPlot(pts, false);
    this._rebuildPointList(which);
    (isVp ? this._vpPlot : this._psPlot)?.setData(pts, isVp ? this._vpAxis() : this._psAxis());
  }

  // --- scalar fields ---
  private _bindScalars(): void {
    const maxv = document.getElementById('sp-maxv') as HTMLInputElement | null;
    const top = document.getElementById('sp-topspeed') as HTMLInputElement | null;

    if (maxv)
      maxv.oninput = () => this._onScalar(maxv, (v) => {
        const vp = this._profile!.voltage_power_curve;
        if (v <= vp[0][0]) return; // keep above lighting_voltage
        vp[vp.length - 1][0] = v;
        this._profile!.max_voltage = v;
        this._vpPlot?.setData(this._vpPlotPoints(), this._vpAxis());
      });
    if (top)
      top.oninput = () => this._onScalar(top, (v) => {
        const ps = this._profile!.power_speed_curve;
        if (ps.length >= 2 && v <= ps[ps.length - 2][1]) return; // above previous point
        ps[ps.length - 1][1] = v;
        this._psPlot?.setData(this._psPlotPoints(), this._psAxis());
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
    this._recomputeConverter();
    this._commitDebounced(250);
  }

  // --- converter ---
  private _bindConverter(): void {
    const bind = (id: string, compute: (val: number) => void) => {
      const el = document.getElementById(id) as HTMLInputElement | null;
      if (el)
        el.oninput = () => {
          const v = parseFloat(el.value);
          if (Number.isFinite(v)) compute(v);
        };
    };
    const vp = () => this._profile!.voltage_power_curve;
    const ps = () => this._profile!.power_speed_curve;
    const set = (id: string, val: number, digits = 2) => {
      const el = document.getElementById(id) as HTMLInputElement | null;
      if (el && document.activeElement !== el) el.value = fmt(val, digits);
    };
    bind('cv-vp-v', (v) => set('cv-vp-p', voltageToPower(vp(), v), 1));
    bind('cv-vp-p', (p) => set('cv-vp-v', powerToVoltage(vp(), p), 2));
    bind('cv-ps-p', (p) => set('cv-ps-s', powerPercentToSpeed(ps(), p), 1));
    bind('cv-ps-s', (s) => set('cv-ps-p', speedToPower(ps(), s), 1));
    bind('cv-vs-v', (v) => set('cv-vs-s', voltageToSpeedTwoStage(vp(), ps(), v), 1));
    bind('cv-vs-s', (s) => set('cv-vs-v', speedToVoltageTwoStage(vp(), ps(), s), 2));
  }

  /** Seed the converter with a representative operating point (50% power). */
  private _recomputeConverter(): void {
    if (!this._profile) return;
    const vp = this._profile.voltage_power_curve;
    const ps = this._profile.power_speed_curve;
    const setIfBlank = (id: string, val: number, digits = 2) => {
      const el = document.getElementById(id) as HTMLInputElement | null;
      if (el && document.activeElement !== el) el.value = fmt(val, digits);
    };
    setIfBlank('cv-vp-v', powerToVoltage(vp, 50), 2);
    setIfBlank('cv-vp-p', 50, 0);
    setIfBlank('cv-ps-p', 50, 0);
    setIfBlank('cv-ps-s', powerPercentToSpeed(ps, 50), 1);
    setIfBlank('cv-vs-v', powerToVoltage(vp, 50), 2);
    setIfBlank('cv-vs-s', voltageToSpeedTwoStage(vp, ps, powerToVoltage(vp, 50)), 1);
  }

  /** Refresh point-list inputs + scalar inputs + converter from the working
   *  profile without rebuilding DOM. Skips whatever input has focus. */
  private _syncValues(): void {
    if (!this._profile) return;
    const setVal = (el: Element | null, val: string) => {
      const input = el as HTMLInputElement | null;
      if (input && document.activeElement !== input) input.value = val;
    };
    const refill = (which: 'vp' | 'ps', plotPts: CurvePoint[], yDigits: number) => {
      const c = document.getElementById(which === 'vp' ? 'sp-vp-list' : 'sp-ps-list');
      if (!c) return;
      plotPts.forEach((pt, i) => {
        setVal(c.querySelector(`.sp-px[data-i="${i}"]`), fmt(pt[0]));
        setVal(c.querySelector(`.sp-py[data-i="${i}"]`), fmt(pt[1], yDigits));
      });
    };
    refill('vp', this._vpPlotPoints(), 2);
    refill('ps', this._psPlotPoints(), 1);
    setVal(document.getElementById('sp-maxv'), fmt(this._profile.max_voltage));
    setVal(document.getElementById('sp-topspeed'), fmt(topSpeed(this._profile.power_speed_curve)));
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
      const hadOwnCurve = this._ownFields.includes('power_speed_curve');
      this._applyView(view);
      // The first hand edit of an instance's curve changes what the note says.
      if (this._isInstance() && hadOwnCurve !== this._ownFields.includes('power_speed_curve')) {
        const note = document.querySelector('.sp-instance-note');
        if (note) note.outerHTML = this._instanceNoteHtml();
        const openModel = document.getElementById('sp-open-model') as HTMLButtonElement | null;
        if (openModel) openModel.onclick = () => void this.showProfileEditor(openModel.dataset.profileId || '');
      }
      this._vpPlot?.setData(this._vpPlotPoints(), this._vpAxis());
      this._psPlot?.setData(this._psPlotPoints(), this._psAxis());
      this._syncValues();
      if (this._cal?.lighting) this._paintLightingRun();
    } catch (err: any) {
      console.error('[SpeedProfileEditorHandler] save failed:', err);
      showToast('Failed to save profile: ' + (err.message || err), 'error', 5000);
      // Revert optimistic edits to the last server-confirmed state.
      if (this._lastSaved) this._profile = this._clone(this._lastSaved);
      this._rebuildPointList('vp');
      this._rebuildPointList('ps');
      this._vpPlot?.setData(this._vpPlotPoints(), this._vpAxis());
      this._psPlot?.setData(this._psPlotPoints(), this._psAxis());
      this._syncValues();
      if (this._cal?.lighting) this._paintLightingRun();
    }
  }

  private _teardownPlots(): void {
    if (this._saveTimer) {
      clearTimeout(this._saveTimer);
      this._saveTimer = null;
    }
    this._vpPlot?.destroy();
    this._psPlot?.destroy();
    this._vpPlot = null;
    this._psPlot = null;
  }

  /** The secondary sidebar calls this when it closes or swaps the panel out.
   *  A hosted panel is not in the sidebar, so only its host closes it. */
  _cleanup(): void {
    if (this._host) return;
    this.close();
  }

  /** Closing the panel ends the run the calibration group shows, and
   *  forgets the named train. */
  close(): void {
    this._teardownCalibration();
    this._calTrainId = null;
    this._lightingSavedFor = null;
    this._speedCommittedFor = null;
    this._teardownPlots();
    this._profile = null;
    this._lastSaved = null;
    if (this._host) this._host.el.innerHTML = '';
  }
}
