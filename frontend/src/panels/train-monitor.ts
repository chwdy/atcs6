// ── Payload shapes the monitor reads (loosely typed; the server owns them) ──

import { api } from '../lib/api';
import { stateManager } from '../lib/state-manager';
import { showToast } from '../lib/toast';
import { getBlockStateColor } from '../lib/helpers';

// Signal-aspect dot colors for the lookahead chart — the single home for the
// fixed hexes the chart paints. double_yellow shares yellow's fill and is
// distinguished by an outer ring (see drawSignals).
const SIGNAL_ASPECT_COLORS: Record<string, string> = {
  green: '#04d361',
  yellow: '#ffcd1e',
  double_yellow: '#ffcd1e',
  red: '#e83f5b',
};

interface CurvePoint {
    distance_cm: number;
    atc_limit: number;
    operational_limit: number;
}

interface BlockDatum {
    start_cm: number;
    end_cm: number;
    state?: string;
    name?: string;
    id?: string | number;
    tile_count?: number;
    length_cm: number;
    sequence?: number;
}

interface StationDatum {
    distance_cm: number;
    length_cm?: number;
    name?: string;
    platform_id?: string;
    station_id?: string;
    tile_count?: number;
    sequence?: number;
}

interface SwitchDatum {
    distance_cm: number;
    position?: string | number;
    locked?: boolean;
    name?: string;
    x?: number;
    y?: number;
    sequence?: number;
}

interface SignalDatum {
    distance_cm: number;
    state?: string;
    name?: string;
    id?: string | number;
    sequence?: number;
}

interface LookaheadData {
    current_speed_kmh: number;
    state?: string | null;
    speed_limit_reason: string | null;
    blocks: BlockDatum[];
    signals: SignalDatum[];
    switches: SwitchDatum[];
    stations: StationDatum[];
    distance_into_path_cm: number;
    clearance_cm: number;
    safety_margin_cm: number;
    target_stop_cm: number;
    speed_curves: CurvePoint[];
    current_block_speed_limit: number | null;
}

/** The lookahead sub-object embedded in the per-tick payload. */
interface LookaheadPayloadInner {
    safety_margin_cm?: number;
    clearance_cm?: number;
    target_stop_cm?: number;
    blocks?: BlockDatum[];
    signals?: SignalDatum[];
    switches?: SwitchDatum[];
    stations?: StationDatum[];
    speed_curves?: CurvePoint[];
}

/** The per-tick payload app.js hands to updateFromController. */
interface MonitorPayload {
    lookahead?: LookaheadPayloadInner | null;
    speed_limit_reason?: string | null;
    current_block_speed_limit?: number | null;
    current_task_kind?: string;
    dwell_remaining_sec?: number | null;
    dwell_finish_sim_sec?: number | null;
}

interface TrainRef {
    id: string;
    name?: string;
}

interface BlockRect {
    x: number;
    y: number;
    w: number;
    h: number;
    data: BlockDatum;
}

/** A normalized row in the "upcoming elements" list (block/station/switch/signal). */
interface UpcomingElement {
    type: 'block' | 'station' | 'switch' | 'signal';
    name?: string | number;
    distance: number;
    length: number;
    state?: string;
    position?: string | number;
    sequence: number;
}

interface MonitorView {
    padding: { left: number; right: number; top: number; bottom: number };
    width: number;
    height: number;
    visibleStartCm: number;
    visibleEndCm: number;
}

class TrainMonitor {
    bar!: HTMLElement;
    canvas!: HTMLCanvasElement;
    ctx!: CanvasRenderingContext2D;
    titleEl!: HTMLElement;
    currentSpeedEl!: HTMLElement;
    atcSpeedEl!: HTMLElement;
    atoSpeedEl!: HTMLElement;
    speedLimitReasonEl!: HTMLElement;
    speedCmsEl!: HTMLElement;
    clearanceEl!: HTMLElement;
    dwellRemainingContainer!: HTMLElement;
    dwellRemainingEl!: HTMLElement;
    dwellFinishContainer!: HTMLElement;
    dwellFinishEl!: HTMLElement;
    upcomingEl!: HTMLElement;
    upcomingContainer!: HTMLElement;
    upcomingCountEl!: HTMLElement;
    monitorContent!: HTMLElement;
    resizerEl!: HTMLElement;
    closeBtn!: HTMLElement;
    viewLookaheadBtn!: HTMLElement | null;

    lookaheadData: LookaheadData | null = null;
    trainId: string | null = null;
    currentScale = 160;
    viewDistance = 500;
    hoveredBlock: BlockDatum | null = null;
    blockRects: BlockRect[] = [];
    isColumnResizing = false;
    mouseX: number | null = null;
    mouseY: number | null = null;
    pinnedCm: number | null = null;

    // View transform exposed by render() for the mouse/click handlers.
    _cmToX?: (cm: number) => number;
    _xToCm?: (x: number) => number;
    _view!: MonitorView;

    constructor() {
        this.bar = document.getElementById('train-monitor-bar')!;
        this.canvas = document.getElementById('train-monitor-canvas') as HTMLCanvasElement;
        this.ctx = this.canvas.getContext('2d')!;
        this.titleEl = document.getElementById('train-monitor-title')!;
        this.currentSpeedEl = document.getElementById('monitor-current-speed')!;
        this.atcSpeedEl = document.getElementById('monitor-atc-speed')!;
        this.atoSpeedEl = document.getElementById('monitor-ato-speed')!;
        this.speedLimitReasonEl = document.getElementById('monitor-speed-limit-reason')!;
        this.speedCmsEl = document.getElementById('monitor-speed-cms')!;
        this.clearanceEl = document.getElementById('monitor-clearance')!;
        this.dwellRemainingContainer = document.getElementById('monitor-dwell-remaining-container')!;
        this.dwellRemainingEl = document.getElementById('monitor-dwell-remaining')!;
        this.dwellFinishContainer = document.getElementById('monitor-dwell-finish-container')!;
        this.dwellFinishEl = document.getElementById('monitor-dwell-finish')!;
        this.upcomingEl = document.getElementById('monitor-upcoming')!;
        this.upcomingContainer = document.getElementById('monitor-upcoming-container')!;
        this.upcomingCountEl = document.getElementById('monitor-upcoming-count')!;
        this.monitorContent = document.querySelector('.tm-content')!;
        this.resizerEl = document.getElementById('train-monitor-resizer')!;
        this.closeBtn = document.getElementById('train-monitor-close')!;
        
        this.lookaheadData = null;
        this.trainId = null;
        this.currentScale = 160;
        
        this.loadScale();
        
        this.viewDistance = 500; // Default to 5m
        this.hoveredBlock = null;
        this.blockRects = [];
        this.isColumnResizing = false;

        // Reference lines: live crosshair follows the cursor; pinnedCm is a
        // fixed line the user drops by clicking (stored as absolute path-cm so
        // it stays anchored to the same distance across view-distance changes).
        this.mouseX = null;
        this.mouseY = null;
        this.pinnedCm = null;
        
        this.initViewDistanceButtons();
        this.initColumnResizer();
        this.initPanelResizer();
        
        this.viewLookaheadBtn = document.getElementById('view-lookahead-btn');
        if (this.viewLookaheadBtn) {
            this.viewLookaheadBtn.addEventListener('click', () => this.dumpLookaheadData());
        }
        
        this.canvas.addEventListener('mousemove', (e) => this.handleMouseMove(e));
        this.canvas.addEventListener('mouseleave', () => {
            this.hoveredBlock = null;
            this.mouseX = null;
            this.mouseY = null;
            this.render();
        });
        this.canvas.addEventListener('click', (e) => this.handleClick(e));
        
        this.closeBtn.addEventListener('click', () => this.hide());
        window.addEventListener('resize', () => this.resizeCanvas());
    }
    
    show(trainId: string, trainName?: string) {
        this.trainId = trainId;
        this.titleEl.textContent = trainName || `Train ${trainId}`;
        this.bar.classList.remove('hidden');
        this.pinnedCm = null;
        this.mouseX = null;
        this.mouseY = null;
        this.resizeCanvas();
    }
    
    hide() {
        this.bar.classList.add('hidden');
        this.trainId = null;
        this.lookaheadData = null;
    }
    
    isVisible() {
        return !this.bar.classList.contains('hidden');
    }

    initViewDistanceButtons() {
        const buttons = document.querySelectorAll('.view-dist-btn');
        buttons.forEach(btn => {
            btn.addEventListener('click', () => {
                buttons.forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.viewDistance = parseInt((btn as HTMLElement).dataset.distance!, 10);
                this.render();
            });
        });
    }

    initColumnResizer() {
        if (!this.resizerEl || !this.upcomingContainer || !this.monitorContent) {
            return;
        }

        const onMouseMove = (e: MouseEvent) => {
            if (!this.isColumnResizing) return;

            const contentRect = this.monitorContent.getBoundingClientRect();
            const minWidth = parseInt(getComputedStyle(this.upcomingContainer).minWidth, 10) || 140;
            const maxWidth = parseInt(getComputedStyle(this.upcomingContainer).maxWidth, 10) || 320;
            let newWidth = contentRect.right - e.clientX;

            newWidth = Math.max(minWidth, Math.min(maxWidth, newWidth));
            this.upcomingContainer.style.width = `${newWidth}px`;
            this.upcomingContainer.style.flex = `0 0 ${newWidth}px`;
            this.resizeCanvas();
        };

        this.resizerEl.addEventListener('mousedown', (e) => {
            this.isColumnResizing = true;
            this.resizerEl.classList.add('resizing');
            document.body.style.cursor = 'ew-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });

        document.addEventListener('mousemove', onMouseMove);
        document.addEventListener('mouseup', () => {
            if (!this.isColumnResizing) return;
            this.isColumnResizing = false;
            this.resizerEl.classList.remove('resizing');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        });
    }

    initPanelResizer() {
        if (!this.bar) return;
        const EDGE = 8;
        let dragging = false;
        let startY = 0;
        let startH = 0;

        this.bar.addEventListener('mousedown', (e) => {
            const rect = this.bar.getBoundingClientRect();
            if (e.clientY > rect.top + EDGE) return;
            dragging = true;
            startY = e.clientY;
            startH = rect.height;
            this.bar.classList.add('resizing');
            document.body.style.cursor = 'ns-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });

        document.addEventListener('mousemove', (e) => {
            if (!dragging) return;
            const minH = parseInt(getComputedStyle(this.bar).minHeight, 10) || 200;
            const maxH = window.innerHeight * 0.8;
            const newH = Math.max(minH, Math.min(maxH, startH + (startY - e.clientY)));
            this.bar.style.height = `${newH}px`;
        });

        document.addEventListener('mouseup', () => {
            if (!dragging) return;
            dragging = false;
            this.bar.classList.remove('resizing');
            document.body.style.cursor = '';
            document.body.style.userSelect = '';
        });
    }

    dumpLookaheadData() {
        if (this.lookaheadData) {
            if (window.app && window.app.editor) {
                window.app.editor.showFullJSONViewer(this.lookaheadData, 'Train Lookahead Data');
            } else {
                console.log('=== LOOKAHEAD DATA ===');
                console.log(JSON.stringify(this.lookaheadData, null, 2));
                if (this.lookaheadData.blocks) console.table(this.lookaheadData.blocks);
                if (this.lookaheadData.signals) console.table(this.lookaheadData.signals);
                if (this.lookaheadData.switches) console.table(this.lookaheadData.switches);
                if (this.lookaheadData.stations) console.table(this.lookaheadData.stations);
            }
        } else {
            console.log('No lookahead data available');
            if (showToast) showToast('No lookahead data available', 'info');
        }
    }

    _formatSimClock(totalSec: number) {
        const t = Math.max(0, Math.floor(totalSec));
        const h = Math.floor(t / 3600);
        const m = Math.floor((t % 3600) / 60);
        const s = t % 60;
        return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }

    handleMouseMove(e: MouseEvent) {
        if (!this.lookaheadData) return;

        // CSS pixels: the canvas context is scaled by devicePixelRatio, so all
        // drawn geometry (including blockRects) lives in CSS-pixel space.
        const rect = this.canvas.getBoundingClientRect();
        const mouseX = e.clientX - rect.left;
        const mouseY = e.clientY - rect.top;

        let found = null;
        for (const block of this.blockRects) {
            if (mouseX >= block.x && mouseX <= block.x + block.w &&
                mouseY >= block.y && mouseY <= block.y + block.h) {
                found = block.data;
                break;
            }
        }

        const moved = mouseX !== this.mouseX || mouseY !== this.mouseY;
        this.mouseX = mouseX;
        this.mouseY = mouseY;
        if (moved || this.hoveredBlock !== found) {
            this.hoveredBlock = found;
            this.render();
        }
    }

    handleClick(e: MouseEvent) {
        if (!this.lookaheadData || !this._xToCm) return;

        const rect = this.canvas.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const { padding, width, visibleStartCm, visibleEndCm } = this._view;
        if (x < padding.left || x > width - padding.right) return;

        // Clicking on (or within a few px of) the existing pin clears it.
        if (this.pinnedCm != null) {
            const pinX = this._cmToX!(this.pinnedCm);
            if (Math.abs(pinX - x) <= 6) {
                this.pinnedCm = null;
                this.render();
                return;
            }
        }

        const cm = this._xToCm(x);
        this.pinnedCm = Math.max(visibleStartCm, Math.min(visibleEndCm, cm));
        this.render();
    }
    
    async loadScale() {
        try {
            const data = await api.getScale();
            this.currentScale = data.scale || 160;
        } catch (e) {
            console.error('[TrainMonitor] Failed to load scale:', e);
        }
    }
    
    resizeCanvas() {
        const rect = this.canvas.getBoundingClientRect();
        this.canvas.width = rect.width * window.devicePixelRatio;
        this.canvas.height = rect.height * window.devicePixelRatio;
        this.ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
        if (this.lookaheadData) {
            this.render();
        }
    }
    
    updateFromController(train: TrainRef | null, payload: MonitorPayload | null) {
        if (!train) {
            console.warn('[TrainMonitor] No train data');
            this.lookaheadData = null;
            this.render();
            return;
        }

        // Live runtime fields (state, current_speed, target_speed,
        // speed_limit_reason) live on the TrainController per design_new.md
        // "Data Flow To UI". Resolve via the state-manager sibling cache.
        const controller = stateManager?.getController(train.id);

        const lookahead = payload && payload.lookahead ? payload.lookahead : null;
        const speedLimitReason =
            payload?.speed_limit_reason ?? controller?.speed_limit_reason ?? null;

        const safetyMarginCm = lookahead?.safety_margin_cm ?? 25;
        const clearanceCm = lookahead?.clearance_cm || 0;
        const rawTargetStopCm = lookahead?.target_stop_cm ?? (clearanceCm - safetyMarginCm);
        const targetStopCm = Math.max(0, rawTargetStopCm);

        this.lookaheadData = {
            current_speed_kmh: controller?.current_speed ?? 0,
            state: controller?.state,
            speed_limit_reason: speedLimitReason,
            blocks: lookahead?.blocks || [],
            signals: lookahead?.signals || [],
            switches: lookahead?.switches || [],
            stations: lookahead?.stations || [],
            distance_into_path_cm: 0,
            clearance_cm: clearanceCm,
            safety_margin_cm: safetyMarginCm,
            target_stop_cm: targetStopCm,
            speed_curves: lookahead?.speed_curves || [],
            current_block_speed_limit: payload?.current_block_speed_limit ?? null,
        };

        this.currentSpeedEl.textContent = String(Math.round(this.lookaheadData.current_speed_kmh || 0));

        // Per design_new.md: target speed and ATC/ATO limits live at position 0
        // of `lookahead.speed_curves`. There is no standalone scalar.
        const curves = this.lookaheadData.speed_curves || [];
        const atcSpeed = curves.length > 0 ? (curves[0].atc_limit ?? 0) : 0;
        const atoSpeed = curves.length > 0 ? (curves[0].operational_limit ?? 0) : 0;
        if (this.atcSpeedEl) this.atcSpeedEl.textContent = String(Math.round(atcSpeed));
        if (this.atoSpeedEl) this.atoSpeedEl.textContent = String(Math.round(atoSpeed));

        if (this.speedCmsEl) {
            const speedCms = ((this.lookaheadData.current_speed_kmh || 0) * 27.78 / this.currentScale).toFixed(1);
            this.speedCmsEl.textContent = speedCms;
        }
        if (this.clearanceEl) {
            const { val, unit } = this._fmtDist(this.lookaheadData.clearance_cm || 0);
            this.clearanceEl.textContent = val;
            const unitEl = this.clearanceEl.nextElementSibling;
            if (unitEl) unitEl.textContent = unit;
        }

        // Update distance to next destination (target stop point)
        const nextDestContainer = document.getElementById('monitor-next-dest-container');
        const nextDestEl = document.getElementById('monitor-next-dest-dist');
        if (nextDestContainer && nextDestEl && this.lookaheadData.target_stop_cm > 0) {
            const { val, unit } = this._fmtDist(this.lookaheadData.target_stop_cm);
            nextDestEl.textContent = val;
            const unitEl = nextDestEl.nextElementSibling;
            if (unitEl) unitEl.textContent = unit;
            nextDestContainer.classList.remove('hidden');
        } else if (nextDestContainer) {
            nextDestContainer.classList.add('hidden');
        }

        if (this.speedLimitReasonEl) {
            const blockLimit = this.lookaheadData.current_block_speed_limit;
            if (blockLimit != null && blockLimit > 0) {
                this.speedLimitReasonEl.textContent = `Block limit: ${Math.round(blockLimit)} km/h`;
                this.speedLimitReasonEl.classList.remove('hidden');
            } else {
                this.speedLimitReasonEl.classList.add('hidden');
            }
        }

        // Dwell task: show remaining time and target finish time
        const isDwell = payload?.current_task_kind === 'dwell';
        const dwellRemaining = payload?.dwell_remaining_sec ?? null;
        const dwellFinishSimSec = payload?.dwell_finish_sim_sec ?? null;
        if (isDwell && dwellRemaining !== null) {
            if (this.dwellRemainingEl) this.dwellRemainingEl.textContent = String(Math.ceil(dwellRemaining));
            this.dwellRemainingContainer?.classList.remove('hidden');
            if (dwellFinishSimSec !== null) {
                if (this.dwellFinishEl) this.dwellFinishEl.textContent = this._formatSimClock(dwellFinishSimSec);
                this.dwellFinishContainer?.classList.remove('hidden');
            } else {
                this.dwellFinishContainer?.classList.add('hidden');
            }
        } else {
            this.dwellRemainingContainer?.classList.add('hidden');
            this.dwellFinishContainer?.classList.add('hidden');
        }

        this.updateUpcomingElements(this.lookaheadData);
        this.render();
    }

    updateUpcomingElements(data: LookaheadData) {
        if (!this.upcomingEl) return;

        const elements: UpcomingElement[] = [];

        (data.blocks || []).forEach((block: BlockDatum) => {
            elements.push({
                type: 'block',
                name: block.name || block.id,
                distance: block.start_cm,
                length: block.length_cm || 0,
                state: block.state,
                sequence: block.sequence || 0
            });
        });

        (data.stations || []).forEach((station: StationDatum) => {
            elements.push({
                type: 'station',
                name: station.name || station.station_id,
                distance: station.distance_cm,
                length: station.length_cm || 0,
                sequence: station.sequence || 0
            });
        });

        (data.switches || []).forEach((sw: SwitchDatum) => {
            elements.push({
                type: 'switch',
                name: sw.name || `Switch (${sw.x},${sw.y})`,
                distance: sw.distance_cm,
                length: 0,
                position: sw.position,
                sequence: sw.sequence || 0
            });
        });

        (data.signals || []).forEach((sig: SignalDatum) => {
            elements.push({
                type: 'signal',
                name: sig.name || sig.id,
                distance: sig.distance_cm,
                length: 0,
                state: sig.state,
                sequence: sig.sequence || 0
            });
        });
        
        elements.sort((a, b) => {
            if (Math.abs(a.distance - b.distance) < 0.01) {
                return a.sequence - b.sequence;
            }
            return a.distance - b.distance;
        });
        
        const upcoming = elements;

        if (this.upcomingCountEl) {
            this.upcomingCountEl.textContent = String(upcoming.length);
        }

        if (upcoming.length === 0) {
            this.upcomingEl.innerHTML = '<div class="empty-state">No elements ahead</div>';
            return;
        }

        const trainDist = data.distance_into_path_cm || 0;
        const escapeHtml = (s: unknown) => String(s ?? '').replace(/[<>&"']/g, (c: string) => (
            {'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c] as string
        ));

        this.upcomingEl.innerHTML = upcoming.map(el => {
            const distToStart = el.distance - trainDist;
            const distToEnd = (el.distance + el.length) - trainDist;
            const totalLength = el.length;

            const fmtStart = this._fmtDist(distToStart);
            const fmtEnd = this._fmtDist(distToEnd);
            const fmtLength = this._fmtDist(totalLength);

            let icon = '▣';
            let kindClass = '';
            let typeLabel: string = el.type;

            if (el.type === 'station') {
                icon = '<i class="ti ti-building"></i>';
                kindClass = 'kind-station';
                typeLabel = totalLength > 0 ? `station · ${fmtLength.val}${fmtLength.unit}` : 'station';
            } else if (el.type === 'switch') {
                icon = '⇋';
                kindClass = 'kind-switch';
                typeLabel = el.position ? `switch · ${el.position}` : 'switch';
            } else if (el.type === 'signal') {
                icon = '<i class="ti ti-traffic-lights"></i>';
                kindClass = `signal-${el.state || 'unknown'}`;
                typeLabel = el.state ? `signal · ${el.state}` : 'signal';
            } else if (el.type === 'block') {
                icon = '▣';
                kindClass = `state-${el.state || 'unknown'}`;
                typeLabel = totalLength > 0
                    ? `block${el.state ? ' · ' + el.state : ''} · ${fmtLength.val}${fmtLength.unit}`
                    : `block${el.state ? ' · ' + el.state : ''}`;
            }

            const distEnd = totalLength > 0
                ? `<span class="tm-up-row-dist-end" title="Distance to end">→ ${fmtEnd.val}${fmtEnd.unit}</span>`
                : '';

            return `<div class="tm-up-row ${kindClass}" data-type="${escapeHtml(el.type)}">
                <span class="tm-up-row-icon">${icon}</span>
                <div class="tm-up-row-info">
                    <span class="tm-up-row-name" title="${escapeHtml(el.name)}">${escapeHtml(el.name)}</span>
                    <span class="tm-up-row-type">${escapeHtml(typeLabel)}</span>
                </div>
                <div class="tm-up-row-dist">
                    <span class="tm-up-row-dist-primary" title="Distance to start">${fmtStart.val}${fmtStart.unit}</span>
                    ${distEnd}
                </div>
            </div>`;
        }).join('');
    }

    _fmtDist(cm: number): { val: string; unit: string } {
        if (cm < 100) return { val: cm.toFixed(2), unit: 'cm' };
        return { val: (cm / 100).toFixed(2), unit: 'm' };
    }

    fitTextToWidth(text: string, maxWidth: number): string {
        if (!text || maxWidth <= 0) return '';
        if (this.ctx.measureText(text).width <= maxWidth) {
            return text;
        }

        const ellipsis = '...';
        let end = text.length;
        while (end > 0) {
            const candidate = text.slice(0, end).trimEnd() + ellipsis;
            if (this.ctx.measureText(candidate).width <= maxWidth) {
                return candidate;
            }
            end -= 1;
        }
        return '';
    }
    
    getMaxDistanceAhead(data: LookaheadData, trainDist: number): number {
        /**
         * Calculate the furthest element ahead of train in lookahead data.
         * Returns distance in cm from train's current position.
         */
        let maxDist = 0;
        
        // Check blocks (use end position)
        (data.blocks || []).forEach((b: BlockDatum) => {
            const blockEnd = b.end_cm;
            if (blockEnd > trainDist) {
                maxDist = Math.max(maxDist, blockEnd);
            }
        });
        
        // Check stations (use distance + length)
        (data.stations || []).forEach((s: StationDatum) => {
            const stationEnd = s.distance_cm + (s.length_cm || 0);
            if (stationEnd > trainDist) {
                maxDist = Math.max(maxDist, stationEnd);
            }
        });
        
        // Check signals (point elements, use distance only)
        (data.signals || []).forEach((sig: SignalDatum) => {
            if (sig.distance_cm > trainDist) {
                maxDist = Math.max(maxDist, sig.distance_cm);
            }
        });
        
        // Check switches (point elements, use distance only)
        (data.switches || []).forEach((sw: SwitchDatum) => {
            if (sw.distance_cm > trainDist) {
                maxDist = Math.max(maxDist, sw.distance_cm);
            }
        });
        
        // Return distance ahead of train (not absolute position)
        return Math.max(0, maxDist - trainDist);
    }
    
    render() {
        const data = this.lookaheadData;
        if (!data) return;
        
        const rect = this.canvas.getBoundingClientRect();
        const width = rect.width;
        const height = rect.height;
        
        this.ctx.clearRect(0, 0, width, height);
        
        const padding = { left: 30, right: 10, top: 15, bottom: 25 };
        
        // Section height constants derived from actual content extents
        // Block: inline signal dot reaches trackY-14 → 16px above trackY
        //        measurement labels bottom at trackY+27 → 28px below trackY
        const BLOCK_TOP       = 16;
        const BLOCK_BOT       = 28;
        const BLOCK_SECTION_H = BLOCK_TOP + BLOCK_BOT;   // 44

        // Station: 7px rect-half + 1px gap + 8px name above trackY → 16px
        //          7px rect-half + 4px gap + 8px info + ~5px margin below trackY → 24px
        const STN_TOP         = 16;
        const STN_BOT         = 24;
        const STN_SECTION_H   = STN_TOP + STN_BOT;        // 40

        const SECTION_GAP     = 8;
        const CURVE_H         = 120;

        const hasStations = (data.stations || []).length > 0;

        // Speed curve sits at top with a fixed height. Blocks and stations
        // follow underneath in order; whatever vertical room remains is left
        // blank above the distance scale.
        const speedCurveAreaHeight = CURVE_H;
        const blockStartY  = padding.top + speedCurveAreaHeight + SECTION_GAP;
        const blockTrackY  = blockStartY + BLOCK_TOP;
        const blockEndY    = blockStartY + BLOCK_SECTION_H;

        const stationStartY = hasStations ? blockEndY + SECTION_GAP : null;
        const stationTrackY = hasStations ? stationStartY! + STN_TOP : null;
        const trackWidth = width - padding.left - padding.right;
        
        const trainDist = data.distance_into_path_cm || 0;
        
        let lookaheadDistance;
        if (this.viewDistance > 0) {
            lookaheadDistance = this.viewDistance;
        } else {
            const maxAheadElement = this.getMaxDistanceAhead(data, trainDist);
            lookaheadDistance = Math.max(2000, Math.min(maxAheadElement + 1000, 10000));
        }

        const visibleStartCm = trainDist;
        const visibleEndCm = trainDist + lookaheadDistance;
        const visibleRangeCm = visibleEndCm - visibleStartCm;
        const cmToX = (cm: number) => padding.left + ((cm - visibleStartCm) / visibleRangeCm) * trackWidth;
        const xToCm = (x: number) => visibleStartCm + ((x - padding.left) / trackWidth) * visibleRangeCm;

        // Expose the current view transform so the mouse/click handlers can map
        // pixel positions back to path-cm without recomputing the layout.
        this._cmToX = cmToX;
        this._xToCm = xToCm;
        this._view = { padding, width, height, visibleStartCm, visibleEndCm };
        
        // Draw speed curves (ATC and operational limits)
        if (data.speed_curves && data.speed_curves.length > 0) {
            this.drawSpeedCurves(
                data.speed_curves,
                cmToX,
                data.current_speed_kmh,
                padding,
                speedCurveAreaHeight
            );
        }
        
        // Draw grey overlay for area already passed (behind train, negative distances)
        const trainX = cmToX(trainDist);
        if (trainX > padding.left) {
            const passedWidth = trainX - padding.left;
            this.ctx.fillStyle = '#1a1a1a';
            this.ctx.fillRect(padding.left, 0, passedWidth, height - padding.bottom);
        }
        
        // Draw grey overlay beyond clearance distance (unknown territory)
        const clearanceCm = data.clearance_cm || 0;
        if (clearanceCm > 0 && clearanceCm < lookaheadDistance) {
            const clearanceX = cmToX(trainDist + clearanceCm);
            const greyWidth = width - clearanceX;
            if (greyWidth > 0) {
                this.ctx.fillStyle = '#1a1a1a';
                this.ctx.fillRect(clearanceX, 0, greyWidth, height - padding.bottom);
            }
        }
        
        // Draw safety margin indicator (target stop point)
        const safetyMarginCm = data.safety_margin_cm || 25;
        const rawTargetStopCm = data.target_stop_cm ?? (clearanceCm - safetyMarginCm);
        const targetStopCm = Math.max(0, rawTargetStopCm);
        if (targetStopCm > 0 && targetStopCm < lookaheadDistance) {
            const targetStopX = cmToX(trainDist + targetStopCm);
            // Draw vertical dashed line at target stop point
            this.ctx.save();
            this.ctx.strokeStyle = '#ff6b6b';
            this.ctx.lineWidth = 1.5;
            this.ctx.setLineDash([4, 3]);
            this.ctx.beginPath();
            this.ctx.moveTo(targetStopX, padding.top);
            this.ctx.lineTo(targetStopX, height - padding.bottom);
            this.ctx.stroke();
            this.ctx.setLineDash([]);
            
            // Draw safety margin zone (between target stop and clearance)
            if (clearanceCm > targetStopCm) {
                const clearanceX = cmToX(trainDist + clearanceCm);
                this.ctx.fillStyle = 'rgba(255, 107, 107, 0.15)';
                this.ctx.fillRect(targetStopX, padding.top, clearanceX - targetStopX, speedCurveAreaHeight);
            }
            this.ctx.restore();
        }
        
        this.drawTrackLine(padding.left, blockTrackY, trackWidth, '#333');
        this.drawBlocks(data.blocks || [], cmToX, blockTrackY);
        this.drawSwitches(data.switches || [], cmToX, blockTrackY);
        this.drawSignals(data.signals || [], cmToX, blockTrackY);
        
        if (hasStations) {
            this.drawTrackLine(padding.left, stationTrackY!, trackWidth, '#2a4a5f');
            this.drawStations(data.stations || [], cmToX, stationTrackY!);
        }

        this.drawTrainPosition(trainX, blockTrackY);
        if (hasStations) {
            this.drawTrainPosition(trainX, stationTrackY!);
        }
        
        this.drawDistanceScale(padding.left, height - 5, trackWidth, trainDist, visibleRangeCm);

        this.drawReferenceLines(cmToX, xToCm, padding, width, height, data);

        this.drawHoverTooltip();
    }
    
    drawTrackLine(x: number, y: number, width: number, color: string) {
        this.ctx.strokeStyle = color;
        this.ctx.lineWidth = 3;
        this.ctx.beginPath();
        this.ctx.moveTo(x, y);
        this.ctx.lineTo(x + width, y);
        this.ctx.stroke();
    }
    
    drawTrainPosition(x: number, y: number) {
        this.ctx.fillStyle = '#ffcd1e';
        this.ctx.beginPath();
        this.ctx.moveTo(x, y - 6);
        this.ctx.lineTo(x + 8, y);
        this.ctx.lineTo(x, y + 6);
        this.ctx.closePath();
        this.ctx.fill();
    }
    
    drawBlocks(blocks: BlockDatum[], cmToX: (cm: number) => number, trackY: number) {
        this.blockRects = [];
        const blockHeight = 16;

        blocks.forEach((block: BlockDatum) => {
            const startX = cmToX(block.start_cm);
            const endX = cmToX(block.end_cm);
            
            if (endX < 0 || startX > this.canvas.width) return;
            
            const blockWidth = Math.max(endX - startX, 2);
            
            this.blockRects.push({
                x: startX,
                y: trackY - blockHeight/2,
                w: blockWidth,
                h: blockHeight,
                data: block
            });

            const color = getBlockStateColor(block.state ?? '');

            this.ctx.fillStyle = color;
            this.ctx.globalAlpha = 0.3;
            this.ctx.fillRect(startX, trackY - blockHeight/2, blockWidth, blockHeight);
            this.ctx.globalAlpha = 1.0;
            
            this.ctx.strokeStyle = color;
            this.ctx.lineWidth = 2;
            this.ctx.strokeRect(startX, trackY - blockHeight/2, blockWidth, blockHeight);
            
            const tileCount = block.tile_count || 1;
            if (tileCount > 1 && blockWidth > 10) {
                this.ctx.strokeStyle = color;
                this.ctx.lineWidth = 1;
                this.ctx.globalAlpha = 0.5;
                const tileWidth = blockWidth / tileCount;
                for (let i = 1; i < tileCount; i++) {
                    const dividerX = startX + i * tileWidth;
                    this.ctx.beginPath();
                    this.ctx.moveTo(dividerX, trackY - blockHeight/2);
                    this.ctx.lineTo(dividerX, trackY + blockHeight/2);
                    this.ctx.stroke();
                }
                this.ctx.globalAlpha = 1.0;
            }
            
            // Draw block name (similar to station graph)
            const name = block.name || block.id || '';
            this.ctx.font = '9px Inter, sans-serif';
            if (blockWidth > 30) {
                const labelMaxWidth = Math.max(0, blockWidth - 6);
                const displayName = this.fitTextToWidth(String(name), labelMaxWidth);
                if (displayName) {
                    this.ctx.fillStyle = '#fff';
                    this.ctx.textAlign = 'center';
                    this.ctx.textBaseline = 'middle';
                    this.ctx.fillText(displayName, startX + blockWidth/2, trackY);
                }
            }
            
            // Draw measurements below the block
            const labelY = trackY + blockHeight/2 + 2;

            const lengthM = (block.length_cm / 100).toFixed(1);
            const lengthLabel = `${lengthM}m (${tileCount} tile${tileCount > 1 ? 's' : ''})`;

            this.ctx.font = '8px JetBrains Mono, monospace';
            this.ctx.fillStyle = '#999';
            this.ctx.textAlign = 'center';
            this.ctx.textBaseline = 'top';
            this.ctx.fillText(lengthLabel, startX + blockWidth/2, labelY);

            const startM = (block.start_cm / 100).toFixed(1);
            const endM = (block.end_cm / 100).toFixed(1);
            const posLabel = `${startM}m - ${endM}m`;

            this.ctx.fillText(posLabel, startX + blockWidth/2, labelY + 9);
        });
    }
    
    drawSignals(signals: SignalDatum[], cmToX: (cm: number) => number, trackY: number) {
        signals.forEach((signal: SignalDatum) => {
            const x = cmToX(signal.distance_cm);

            if (x < 0 || x > this.canvas.width) return;

            const state = signal.state ? signal.state.toLowerCase() : null;
            const color = SIGNAL_ASPECT_COLORS[state ?? ''] || '#888';

            // Inline indicator just above the block bar (bar top sits at trackY-8).
            // Filled dot in state color; double-yellow gets an outer ring to
            // distinguish it from single-yellow without a second stacked dot.
            this.ctx.fillStyle = color;
            this.ctx.beginPath();
            this.ctx.arc(x, trackY - 11, 3, 0, Math.PI * 2);
            this.ctx.fill();

            if (state === 'double_yellow') {
                this.ctx.strokeStyle = color;
                this.ctx.lineWidth = 1;
                this.ctx.beginPath();
                this.ctx.arc(x, trackY - 11, 5, 0, Math.PI * 2);
                this.ctx.stroke();
            }
        });
    }
    
    drawSwitches(switches: SwitchDatum[], cmToX: (cm: number) => number, trackY: number) {
        if (!switches) return;

        switches.forEach((sw: SwitchDatum) => {
            const x = cmToX(sw.distance_cm);
            
            if (x < 0 || x > this.canvas.width) return;
            
            const isMain = sw.position === 'main' || sw.position === 0;
            
            // Draw orange lock circle if switch is locked
            if (sw.locked) {
                this.ctx.strokeStyle = '#ff8c00'; // Orange
                this.ctx.lineWidth = 2;
                this.ctx.beginPath();
                this.ctx.arc(x, trackY, 8, 0, Math.PI * 2);
                this.ctx.stroke();
            }
            
            this.ctx.strokeStyle = isMain ? '#04d361' : '#ffcd1e';
            this.ctx.lineWidth = 2.5;
            
            this.ctx.beginPath();
            this.ctx.moveTo(x - 5, trackY - 4);
            this.ctx.lineTo(x + 5, trackY + 4);
            this.ctx.stroke();
            
            this.ctx.beginPath();
            this.ctx.moveTo(x - 5, trackY + 4);
            this.ctx.lineTo(x + 5, trackY - 4);
            this.ctx.stroke();
            
            this.ctx.fillStyle = isMain ? '#04d361' : '#ffcd1e';
            this.ctx.beginPath();
            this.ctx.arc(x, trackY, 3, 0, Math.PI * 2);
            this.ctx.fill();
        });
    }
    
    drawStations(stations: StationDatum[], cmToX: (cm: number) => number, trackY: number) {
        const RECT_HALF = 7;   // station rect is 14px centered at trackY
        const NAME_GAP  = 1;   // gap between name text baseline and rect top
        const INFO_GAP  = 4;   // gap between rect bottom and info text top

        stations.forEach((station: StationDatum) => {
            const startX = cmToX(station.distance_cm);
            const stationLength = station.length_cm || 50;
            const endX = cmToX(station.distance_cm + stationLength);

            if (endX < 0 || startX > this.canvas.width) return;

            const stationWidth = Math.max(endX - startX, 10);
            const centerX = startX + stationWidth / 2;

            // Rect
            this.ctx.fillStyle = '#3498db';
            this.ctx.globalAlpha = 0.4;
            this.ctx.fillRect(startX, trackY - RECT_HALF, stationWidth, RECT_HALF * 2);
            this.ctx.globalAlpha = 1.0;

            this.ctx.strokeStyle = '#3498db';
            this.ctx.lineWidth = 1.5;
            this.ctx.strokeRect(startX, trackY - RECT_HALF, stationWidth, RECT_HALF * 2);

            // Tile dividers
            const tileCount = station.tile_count || 1;
            if (tileCount > 1) {
                this.ctx.strokeStyle = '#3498db';
                this.ctx.lineWidth = 0.5;
                this.ctx.globalAlpha = 0.6;
                const tileWidth = stationWidth / tileCount;
                for (let i = 1; i < tileCount; i++) {
                    const divX = startX + i * tileWidth;
                    this.ctx.beginPath();
                    this.ctx.moveTo(divX, trackY - RECT_HALF);
                    this.ctx.lineTo(divX, trackY + RECT_HALF);
                    this.ctx.stroke();
                }
                this.ctx.globalAlpha = 1.0;
            }

            // Station name ABOVE rect — baseline at trackY - RECT_HALF - NAME_GAP
            const name = station.name || station.platform_id || station.station_id || 'Platform';
            this.ctx.font = '8px Inter, sans-serif';
            this.ctx.fillStyle = '#7ec8e3';
            this.ctx.textAlign = 'center';
            this.ctx.textBaseline = 'alphabetic';
            const labelMaxWidth = Math.max(0, stationWidth - 4);
            const displayName = stationWidth > 20
                ? (this.fitTextToWidth(name, labelMaxWidth) || name)
                : name;
            this.ctx.fillText(displayName, centerX, trackY - RECT_HALF - NAME_GAP);

            // Platform length BELOW rect — top at trackY + RECT_HALF + INFO_GAP
            const lengthM = ((station.length_cm || 0) / 100).toFixed(0);
            this.ctx.font = '8px JetBrains Mono, monospace';
            this.ctx.fillStyle = '#888';
            this.ctx.textAlign = 'center';
            this.ctx.textBaseline = 'top';
            this.ctx.fillText(`${lengthM}m`, centerX, trackY + RECT_HALF + INFO_GAP);
        });
    }
    
    drawDistanceScale(x: number, y: number, width: number, trainDistCm: number, rangeCm: number) {
        this.ctx.font = '9px JetBrains Mono, monospace';
        this.ctx.fillStyle = '#666';
        this.ctx.textBaseline = 'bottom';
        this.ctx.textAlign = 'left';
        this.ctx.fillText('0m', x, y);
        
        this.ctx.textAlign = 'center';
        const midM = Math.round(rangeCm / 200);
        this.ctx.fillText(`${midM}m`, x + width / 2, y);
        
        this.ctx.textAlign = 'right';
        const maxM = Math.round(rangeCm / 100);
        this.ctx.fillText(`${maxM}m`, x + width, y);
    }

    /**
     * Linearly interpolate a speed-curve field (atc_limit / operational_limit)
     * at an arbitrary path-cm, matching the polyline drawn by drawSpeedCurves.
     * Clamps to the endpoints outside the curve's range; null if no curve.
     */
    interpolateCurve(curves: CurvePoint[], cm: number, key: 'atc_limit' | 'operational_limit'): number | null {
        if (!curves || curves.length === 0) return null;
        if (cm <= curves[0].distance_cm) return curves[0][key];
        const last = curves[curves.length - 1];
        if (cm >= last.distance_cm) return last[key];
        for (let i = 1; i < curves.length; i++) {
            const a = curves[i - 1];
            const b = curves[i];
            if (cm <= b.distance_cm) {
                const span = b.distance_cm - a.distance_cm;
                const t = span > 0 ? (cm - a.distance_cm) / span : 0;
                return a[key] + t * (b[key] - a[key]);
            }
        }
        return last[key];
    }

    drawReferenceLines(cmToX: (cm: number) => number, xToCm: (x: number) => number, padding: MonitorView['padding'], width: number, height: number, data: LookaheadData) {
        const top = padding.top;
        const bottom = height - padding.bottom;
        const { visibleStartCm, visibleEndCm } = this._view;

        // Pinned line first so the live crosshair renders on top of it.
        if (this.pinnedCm != null &&
            this.pinnedCm >= visibleStartCm && this.pinnedCm <= visibleEndCm) {
            this._drawRefMarker(cmToX(this.pinnedCm), top, bottom, this.pinnedCm, data, {
                color: '#8257e5', dash: [5, 3], lineWidth: 1.5, anchor: 'top', tag: 'PIN'
            });
        }

        if (this.mouseX != null &&
            this.mouseX >= padding.left && this.mouseX <= width - padding.right) {
            this._drawRefMarker(this.mouseX, top, bottom, xToCm(this.mouseX), data, {
                color: 'rgba(255, 255, 255, 0.55)', dash: [], lineWidth: 1, anchor: 'bottom', tag: null
            });
        }
    }

    _drawRefMarker(
        x: number,
        top: number,
        bottom: number,
        cm: number,
        data: LookaheadData,
        opts: { color: string; dash: number[]; lineWidth: number; anchor: 'top' | 'bottom'; tag: string | null }
    ) {
        const ctx = this.ctx;
        ctx.save();

        // Vertical line spanning the full chart (curves + track + stations).
        ctx.strokeStyle = opts.color;
        ctx.lineWidth = opts.lineWidth;
        ctx.setLineDash(opts.dash);
        ctx.beginPath();
        ctx.moveTo(x, top);
        ctx.lineTo(x, bottom);
        ctx.stroke();
        ctx.setLineDash([]);

        // Readout: distance from the train plus the ATC/ATO limits at this point.
        const distM = (cm - (data.distance_into_path_cm || 0)) / 100;
        const atc = this.interpolateCurve(data.speed_curves, cm, 'atc_limit');
        const ato = this.interpolateCurve(data.speed_curves, cm, 'operational_limit');

        const lines: string[] = [];
        if (opts.tag) lines.push(opts.tag);
        lines.push(`${distM.toFixed(0)}m`);
        if (atc != null) lines.push(`ATC ${Math.round(atc)}`);
        if (ato != null) lines.push(`ATO ${Math.round(ato)}`);

        ctx.font = '9px JetBrains Mono, monospace';
        const padX = 5;
        const padY = 4;
        const lh = 11;
        let boxW = 0;
        lines.forEach(l => { boxW = Math.max(boxW, ctx.measureText(l).width); });
        boxW += padX * 2;
        const boxH = lines.length * lh + padY * 2;

        // Sit beside the line, flipping to the left near the right edge.
        const cssW = this._view.width;
        const cssH = this._view.height;
        let bx = x + 6;
        if (bx + boxW > cssW - 4) bx = x - 6 - boxW;
        bx = Math.max(4, bx);
        let by = opts.anchor === 'top' ? top + 2 : bottom - boxH - 2;
        by = Math.max(2, Math.min(by, cssH - boxH - 2));

        ctx.fillStyle = 'rgba(20, 20, 28, 0.92)';
        ctx.fillRect(bx, by, boxW, boxH);
        ctx.strokeStyle = opts.color;
        ctx.lineWidth = 1;
        ctx.strokeRect(bx, by, boxW, boxH);

        ctx.textAlign = 'left';
        ctx.textBaseline = 'top';
        lines.forEach((l, i) => {
            ctx.fillStyle = (opts.tag && i === 0) ? opts.color : '#e0e0e8';
            ctx.fillText(l, bx + padX, by + padY + i * lh);
        });

        ctx.restore();
    }

    drawHoverTooltip() {
        if (!this.hoveredBlock) return;
        
        const block = this.hoveredBlock;
        const padding = 8;
        const lineHeight = 14;
        
        const lines = [
            `Block: ${block.name || block.id}`,
            `State: ${block.state}`,
            `Length: ${(block.length_cm/100).toFixed(1)}m`,
            `Start: ${(block.start_cm/100).toFixed(1)}m`
        ];
        
        this.ctx.font = '11px Inter, sans-serif';
        let maxWidth = 0;
        lines.forEach(line => {
            const w = this.ctx.measureText(line).width;
            maxWidth = Math.max(maxWidth, w);
        });
        
        const width = maxWidth + padding * 2;
        const height = lines.length * lineHeight + padding * 2;
        
        const x = this.canvas.width / 2 - width / 2;
        const y = 10;
        
        this.ctx.fillStyle = 'rgba(0, 0, 0, 0.85)';
        this.ctx.fillRect(x, y, width, height);
        this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.2)';
        this.ctx.strokeRect(x, y, width, height);
        
        this.ctx.fillStyle = '#fff';
        this.ctx.textAlign = 'left';
        this.ctx.textBaseline = 'top';
        
        lines.forEach((line, i) => {
            this.ctx.fillText(line, x + padding, y + padding + i * lineHeight);
        });
    }

    drawSpeedCurves(speedCurves: CurvePoint[], cmToX: (cm: number) => number, currentSpeed: number, padding: MonitorView['padding'], areaHeight: number) {
        if (!speedCurves || speedCurves.length === 0) return;
        
        const curveTop = padding.top;
        const curveBottom = curveTop + areaHeight;
        
        let maxSpeed = 80;
        speedCurves.forEach((p: CurvePoint) => {
            maxSpeed = Math.max(maxSpeed, p.atc_limit, p.operational_limit);
        });
        maxSpeed = Math.ceil(maxSpeed / 10) * 10;
        
        const speedToY = (speed: number) => {
            const ratio = speed / maxSpeed;
            return curveBottom - ratio * areaHeight;
        };
        
        this.ctx.font = '8px JetBrains Mono, monospace';
        this.ctx.fillStyle = '#666';
        this.ctx.textAlign = 'right';
        this.ctx.textBaseline = 'middle';
        this.ctx.fillText(`${maxSpeed}`, padding.left - 5, curveTop);
        this.ctx.fillText(`${Math.round(maxSpeed/2)}`, padding.left - 5, curveTop + areaHeight/2);
        this.ctx.fillText('0', padding.left - 5, curveBottom);
        
        this.ctx.strokeStyle = '#333';
        this.ctx.lineWidth = 1;
        this.ctx.beginPath();
        this.ctx.moveTo(padding.left, curveTop);
        this.ctx.lineTo(padding.left, curveBottom);
        this.ctx.stroke();
        
        // Draw horizontal reference lines every 20km/h
        this.ctx.strokeStyle = '#444';
        this.ctx.lineWidth = 0.5;
        this.ctx.setLineDash([2, 3]);
        for (let speed = 20; speed < maxSpeed; speed += 20) {
            const y = speedToY(speed);
            this.ctx.beginPath();
            this.ctx.moveTo(padding.left, y);
            this.ctx.lineTo(this.canvas.width - padding.right, y);
            this.ctx.stroke();
            
            // Draw speed label on the left with same color/size as y-axis labels
            this.ctx.font = '8px JetBrains Mono, monospace';
            this.ctx.fillStyle = '#666';
            this.ctx.textAlign = 'right';
            this.ctx.textBaseline = 'middle';
            this.ctx.fillText(`${speed}`, padding.left - 5, y);
        }
        this.ctx.setLineDash([]);
        
        // Draw solid reference line at 0 km/h (bottom of the speed curve area)
        this.ctx.strokeStyle = '#555';
        this.ctx.lineWidth = 1;
        this.ctx.beginPath();
        this.ctx.moveTo(padding.left, curveBottom);
        this.ctx.lineTo(this.canvas.width - padding.right, curveBottom);
        this.ctx.stroke();
        
        this.ctx.strokeStyle = '#e83f5b';
        this.ctx.lineWidth = 1.5;
        this.ctx.beginPath();
        speedCurves.forEach((point: CurvePoint, i: number) => {
            const x = cmToX(point.distance_cm);
            const y = speedToY(point.atc_limit);
            if (x < padding.left || x > this.canvas.width - padding.right) return;
            if (i === 0) this.ctx.moveTo(x, y);
            else this.ctx.lineTo(x, y);
        });
        this.ctx.stroke();
        
        this.ctx.strokeStyle = '#04d361';
        this.ctx.lineWidth = 2;
        this.ctx.beginPath();
        speedCurves.forEach((point: CurvePoint, i: number) => {
            const x = cmToX(point.distance_cm);
            const y = speedToY(point.operational_limit);
            if (x < padding.left || x > this.canvas.width - padding.right) return;
            if (i === 0) this.ctx.moveTo(x, y);
            else this.ctx.lineTo(x, y);
        });
        this.ctx.stroke();
        
        if (currentSpeed > 0) {
            const currentY = speedToY(currentSpeed);
            const triangleSize = 6;
            const labelAreaWidth = 25;
            const tipX = padding.left - labelAreaWidth;
            const lineExtension = labelAreaWidth + 2;
            
            // Draw yellow line extending across the full y-axis label width and 2px into graph
            this.ctx.beginPath();
            this.ctx.strokeStyle = '#ffcd1e';
            this.ctx.lineWidth = 1.5;
            this.ctx.moveTo(tipX, currentY);
            this.ctx.lineTo(tipX + lineExtension, currentY);
            this.ctx.stroke();
            
            // Draw yellow triangle indicator on the left of y-axis
            this.ctx.beginPath();
            this.ctx.moveTo(tipX, currentY);
            this.ctx.lineTo(tipX - triangleSize, currentY - triangleSize/2);
            this.ctx.lineTo(tipX - triangleSize, currentY + triangleSize/2);
            this.ctx.closePath();
            this.ctx.fillStyle = '#ffcd1e';  // Yellow (current-speed marker, not a signal aspect)
            this.ctx.fill();
            this.ctx.strokeStyle = '#fff';
            this.ctx.lineWidth = 1;
            this.ctx.stroke();
            
            // Draw current speed value on the right
            this.ctx.fillStyle = '#ffcd1e';
            this.ctx.textAlign = 'left';
            this.ctx.fillText(`${Math.round(currentSpeed)}`, this.canvas.width - padding.right + 3, currentY);
        }

        this.ctx.font = '8px Inter, sans-serif';
        const legendX = padding.left + 10;
        const legendY = curveTop + 8;
        
        this.ctx.fillStyle = '#e83f5b';
        this.ctx.fillRect(legendX, legendY - 4, 12, 2);
        this.ctx.fillStyle = '#999';
        this.ctx.textAlign = 'left';
        this.ctx.fillText('ATC', legendX + 15, legendY);
        
        this.ctx.fillStyle = '#04d361';
        this.ctx.fillRect(legendX + 45, legendY - 4, 12, 2);
        this.ctx.fillStyle = '#999';
        this.ctx.fillText('ATO', legendX + 60, legendY);
    }
}

export { TrainMonitor };
