// editor-route-runs.ts — RouteRun Editor Handler.
//
// Migrated from static/js/editor-route-runs.js (longterm_todo.md item #7,
// Wave 2). Behavior is unchanged.
//
// Shows the runtime state of every `RouteRun` (per design_new.md "Route And
// RouteExecutor" → "Route-run state"): the per-train runtime instance of an
// assigned route, owning task cursors, resolved targets, dwell timing, and
// its current error. That error is not stored on the run: the backend derives
// it from the one NotificationCenter stream (design_new.md "Error events"), so
// the record carries `{text, origin}` and the event owns the time.
// Visual language follows docs/DESIGN_LANGUAGE.md.
//
// RouteRun has no operator-facing field writers in v1 — the only mutation an
// operator can trigger from this panel is unassigning a train (which goes
// through the existing route teardown latch via PUT /api/trains/<id>/assign-route)
// and editing the three allowlisted per-run overrides.
//
// Owned by the Editor (`new RouteRunEditorHandler(this)`).

import { showToast } from '../lib/toast';
import { copyIdButton } from '../lib/copy-id';
import type { EditorHost } from './types';
import { formatBlockDirection, validatePriorityInput, showConfirmModal } from '../lib/helpers';

export class RouteRunEditorHandler {
  editor: EditorHost;
  runs: any[];
  currentRun: any;
  _pollHandle: ReturnType<typeof setInterval> | null;
  _pollIntervalMs: number;
  _pollMode: string | null;
  _inflight: boolean;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this.runs = [];
    this.currentRun = null;

    // Poll lifecycle — the list and per-run editor both reflect live
    // executor state (run_count growing, new tasks appended on
    // CIRCULAR/ROUND_TRIP wrap, resolved_target landing once
    // create/extend succeeds). 1.5s matches the simulation Phase 1
    // cadence well enough without hammering the API.
    this._pollHandle = null;
    this._pollIntervalMs = 1500;
    this._pollMode = null; // 'list' | 'editor' | null
    this._inflight = false;
  }

  get api(): any {
    return this.editor.api;
  }

  /** Open the "all route runs" panel. */
  async showRouteRunsPanel(): Promise<void> {
    this.editor._currentEditorType = 'route-runs-list';
    this.editor._currentEditorId = null;

    try {
      this.runs = await this.api.getRouteRuns();
    } catch (err: any) {
      console.error('[RouteRunEditor] Failed to load route runs:', err);
      showToast('Failed to load route runs: ' + err.message, 'error', 5000);
      this.runs = [];
    }

    this._renderRunsListSidebar();
    this._startPolling('list');
  }

  /** Re-render the runs list panel without re-fetching. Used by the
   *  background poller so a one-shot refresh doesn't reset focus or
   *  scroll. */
  _renderRunsListSidebar(): void {
    let content = `
            <div class="rr-panel">
        `;

    if (!this.runs.length) {
      content += `
                <div class="empty-state">
                    No route runs yet — assign a train to an active route to start one.
                </div>
            `;
    } else {
      const sorted = this.runs.slice().sort((a, b) => {
        const rank = (s: string) => (s === 'RUNNING' ? 0 : 1);
        if (rank(a.state) !== rank(b.state)) return rank(a.state) - rank(b.state);
        return (b.started_at || 0) - (a.started_at || 0);
      });
      content += '<div class="rr-list">';
      for (const run of sorted) content += this.renderRunListItem(run);
      content += '</div>';
    }
    content += '</div>';

    this.editor.showSecondarySidebar('Route Runs', content);
    setTimeout(() => this.initRunsPanelHandlers(), 0);
  }

  renderRunListItem(run: any): string {
    const display = run.display_state || run.state;
    const accent = this._accentClass(display);
    const routeName = run.route_name || run.route_id;
    const progress = `${run.finished_task_count}/${run.task_count}`;
    const startedText = this._formatTime(run.started_at);
    const errorChip = run.last_error
      ? `<span class="chip chip-danger" title="${this._esc(run.last_error.text)}${
          run.last_error.origin ? ` (${this._esc(run.last_error.origin)})` : ''
        }">err</span>`
      : '';

    // Direction chip — no per-run geometry in the list payload, so the
    // chip shows the bare F/R/↔ prefix; the single-run editor adds the
    // (x,y)-heading detail once it has the resolved_target geometry.
    const dirCompact = formatBlockDirection(run.direction, null, null);
    return `
            <div class="rr-card accent-${accent}" data-run-number="${run.run_number}">
                <div class="rr-card-head">
                    <div class="rr-card-title-col">
                        <div class="rr-card-train font-mono" title="${this._esc(run.train_id)}">${this._esc(run.train_id)}</div>
                        <div class="rr-card-route" title="${this._esc(routeName)}">${this._esc(routeName)}</div>
                    </div>
                    <button class="rr-flat-btn view-run-btn" data-run-number="${run.run_number}">View</button>
                </div>
                <div class="chip-row">
                    <span class="chip chip-state chip-${accent}">${this._esc(display)}</span>
                    <span class="chip" title="run_number">#${run.run_number}</span>
                    <span class="chip" title="direction (${this._esc(run.direction)})">${this._esc(dirCompact)}</span>
                    <span class="chip" title="task progress">${progress} task${run.task_count === 1 ? '' : 's'}</span>
                    ${errorChip}
                </div>
                <div class="rr-card-foot">started ${this._esc(startedText)}</div>
            </div>
        `;
  }

  initRunsPanelHandlers(): void {
    document.querySelectorAll<HTMLElement>('.view-run-btn').forEach((btn) => {
      btn.onclick = () => this.showRouteRunEditor(parseInt(btn.dataset.runNumber ?? '', 10));
    });
  }

  /** Open the single-run editor. */
  async showRouteRunEditor(runNumber: number): Promise<void> {
    this.editor._currentEditorType = 'route-run-editor';
    this.editor._currentEditorId = String(runNumber);

    try {
      this.currentRun = await this.api.getRouteRun(runNumber);
    } catch (err: any) {
      if (err && err.status === 404) {
        console.warn(`[RouteRunEditor] Run ${runNumber} no longer exists`);
        showToast('Route run not found', 'warning', 4000);
        this.editor._currentEditorType = null;
        this.editor._currentEditorId = null;
        await this.editor.uiHandler?.hideSecondarySidebar();
        return;
      }
      console.error('[RouteRunEditor] Failed to load run:', err);
      showToast('Failed to load route run: ' + err.message, 'error', 5000);
      return;
    }

    this.rerenderRunEditor();
    this._startPolling('editor');
  }

  rerenderRunEditor(): void {
    const content = this.buildRunEditorHTML();
    this.editor.showSecondarySidebar('Route Run', content);
    document.getElementById('secondary-sidebar')?.classList.add('route-editor-wide');
    setTimeout(() => this.initRunEditorHandlers(), 0);
  }

  buildRunEditorHTML(): string {
    const run = this.currentRun;
    const display = run.display_state || run.state;
    const accent = this._accentClass(display);
    const routeName = run.route_name || run.route_id;
    const isRunning = run.state === 'RUNNING';
    const cancelled = !!run.last_cancellation;
    const startedText = this._formatTime(run.started_at);

    // §11 status header block — `--bg-hover` bg + dynamic 3px accent strip.
    const headerHtml = `
            <div class="status-header accent-${accent}">
                <div class="status-header-row">
                    <div class="id-copy-group">
                        <div class="status-header-id font-mono" title="${this._esc(run.train_id)}">${this._esc(run.train_id)}</div>
                        ${copyIdButton(run.train_id, 'Train ID')}
                    </div>
                    <span class="chip chip-state chip-${accent}">${this._esc(display)}</span>
                </div>
                <div class="status-header-ident">
                    <span class="status-header-role">Run <span class="font-mono">#${run.run_number}</span></span>
                    <span class="status-sep">·</span>
                    <span class="font-mono">${this._esc(run.route_id)}</span>
                </div>
                <div class="status-header-name" title="${this._esc(routeName)}">${this._esc(routeName)}</div>
            </div>
        `;

    // Decorate run.direction with the next-planning task's resolved
    // terminal geometry, when available, so direction reads as F-(9,4)-N
    // rather than the bare prefix.
    const runDirGeom: { tile: any; heading: any } = { tile: null, heading: null };
    const tasks = run.tasks || [];
    const nextIdx = run.next_planning_index;
    if (nextIdx != null) {
      const nextTask = tasks.find((t: any) => t.task_index === nextIdx && t.resolved_target);
      if (nextTask && nextTask.resolved_target) {
        runDirGeom.tile = nextTask.resolved_target.terminal_tile || null;
        runDirGeom.heading = nextTask.resolved_target.terminal_heading || null;
      }
    }
    const runDirCompact = formatBlockDirection(run.direction, runDirGeom.tile, runDirGeom.heading);
    const runDirFull = formatBlockDirection(run.direction, runDirGeom.tile, runDirGeom.heading, {
      form: 'full',
    });
    const runDirLong = this._longDirection(run.direction, runDirGeom.tile, runDirGeom.heading);

    // Elapsed since start (derived value — rule 7).
    let elapsedText = '';
    if (run.started_at != null && run.started_at > 1e9) {
      const elapsedSec = Date.now() / 1000 - run.started_at;
      if (elapsedSec > 5) {
        // skip noise for just-started runs
        elapsedText = `${this._formatDuration(elapsedSec)} ago`;
      }
    }

    const modeText = run.route_mode ? String(run.route_mode).toLowerCase().replace(/_/g, '-') : '';

    const timingRow =
      run.started_at != null
        ? `started ${this._esc(startedText)}${elapsedText ? ` · ${this._esc(elapsedText)}` : ''}`
        : '';

    const modeDirParts = [
      modeText ? this._esc(modeText) : '',
      runDirLong
        ? `<span title="${this._esc(runDirFull)} · raw=${this._esc(run.direction)}">${this._esc(runDirLong)}</span>`
        : '',
    ].filter(Boolean);

    const summaryHtml = `
            <div class="rr-summary mb-12">
                <div class="rr-summary-hero">
                    <span class="rr-summary-num rr-summary-num-${accent}">${run.finished_task_count}</span><span class="rr-summary-sep">/</span><span class="rr-summary-total">${run.task_count}</span>
                    <span class="rr-summary-unit">tasks</span>
                </div>
                ${timingRow ? `<div class="rr-summary-meta">${timingRow}</div>` : ''}
                ${modeDirParts.length ? `<div class="rr-summary-meta">${modeDirParts.join(' · ')}</div>` : ''}
                <div class="rr-summary-meta">
                    last <span class="rr-summary-idx">${run.current_waypoint_index != null ? '#' + run.current_waypoint_index : '—'}</span>
                    →
                    next <span class="rr-summary-idx">${run.next_planning_index != null ? '#' + run.next_planning_index : '—'}</span>
                </div>
                ${
                  run.start_mode
                    ? `<div class="rr-summary-meta" title="Resolved start mode this run was seeded with at allocation (read-only — set on the route or at assignment).">start: ${this._esc(run.start_mode)}</div>`
                    : ''
                }
            </div>
        `;

    const deviatedBanner = cancelled
      ? `
            <div class="banner banner-warn">
                Path cancelled — ${run.last_cancellation?.origin ?? 'cancelled'}. Cleared on the next path.
            </div>`
      : '';

    const errorBanner = run.last_error
      ? `
            <div class="banner banner-danger">
                <div class="banner-label">Current error</div>
                <div class="banner-text">${this._esc(run.last_error.text)}</div>
                ${run.last_error.origin
                  ? `<div class="rr-banner-time">${this._esc(run.last_error.origin)}</div>`
                  : ''}
            </div>`
      : '';

    const tasksHtml = this.renderTasksList(run.tasks || [], run.next_planning_index);

    const overridesHtml = isRunning ? this.renderOverridesSection(run) : '';

    return `
            <div class="rr-editor route-editor" data-run-number="${run.run_number}">
                ${headerHtml}
                ${deviatedBanner}
                ${errorBanner}
                ${summaryHtml}

                ${overridesHtml}

                <section class="rr-section">
                    <h4 class="rr-section-title">Task runs <span class="chip">${run.task_count}</span></h4>
                    ${tasksHtml}
                </section>

                <section class="rr-section">
                    <h4 class="rr-section-title">Actions</h4>
                    <div class="flex gap-8 mb-8">
                        <button id="open-route-from-run-btn" class="btn btn-secondary btn-sm flex-1">Open route</button>
                    </div>
                    ${
                      isRunning
                        ? `
                        <button id="unassign-train-run-btn" class="btn btn-danger btn-sm btn-full" title="Unassign this train from the route — triggers the route teardown latch (Phase 3 path cancel + DEACTIVATED).">
                            Unassign train
                        </button>
                    `
                        : ''
                    }
                    <button id="back-to-route-runs-btn" class="btn btn-secondary btn-sm btn-full mt-8">Back</button>
                </section>
            </div>
        `;
  }

  /**
   * Per design_new.md "RouteRun overrides": the three allowlisted policy
   * fields (priority, acquisition_mode)
   * may be overridden per-run.
   */
  renderOverridesSection(run: any): string {
    const overrides = run.overrides || {};
    const has = (k: string) => Object.prototype.hasOwnProperty.call(overrides, k);
    const seg = (id: string, options: Array<{ value: string; label: string }>, current: string) => `
            <div id="${id}" class="switch-group compact route-seg" role="radiogroup">
                ${options
                  .map(
                    (o) => `
                    <button type="button" class="switch-btn ${o.value === current ? 'active' : ''}"
                            data-value="${o.value}" data-seg="${id}">${o.label}</button>
                `
                  )
                  .join('')}
            </div>
        `;
    const acqEff = run.effective_acquisition_mode || 'best_effort';
    const priEff = Number.isInteger(run.effective_priority) ? run.effective_priority : 5;
    const resetBtn = (field: string) => `
            <button type="button" class="btn btn-xs btn-secondary rr-override-reset-btn"
                    data-field="${field}" title="Restore the shared route value for this field.">
                Reset to route
            </button>
        `;
    return `
            <section class="rr-section" data-run-overrides>
                <h4 class="rr-section-title">Per-run overrides
                    <span class="chip">${Object.keys(overrides).length}</span>
                </h4>
                <div class="text-xxs text-muted mb-8">
                    Overrides win over the shared route until cleared. Changes apply immediately.
                </div>

                <div class="route-header-row run-overrides-grid">
                    <div class="route-header-col">
                        <label class="property-label">Acquisition</label>
                        ${seg(
                          'rr-acq-seg',
                          [
                            { value: 'best_effort', label: 'Best effort' },
                            { value: 'all_or_nothing', label: 'All or nothing' },
                          ],
                          acqEff
                        )}
                        ${has('acquisition_mode') ? resetBtn('acquisition_mode') : ''}
                    </div>
                    <div class="route-header-col narrow">
                        <label class="property-label">Priority (0-9)</label>
                        <input type="number" id="rr-priority-input" class="property-input"
                               min="0" max="9" step="1" value="${priEff}">
                        ${has('priority') ? resetBtn('priority') : ''}
                    </div>
                </div>
            </section>
        `;
  }

  renderTasksList(tasks: any[], nextPlanningIndex: any): string {
    if (!tasks.length) {
      return `<div class="empty-state">This run has no task entries yet.</div>`;
    }

    const header = `
            <div class="rr-tasks-header">
                <span></span>
                <span>#</span>
                <span>type</span>
                <span>target</span>
                <span>detail</span>
                <span>time</span>
            </div>
        `;
    return `<div class="rr-tasks">${header}${tasks.map((t) => this.renderTaskRow(t, nextPlanningIndex)).join('')}</div>`;
  }

  renderTaskRow(t: any, nextPlanningIndex: any): string {
    const finished = t.finished_at != null;
    const isNext = nextPlanningIndex != null && t.task_index === nextPlanningIndex && !finished;
    const liveness = finished ? 'finished' : isNext ? 'next' : 'pending';

    const loc = t.task_location || {};
    const target = loc.block_id
      ? { kind: 'block', id: loc.block_id }
      : loc.station_id
        ? { kind: 'station', id: loc.station_id }
        : loc.platform_id
          ? { kind: 'platform', id: loc.platform_id }
          : { kind: null, id: '—' };

    // Hue channel — status dot encodes correctness.
    let statusVariant = 'ok';
    let statusTitle = '';
    const errText = t.last_error ? t.last_error.text || 'error' : null;
    const lateLabel = finished && t.delay_class === 'late' ? 'late' : null;
    if (errText && !finished) {
      statusVariant = 'danger';
      statusTitle = errText;
    } else if (errText || lateLabel) {
      statusVariant = 'warn';
      statusTitle = [lateLabel, errText].filter(Boolean).join(' · ');
    }

    // Modifier: target.kind · direction.
    let dirText = '';
    if (t.resolved_target) {
      const rt = t.resolved_target;
      dirText = formatBlockDirection(
        rt.requested_destination_direction,
        rt.terminal_tile || null,
        rt.terminal_heading || null,
        { form: 'full' }
      );
    } else if (t.task_direction) {
      dirText = this._shortDir(t.task_direction);
    }
    const modParts: string[] = [];
    if (target.kind) modParts.push(target.kind);
    if (dirText) modParts.push(dirText);
    const modifier = modParts.join(' · ');

    // Timestamps + derived duration inline.
    const timeHtml = this._formatTimeRange(t.started_at, t.finished_at);

    // Row tooltip carries the resolved-target details.
    let tooltip = `#${t.walk_position + 1} ${t.task_kind || '?'} ${target.id}`;
    if (t.task_direction) tooltip += ` · intent=${t.task_direction}`;
    if (t.resolved_target) {
      const rt = t.resolved_target;
      const rDir = rt.requested_destination_direction;
      const rTile = rt.terminal_tile || null;
      const rHeading = rt.terminal_heading || null;
      const dirFull = formatBlockDirection(rDir, rTile, rHeading, { form: 'full' });
      tooltip += ` · resolved=${rt.requested_destination_block_id} ${dirFull}`;
    }
    if (t.last_error) tooltip += ` · ERROR: ${t.last_error.text}`;

    const verbAccent = this._verbAccent(t.task_kind);

    const escTooltip = this._esc(tooltip);
    const escStatusTitle = statusTitle ? this._esc(statusTitle) : '';
    return `
            <div class="rr-task rr-task-${liveness}" data-tooltip="${escTooltip}" aria-label="${escTooltip}">
                <span class="rr-task-status rr-task-status-${statusVariant}"${escStatusTitle ? ` data-tooltip="${escStatusTitle}" aria-label="${escStatusTitle}"` : ''}></span>
                <span class="rr-task-ord">${t.walk_position + 1}</span>
                <span class="rr-task-verb rr-task-verb-${verbAccent}">${this._esc(t.task_kind || '?')}</span>
                <span class="rr-task-noun font-mono">${this._esc(target.id)}</span>
                <span class="rr-task-mod">${this._esc(modifier)}</span>
                <span class="rr-task-time">${timeHtml}</span>
            </div>
        `;
  }

  initRunEditorHandlers(): void {
    const back = document.getElementById('back-to-route-runs-btn') as HTMLButtonElement | null;
    if (back) back.onclick = () => this.showRouteRunsPanel();

    // Per-run overrides apply immediately on segment click / input change.
    const runNumber = this.currentRun?.run_number;
    if (runNumber != null) {
      const saveOverride = async (field: string, value: any) => {
        try {
          const result = await this.api.patchRouteRunOverrides(runNumber, { [field]: value });
          if (result && result.error) {
            showToast(`Override rejected: ${result.error}`, 'error', 4000);
          } else {
            showToast(`Override saved: ${field}`, 'success', 2000);
            await this.showRouteRunEditor(runNumber);
          }
        } catch (err: any) {
          showToast(`Override failed: ${err.message || err}`, 'error', 4000);
        }
      };

      const segFields: Record<string, string> = {
        'rr-acq-seg': 'acquisition_mode',
      };
      Object.entries(segFields).forEach(([segId, field]) => {
        document.querySelectorAll<HTMLElement>(`#${segId} .switch-btn`).forEach((btn) => {
          btn.onclick = (e) => {
            const target = e.currentTarget as HTMLElement;
            if (target.classList.contains('active')) return;
            const seg = target.parentElement;
            seg
              ?.querySelectorAll<HTMLElement>('.switch-btn')
              .forEach((b) => b.classList.toggle('active', b === target));
            saveOverride(field, target.dataset.value);
          };
        });
      });

      const priorityInput = document.getElementById('rr-priority-input') as HTMLInputElement | null;
      if (priorityInput) {
        priorityInput.onchange = () => {
          const raw = validatePriorityInput(priorityInput.value);
          if (raw == null || raw.kind === 'empty') return;
          if (raw.kind === 'error') {
            showToast(raw.message, 'error', 4000);
            return;
          }
          saveOverride('priority', raw.value);
        };
      }

      document.querySelectorAll<HTMLElement>('.rr-override-reset-btn').forEach((btn) => {
        btn.onclick = async () => {
          const field = btn.dataset.field;
          if (!field) return;
          try {
            const result = await this.api.clearRouteRunOverride(runNumber, field);
            if (result && result.error) {
              showToast(`Reset rejected: ${result.error}`, 'error', 4000);
            } else {
              showToast(`Override cleared: ${field}`, 'success', 2000);
              await this.showRouteRunEditor(runNumber);
            }
          } catch (err: any) {
            showToast(`Reset failed: ${err.message || err}`, 'error', 4000);
          }
        };
      });
    }

    const openRoute = document.getElementById('open-route-from-run-btn') as HTMLButtonElement | null;
    if (openRoute) {
      openRoute.onclick = () => {
        if (this.editor.routeHandler) {
          this.editor.routeHandler.showRouteEditor(this.currentRun.route_id);
        }
      };
    }

    const unassign = document.getElementById('unassign-train-run-btn') as HTMLButtonElement | null;
    if (unassign) {
      unassign.onclick = async () => {
        const trainId = this.currentRun.train_id;
        const confirmed = await showConfirmModal({
          title: 'Unassign Train',
          message: `Unassign train ${trainId} from route?`,
          detail:
            'This triggers route teardown: any live path is cancelled (Phase 3) and the run becomes DEACTIVATED.',
          confirmText: 'Unassign',
          confirmVariant: 'danger',
        });
        if (!confirmed) return;
        try {
          await this.api.assignRoute(trainId, null);
          showToast(`Train ${trainId} unassigned`, 'success', 3000);
          await this.showRouteRunEditor(this.currentRun.run_number);
        } catch (err: any) {
          console.error('[RouteRunEditor] Unassign failed:', err);
          showToast('Failed to unassign: ' + err.message, 'error', 5000);
        }
      };
    }
  }

  // ---- polling lifecycle ----

  /** Called by `TabGroup.cleanupPanelSubscriptions` (tab-group.ts)
   *  when the secondary sidebar hides or switches to a different panel. */
  _cleanup(): void {
    this._stopPolling();
  }

  _startPolling(mode: string): void {
    this._stopPolling();
    this._pollMode = mode;
    this._pollHandle = setInterval(() => this._pollOnce(), this._pollIntervalMs);
  }

  _stopPolling(): void {
    if (this._pollHandle) {
      clearInterval(this._pollHandle);
      this._pollHandle = null;
    }
    this._pollMode = null;
    this._inflight = false;
  }

  async _pollOnce(): Promise<void> {
    if (this._inflight) return;
    // Tab-hidden visibility — pause polling but keep the timer alive
    // so we resume promptly when the tab is foregrounded again.
    if (typeof document !== 'undefined' && document.hidden) return;
    // Re-confirm panel identity each tick.
    const editorType = this.editor?._currentEditorType;
    if (this._pollMode === 'list' && editorType !== 'route-runs-list') return;
    if (this._pollMode === 'editor' && editorType !== 'route-run-editor') return;

    this._inflight = true;
    try {
      if (this._pollMode === 'list') {
        const runs = await this.api.getRouteRuns();
        this.runs = runs || [];
        this._renderRunsListSidebar();
      } else if (this._pollMode === 'editor' && this.currentRun) {
        const runNumber = this.currentRun.run_number;
        const next = await this.api.getRouteRun(runNumber);
        if (next) {
          this.currentRun = next;
          // Skip the innerHTML swap while the user is hovering the editor.
          if (!this._isEditorHovered()) {
            this.rerenderRunEditor();
          }
        }
      }
    } catch (err: any) {
      // The run we were watching is gone — stop the editor poll and fall
      // back to the list.
      if (err && err.status === 404 && this._pollMode === 'editor') {
        this._stopPolling();
        this.showRouteRunsPanel();
        return;
      }
      console.debug('[RouteRunEditor] poll error:', err);
    } finally {
      this._inflight = false;
    }
  }

  // ---- helpers ----

  /** True when the cursor is currently over the open run editor. */
  _isEditorHovered(): boolean {
    const el = document.querySelector('.rr-editor');
    if (!el || typeof el.matches !== 'function') return false;
    try {
      return el.matches(':hover');
    } catch (_) {
      return false;
    }
  }

  _accentClass(displayState: string): string {
    switch (displayState) {
      case 'RUNNING':
        return 'success';
      case 'FINISHED':
        return 'info';
      case 'DEACTIVATED':
        return 'muted';
      case 'CANCELLED':
        return 'warn';
      case 'STALLED':
        return 'warn';
      default:
        return 'muted';
    }
  }

  _verbAccent(kind: any): string {
    if (!kind) return 'muted';
    switch (String(kind).toLowerCase()) {
      case 'stop':
        return 'info';
      case 'dwell':
        return 'warn';
      default:
        return 'muted';
    }
  }

  _shortDir(d: any): string {
    if (!d) return '';
    switch (String(d).toLowerCase()) {
      case 'forward':
        return 'fwd';
      case 'reverse':
        return 'rev';
      case 'either':
        return 'either';
      default:
        return String(d);
    }
  }

  _longDirection(direction: any, tile: any, heading: any): string {
    if (!direction) return '';
    const parts = [String(direction).toLowerCase()];
    if (Array.isArray(tile) && tile.length === 2) {
      parts.push(`(${tile[0]},${tile[1]})`);
    }
    if (heading) {
      const headingWord =
        ({ N: 'north', S: 'south', E: 'east', W: 'west' } as Record<string, string>)[
          String(heading).toUpperCase()
        ] || heading;
      parts.push(headingWord);
    }
    return parts.join(' · ');
  }

  _formatTime(t: any): string {
    if (t == null) return '—';
    if (t > 1e9) {
      try {
        return new Date(t * 1000).toLocaleTimeString();
      } catch (e) {
        /* fall through */
      }
    }
    return `t=${Number(t).toFixed(1)}s`;
  }

  _formatTimeRange(startedAt: any, finishedAt: any): string {
    if (startedAt == null && finishedAt == null) {
      return '<span class="rr-task-time-muted">—</span>';
    }
    if (startedAt != null && finishedAt != null) {
      const dur = finishedAt - startedAt;
      return `<span class="rr-task-time-line">${this._esc(this._formatTime(startedAt))}</span><span class="rr-task-time-line">${this._esc(this._formatTime(finishedAt))}<span class="rr-task-dur">${this._formatDuration(dur)}</span></span>`;
    }
    if (startedAt != null) {
      return `<span class="rr-task-time-line">${this._esc(this._formatTime(startedAt))}</span>`;
    }
    return '<span class="rr-task-time-muted">—</span>';
  }

  _formatDuration(seconds: any): string {
    if (seconds == null || seconds < 0) return '';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) {
      const m = Math.floor(seconds / 60);
      const s = Math.round(seconds % 60);
      return s === 0 ? `${m}m` : `${m}m${s}s`;
    }
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return m === 0 ? `${h}h` : `${h}h${m}m`;
  }

  _esc(s: any): string {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
}
