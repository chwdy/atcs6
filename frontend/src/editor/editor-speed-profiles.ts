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
import type { EditorHost } from './types';
import {
  type Curve,
  type CurvePoint,
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
  // ISO stamp of the last speed-calibration commit; null while the curve is
  // still the seeded linear default.
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
    const rect = this.canvas.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) {
      // Not laid out yet — retry next frame.
      requestAnimationFrame(() => this.resize());
      return;
    }
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(rect.width * dpr);
    this.canvas.height = Math.round(rect.height * dpr);
    this.ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.ctx.scale(dpr, dpr);
    this.draw();
  }

  private draw(): void {
    const ctx = this.ctx;
    const rect = this.canvas.getBoundingClientRect();
    if (rect.width < 2) return;
    ctx.clearRect(0, 0, rect.width, rect.height);

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
      const px = this.xToPx(gx, rect);
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
    const { yMin, yMax } = this.axis;
    ctx.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
      const yv = yMin + ((yMax - yMin) * i) / 4;
      const py = this.yToPx(yv, rect);
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
    if (e.button !== 0) return; // left button only; right-click is handled by contextmenu
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
    const { px, py, rect } = this.localPoint(e);
    const idx = this.handleAt(px, py, rect);
    if (idx === null || idx === 0 || idx === this.points.length - 1) return;
    this.hoverIndex = null;
    this.dragIndex = null;
    this.onRemove(idx);
  }
}

export class SpeedProfileEditorHandler {
  editor: EditorHost;
  private _profile: SpeedProfileData | null = null;
  private _lastSaved: SpeedProfileData | null = null;
  private _vpPlot: CurvePlot | null = null;
  private _psPlot: CurvePlot | null = null;
  private _saveTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(editor: EditorHost) {
    this.editor = editor;
  }

  get api(): any {
    return this.editor.api;
  }

  // === Manager list ===

  async showProfilesPanel(): Promise<void> {
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
      await this.showProfilesPanel();
    } catch (err: any) {
      showToast(err.message || 'Failed to delete profile', 'error', 5000);
    }
  }

  // === Per-profile editor ===

  async showProfileEditor(profileId: string): Promise<void> {
    this._teardownPlots();
    this.editor._currentEditorType = 'speed-profile';
    this.editor._currentEditorId = profileId;

    let data: any;
    try {
      data = await this.api.getSpeedProfile(profileId);
      if (!data || data.error) {
        showToast('Speed profile not found', 'error', 5000);
        return;
      }
    } catch (err: any) {
      showToast('Failed to load speed profile: ' + err.message, 'error', 5000);
      return;
    }

    this._profile = this._normalize(data);
    this._lastSaved = this._clone(this._profile);

    this.editor.showSecondarySidebar(profileId, this._editorHtml(this._profile));
    // Wider canvas room — same affordance the route editor uses.
    document.getElementById('secondary-sidebar')?.classList.add('route-editor-wide');
    setTimeout(() => this._initEditor(), 0);
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

  private _editorHtml(p: SpeedProfileData): string {
    return `
            <div class="sp-editor">
                <div class="control-section">
                    <div class="control-section-header">Voltage ↔ Power</div>
                    <div class="text-muted text-xs mb-8">X = power %, Y = track voltage. Click the line to add a point · drag to move · right-click a point to remove.</div>
                    <div class="sp-plot-block">
                        <div class="sp-point-list" id="sp-vp-list"></div>
                        <canvas class="sp-canvas" id="sp-vp-canvas"></canvas>
                    </div>
                </div>

                <div class="control-section">
                    <div class="control-section-header">Power ↔ Speed</div>
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

                <div class="control-section">
                    <div class="control-section-header">Calibration</div>
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="sp-model">Model</label>
                        <input type="text" id="sp-model" class="input-small-field input-small-field-compact" value="${escapeAttr(p.model || '')}" placeholder="e.g. Kato EF66">
                    </div>
                    <div class="text-muted text-xs mb-8">${
                      p.calibrated_at
                        ? `Measured ${escapeAttr(String(p.calibrated_at).replace('T', ' ').replace('+00:00', ' UTC'))}`
                        : 'Not measured — the curve is still the seeded linear default.'
                    }</div>
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="sp-lighting">Lighting voltage (0% power)</label>
                        <input type="number" id="sp-lighting" class="input-small-field input-small-field-compact" value="${fmt(p.lighting_voltage)}" min="0" step="0.1">
                        <button id="sp-lighting-calibrate" class="btn btn-secondary btn-sm" title="Find this locomotive's lighting voltage on the track">Calibrate</button>
                    </div>
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline">Power → speed curve</label>
                        <button id="sp-speed-calibrate" class="btn btn-secondary btn-sm" title="Measure this locomotive by running it back and forth across a block">Calibrate speed</button>
                    </div>
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="sp-maxv">Max voltage (100% power)</label>
                        <input type="number" id="sp-maxv" class="input-small-field input-small-field-compact" value="${fmt(p.max_voltage)}" min="0" step="0.1">
                    </div>
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="sp-topspeed">Top speed (100% power)</label>
                        <input type="number" id="sp-topspeed" class="input-small-field input-small-field-compact" value="${fmt(topSpeed(p.power_speed_curve))}" min="0" step="1">
                    </div>
                </div>

                <details class="tc-disclosure">
                    <summary>Motion &amp; drag</summary>
                    <div class="tc-disclosure-body">
                    ${this._rateRow('Acceleration (km/h/s)', 'sp-accel', p.acceleration_rate)}
                    ${this._rateRow('Deceleration (km/h/s)', 'sp-decel', p.deceleration_rate)}
                    ${this._rateRow('Emergency brake (km/h/s)', 'sp-eb', p.emergency_brake_rate)}
                    ${this._rateRow('Friction (km/h/s)', 'sp-friction', p.friction_rate)}
                    ${this._rateRow('Operational decel ratio', 'sp-opdecel', p.operational_decel_ratio)}
                    ${this._rateRow('Drag coefficient', 'sp-drag', p.drag_coefficient)}
                    ${this._rateRow('Reference speed (km/h)', 'sp-refspeed', p.reference_speed_kmh)}
                    </div>
                </details>

                <details class="tc-disclosure">
                    <summary>Notch response</summary>
                    <div class="tc-disclosure-body">
                    <div class="input-small-row mb-8"><label class="input-small-label">Power notches (× acceleration)</label></div>
                    ${POWER_NOTCHES.map(([h]) => this._notchRow(h, `sp-pm-${h}`, p.power_multipliers[h])).join('')}
                    <div class="input-small-row mb-8"><label class="input-small-label">Brake notches (× operational decel)</label></div>
                    ${BRAKE_NOTCHES.map(([h]) => this._notchRow(h, `sp-bm-${h}`, p.brake_multipliers[h])).join('')}
                    </div>
                </details>

                ${
                  p.profile_id === 'default'
                    ? ''
                    : `<div class="flex mt-16"><button id="sp-delete-btn" class="btn btn-danger btn-sm">Delete profile</button></div>`
                }
            </div>`;
  }

  private _rateRow(label: string, id: string, value: number): string {
    return `
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="${id}">${label}</label>
                        <input type="number" id="${id}" class="input-small-field input-small-field-compact" value="${fmt(value, 4)}" step="0.1">
                    </div>`;
  }

  private _notchRow(label: string, id: string, value: number): string {
    return `
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="${id}">${label}</label>
                        <input type="number" id="${id}" class="input-small-field input-small-field-compact" value="${fmt(value ?? 0, 2)}" min="0" step="0.05">
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
        lockLeftY: false,
        onChange: (pts, dragging) => this._applyVpPlot(pts, dragging),
        onInsert: (seg, x, y) => this._insertPlotPoint('vp', seg, x, y),
        onRemove: (i) => this._removePoint('vp', i),
      });
      this._vpPlot.setData(this._vpPlotPoints(), this._vpAxis());
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

    const calibrate = document.getElementById('sp-lighting-calibrate');
    if (calibrate) calibrate.onclick = () => this._openLightingCalibration();

    const speedCalibrate = document.getElementById('sp-speed-calibrate');
    if (speedCalibrate) speedCalibrate.onclick = () => this._openSpeedCalibration();

    const model = document.getElementById('sp-model') as HTMLInputElement | null;
    if (model)
      model.onchange = () => {
        this._profile!.model = model.value.trim();
        this._commit();
      };
  }

  // === Speed calibration ===
  //
  // design_new.md "Speed calibration". The pass times the locomotive across one
  // operator-named measured block and turns it around at each end itself, so a
  // plain length of track measures a locomotive with no loop to lap. It writes
  // nothing until the operator picks a commit target: Save to this locomotive
  // writes the measurement onto this loco's instance profile alone, Promote to
  // model writes it onto the model profile every locomotive of that model
  // shares (and clears this loco's override).

  private _calPoll: number | null = null;

  private async _openSpeedCalibration(): Promise<void> {
    const train = await this._calibrationTrain();
    if (!train) return;
    this._showSpeedCalibrationPanel(train);
  }

  /** A placed train running this profile — the pass has to drive one. */
  private async _calibrationTrain(): Promise<any | null> {
    let trains: any[] = [];
    try {
      trains = await this.api.getTrains();
    } catch (err: any) {
      showToast('Failed to load trains: ' + (err.message || err), 'error', 5000);
      return null;
    }
    const profileId = this._profile!.profile_id;
    const matches = (trains || []).filter((t: any) => t?.speed_profile_id === profileId);
    if (!matches.length) {
      showToast(
        'Place a train that uses this profile first — calibration drives the locomotive',
        'warning',
        5000
      );
      return null;
    }
    return matches[0];
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

  private _showSpeedCalibrationPanel(train: any): void {
    document.querySelector('.app-modal-overlay')?.remove();

    const options = this._blockChoices()
      .map((c) => `<option value="${escapeAttr(c.id)}">${escapeAttr(c.label)}</option>`)
      .join('');

    const overlay = document.createElement('div');
    overlay.className = 'app-modal-overlay';
    overlay.innerHTML = `
      <div class="app-modal sp-calibration-modal">
        <div class="app-modal-header">Calibrate speed — ${escapeAttr(train.car_number || train.id)}</div>
        <div class="app-modal-body">
          <div class="app-modal-message">The train runs back and forth across the
          measured block. The two blocks either side are its run-up and its
          stopping room, so give it a stretch long enough to get up to speed and
          pull up again.</div>
          <div class="input-small-row mb-8">
            <label class="input-small-label input-small-label-inline" for="sp-cal-block">Measured block</label>
            <select id="sp-cal-block" class="input-small-field input-small-field-compact">
              <option value="">Choose a block…</option>
              ${options}
            </select>
            <button id="sp-cal-pick" class="btn btn-secondary btn-sm" title="Click the block on the layout">Pick on layout</button>
          </div>
          <div class="text-muted text-xs mb-8" id="sp-cal-stretch">Choose a block to see the stretch.</div>
          <div id="sp-cal-progress" class="text-muted text-xs mb-8"></div>
        </div>
        <div class="app-modal-footer">
          <button class="btn btn-secondary btn-sm" id="sp-cal-close">Close</button>
          <button class="btn btn-primary btn-sm" id="sp-cal-start" disabled>Start</button>
        </div>
      </div>`;

    const blockEl = overlay.querySelector('#sp-cal-block') as HTMLSelectElement;
    const pickBtn = overlay.querySelector('#sp-cal-pick') as HTMLButtonElement;
    const stretchEl = overlay.querySelector('#sp-cal-stretch') as HTMLElement;
    const progressEl = overlay.querySelector('#sp-cal-progress') as HTMLElement;
    const startBtn = overlay.querySelector('#sp-cal-start') as HTMLButtonElement;
    const closeBtn = overlay.querySelector('#sp-cal-close') as HTMLButtonElement;

    // Resolving the stretch before Start is what lets the operator see which
    // two blocks the points currently lead to, rather than finding out by
    // watching the locomotive run into one. The same three blocks light up on
    // the layout, so "which stretch is this?" is answered by looking at the map
    // rather than by reading three ids.
    const resolveStretch = async () => {
      const blockId = blockEl.value.trim();
      startBtn.disabled = true;
      this.editor.highlighter.clear('reachable');
      if (!blockId) {
        stretchEl.textContent = 'Choose a block to see the stretch.';
        this.editor.renderer?.requestRedraw();
        return;
      }
      const lit = this._blockTiles(blockId);
      try {
        const s = await this.api.getCalibrationStretch(train.id, blockId);
        stretchEl.textContent =
          `Run-up / stopping room: ${s.outer_block_ids.join(' ↔ ')} · ` +
          `measured length ${s.length_cm} cm`;
        startBtn.disabled = false;
        for (const outer of s.outer_block_ids) {
          this._blockTiles(outer).forEach((k: string) => lit.add(k));
        }
      } catch (err: any) {
        // The measured block still lights up on a rejection: seeing where it
        // sits is how the operator works out which end has no room.
        stretchEl.textContent = err.message || 'Cannot use that block';
      }
      this.editor.highlighter.show({ keys: lit }, { channel: 'reachable' });
      this.editor.renderer?.requestRedraw();
    };
    blockEl.onchange = resolveStretch;

    // Picking on the layout is the same choice by a different gesture: the
    // modal steps aside, the next click on a block fills the menu, and the
    // stretch resolves exactly as if the block had been chosen from the list.
    pickBtn.onclick = () => {
      overlay.style.display = 'none';
      this.editor.enterMode(this._buildMeasuredBlockPickMode(overlay, blockEl, resolveStretch));
    };

    startBtn.onclick = async () => {
      startBtn.disabled = true;
      try {
        await this.api.startCalibration(train.id, {
          measured_block_id: blockEl.value.trim(),
        });
      } catch (err: any) {
        showToast(err.message || 'Cannot start calibration', 'error', 6000);
        startBtn.disabled = false;
        return;
      }
      blockEl.disabled = true;
      this._pollCalibration(train, overlay, progressEl);
    };

    const close = () => {
      if (this._calPoll !== null) {
        window.clearInterval(this._calPoll);
        this._calPoll = null;
      }
      this.editor.highlighter.clear('reachable');
      this.editor.renderer?.requestRedraw();
      overlay.remove();
    };
    closeBtn.onclick = close;
    document.body.appendChild(overlay);
  }

  /**
   * Layout-click mode for the measured block. The modal is hidden while it
   * runs and restored by `onExit`, so both endings — a click on a block and
   * an Esc through the mode manager — put the dialog back the same way.
   */
  private _buildMeasuredBlockPickMode(
    overlay: HTMLElement,
    blockEl: HTMLSelectElement,
    onPicked: () => void
  ): any {
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
        overlay.style.display = '';
        if (!picked) return;
        // A block the cached network did not carry when the menu was built
        // still has to be selectable — add the row rather than drop the pick.
        if (!Array.from(blockEl.options).some((o) => o.value === picked)) {
          const opt = document.createElement('option');
          opt.value = picked;
          opt.textContent = picked;
          blockEl.appendChild(opt);
        }
        blockEl.value = picked;
        onPicked();
      },
    };
  }

  /** Poll the open pass and render its progress, then its commit controls. */
  private _pollCalibration(train: any, overlay: HTMLElement, progressEl: HTMLElement): void {
    if (this._calPoll !== null) window.clearInterval(this._calPoll);
    this._calPoll = window.setInterval(async () => {
      let run: any;
      try {
        run = await this.api.getCalibration(train.id);
      } catch {
        return;
      }
      if (!run.open) {
        progressEl.textContent = 'The pass ended.';
        window.clearInterval(this._calPoll!);
        this._calPoll = null;
        return;
      }
      if (run.phase === 'failed') {
        progressEl.textContent = `Failed: ${run.failure_reason}`;
        window.clearInterval(this._calPoll!);
        this._calPoll = null;
        return;
      }
      if (run.phase === 'measured') {
        window.clearInterval(this._calPoll!);
        this._calPoll = null;
        this._showCommitControls(train, overlay, run);
        return;
      }
      progressEl.textContent =
        `Measuring ${run.current_power_percent}% — step ${run.step_index + 1} of ` +
        `${run.step_count}, crossing ${run.crossings_done + 1} of ${run.crossings_target}`;
    }, 1000);
  }

  /** The measured curve plus the two commit targets. Nothing is written until
   *  the operator picks one; closing discards the pass. */
  private _showCommitControls(train: any, overlay: HTMLElement, run: any): void {
    const body = overlay.querySelector('.app-modal-body') as HTMLElement;
    const footer = overlay.querySelector('.app-modal-footer') as HTMLElement;
    const points = (run.samples || [])
      .map((s: any) => `${s.power_percent}% → ${s.speed_kmh.toFixed(1)} km/h`)
      .join('<br>');
    const modelName = (this._profile!.model || '').trim();
    const modelLabel = modelName ? escapeAttr(modelName) : 'this model';
    body.innerHTML = `
      <div class="app-modal-message">Measured ${run.samples.length} points.</div>
      <div class="text-muted text-xs mb-8">${points}</div>
      <div class="text-muted text-xs mb-8">Save to this locomotive changes only this loco. Promote to model updates every locomotive of ${modelLabel} and clears this loco's own override.</div>`;
    footer.innerHTML = `
      <button class="btn btn-secondary btn-sm" id="sp-cal-discard">Discard</button>
      <button class="btn btn-secondary btn-sm" id="sp-cal-model">Promote to model</button>
      <button class="btn btn-primary btn-sm" id="sp-cal-locomotive">Save to this locomotive</button>`;

    const commit = async (target: 'locomotive' | 'model') => {
      let result: any;
      try {
        result = await this.api.commitCalibration(train.id, target);
      } catch (err: any) {
        showToast(err.message || 'Commit failed', 'error', 6000);
        return;
      }
      // Clear the measured/outer-block highlight the reachable channel is
      // holding (only the X-button close() did this), so committing doesn't
      // leave stale blocks lit on the canvas after the modal closes.
      this.editor.highlighter.clear('reachable');
      this.editor.renderer?.requestRedraw();
      overlay.remove();
      showToast(
        target === 'model'
          ? 'Promoted to the model profile'
          : 'Saved to this locomotive',
        'success',
        4000
      );
      // Promote to model wrote a different profile (the model's) and cleared
      // this loco's override, so open the profile that was actually written
      // rather than the one on screen.
      const written = result?.profile?.profile_id || this._profile!.profile_id;
      this.showProfileEditor(written);
    };

    (footer.querySelector('#sp-cal-locomotive') as HTMLButtonElement).onclick = () =>
      commit('locomotive');
    (footer.querySelector('#sp-cal-model') as HTMLButtonElement).onclick = () =>
      commit('model');
    (footer.querySelector('#sp-cal-discard') as HTMLButtonElement).onclick = async () => {
      await this.api.cancelCalibration(train.id).catch(() => {});
      // Clear the reachable-channel highlight (as close() does) so discarding
      // doesn't leave stale blocks lit on the canvas.
      this.editor.highlighter.clear('reachable');
      this.editor.renderer?.requestRedraw();
      overlay.remove();
    };
  }

  // === Lighting-voltage calibration ===
  //
  // design_new.md "Lighting-voltage calibration". The panel is deliberately
  // three controls: the candidate voltage, a step up and a step down, and
  // Save. Everything else about the pass is the locomotive in front of the
  // operator — nothing on the rig can tell whether it moved, so the operator's
  // eye is the measurement. The value to save is the highest step at which it
  // still held still, so the operator steps up until it creeps, steps back
  // down one, and saves.

  /** Find a placed train running this profile, or explain why there is none. */
  private async _lightingCalibrationTrain(): Promise<any | null> {
    let trains: any[] = [];
    try {
      trains = await this.api.getTrains();
    } catch (err: any) {
      showToast('Failed to load trains: ' + (err.message || err), 'error', 5000);
      return null;
    }
    const profileId = this._profile!.profile_id;
    // `/api/trains` returns flat `Train.to_dict()` payloads in `trains`.
    const matches = (trains || []).filter(
      (t: any) => t?.speed_profile_id === profileId
    );
    if (!matches.length) {
      showToast(
        'Place a train that uses this profile first — calibration drives the locomotive',
        'warning',
        5000
      );
      return null;
    }
    return matches[0];
  }

  private async _openLightingCalibration(): Promise<void> {
    const train = await this._lightingCalibrationTrain();
    if (!train) return;

    let run: any;
    try {
      run = await this.api.startLightingCalibration(train.id);
    } catch (err: any) {
      showToast(err.message || 'Cannot start lighting calibration', 'error', 6000);
      return;
    }
    this._showLightingPanel(train, run);
  }

  private _showLightingPanel(train: any, run: any): void {
    document.querySelector('.app-modal-overlay')?.remove();

    const overlay = document.createElement('div');
    overlay.className = 'app-modal-overlay';
    overlay.innerHTML = `
      <div class="app-modal sp-lighting-modal">
        <div class="app-modal-header">Lighting voltage — ${train.car_number || train.id}</div>
        <div class="app-modal-body">
          <div class="app-modal-message">Step up until the locomotive just starts to
          creep, then step back down one and save.</div>
          <div class="sp-lighting-stepper">
            <button class="btn btn-secondary sp-lighting-step" data-dir="-1" aria-label="Lower">−</button>
            <div class="sp-lighting-readout"><span id="sp-lighting-value">—</span><small>V</small></div>
            <button class="btn btn-secondary sp-lighting-step" data-dir="1" aria-label="Raise">+</button>
          </div>
        </div>
        <div class="app-modal-footer">
          <button class="btn btn-secondary btn-sm" id="sp-lighting-cancel">Cancel</button>
          <button class="btn btn-primary btn-sm" id="sp-lighting-save">Save</button>
        </div>
      </div>`;

    const valueEl = overlay.querySelector('#sp-lighting-value') as HTMLElement;
    const stepBtns = Array.from(
      overlay.querySelectorAll('.sp-lighting-step')
    ) as HTMLButtonElement[];
    let candidate = Number(run.candidate_voltage) || 0;
    let ceiling = Number(run.ceiling_voltage) || 0;

    const paint = () => {
      valueEl.textContent = candidate.toFixed(2);
      // The step control stops at the ceiling and at 0, so the panel can never
      // show a value Save would refuse.
      stepBtns[0].disabled = candidate <= 0;
      stepBtns[1].disabled = candidate >= ceiling;
    };
    paint();

    let busy = false;
    const step = async (dir: number) => {
      if (busy) return;
      busy = true;
      try {
        const next = await this.api.stepLightingCalibration(train.id, dir);
        candidate = Number(next.candidate_voltage);
        ceiling = Number(next.ceiling_voltage);
        paint();
      } catch (err: any) {
        showToast(err.message || 'Step failed', 'error', 5000);
      } finally {
        busy = false;
      }
    };

    const close = () => {
      document.removeEventListener('keydown', onKey);
      overlay.remove();
    };

    const cancel = async () => {
      close();
      try {
        await this.api.cancelLightingCalibration(train.id);
      } catch {
        /* the run is transient; a failed cancel leaves nothing to clean up */
      }
    };

    const save = async () => {
      try {
        const result = await this.api.saveLightingCalibration(train.id);
        close();
        showToast(
          `Lighting voltage saved at ${Number(result.lighting_voltage).toFixed(2)} V`,
          'success',
          3000
        );
        // The commit moved the profile's 0%-power anchor, so reload rather
        // than patching the field: the voltage/power curve changed with it.
        await this.showProfileEditor(this._profile!.profile_id);
      } catch (err: any) {
        showToast(err.message || 'Failed to save lighting voltage', 'error', 5000);
      }
    };

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        void cancel();
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        void step(1);
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        void step(-1);
      }
    };

    stepBtns.forEach((btn) => {
      btn.onclick = () => void step(Number(btn.dataset.dir));
    });
    (overlay.querySelector('#sp-lighting-cancel') as HTMLElement).onclick = () =>
      void cancel();
    (overlay.querySelector('#sp-lighting-save') as HTMLElement).onclick = () => void save();
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) void cancel();
    });
    document.addEventListener('keydown', onKey);
    document.body.appendChild(overlay);
  }

  // --- plot edits → working profile ---
  private _applyVpPlot(plotPts: CurvePoint[], dragging: boolean): void {
    // plot space [power, voltage] → storage [voltage, power], voltage-ascending.
    const storage = plotPts
      .map((pt) => [pt[1], pt[0]] as CurvePoint)
      .sort((a, b) => a[0] - b[0]);
    this._profile!.voltage_power_curve = storage;
    this._profile!.lighting_voltage = storage[0][0];
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
  private _rebuildPointList(which: 'vp' | 'ps'): void {
    const container = document.getElementById(which === 'vp' ? 'sp-vp-list' : 'sp-ps-list');
    if (!container || !this._profile) return;
    const isVp = which === 'vp';
    const plotPts = isVp ? this._vpPlotPoints() : this._psPlotPoints();
    const yLabel = isVp ? 'V' : 'km/h';
    let html = `<div class="sp-point-head"><span>P%</span><span>${yLabel}</span><span></span></div>`;
    plotPts.forEach((pt, i) => {
      const isEnd = i === 0 || i === plotPts.length - 1;
      const lockX = isEnd; // endpoints' power is fixed at 0 / 100
      const lockY = !isVp && i === 0; // plot-2 origin Y locked
      html += `
                <div class="sp-point-row" data-i="${i}">
                    <input type="number" class="property-input sp-px" data-which="${which}" data-i="${i}" value="${fmt(pt[0])}" step="1" ${lockX ? 'disabled' : ''}>
                    <input type="number" class="property-input sp-py" data-which="${which}" data-i="${i}" value="${fmt(pt[1])}" step="${isVp ? '0.1' : '1'}" ${lockY ? 'disabled' : ''}>
                    <button class="rr-flat-btn sp-pt-del" data-which="${which}" data-i="${i}" ${isEnd ? 'disabled title="Endpoints cannot be removed"' : ''}>✕</button>
                </div>`;
    });
    html += `<button class="rr-flat-btn sp-pt-add" data-which="${which}">+ add point</button>`;
    container.innerHTML = html;

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
    const lighting = document.getElementById('sp-lighting') as HTMLInputElement | null;
    const maxv = document.getElementById('sp-maxv') as HTMLInputElement | null;
    const top = document.getElementById('sp-topspeed') as HTMLInputElement | null;

    if (lighting)
      lighting.oninput = () => this._onScalar(lighting, (v) => {
        const vp = this._profile!.voltage_power_curve;
        if (v >= vp[vp.length - 1][0]) return; // keep below max_voltage
        vp[0][0] = v;
        this._profile!.lighting_voltage = v;
        this._vpPlot?.setData(this._vpPlotPoints(), this._vpAxis());
      });
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
    setVal(document.getElementById('sp-lighting'), fmt(this._profile.lighting_voltage));
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
    try {
      const updated = await this.api.updateSpeedProfile(this._profile.profile_id, this._profile);
      this._profile = this._normalize(updated);
      this._lastSaved = this._clone(this._profile);
      this._vpPlot?.setData(this._vpPlotPoints(), this._vpAxis());
      this._psPlot?.setData(this._psPlotPoints(), this._psAxis());
      this._syncValues();
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

  _cleanup(): void {
    this._teardownPlots();
    this._profile = null;
    this._lastSaved = null;
  }
}
