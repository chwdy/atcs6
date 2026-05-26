/**
 * RouteRun Editor Handler
 *
 * Shows the runtime state of every `RouteRun` (per `design_new.md`
 * "Route And RouteExecutor" → "Route-run state"): the per-train runtime
 * instance of an assigned route, owning task cursors, resolved targets,
 * dwell timing, and last_error.
 *
 * Visual language follows `docs/DESIGN_LANGUAGE.md`:
 *   - §5A stacked label/value rows for the summary (10px uppercase
 *     `--text-tertiary` labels, 12px mono weight-600 `--text-primary`
 *     values, dotted separators).
 *   - §6 3px left-border row accents for status (running / finished /
 *     deactivated / deviated).
 *   - §7 pill chips for run number, direction, and progress count.
 *   - §11 Standard card with flat-ghost actions inside (no box-in-a-box).
 *   - §13 monochrome symbols only — no color emoji.
 *   - Long auto-generated route names are clamped to one line with
 *     `text-overflow: ellipsis` and the full string in a `title` tooltip.
 *
 * RouteRun has no operator-facing field writers in v1 — the only mutation
 * an operator can trigger from this panel is unassigning a train (which
 * goes through the existing route teardown latch via
 * PUT /api/trains/<id>/assign-route).
 */

class RouteRunEditorHandler {
    constructor(editor) {
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
        this._pollMode = null;  // 'list' | 'editor' | null
        this._inflight = false;
    }

    get api() { return this.editor.api; }

    /** Open the "all route runs" panel. */
    async showRouteRunsPanel() {
        this.editor._currentEditorType = 'route-runs-list';
        this.editor._currentEditorId = null;

        try {
            this.runs = await this.api.getRouteRuns();
        } catch (err) {
            console.error('[RouteRunEditor] Failed to load route runs:', err);
            window.showToast('Failed to load route runs: ' + err.message, 'error', 5000);
            this.runs = [];
        }

        this._renderRunsListSidebar();
        this._startPolling('list');
    }

    /** Re-render the runs list panel without re-fetching. Used by the
     *  background poller so a one-shot refresh doesn't reset focus or
     *  scroll. */
    _renderRunsListSidebar() {
        let content = `
            <div class="rr-panel">
                <div class="routes-toolbar mb-12">
                    <button id="refresh-route-runs-btn" class="btn btn-secondary btn-sm flex-1" title="Refresh runs" aria-label="Refresh runs">
                        ↻ Refresh
                    </button>
                </div>
        `;

        if (!this.runs.length) {
            content += `
                <div class="rr-empty">
                    No route runs yet — assign a train to an active route to start one.
                </div>
            `;
        } else {
            const sorted = this.runs.slice().sort((a, b) => {
                const rank = (s) => s === 'RUNNING' ? 0 : 1;
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

    renderRunListItem(run) {
        const display = run.display_state || run.state;
        const accent = this._accentClass(display);
        const routeName = run.route_name || run.route_id;
        const progress = `${run.finished_task_count}/${run.task_count}`;
        const startedText = this._formatTime(run.started_at);
        const errorChip = run.last_error
            ? `<span class="rr-chip rr-chip-danger" title="${this._esc(run.last_error.text)}">err</span>`
            : '';

        // Direction chip — no per-run geometry in the list payload, so the
        // chip shows the bare F/R/↔ prefix; the single-run editor adds the
        // (x,y)-heading detail once it has the resolved_target geometry.
        const dirCompact = formatBlockDirection(run.direction, null, null);
        return `
            <div class="rr-card rr-accent-${accent}" data-run-number="${run.run_number}">
                <div class="rr-card-head">
                    <div class="rr-card-title-col">
                        <div class="rr-card-train font-mono" title="${this._esc(run.train_id)}">${this._esc(run.train_id)}</div>
                        <div class="rr-card-route" title="${this._esc(routeName)}">${this._esc(routeName)}</div>
                    </div>
                    <button class="rr-flat-btn view-run-btn" data-run-number="${run.run_number}">View</button>
                </div>
                <div class="rr-chip-row">
                    <span class="rr-chip rr-chip-state rr-chip-${accent}">${this._esc(display)}</span>
                    <span class="rr-chip" title="run_number">#${run.run_number}</span>
                    <span class="rr-chip" title="direction (${this._esc(run.direction)})">${this._esc(dirCompact)}</span>
                    <span class="rr-chip" title="task progress">${progress} task${run.task_count === 1 ? '' : 's'}</span>
                    ${errorChip}
                </div>
                <div class="rr-card-foot">started ${this._esc(startedText)}</div>
            </div>
        `;
    }

    initRunsPanelHandlers() {
        const refresh = document.getElementById('refresh-route-runs-btn');
        if (refresh) refresh.onclick = () => this.showRouteRunsPanel();
        document.querySelectorAll('.view-run-btn').forEach(btn => {
            btn.onclick = () => this.showRouteRunEditor(parseInt(btn.dataset.runNumber, 10));
        });
    }

    /** Open the single-run editor. */
    async showRouteRunEditor(runNumber) {
        this.editor._currentEditorType = 'route-run-editor';
        this.editor._currentEditorId = String(runNumber);

        try {
            this.currentRun = await this.api.getRouteRun(runNumber);
        } catch (err) {
            if (err && err.status === 404) {
                console.warn(`[RouteRunEditor] Run ${runNumber} no longer exists`);
                window.showToast('Route run not found', 'warning', 4000);
                this.editor._currentEditorType = null;
                this.editor._currentEditorId = null;
                await this.editor.uiHandler?.hideSecondarySidebar();
                return;
            }
            console.error('[RouteRunEditor] Failed to load run:', err);
            window.showToast('Failed to load route run: ' + err.message, 'error', 5000);
            return;
        }

        this.rerenderRunEditor();
        this._startPolling('editor');
    }

    rerenderRunEditor() {
        const content = this.buildRunEditorHTML();
        this.editor.showSecondarySidebar('Route Run', content);
        document.getElementById('secondary-sidebar')?.classList.add('route-editor-wide');
        setTimeout(() => this.initRunEditorHandlers(), 0);
    }

    buildRunEditorHTML() {
        const run = this.currentRun;
        const display = run.display_state || run.state;
        const accent = this._accentClass(display);
        const routeName = run.route_name || run.route_id;
        const isRunning = run.state === 'RUNNING';
        const deviated = !!run.path_cancelled;
        const startedText = this._formatTime(run.started_at);

        // §11 status header block — `--bg-hover` bg + dynamic 3px accent strip.
        // Run #N is the identity of this run instance, treated as an inline
        // mono identifier (the equivalent of the `#` ordinal in the task list,
        // but at the view level). Route name wraps freely below — it's often
        // long and worth seeing in full.
        const headerHtml = `
            <div class="rr-status-header rr-accent-${accent}">
                <div class="rr-status-row">
                    <div class="rr-status-train font-mono" title="${this._esc(run.train_id)}">${this._esc(run.train_id)}</div>
                    <span class="rr-chip rr-chip-state rr-chip-${accent}">${this._esc(display)}</span>
                </div>
                <div class="rr-status-ident">
                    <span class="rr-status-runid">Run <span class="font-mono">#${run.run_number}</span></span>
                    <span class="rr-status-sep">·</span>
                    <span class="font-mono">${this._esc(run.route_id)}</span>
                </div>
                <div class="rr-status-route" title="${this._esc(routeName)}">${this._esc(routeName)}</div>
            </div>
        `;

        // Decorate run.direction with the next-planning task's resolved
        // terminal geometry, when available, so direction reads as F-(9,4)-N
        // rather than the bare prefix.
        let runDirGeom = { tile: null, heading: null };
        const tasks = run.tasks || [];
        const nextIdx = run.next_planning_index;
        if (nextIdx != null) {
            const nextTask = tasks.find(t =>
                t.task_index === nextIdx && t.resolved_target);
            if (nextTask && nextTask.resolved_target) {
                runDirGeom.tile = nextTask.resolved_target.terminal_tile || null;
                runDirGeom.heading = nextTask.resolved_target.terminal_heading || null;
            }
        }
        const runDirCompact = formatBlockDirection(run.direction, runDirGeom.tile, runDirGeom.heading);
        const runDirFull = formatBlockDirection(run.direction, runDirGeom.tile, runDirGeom.heading, { form: 'full' });
        const runDirLong = this._longDirection(run.direction, runDirGeom.tile, runDirGeom.heading);

        // Elapsed since start (derived value — rule 7). Useful for "how long
        // has this been running" without forcing the user to subtract from
        // wall clock.
        let elapsedText = '';
        if (run.started_at != null && run.started_at > 1e9) {
            const elapsedSec = Date.now() / 1000 - run.started_at;
            if (elapsedSec > 5) {  // skip noise for just-started runs
                elapsedText = `${this._formatDuration(elapsedSec)} ago`;
            }
        }

        // Summary block: hero progress + a few metadata rows. Each row groups
        // related fields (timing, mode/direction, planning indices). Spelled-
        // out words over abbreviations — there's room, and "forward · east"
        // reads better than "F·E".
        const modeText = run.route_mode
            ? String(run.route_mode).toLowerCase().replace(/_/g, '-')
            : '';

        const timingRow = run.started_at != null
            ? `started ${this._esc(startedText)}${elapsedText ? ` · ${this._esc(elapsedText)}` : ''}`
            : '';

        const modeDirParts = [
            modeText ? this._esc(modeText) : '',
            runDirLong ? `<span title="${this._esc(runDirFull)} · raw=${this._esc(run.direction)}">${this._esc(runDirLong)}</span>` : '',
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
            </div>
        `;

        const deviatedBanner = deviated ? `
            <div class="rr-banner rr-banner-warn">
                Path cancelled — display state is DEVIATED until controller cleanup runs.
            </div>` : '';

        const errorBanner = run.last_error ? `
            <div class="rr-banner rr-banner-danger">
                <div class="rr-banner-label">Last error</div>
                <div class="rr-banner-text">${this._esc(run.last_error.text)}</div>
                <div class="rr-banner-time">at ${this._esc(this._formatTime(run.last_error.time))}</div>
            </div>` : '';

        const tasksHtml = this.renderTasksList(run.tasks || [], run.next_planning_index);

        return `
            <div class="rr-editor route-editor" data-run-number="${run.run_number}">
                ${headerHtml}
                ${deviatedBanner}
                ${errorBanner}
                ${summaryHtml}

                <section class="rr-section">
                    <h4 class="rr-section-title">Task runs <span class="rr-section-count">${run.task_count}</span></h4>
                    ${tasksHtml}
                </section>

                <section class="rr-section">
                    <h4 class="rr-section-title">Actions</h4>
                    <div class="flex gap-8 mb-8">
                        <button id="open-route-from-run-btn" class="btn btn-secondary btn-sm flex-1">Open route</button>
                        <button id="refresh-route-run-btn" class="btn btn-secondary btn-sm flex-1">Refresh</button>
                    </div>
                    ${isRunning ? `
                        <button id="unassign-train-run-btn" class="btn btn-danger btn-sm btn-full" title="Unassign this train from the route — triggers the route teardown latch (Phase 3 path cancel + DEACTIVATED).">
                            Unassign train
                        </button>
                    ` : ''}
                    <button id="back-to-route-runs-btn" class="btn btn-secondary btn-sm btn-full mt-8">Back</button>
                </section>
            </div>
        `;
    }

    renderTasksList(tasks, nextPlanningIndex) {
        if (!tasks.length) {
            return `<div class="rr-empty">This run has no task entries yet.</div>`;
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
        return `<div class="rr-tasks">${header}${tasks.map(t => this.renderTaskRow(t, nextPlanningIndex)).join('')}</div>`;
    }

    renderTaskRow(t, nextPlanningIndex) {
        const finished = t.finished_at != null;
        const isNext = (nextPlanningIndex != null)
            && (t.task_index === nextPlanningIndex)
            && !finished;
        const liveness = finished ? 'finished' : (isNext ? 'next' : 'pending');

        const loc = t.task_location || {};
        const target = loc.block_id
            ? { kind: 'block', id: loc.block_id }
            : loc.station_id
                ? { kind: 'station', id: loc.station_id }
                : loc.platform_id
                    ? { kind: 'platform', id: loc.platform_id }
                    : { kind: null, id: '—' };

        // Hue channel — status dot encodes correctness.
        //   danger = last_error present
        //   warn   = finished late
        //   ok     = anything else (transparent dot)
        let statusVariant = 'ok';
        let statusTitle = '';
        if (t.last_error) {
            statusVariant = 'danger';
            statusTitle = t.last_error.text || 'error';
        } else if (finished && t.delay_class === 'late') {
            statusVariant = 'warn';
            statusTitle = 'late';
        }

        // Modifier: target.kind · direction. When resolved, the design-time
        // intent has collapsed into a concrete geometry (e.g. F-(9,4)-N) —
        // show that and drop the intent. When not resolved, show the intent
        // word (either / fwd / rev) so the design-time definition is visible.
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
        const modParts = [];
        if (target.kind) modParts.push(target.kind);
        if (dirText) modParts.push(dirText);
        const modifier = modParts.join(' · ');

        // Timestamps + derived duration inline. Arrow does the work of two
        // labels; duration is the bright element (it's the answer the
        // debugger is scanning for).
        const timeHtml = this._formatTimeRange(t.started_at, t.finished_at);

        // Row tooltip carries the resolved-target details (kept off-row to
        // preserve scan density).
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

        return `
            <div class="rr-task rr-task-${liveness}" title="${this._esc(tooltip)}">
                <span class="rr-task-status rr-task-status-${statusVariant}"${statusTitle ? ` title="${this._esc(statusTitle)}"` : ''}></span>
                <span class="rr-task-ord">${t.walk_position + 1}</span>
                <span class="rr-task-verb rr-task-verb-${verbAccent}">${this._esc(t.task_kind || '?')}</span>
                <span class="rr-task-noun font-mono">${this._esc(target.id)}</span>
                <span class="rr-task-mod">${this._esc(modifier)}</span>
                <span class="rr-task-time">${timeHtml}</span>
            </div>
        `;
    }

    initRunEditorHandlers() {
        const back = document.getElementById('back-to-route-runs-btn');
        if (back) back.onclick = () => this.showRouteRunsPanel();

        const refresh = document.getElementById('refresh-route-run-btn');
        if (refresh) refresh.onclick = () => this.showRouteRunEditor(this.currentRun.run_number);

        const openRoute = document.getElementById('open-route-from-run-btn');
        if (openRoute) {
            openRoute.onclick = () => {
                if (this.editor.routeHandler) {
                    this.editor.routeHandler.showRouteEditor(this.currentRun.route_id);
                }
            };
        }

        const unassign = document.getElementById('unassign-train-run-btn');
        if (unassign) {
            unassign.onclick = async () => {
                const trainId = this.currentRun.train_id;
                const confirmed = await showConfirmModal({
                    title: 'Unassign Train',
                    message: `Unassign train ${trainId} from route?`,
                    detail: 'This triggers route teardown: any live path is cancelled (Phase 3) and the run becomes DEACTIVATED.',
                    confirmText: 'Unassign',
                    confirmVariant: 'danger'
                });
                if (!confirmed) return;
                try {
                    await this.api.assignRoute(trainId, null);
                    window.showToast(`Train ${trainId} unassigned`, 'success', 3000);
                    await this.showRouteRunEditor(this.currentRun.run_number);
                } catch (err) {
                    console.error('[RouteRunEditor] Unassign failed:', err);
                    window.showToast('Failed to unassign: ' + err.message, 'error', 5000);
                }
            };
        }
    }

    // ---- polling lifecycle ----

    /** Called by `editor-ui.cleanupSecondaryPanelSubscriptions`
     *  when the secondary sidebar hides or switches to a different panel. */
    _cleanup() {
        this._stopPolling();
    }

    _startPolling(mode) {
        this._stopPolling();
        this._pollMode = mode;
        this._pollHandle = setInterval(
            () => this._pollOnce(),
            this._pollIntervalMs,
        );
    }

    _stopPolling() {
        if (this._pollHandle) {
            clearInterval(this._pollHandle);
            this._pollHandle = null;
        }
        this._pollMode = null;
        this._inflight = false;
    }

    async _pollOnce() {
        if (this._inflight) return;
        // Tab-hidden visibility — pause polling but keep the timer alive
        // so we resume promptly when the tab is foregrounded again.
        if (typeof document !== 'undefined' && document.hidden) return;
        // Re-confirm panel identity each tick: if the user has navigated
        // to a different secondary panel we should not be issuing fetches
        // here. `_cleanup()` normally handles this, but the visibility
        // guard is cheap and protects against missed cleanup events.
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
                    this.rerenderRunEditor();
                }
            }
        } catch (err) {
            // The run we were watching is gone (server restart, route
            // unregistered, etc.). Stop the editor poll and fall back to
            // the list, which will refresh the user's view to whatever
            // does exist now.
            if (err && err.status === 404 && this._pollMode === 'editor') {
                this._stopPolling();
                this.showRouteRunsPanel();
                return;
            }
            // Best-effort refresh: a transient API error should not kill
            // the panel. Surface to console for debugging; keep polling.
            console.debug('[RouteRunEditor] poll error:', err);
        } finally {
            this._inflight = false;
        }
    }

    // ---- helpers ----

    _accentClass(displayState) {
        switch (displayState) {
            case 'RUNNING':     return 'success';
            case 'FINISHED':    return 'info';
            case 'DEACTIVATED': return 'muted';
            case 'DEVIATED':    return 'warn';
            default:            return 'muted';
        }
    }

    _verbAccent(kind) {
        if (!kind) return 'muted';
        switch (String(kind).toLowerCase()) {
            case 'stop':     return 'info';
            case 'dwell':    return 'warn';
            default:         return 'muted';
        }
    }

    _shortDir(d) {
        if (!d) return '';
        switch (String(d).toLowerCase()) {
            case 'forward': return 'fwd';
            case 'reverse': return 'rev';
            case 'either':  return 'either';
            default:        return String(d);
        }
    }

    _longDirection(direction, tile, heading) {
        if (!direction) return '';
        const parts = [String(direction).toLowerCase()];
        if (Array.isArray(tile) && tile.length === 2) {
            parts.push(`(${tile[0]},${tile[1]})`);
        }
        if (heading) {
            const headingWord = { N: 'north', S: 'south', E: 'east', W: 'west' }[String(heading).toUpperCase()] || heading;
            parts.push(headingWord);
        }
        return parts.join(' · ');
    }

    _formatTime(t) {
        if (t == null) return '—';
        if (t > 1e9) {
            try { return new Date(t * 1000).toLocaleTimeString(); } catch (e) { /* fall through */ }
        }
        return `t=${Number(t).toFixed(1)}s`;
    }

    _formatTimeRange(startedAt, finishedAt) {
        if (startedAt == null && finishedAt == null) {
            return '<span class="rr-task-time-muted">—</span>';
        }
        if (startedAt != null && finishedAt != null) {
            // Two-line stack: started on top, finished + duration on bottom.
            // Vertical stacking implies the sequence; no arrow needed.
            const dur = finishedAt - startedAt;
            return `<span class="rr-task-time-line">${this._esc(this._formatTime(startedAt))}</span><span class="rr-task-time-line">${this._esc(this._formatTime(finishedAt))}<span class="rr-task-dur">${this._formatDuration(dur)}</span></span>`;
        }
        if (startedAt != null) {
            // Running — single line, the row's live brightness signals ongoing.
            return `<span class="rr-task-time-line">${this._esc(this._formatTime(startedAt))}</span>`;
        }
        return '<span class="rr-task-time-muted">—</span>';
    }

    _formatDuration(seconds) {
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

    _esc(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }
}
