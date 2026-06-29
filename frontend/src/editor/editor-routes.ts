// editor-routes.ts — Route Editor Handler.
//
// Migrated from static/js/editor-routes.js (longterm_todo.md item #7, Wave 2 —
// the largest handler). Behavior is unchanged.
//
// Manages train routes — ordered lists of RouteTasks (per design_new.md "Route
// And RouteExecutor"). Distinct from the Path Editor (block-level reservations).
//
// Owned by the Editor (`new RouteEditorHandler(this)`).

import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import { showConfirmModal, validatePriorityInput } from '../lib/helpers';
import { showResourcePicker, dismissResourcePicker } from './resource-picker';

export class RouteEditorHandler {
  editor: EditorHost;
  routes: any[];
  currentRoute: any;
  currentRouteStatus: any;
  viabilityResults: Map<number, any>;
  locationHighlightTimeout: ReturnType<typeof setTimeout> | null;
  _waypointReachablePositions: Set<string> | null;
  _assignForce: boolean;
  _assignTileTrainMap: Map<string, any> | null;
  _historyExpanded: boolean;
  _historyData: any;
  _historyMaxRunsPerTrain: number;
  _taskExpanded: WeakMap<object, boolean>;
  _routeSyncInterval: ReturnType<typeof setInterval> | null;
  _routeSyncIntervalMs: number;
  _routesListInterval: ReturnType<typeof setInterval> | null;
  _routesListIntervalMs: number;
  _savedTasksSerialized: string | null;

  constructor(editor: EditorHost) {
    this.editor = editor;
    this.routes = [];
    this.currentRoute = null;
    this.currentRouteStatus = null;
    this.viabilityResults = new Map(); // row index -> viability result
    this.locationHighlightTimeout = null;
    this._waypointReachablePositions = null;
    this._assignForce = false;
    this._assignTileTrainMap = null;
    this._historyExpanded = true;
    this._historyData = null;
    this._historyMaxRunsPerTrain = 10;
    this._taskExpanded = new WeakMap();
    this._routeSyncInterval = null;
    this._routeSyncIntervalMs = 500;
    this._routesListInterval = null;
    this._routesListIntervalMs = 1500;
    this._savedTasksSerialized = null;
  }

  get api(): any {
    return this.editor.api;
  }
  get renderer(): any {
    return this.editor.renderer;
  }
  get cachedNetwork(): any {
    return this.editor.cachedNetwork;
  }

  rerenderRouteEditor(): void {
    const content = this.buildRouteEditorHTML();
    this.editor.showSecondarySidebar('Route Editor', content);
    document.getElementById('secondary-sidebar')?.classList.add('route-editor-wide');
    setTimeout(() => this.initRouteEditorHandlers(), 0);
  }

  /** Start the route-editor's server-sync poller. */
  _startRouteSyncPoller(): void {
    this._stopRouteSyncPoller();
    // The editor and the list poller never run at the same time.
    this._stopRoutesListPoller();
    if (!this.currentRoute?.route_id) return;
    this._routeSyncInterval = setInterval(
      () =>
        this._syncRouteFromServer().catch((err) => {
          console.warn('[RouteEditor] route sync failed:', err);
        }),
      this._routeSyncIntervalMs
    );
  }

  _stopRouteSyncPoller(): void {
    if (this._routeSyncInterval) {
      clearInterval(this._routeSyncInterval);
      this._routeSyncInterval = null;
    }
  }

  /** Start the routes-list background poller. Keeps the list live (active
   *  state, assigned trains, task counts) without a manual refresh. */
  _startRoutesListPoller(): void {
    this._stopRoutesListPoller();
    // The list and the per-route editor poller never run at the same time.
    this._stopRouteSyncPoller();
    this._routesListInterval = setInterval(
      () =>
        this._syncRoutesListFromServer().catch((err) => {
          console.debug('[RouteEditor] routes-list sync failed:', err);
        }),
      this._routesListIntervalMs
    );
  }

  _stopRoutesListPoller(): void {
    if (this._routesListInterval) {
      clearInterval(this._routesListInterval);
      this._routesListInterval = null;
    }
  }

  /** Pull the latest routes and re-render only when something the list
   *  shows actually changed. Skips the DOM swap while the user is hovering
   *  the list so a re-render can't steal an Edit/Delete click. */
  async _syncRoutesListFromServer(): Promise<void> {
    // Panel changed under us (navigated to an editor / closed) — stop.
    // Checked before the tab-visibility gate so a stale poller is cleared
    // even while the tab is backgrounded.
    if (this.editor._currentEditorType !== 'routes-list') {
      this._stopRoutesListPoller();
      return;
    }
    // Tab hidden — pause fetching but keep the timer alive so it resumes
    // promptly when the tab is foregrounded again.
    if (typeof document !== 'undefined' && document.hidden) return;
    if (this._isRoutesListHovered()) return;

    const result = await this.api.getRoutes();
    const next = Array.isArray(result) ? result : result?.routes || [];
    if (this._routesListSignature(next) === this._routesListSignature(this.routes)) {
      return;
    }
    this.routes = next;
    this._renderRoutesListSidebar();
  }

  /** Signature of just the fields the routes list renders, so unrelated
   *  payload churn doesn't trigger a needless re-render. */
  _routesListSignature(routes: any[]): string {
    return JSON.stringify(
      (routes || []).map((r) => [
        r.route_id,
        r.is_active,
        r.name,
        r.mode,
        r.assigned_train_ids || [],
        r.tasks?.length || 0,
      ])
    );
  }

  /** True when the cursor is over the open routes list. */
  _isRoutesListHovered(): boolean {
    const el = document.querySelector('.routes-panel');
    if (!el || typeof el.matches !== 'function') return false;
    try {
      return el.matches(':hover');
    } catch (_) {
      return false;
    }
  }

  /** Called by `editor-ui.cleanupSecondaryPanelSubscriptions` when the
   *  secondary sidebar hides or switches to a different panel. */
  _cleanup(): void {
    this._stopRouteSyncPoller();
    this._stopRoutesListPoller();
  }

  /** Pull server-truth for the currently-open route. */
  async _syncRouteFromServer(): Promise<void> {
    const routeId = this.currentRoute?.route_id;
    if (!routeId) return;
    if (
      this.editor._currentEditorType !== 'route-editor' ||
      this.editor._currentEditorId !== routeId
    ) {
      // Panel changed under us — stop polling and bail.
      this._stopRouteSyncPoller();
      return;
    }
    let route;
    let status;
    try {
      [route, status] = await Promise.all([
        this.api.getRoute(routeId),
        this.api.getRouteStatus(routeId),
      ]);
    } catch (err: any) {
      if (err && err.status === 404) {
        // Route was deleted server-side; stop polling.
        this._stopRouteSyncPoller();
      }
      throw err;
    }
    const changed =
      this.currentRoute.is_active !== route.is_active ||
      this.currentRoute.started_at !== route.started_at ||
      !this._arraysEqual(
        this.currentRoute.assigned_train_ids || [],
        route.assigned_train_ids || []
      ) ||
      JSON.stringify(this.currentRouteStatus?.trains) !== JSON.stringify(status?.trains);
    // Sync only server-controlled fields.
    this.currentRoute.is_active = route.is_active;
    this.currentRoute.started_at = route.started_at;
    this.currentRoute.assigned_train_ids = route.assigned_train_ids;
    this.currentRouteStatus = status;
    if (changed) {
      this.rerenderRouteEditor();
    }
  }

  _arraysEqual(a: any, b: any): boolean {
    if (a === b) return true;
    if (!Array.isArray(a) || !Array.isArray(b)) return false;
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) {
      if (a[i] !== b[i]) return false;
    }
    return true;
  }

  /** HTML-escape a string for safe embedding. */
  _esc(s: any): string {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /** Snapshot the currently-loaded tasks as the "saved baseline". */
  _refreshSavedTasksBaseline(): void {
    const tasks = (this.currentRoute && this.currentRoute.tasks) || [];
    this._savedTasksSerialized = JSON.stringify(tasks);
  }

  /** Return true when the editor's local task list differs from the saved version. */
  _hasUnsavedTaskChanges(): boolean {
    if (!this.currentRoute) return false;
    if (this._savedTasksSerialized === null) {
      return (this.currentRoute.tasks || []).length > 0;
    }
    const current = JSON.stringify(this.currentRoute.tasks || []);
    return current !== this._savedTasksSerialized;
  }

  /** Save-and-continue gate for actions that depend on server-side task state. */
  async _promptSaveIfDirty(actionLabel: string): Promise<boolean> {
    if (!this._hasUnsavedTaskChanges()) return true;
    const localCount = (this.currentRoute.tasks || []).length;
    const confirmed = await showConfirmModal({
      title: `${actionLabel}: unsaved changes`,
      message: 'Save your task edits before continuing?',
      detail:
        `You have unsaved task changes in this panel (${localCount} ` +
        `task${localCount === 1 ? '' : 's'} locally). The server ` +
        `still has the previous version, so ${actionLabel.toLowerCase()} ` +
        `would otherwise act on stale data. "Save and continue" ` +
        `pushes your edits then runs ${actionLabel.toLowerCase()}.`,
      confirmText: 'Save and continue',
      confirmVariant: 'primary',
    });
    if (!confirmed) return false;
    const ok = await this.saveRoute();
    return !!ok;
  }

  /** Show the routes list panel. */
  async showRoutesPanel(): Promise<void> {
    // Leaving the per-route editor — stop the server-sync poller.
    this._stopRouteSyncPoller();

    this.editor._currentEditorType = 'routes-list';
    this.editor._currentEditorId = null;

    try {
      const result = await this.api.getRoutes();
      // Handle both array response and {routes: [...]} response
      this.routes = Array.isArray(result) ? result : result?.routes || [];
    } catch (err: any) {
      console.error('[RouteEditor] Failed to load routes:', err);
      showToast('Failed to load routes: ' + err.message, 'error', 5000);
      this.routes = [];
    }

    this._renderRoutesListSidebar();
    this._startRoutesListPoller();
  }

  /** Re-render the routes list from `this.routes` without re-fetching.
   *  Shared by showRoutesPanel and the background list poller so the
   *  list stays live without a manual refresh button. */
  _renderRoutesListSidebar(): void {
    let content = `
            <div class="routes-panel">
                <div class="routes-toolbar mb-16">
                    <button id="create-route-btn" class="btn btn-primary btn-sm flex-1">
                        + New Route
                    </button>
                </div>
        `;

    if (this.routes.length === 0) {
      content += `
                <div class="empty-state">
                    <div class="mb-8">No routes defined</div>
                    <div class="text-sm">Click "New Route" to create a train route</div>
                </div>
            `;
    } else {
      content += '<div class="routes-list">';
      for (const route of this.routes) {
        content += this.renderRouteListItem(route);
      }
      content += '</div>';
    }

    content += '</div>';

    this.editor.showSecondarySidebar('Routes', content);
    setTimeout(() => this.initRoutesPanelHandlers(), 0);
  }

  renderRouteListItem(route: any): string {
    const isActive = route.is_active;
    const statusText = isActive ? 'ACTIVE' : 'INACTIVE';
    const accent = isActive ? 'success' : 'muted';
    const trainIds = Array.isArray(route.assigned_train_ids) ? route.assigned_train_ids : [];
    const trainCount = trainIds.length;
    const taskCount = route.tasks?.length || 0;
    const modeText = (route.mode || 'one_off').replaceAll('_', ' ');
    const routeName = route.name || route.route_id;
    const trainsChip =
      trainCount === 0 ? 'no trains' : trainCount === 1 ? '1 train' : `${trainCount} trains`;
    const trainsTitle = trainIds.join(', ') || 'no trains assigned';

    // Reuses the canonical .rr-card list-card shell (DESIGN_LANGUAGE.md §11),
    // matching the active-paths list (.path-row): a title row (route name +
    // flat-ghost Edit/Delete) then a chip row (state · mode · trains · tasks).
    // The §6 status accent is the 3px left border via .accent-{success,muted};
    // the route name wraps because it is a display name (§5 "Overflow").
    return `
            <div class="rr-card route-row accent-${accent}" data-route-id="${this._esc(route.route_id)}">
                <div class="rr-card-head">
                    <div class="rr-card-title-col">
                        <div class="route-row-title">${this._esc(routeName)}</div>
                    </div>
                    <div class="route-row-actions">
                        <button class="rr-flat-btn edit-route-btn" data-route-id="${this._esc(route.route_id)}">Edit</button>
                        <button class="rr-flat-btn rr-flat-btn-danger delete-route-btn" data-route-id="${this._esc(route.route_id)}">Delete</button>
                    </div>
                </div>
                <div class="chip-row">
                    <span class="chip chip-state chip-${accent}">${statusText}</span>
                    <span class="chip uppercase">${this._esc(modeText)}</span>
                    <span class="chip" title="${this._esc(trainsTitle)}">${trainsChip}</span>
                    <span class="chip">${taskCount} task${taskCount !== 1 ? 's' : ''}</span>
                </div>
            </div>
        `;
  }

  initRoutesPanelHandlers(): void {
    const createBtn = document.getElementById('create-route-btn') as HTMLButtonElement | null;
    if (createBtn) {
      createBtn.onclick = () => this.createNewRoute();
    }

    const editBtns = document.querySelectorAll<HTMLElement>('.edit-route-btn');
    editBtns.forEach((btn) => {
      btn.onclick = () => {
        const routeId = btn.dataset.routeId;
        this.showRouteEditor(routeId);
      };
    });

    const deleteBtns = document.querySelectorAll<HTMLElement>('.delete-route-btn');
    deleteBtns.forEach((btn) => {
      btn.onclick = async () => {
        const routeId = btn.dataset.routeId;
        const route = this.routes.find((r) => r.route_id === routeId);
        const confirmed = await showConfirmModal({
          title: 'Delete Route',
          message: `Delete route "${route?.name || routeId}"?`,
          detail: 'This removes its tasks and train assignments.',
          confirmText: 'Delete',
          confirmVariant: 'danger',
        });
        if (!confirmed) return;

        try {
          await this.api.deleteRoute(routeId);
          showToast('Route deleted', 'success', 3000);
          await this.showRoutesPanel();
        } catch (err: any) {
          console.error('[RouteEditor] Failed to delete route:', err);
          showToast('Failed to delete route: ' + err.message, 'error', 5000);
        }
      };
    });
  }

  /** Create a new route (inactive, empty) and open the editor. */
  async createNewRoute(): Promise<void> {
    const routeId = `ROUTE_${Date.now()}`;
    const newRoute = {
      route_id: routeId,
      name: `Route ${(this.routes?.length || 0) + 1}`,
      mode: 'one_off',
      tasks: [],
      assigned_train_ids: [],
      is_active: false,
    };

    try {
      await this.api.createRoute(newRoute);
      showToast('Route created', 'success', 3000);
      await this.showRouteEditor(routeId);
    } catch (err: any) {
      console.error('[RouteEditor] Failed to create route:', err);
      showToast('Failed to create route: ' + err.message, 'error', 5000);
    }
  }

  /** Show the route editor for a specific route. */
  async showRouteEditor(routeId: any): Promise<void> {
    this.editor._currentEditorType = 'route-editor';
    this.editor._currentEditorId = routeId;

    try {
      this.currentRoute = await this.api.getRoute(routeId);
      this.currentRouteStatus = await this.api.getRouteStatus(routeId);
    } catch (err: any) {
      if (err && err.status === 404) {
        // Stale tab — the route was removed. Close it silently.
        console.warn(`[RouteEditor] Route ${routeId} no longer exists; closing stale tab.`);
        this.editor._currentEditorType = null;
        this.editor._currentEditorId = null;
        const ui = this.editor.uiHandler;
        if (ui) {
          const staleTab = (ui.secondaryTabs || []).find(
            (tab: any) => tab.editorType === 'route-editor' && tab.editorId === routeId
          );
          if (staleTab) {
            await ui.closeSecondaryTab(staleTab.tabId);
          } else {
            await ui.hideSecondarySidebar();
          }
        }
        return;
      }
      console.error('[RouteEditor] Failed to load route:', err);
      showToast('Failed to load route', 'error', 5000);
      return;
    }

    if (!Array.isArray(this.currentRoute.tasks)) {
      this.currentRoute.tasks = [];
    }

    // Take the saved-tasks baseline so subsequent edits register as dirty.
    this._refreshSavedTasksBaseline();

    // Check viability for all consecutive movement-task pairs
    await this.checkAllViability();

    this.rerenderRouteEditor();
    this._startRouteSyncPoller();
  }

  /** Build the route editor HTML. */
  buildRouteEditorHTML(): string {
    const route = this.currentRoute;
    const isActive = route.is_active;
    const tasks = route.tasks || [];

    let tasksHtml = '';
    if (tasks.length > 0) {
      tasksHtml = '<div class="waypoints-list">';
      tasks.forEach((task: any, index: number) => {
        tasksHtml += this.renderTaskRow(task, index);
      });
      tasksHtml += '</div>';
    } else {
      tasksHtml = `
                <div class="empty-state">
                    Click "+ Add Task" to add a step, or "Add on map" then click tiles on the layout.
                </div>
            `;
    }

    const trainAssignmentHtml = this.renderTrainAssignmentSection();

    const isDirty = this._hasUnsavedTaskChanges();
    return `
            <div class="route-editor" data-route-id="${route.route_id}">
                <div class="re-toolbar">
                    <div class="re-toolbar-spacer">
                        ${
                          isDirty
                            ? `<span class="re-toolbar-dirty" title="You have unsaved task edits in this panel.">● unsaved</span>`
                            : ''
                        }
                    </div>
                    <button id="save-route-btn"
                            class="btn btn-primary btn-sm ${isDirty ? 're-toolbar-save-dirty' : ''}"
                            title="Save name, mode, tasks, and policy edits">
                        Save${isDirty ? ' *' : ''}
                    </button>
                </div>

                <div class="mb-16">
                    ${this.renderRouteHeader(route)}
                </div>

                <div class="control-section mb-16">
                    <div class="flex justify-between items-center mb-12">
                        <h4 class="text-sm text-muted">Tasks</h4>
                        <div class="flex gap-4">
                            <button id="add-task-btn" class="btn btn-sm btn-primary">+ Add Task</button>
                            <button id="add-task-map-btn" class="btn btn-sm btn-secondary" title="Click tiles on the map to add waypoints; reachable tiles light up. Esc to finish.">Add on map</button>
                        </div>
                    </div>
                    ${tasksHtml}
                </div>

                <div class="control-section mb-16">
                    <h4 class="text-sm text-muted mb-12">Assigned Trains</h4>
                    ${trainAssignmentHtml}
                </div>

                <div class="control-section">
                    <h4 class="text-sm text-muted mb-12">Controls</h4>
                    ${this.renderRouteStatusPill()}
                    <div class="flex gap-8 mt-12">
                        ${
                          isActive
                            ? `<button id="deactivate-route-btn" class="btn btn-danger btn-sm flex-1">Deactivate</button>`
                            : `<button id="activate-route-btn" class="btn btn-success btn-sm flex-1">Activate</button>`
                        }
                        <button id="delete-route-btn" class="btn btn-danger btn-sm flex-1">Delete Route</button>
                    </div>
                </div>

                ${this.renderHistorySection()}
            </div>
        `;
  }

  renderHistorySection(): string {
    return `
            <div class="control-section mt-16">
                <div class="flex justify-between items-center mb-12">
                    <h4 class="text-sm text-muted">History</h4>
                    <button id="toggle-history-btn" class="btn btn-sm btn-secondary">${this._historyExpanded ? 'Hide' : 'Show'}</button>
                </div>
                <div id="route-history-body" class="route-history">${this._historyExpanded ? this.renderHistoryBody() : ''}</div>
            </div>
        `;
  }

  /** Background-fetch the history payload if the section is open and empty. */
  async _maybeAutoLoadHistory(): Promise<void> {
    if (!this._historyExpanded) return;
    if (this._historyData != null) return;
    const body = document.getElementById('route-history-body');
    if (!body) return;
    try {
      this._historyData = await this.api.getRouteHistory(this.currentRoute.route_id);
    } catch (err) {
      console.error('[RouteEditor] history auto-load failed:', err);
      body.innerHTML = '<div class="empty-state">Failed to load history</div>';
      return;
    }
    body.innerHTML = this.renderHistoryBody();
  }

  /** Toggle the history section; fetch on expand. */
  async toggleHistory(): Promise<void> {
    this._historyExpanded = !this._historyExpanded;
    const btn = document.getElementById('toggle-history-btn');
    const body = document.getElementById('route-history-body');
    if (btn) btn.textContent = this._historyExpanded ? 'Hide' : 'Show';
    if (!body) return;
    if (!this._historyExpanded) {
      body.innerHTML = '';
      return;
    }
    body.innerHTML = '<div class="empty-state">Loading…</div>';
    try {
      this._historyData = await this.api.getRouteHistory(this.currentRoute.route_id);
    } catch (err) {
      console.error('[RouteEditor] history load failed:', err);
      body.innerHTML = '<div class="empty-state">Failed to load history</div>';
      return;
    }
    body.innerHTML = this.renderHistoryBody();
  }

  renderHistoryBody(): string {
    const data = this._historyData;
    if (!data) return '<div class="empty-state">Loading…</div>';

    const metaRows = [];
    if (data.created_at != null) {
      metaRows.push(`<div>Created ${this._formatTime(data.created_at)}</div>`);
    }
    if (data.last_edited_at != null) {
      metaRows.push(`<div>Last edited ${this._formatTime(data.last_edited_at)}</div>`);
    }
    metaRows.push(
      `<div>${
        data.activated_at != null
          ? `Last activated ${this._formatTime(data.activated_at)}`
          : 'Never activated'
      }</div>`
    );

    const allRuns = (data.runs || []).slice().reverse(); // newest first
    const perTrainCap = this._historyMaxRunsPerTrain;
    const seenPerTrain = new Map<string, number>();
    const capped = [];
    let truncatedCount = 0;
    for (const run of allRuns) {
      const tid = run.train_id || '<unknown>';
      const used = seenPerTrain.get(tid) || 0;
      if (used >= perTrainCap) {
        truncatedCount += 1;
        continue;
      }
      seenPerTrain.set(tid, used + 1);
      capped.push(run);
    }
    const runs = capped;
    const truncatedHtml =
      truncatedCount > 0
        ? `<div class="empty-state">+ ${truncatedCount} older run${truncatedCount === 1 ? '' : 's'} hidden (top ${perTrainCap} shown per train)</div>`
        : '';
    const runsHtml =
      runs.length === 0
        ? '<div class="empty-state">No runs yet</div>'
        : runs.map((run) => this.renderHistoryRun(run)).join('');
    return `
            <div class="route-history-activated">${metaRows.join('')}</div>
            ${runsHtml}
            ${truncatedHtml}
        `;
  }

  renderHistoryRun(run: any): string {
    const statusClass =
      run.status === 'FINISHED'
        ? 'is-finished'
        : run.status === 'DEACTIVATED'
          ? 'is-deactivated'
          : 'is-running';
    const tasksHtml = (run.tasks || [])
      .map((t: any) => {
        const task = this.currentRoute?.tasks?.[t.task_index];
        let targetName = `Task ${t.task_index + 1}`;
        if (task) {
          const loc = task.task_location || {};
          const type = 'block_id' in loc ? 'block' : 'station_id' in loc ? 'station' : 'platform';
          const id = loc.block_id || loc.station_id || loc.platform_id || '';
          targetName = this.getTargetDisplayName(type, id) || id || targetName;
        }
        const finished = t.finished_at != null;
        const timeText = finished ? this._formatTime(t.finished_at) : '—';
        const delayChip =
          finished && t.delay_class
            ? `<span class="chip chip-state ${(({ early: 'chip-info', on_time: 'chip-success', late: 'chip-warn' }) as Record<string, string>)[t.delay_class] || 'chip-muted'}">${t.delay_class.replace('_', '-')}</span>`
            : '';
        return `
                <div class="route-history-task ${finished ? '' : 'is-pending'}">
                    <span class="route-history-task-name">${t.task_index + 1}. ${targetName}</span>
                    <span class="route-history-task-meta">${delayChip}<span class="route-history-time">${timeText}</span></span>
                </div>`;
      })
      .join('');
    return `
            <div class="route-history-run">
                <div class="route-history-run-head">
                    <span class="chip chip-state chip-${statusClass === 'is-finished' ? 'success' : statusClass === 'is-deactivated' ? 'muted' : 'info'}">${run.status}</span>
                    <span class="font-mono text-xs">${run.train_id}</span>
                    <span class="text-xs text-muted">${this._formatTime(run.started_at)}</span>
                </div>
                <div class="route-history-tasks">${tasksHtml}</div>
            </div>
        `;
  }

  /** Format a timestamp. */
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

  renderRouteHeader(route: any): string {
    const modeOptions = [
      { value: 'one_off', label: 'One-off', glyph: '<i class="ti ti-arrow-narrow-right"></i>' },
      { value: 'circular', label: 'Circular', glyph: '<i class="ti ti-rotate"></i>' },
      { value: 'round_trip', label: 'Round-trip', glyph: '<i class="ti ti-arrows-exchange"></i>' },
    ];

    const modeSeg = modeOptions
      .map(
        (opt) => `
            <button type="button"
                    class="switch-btn ${route.mode === opt.value ? 'active' : ''}"
                    data-mode="${opt.value}" title="${opt.label}">
                <span class="route-seg-glyph">${opt.glyph}</span>
                <span class="route-seg-label">${opt.label}</span>
            </button>
        `
      )
      .join('');

    const taskCount = route.tasks?.length || 0;
    const assignedCount = route.assigned_train_ids?.length || 0;

    return `
            <div class="route-editor-header">
                <div class="route-header-row">
                    <div class="route-header-col">
                        <label class="property-label">Route Name</label>
                        <input type="text" id="route-name-input" class="property-input"
                               value="${route.name || ''}" placeholder="Enter name">
                    </div>
                </div>

                <div class="route-header-row">
                    <div class="route-header-col">
                        <label class="property-label">Mode</label>
                        <div id="route-mode-seg" class="switch-group compact route-seg">
                            ${modeSeg}
                        </div>
                    </div>
                    <div class="route-header-col narrow info-display-small">
                         <label class="property-label info-label">Statistics</label>
                         <div class="route-stats-value info-meta">
                            ${taskCount} task${taskCount !== 1 ? 's' : ''} · ${assignedCount} train${assignedCount !== 1 ? 's' : ''}
                         </div>
                    </div>
                </div>

                ${this.renderRoutePolicyRow(route)}
                ${this.renderRouteStartModeRow(route)}
            </div>
        `;
  }

  /** `<option>`s for each `stop` task in the route, for a start-stop picker. */
  _renderStopOptions(route: any, selectedIndex: number | null): string {
    const tasks = (route && route.tasks) || [];
    let html = '';
    tasks.forEach((t: any, i: number) => {
      if (t.task_kind !== 'stop') return;
      const loc = t.task_location || {};
      let label = '';
      if (loc.block_id) label = this.getTargetDisplayName('block', loc.block_id);
      else if (loc.station_id) label = this.getTargetDisplayName('station', loc.station_id);
      else if (loc.platform_id) label = this.getTargetDisplayName('platform', loc.platform_id);
      const sel = selectedIndex === i ? 'selected' : '';
      html += `<option value="${i}" ${sel}>Stop ${i + 1}${label ? ': ' + label : ''}</option>`;
    });
    return html || '<option value="">(no stops in route)</option>';
  }

  /**
   * Route-level default for which stop a freshly-assigned train begins at
   * (per design_new.md "Run start-mode seeding"). FIRST keeps the current
   * behavior; NEAREST auto-picks the closest non-occupied stop; SPECIFIC uses
   * the chosen stop. This is a route default — a per-assignment override is
   * offered in the train-assignment section.
   */
  renderRouteStartModeRow(route: any): string {
    const start = route.start_mode || 'first';
    const isSpecific = start === 'specific';
    const seg = ['first', 'nearest', 'specific']
      .map((v) => {
        const label = v === 'first' ? 'First stop' : v === 'nearest' ? 'Nearest' : 'Specific';
        return `<button type="button" class="switch-btn ${v === start ? 'active' : ''}"
                        data-value="${v}" data-seg="route-start-mode-seg">${label}</button>`;
      })
      .join('');
    return `
            <div class="route-header-row" data-route-start-mode-row>
                <div class="route-header-col">
                    <label class="property-label">Start at</label>
                    <div id="route-start-mode-seg" class="switch-group compact route-seg" role="radiogroup">
                        ${seg}
                    </div>
                    <div class="text-xxs text-muted mt-4">Which stop a freshly-assigned train begins from.</div>
                </div>
                <div class="route-header-col" id="route-start-specific-col" style="${isSpecific ? '' : 'display:none'}">
                    <label class="property-label">Specific stop</label>
                    <select id="route-start-task-index-select" class="property-input">
                        ${this._renderStopOptions(route, Number.isInteger(route.start_task_index) ? route.start_task_index : null)}
                    </select>
                </div>
            </div>
        `;
  }

  renderRoutePolicyRow(route: any): string {
    const priority = Number.isInteger(route.priority) ? route.priority : 5;
    const acq = route.acquisition_mode || 'best_effort';
    const seg = (id: string, options: Array<{ value: string; label: string }>, current: string) => {
      return `
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
    };
    const pushSeg = (id: string, current: string) =>
      seg(
        id,
        [
          { value: 'soft', label: 'Soft' },
          { value: 'force', label: 'Force' },
        ],
        current || 'soft'
      );
    return `
            <div class="route-header-row route-policy-grid" data-route-policy-row>
                <div class="route-header-col">
                    <label class="property-label">Acquisition</label>
                    ${seg(
                      'route-acq-seg',
                      [
                        { value: 'best_effort', label: 'Best effort' },
                        { value: 'all_or_nothing', label: 'All or nothing' },
                      ],
                      acq
                    )}
                    <div class="text-xxs text-muted mt-4">Push to runs:</div>
                    ${pushSeg('route-acq-push-seg', 'soft')}
                </div>
                <div class="route-header-col">
                    <label class="property-label">Priority (0-9)</label>
                    <input type="number" id="route-priority-input" class="property-input"
                           min="0" max="9" step="1" value="${priority}">
                    <div class="text-xxs text-muted mt-4">Push to runs:</div>
                    ${pushSeg('route-priority-push-seg', 'soft')}
                </div>
            </div>
        `;
  }

  renderRouteStatusPill(): string {
    if (!this.currentRoute?.is_active) {
      return `<div class="route-status-box is-inactive"><span class="route-status-dot"></span>Inactive</div>`;
    }
    const trainCount = (this.currentRouteStatus?.trains || []).length;
    if (trainCount === 0) {
      return `<div class="route-status-box is-info"><span class="route-status-dot"></span>Active · no trains</div>`;
    }
    return `<div class="route-status-box is-active"><span class="route-status-dot"></span>Active · ${trainCount} train${trainCount !== 1 ? 's' : ''}</div>`;
  }

  /** Render one task row (1:1 with a RouteTask). */
  renderTaskRow(task: any, index: number): string {
    const kind = task.task_kind || 'waypoint';
    const loc = task.task_location || {};
    const targetType = 'block_id' in loc ? 'block' : 'station_id' in loc ? 'station' : 'platform';
    const targetId = loc.block_id || loc.platform_id || loc.station_id || '';
    const direction = task.task_direction || 'either';
    const isDwell = kind === 'dwell';
    const isExpanded = this._taskExpanded.has(task);
    const lastIndex = (this.currentRoute.tasks?.length || 1) - 1;

    const KIND_ICON: Record<string, string> = { waypoint: 'arrow-narrow-right', stop: 'map-pin', dwell: 'clock' };
    const TYPE_ICON: Record<string, string> = { block: 'rectangle', platform: 'train', station: 'home' };
    const DIR_ICON: Record<string, string> = { forward: 'arrow-narrow-right', reverse: 'arrow-narrow-left', either: 'arrows-horizontal' };
    const KIND_LABEL: Record<string, string> = { waypoint: 'Waypoint', stop: 'Stop', dwell: 'Dwell' };
    const TYPE_LABEL: Record<string, string> = { block: 'Block', platform: 'Platform', station: 'Station' };
    const DIR_LABEL: Record<string, string> = { forward: 'Forward', reverse: 'Reverse', either: 'Either' };

    const ti = (name: string) => `<i class="ti ti-${name}"></i>`;

    const renderSeg = (
      segClass: string,
      choices: string[],
      current: string,
      dataKey: string,
      iconMap: Record<string, string>,
      labelMap: Record<string, string>
    ) => {
      const items = choices
        .map((v) => {
          const icon = iconMap[v] ? ti(iconMap[v]) : '';
          const label = labelMap ? `<span class="route-seg-label">${labelMap[v]}</span>` : '';
          return `<button type="button" class="switch-btn ${current === v ? 'active' : ''}"
                                 data-index="${index}" data-${dataKey}="${v}" title="${labelMap?.[v] || v}">
                            ${icon}${label}
                        </button>`;
        })
        .join('');
      return `<div class="switch-group compact route-seg ${segClass}" data-index="${index}">${items}</div>`;
    };

    const kindSeg = renderSeg('task-kind-seg', ['waypoint', 'stop', 'dwell'], kind, 'kind', KIND_ICON, KIND_LABEL);
    const typeSeg = renderSeg('task-target-type-seg', ['block', 'platform', 'station'], targetType, 'type', TYPE_ICON, TYPE_LABEL);
    const dirSeg = renderSeg('task-direction-seg', ['forward', 'reverse', 'either'], direction, 'direction', DIR_ICON, DIR_LABEL);

    // Viability badge.
    let viabilityHtml = '';
    if (!isDwell) {
      const viability = this.viabilityResults.get(index);
      if (viability !== undefined) {
        if (viability.viable) {
          viabilityHtml = `<span class="task-summary-viability text-success" title="Path viable: ${viability.hop_count} blocks">✓ ${viability.hop_count}</span>`;
        } else {
          viabilityHtml = `<span class="task-summary-viability text-danger" title="${viability.error || 'No path'}">✗</span>`;
        }
      }
    }

    const targetName = isDwell
      ? 'Dwell'
      : this.getTargetDisplayName(targetType, targetId) || targetId || 'No target';
    // Per design_new.md "Unreachable and broken task handling": red cross when
    // the target ID is missing from the network (broken), grey when the target
    // exists but is disabled/manual (unavailable).
    const targetStatus = isDwell ? 'ok' : this.getTargetStatus(targetType, targetId);
    const hasTime = task.task_time !== null && task.task_time !== undefined && task.task_time !== '';
    const timeText = hasTime ? `${task.task_time}s ${isDwell ? 'dwell' : 'travel'}` : '';

    const summaryHtml = `
            <button type="button" class="task-summary" data-index="${index}" aria-expanded="${isExpanded}">
                <span class="waypoint-ordinal">${index + 1}</span>
                <span class="chip chip-state ${(({ waypoint: 'chip-info', stop: 'chip-accent', dwell: 'chip-warn' }) as Record<string, string>)[kind] || 'chip-muted'} task-summary-chip">
                    ${ti(KIND_ICON[kind])}
                    <span>${KIND_LABEL[kind]}</span>
                </span>
                ${
                  isDwell
                    ? ''
                    : `
                    <span class="chip task-summary-chip task-summary-target task-target-${targetStatus}" title="${targetStatus === 'missing' ? 'Target missing from network — task will be skipped' : targetStatus === 'unavailable' ? 'Target disabled or set to manual — train will wait' : targetName}">
                        ${targetStatus === 'missing' ? ti('circle-x') : ti(TYPE_ICON[targetType])}
                        <span class="task-summary-target-name">${targetName}</span>
                    </span>
                    <span class="chip task-summary-chip task-summary-direction">
                        ${ti(DIR_ICON[direction])}
                        <span>${DIR_LABEL[direction]}</span>
                    </span>
                `
                }
                ${timeText ? `<span class="task-summary-time">${timeText}</span>` : ''}
                ${viabilityHtml}
                <span class="task-summary-toggle">${ti(isExpanded ? 'chevron-down' : 'chevron-right')}</span>
            </button>
        `;

    if (!isExpanded) {
      return `<div class="waypoint-item waypoint-card" data-index="${index}">${summaryHtml}</div>`;
    }

    const targetRow = isDwell
      ? `<div class="task-dwell-target-note">Holds at previous task's target</div>`
      : `
                <div class="task-field-row">
                    <label>Type</label>
                    ${typeSeg}
                </div>
                <div class="task-field-row">
                    <label>Target</label>
                    <select class="property-input input-compact task-target-id-select" data-index="${index}">${this.renderTargetOptions(targetType, targetId)}</select>
                    <button class="btn btn-sm btn-secondary btn-icon-only locate-task-btn" data-index="${index}" title="Locate on map" aria-label="Locate on map">${ti('target')}</button>
                    <button class="btn btn-sm btn-secondary btn-icon-only task-target-pick-btn" data-index="${index}" title="Pick element from map" aria-label="Pick element from map">${ti('pointer')}</button>
                </div>`;

    const timeLabel = isDwell ? 'Dwell' : 'Travel';

    return `
            <div class="waypoint-item waypoint-card" data-index="${index}">
                ${summaryHtml}
                <div class="task-body">
                    <div class="task-tier task-tier-primary">
                        <div class="task-field-row">
                            <label>Kind</label>
                            ${kindSeg}
                        </div>
                        ${targetRow}
                    </div>

                    <div class="task-tier task-tier-secondary">
                        <div class="task-field-row">
                            <label>Direction</label>
                            ${dirSeg}
                        </div>
                        <div class="task-field-row task-field-row-times">
                            <label>${timeLabel}</label>
                            <input type="number" min="0" step="1"
                                   class="property-input input-compact task-time-input"
                                   data-index="${index}" value="${task.task_time ?? ''}"
                                   placeholder="seconds">
                            <label class="task-sublabel">Depart</label>
                            <input type="number" min="0" step="1"
                                   class="property-input input-compact task-target-time-input"
                                   data-index="${index}" value="${task.target_time ?? ''}"
                                   placeholder="sim time">
                        </div>
                    </div>

                    <div class="task-actions-row">
                        <button class="btn btn-sm btn-secondary btn-icon-only move-task-up" data-index="${index}" ${index === 0 ? 'disabled' : ''} title="Move up" aria-label="Move up">${ti('arrow-up')}</button>
                        <button class="btn btn-sm btn-secondary btn-icon-only move-task-down" data-index="${index}" ${index >= lastIndex ? 'disabled' : ''} title="Move down" aria-label="Move down">${ti('arrow-down')}</button>
                        <button class="btn btn-sm btn-danger delete-task" data-index="${index}" title="Remove task">${ti('trash')} Delete</button>
                    </div>
                </div>
            </div>
        `;
  }

  /** Build <option> markup for the target dropdown of a given target type. */
  renderTargetOptions(targetType: string, selectedId: string): string {
    let items: Array<{ id: string; name: string }> = [];
    if (targetType === 'block') {
      items = this.getBlocks().map((b: any) => ({ id: b.block_id, name: b.name || b.block_id }));
    } else if (targetType === 'station') {
      items = this.getStationGroups().map((s: any) => ({ id: s.station_id, name: s.name || s.station_id }));
    } else {
      items = this.getPlatforms().map((p: any) => ({ id: this.getPlatformId(p), name: p.name || this.getPlatformId(p) }));
    }

    let html = '<option value="">Select…</option>';
    let found = false;
    items.forEach((it) => {
      if (!it.id) return;
      const sel = it.id === selectedId ? 'selected' : '';
      if (sel) found = true;
      html += `<option value="${it.id}" ${sel}>${it.name}</option>`;
    });
    if (selectedId && !found) {
      html += `<option value="${selectedId}" selected>${selectedId} (?)</option>`;
    }
    return html;
  }

  getBlocks(): any[] {
    const blocks = this.cachedNetwork?.blocks;
    if (Array.isArray(blocks)) return blocks;
    if (blocks && typeof blocks === 'object') return Object.values(blocks);
    return [];
  }

  getPlatforms(): any[] {
    if (Array.isArray(this.cachedNetwork?.platforms)) {
      return this.cachedNetwork.platforms;
    }
    if (this.cachedNetwork?.platforms && typeof this.cachedNetwork.platforms === 'object') {
      return Object.values(this.cachedNetwork.platforms);
    }
    return [];
  }

  getStationGroups(): any[] {
    if (Array.isArray(this.cachedNetwork?.station_groups)) {
      return this.cachedNetwork.station_groups;
    }
    if (this.cachedNetwork?.station_groups && typeof this.cachedNetwork.station_groups === 'object') {
      return Object.values(this.cachedNetwork.station_groups);
    }
    return [];
  }

  getPlatformId(platform: any): string {
    return platform?.platform_id || platform?.station_id || '';
  }

  getPlatformInfo(platformId: string): { id: string; name: string; positions: any[] } {
    const platform = this.getPlatforms().find((p: any) => this.getPlatformId(p) === platformId);
    return {
      id: platformId || 'Unknown',
      name: platform?.name || '',
      positions: Array.isArray(platform?.positions) ? platform.positions : [],
    };
  }

  getStationGroupForPlatform(platformId: string): any {
    return (
      this.getStationGroups().find(
        (group: any) =>
          Array.isArray(group?.platform_ids) && group.platform_ids.includes(platformId)
      ) || null
    );
  }

  /**
   * Status of a task's target for editor rendering, per design_new.md
   * "Unreachable and broken task handling":
   *   'missing'     — target ID is not in the network (broken → red cross)
   *   'unavailable' — target exists but is disabled/manual (grey out)
   *   'ok'          — present and available
   */
  getTargetStatus(targetType: string, targetId: string): 'ok' | 'missing' | 'unavailable' {
    if (!targetId) return 'ok';
    let entity: any;
    if (targetType === 'block') {
      entity = this.getBlocks().find((b: any) => b.block_id === targetId);
    } else if (targetType === 'station') {
      entity = this.getStationGroups().find((s: any) => s.station_id === targetId);
    } else {
      entity = this.getPlatforms().find((p: any) => this.getPlatformId(p) === targetId);
    }
    if (!entity) return 'missing';
    if (entity.disabled || entity.manual) return 'unavailable';
    return 'ok';
  }

  getTargetDisplayName(targetType: string, targetId: string): string {
    if (!targetId) return '';
    if (targetType === 'block') {
      const b = this.getBlocks().find((b: any) => b.block_id === targetId);
      return b?.name || targetId;
    }
    if (targetType === 'station') {
      const s = this.getStationGroups().find((s: any) => s.station_id === targetId);
      return s?.name || targetId;
    }
    return this.getPlatformInfo(targetId).name || targetId;
  }

  getPlatformPositionSet(platformIds: any[]): Set<string> {
    const ids = new Set(platformIds);
    const positions = new Set<string>();
    this.getPlatforms().forEach((platform: any) => {
      const platformId = this.getPlatformId(platform);
      if (!ids.has(platformId)) return;
      (platform.positions || []).forEach((pos: any) => {
        if (Array.isArray(pos) && pos.length >= 2) {
          positions.add(`${pos[0]},${pos[1]}`);
        }
      });
    });
    return positions;
  }

  getBlockPositionSet(blockId: string): Set<string> {
    const positions = new Set<string>();
    (this.cachedNetwork?.tracks || []).forEach((t: any) => {
      if (t.block_section_id === blockId) {
        positions.add(`${t.x},${t.y}`);
      }
    });
    return positions;
  }

  _applyHighlight(positions: Set<string>, autoClearMs = 0): void {
    if (this.renderer.setReachableHighlights) {
      this.renderer.setReachableHighlights(positions);
    }
    if (this.cachedNetwork) {
      this.renderer.requestRedraw();
    }
    if (this.locationHighlightTimeout) {
      clearTimeout(this.locationHighlightTimeout);
      this.locationHighlightTimeout = null;
    }
    if (autoClearMs > 0) {
      this.locationHighlightTimeout = setTimeout(() => {
        this.clearLocationHighlight();
      }, autoClearMs);
    }
  }

  clearLocationHighlight(): void {
    if (this.locationHighlightTimeout) {
      clearTimeout(this.locationHighlightTimeout);
      this.locationHighlightTimeout = null;
    }
    if (this.renderer.setReachableHighlights) {
      this.renderer.setReachableHighlights(new Set());
    }
    if (this.cachedNetwork) {
      this.renderer.requestRedraw();
    }
  }

  /** Highlight (and briefly flash) the map geometry for a task's target. */
  focusTaskLocation(index: number): void {
    const task = this.currentRoute?.tasks?.[index];
    if (!task) return;

    const loc = task.task_location || {};
    let positions = new Set<string>();
    let label = '';

    if ('block_id' in loc && loc.block_id) {
      label = this.getTargetDisplayName('block', loc.block_id);
      positions = this.getBlockPositionSet(loc.block_id);
    } else if ('station_id' in loc && loc.station_id) {
      label = this.getTargetDisplayName('station', loc.station_id);
      const group = this.getStationGroups().find((g: any) => g.station_id === loc.station_id);
      positions = this.getPlatformPositionSet(group?.platform_ids || []);
    } else if (loc.platform_id) {
      label = this.getTargetDisplayName('platform', loc.platform_id);
      positions = this.getPlatformPositionSet([loc.platform_id]);
    }

    if (!positions || positions.size === 0) {
      showToast(`No map geometry found for task ${index + 1}`, 'warning', 3000);
      return;
    }

    this._applyHighlight(positions, 5000);
    this._setStatus(`Route Editor: Highlighting task ${index + 1} (${label})`);
  }

  renderAssignedTrainRow(trainId: string, trainStatus: any): string {
    const tasks = this.currentRoute?.tasks || [];
    const finishedIdx = trainStatus?.current_waypoint_index;
    const nextIdx = trainStatus ? (finishedIdx ?? -1) + 1 : null;

    const cap = (s: any) =>
      typeof s === 'string' && s.length ? s.charAt(0).toUpperCase() + s.slice(1) : '';

    // Resolve target name from the live task definition.
    let targetName = '';
    if (trainStatus?.current_task_location) {
      const loc = trainStatus.current_task_location;
      if (loc.block_id) targetName = this.getTargetDisplayName('block', loc.block_id);
      else if (loc.station_id) targetName = this.getTargetDisplayName('station', loc.station_id);
      else if (loc.platform_id) targetName = this.getTargetDisplayName('platform', loc.platform_id);
    }

    const kind = trainStatus?.current_task_kind || null;
    const verb = kind ? kind.toUpperCase() : 'IDLE';
    const verbAccent =
      kind === 'dwell' ? 'warn' : kind === 'stop' ? 'info' : kind === 'waypoint' ? 'info' : 'muted';

    let dotVariant = '';
    if (!trainStatus) dotVariant = 'danger';
    else if (kind === 'dwell') dotVariant = 'warn';

    const modParts = [];
    if (trainStatus?.direction) modParts.push(cap(trainStatus.direction));
    if (trainStatus?.at_platform) modParts.push('at platform');
    const dwellRaw = trainStatus?.dwell_remaining_sec;
    if (dwellRaw != null && Number.isFinite(dwellRaw) && dwellRaw >= 0 && dwellRaw <= 3600) {
      modParts.push(`${Math.round(dwellRaw)}s left`);
    }
    const modifier = modParts.join(' · ');

    let nounText;
    if (!trainStatus) {
      nounText = '—';
    } else if (nextIdx === null || tasks.length === 0) {
      nounText = 'queued';
    } else {
      nounText = targetName || `task ${(nextIdx % Math.max(tasks.length, 1)) + 1}`;
    }

    let tooltip = `${trainId}`;
    if (kind) tooltip += ` · ${verb}`;
    if (targetName) tooltip += ` → ${targetName}`;
    if (modifier) tooltip += ` · ${modifier}`;
    const escTooltip = this._esc(tooltip);

    return `
            <div class="atr-row" data-train-id="${trainId}" title="${escTooltip}">
                <span class="atr-status atr-status-${dotVariant || 'ok'}"></span>
                <span class="atr-id font-mono">${this._esc(trainId)}</span>
                <span class="atr-verb atr-verb-${verbAccent}">${this._esc(verb)}</span>
                <span class="atr-noun">${this._esc(nounText)}</span>
                <span class="atr-mod">${this._esc(modifier)}</span>
                <button class="btn btn-sm btn-secondary unassign-train-btn atr-action"
                        data-train-id="${trainId}"
                        title="Unassign ${trainId} from this route">Unassign</button>
            </div>
        `;
  }

  renderTrainAssignmentSection(): string {
    const route = this.currentRoute;
    const assignedTrains = route.assigned_train_ids || [];
    const statusByTrainId = new Map<string, any>();
    (this.currentRouteStatus?.trains || []).forEach((ts: any) => {
      statusByTrainId.set(ts.train_id, ts);
    });

    let assignedHtml = '';
    if (assignedTrains.length > 0) {
      assignedHtml = '<div class="assigned-trains-list mb-12">';
      assignedTrains.forEach((trainId: string) => {
        assignedHtml += this.renderAssignedTrainRow(trainId, statusByTrainId.get(trainId));
      });
      assignedHtml += '</div>';
    } else {
      assignedHtml = '<div class="text-muted text-sm mb-12">No trains assigned</div>';
    }

    // Get available trains (not assigned to this route)
    const allTrainsRaw = Array.isArray(this.cachedNetwork?.trains)
      ? this.cachedNetwork.trains
      : Object.values(this.cachedNetwork?.trains || {});
    const availableTrains = allTrainsRaw
      .map((t: any) => ({
        train_id: t?.train_id || t?.id || '',
        model: t?.model || '',
      }))
      .filter((t: any) => t.train_id && !assignedTrains.includes(t.train_id));

    let trainSelectHtml = '<option value="">Select train...</option>';
    availableTrains.forEach((train: any) => {
      const label = train.model ? `${train.train_id} (${train.model})` : train.train_id;
      trainSelectHtml += `<option value="${train.train_id}">${label}</option>`;
    });

    return `
            ${assignedHtml}
            <div class="atr-assign-row">
                <select id="assign-train-select" class="property-input route-input-compact atr-assign-select">
                    ${trainSelectHtml}
                </select>
                <label class="checkbox-wrapper atr-force-label" title="Reassign the train even if it is already on another route">
                    <input type="checkbox" id="assign-train-force" class="checkbox-input"
                           ${this._assignForce ? 'checked' : ''}>
                    <span class="checkbox-custom"></span>
                    <span class="checkbox-label">Force</span>
                </label>
                <button id="assign-train-btn" class="btn btn-sm btn-primary">Assign</button>
            </div>
            <div class="atr-assign-row mt-4">
                <select id="assign-start-mode" class="property-input route-input-compact atr-assign-select"
                        title="Which stop this train starts from. 'Route default' uses the route's Start at setting.">
                    <option value="">Start: route default</option>
                    <option value="first">Start: first stop</option>
                    <option value="nearest">Start: nearest stop</option>
                    <option value="specific">Start: specific stop</option>
                </select>
                <select id="assign-start-task-index" class="property-input route-input-compact atr-assign-select" style="display:none">
                    ${this._renderStopOptions(route, null)}
                </select>
            </div>
            <div class="flex gap-8 mt-8">
                <button id="assign-train-map-btn" class="btn btn-sm btn-secondary flex-1" title="Click trains on the layout to assign them; eligible trains light up. Esc to finish.">Assign on map</button>
            </div>
        `;
  }

  initRouteEditorHandlers(): void {
    const saveBtn = document.getElementById('save-route-btn') as HTMLButtonElement | null;
    if (saveBtn) {
      saveBtn.onclick = () => this.saveRoute();
    }

    const deleteRouteBtn = document.getElementById('delete-route-btn') as HTMLButtonElement | null;
    if (deleteRouteBtn) {
      deleteRouteBtn.onclick = async () => {
        const confirmed = await showConfirmModal({
          title: 'Delete Route',
          message: `Delete route "${this.currentRoute?.name || this.currentRoute?.route_id}"?`,
          detail: 'This removes all tasks and train assignments for this route.',
          confirmText: 'Delete',
          confirmVariant: 'danger',
        });
        if (!confirmed) {
          return;
        }
        try {
          await this.api.deleteRoute(this.currentRoute.route_id);
          showToast('Route deleted', 'success', 3000);
          await this.showRoutesPanel();
        } catch (err: any) {
          console.error('[RouteEditor] Failed to delete route:', err);
          showToast('Failed to delete route: ' + err.message, 'error', 5000);
        }
      };
    }

    // Add task buttons
    const addTaskBtn = document.getElementById('add-task-btn') as HTMLButtonElement | null;
    if (addTaskBtn) {
      addTaskBtn.onclick = () => this.addTask();
    }
    const addTaskMapBtn = document.getElementById('add-task-map-btn') as HTMLButtonElement | null;
    if (addTaskMapBtn) {
      addTaskMapBtn.onclick = () => this.editor.enterMode(this.buildWaypointAddMode());
    }

    // Activate/Deactivate buttons
    const activateBtn = document.getElementById('activate-route-btn') as HTMLButtonElement | null;
    if (activateBtn) {
      activateBtn.onclick = () => this.activateRoute();
    }

    const deactivateBtn = document.getElementById('deactivate-route-btn') as HTMLButtonElement | null;
    if (deactivateBtn) {
      deactivateBtn.onclick = () => this.deactivateRoute();
    }

    const toggleHistoryBtn = document.getElementById('toggle-history-btn') as HTMLButtonElement | null;
    if (toggleHistoryBtn) {
      toggleHistoryBtn.onclick = () => this.toggleHistory();
    }

    this._maybeAutoLoadHistory().catch((err) => {
      console.warn('[RouteEditor] history auto-load swallowed:', err);
    });

    // Route-mode segmented control — click toggles the active button.
    document.querySelectorAll<HTMLElement>('#route-mode-seg .switch-btn').forEach((btn) => {
      btn.onclick = (e) => {
        const target = e.currentTarget as HTMLElement;
        const seg = target.parentElement;
        seg?.querySelectorAll('.switch-btn').forEach((b) => b.classList.toggle('active', b === target));
      };
    });

    // Route-policy + per-field push segmented controls.
    ['route-acq-seg', 'route-acq-push-seg', 'route-priority-push-seg'].forEach(
      (segId) => {
        document.querySelectorAll<HTMLElement>(`#${segId} .switch-btn`).forEach((btn) => {
          btn.onclick = (e) => {
            const target = e.currentTarget as HTMLElement;
            const seg = target.parentElement;
            seg?.querySelectorAll('.switch-btn').forEach((b) => b.classList.toggle('active', b === target));
          };
        });
      }
    );

    // Route start-mode segmented control — also toggles the Specific-stop col.
    document.querySelectorAll<HTMLElement>('#route-start-mode-seg .switch-btn').forEach((btn) => {
      btn.onclick = (e) => {
        const target = e.currentTarget as HTMLElement;
        const seg = target.parentElement;
        seg?.querySelectorAll('.switch-btn').forEach((b) => b.classList.toggle('active', b === target));
        const col = document.getElementById('route-start-specific-col');
        if (col) col.style.display = target.dataset.value === 'specific' ? '' : 'none';
      };
    });

    // Task reorder/delete/locate buttons
    document.querySelectorAll<HTMLElement>('.move-task-up').forEach((btn) => {
      btn.onclick = (e) => {
        const index = parseInt((e.currentTarget as HTMLElement).dataset.index ?? '', 10);
        this.moveTask(index, -1);
      };
    });

    document.querySelectorAll<HTMLElement>('.move-task-down').forEach((btn) => {
      btn.onclick = (e) => {
        const index = parseInt((e.currentTarget as HTMLElement).dataset.index ?? '', 10);
        this.moveTask(index, 1);
      };
    });

    document.querySelectorAll<HTMLElement>('.delete-task').forEach((btn) => {
      btn.onclick = (e) => {
        const index = parseInt((e.currentTarget as HTMLElement).dataset.index ?? '', 10);
        this.deleteTask(index);
      };
    });

    document.querySelectorAll<HTMLElement>('.locate-task-btn').forEach((btn) => {
      btn.onclick = (e) => {
        const index = parseInt((e.currentTarget as HTMLElement).dataset.index ?? '', 10);
        this.focusTaskLocation(index);
      };
    });

    // Tap the summary row to toggle expansion.
    document.querySelectorAll<HTMLElement>('.task-summary').forEach((btn) => {
      btn.onclick = (e) => {
        const index = parseInt((e.currentTarget as HTMLElement).dataset.index ?? '', 10);
        const task = this.currentRoute.tasks[index];
        if (!task) return;
        if (this._taskExpanded.has(task)) {
          this._taskExpanded.delete(task);
        } else {
          this._taskExpanded.set(task, true);
        }
        this.rerenderRouteEditor();
      };
    });

    // Task field handlers — segmented controls.
    document.querySelectorAll<HTMLElement>('.task-kind-seg .switch-btn').forEach((btn) => {
      btn.onclick = (e) => {
        const seg = (e.currentTarget as HTMLElement).closest('.task-kind-seg') as HTMLElement | null;
        const index = parseInt(seg?.dataset.index ?? '', 10);
        const task = this.currentRoute.tasks[index];
        if (!task) return;
        task.task_kind = (e.currentTarget as HTMLElement).dataset.kind;
        this.rerenderRouteEditor();
      };
    });

    document.querySelectorAll<HTMLElement>('.task-target-type-seg .switch-btn').forEach((btn) => {
      btn.onclick = (e) => {
        const seg = (e.currentTarget as HTMLElement).closest('.task-target-type-seg') as HTMLElement | null;
        const index = parseInt(seg?.dataset.index ?? '', 10);
        const task = this.currentRoute.tasks[index];
        if (!task) return;
        const type = (e.currentTarget as HTMLElement).dataset.type;
        const key = type === 'block' ? 'block_id' : type === 'station' ? 'station_id' : 'platform_id';
        task.task_location = { [key]: '' };
        this.rerenderRouteEditor();
      };
    });

    document.querySelectorAll<HTMLSelectElement>('.task-target-id-select').forEach((select) => {
      select.onchange = async (e) => {
        const el = e.target as HTMLSelectElement;
        const index = parseInt(el.dataset.index ?? '', 10);
        const task = this.currentRoute.tasks[index];
        if (!task) return;
        const loc = task.task_location || {};
        const key = 'block_id' in loc ? 'block_id' : 'station_id' in loc ? 'station_id' : 'platform_id';
        task.task_location = { [key]: el.value };
        await this.checkAllViability();
        this.rerenderRouteEditor();
      };
    });

    document.querySelectorAll<HTMLElement>('.task-direction-seg .switch-btn').forEach((btn) => {
      btn.onclick = (e) => {
        const seg = (e.currentTarget as HTMLElement).closest('.task-direction-seg') as HTMLElement | null;
        const index = parseInt(seg?.dataset.index ?? '', 10);
        const task = this.currentRoute.tasks[index];
        if (!task) return;
        task.task_direction = (e.currentTarget as HTMLElement).dataset.direction;
        this.rerenderRouteEditor();
      };
    });

    document.querySelectorAll<HTMLInputElement>('.task-time-input').forEach((input) => {
      input.onchange = (e) => {
        const el = e.target as HTMLInputElement;
        const index = parseInt(el.dataset.index ?? '', 10);
        const task = this.currentRoute.tasks[index];
        if (!task) return;
        task.task_time = this._parseOptionalNumber(el.value);
        this.rerenderRouteEditor();
      };
    });

    document.querySelectorAll<HTMLInputElement>('.task-target-time-input').forEach((input) => {
      input.onchange = (e) => {
        const el = e.target as HTMLInputElement;
        const index = parseInt(el.dataset.index ?? '', 10);
        const task = this.currentRoute.tasks[index];
        if (task) task.target_time = this._parseOptionalNumber(el.value);
      };
    });

    // Train assignment buttons
    const assignTrainBtn = document.getElementById('assign-train-btn') as HTMLButtonElement | null;
    if (assignTrainBtn) {
      assignTrainBtn.onclick = () => this.assignTrain();
    }

    const assignTrainMapBtn = document.getElementById('assign-train-map-btn') as HTMLButtonElement | null;
    if (assignTrainMapBtn) {
      assignTrainMapBtn.onclick = () => this.editor.enterMode(this.buildTrainAssignMode());
    }

    const forceCheckbox = document.getElementById('assign-train-force') as HTMLInputElement | null;
    if (forceCheckbox) {
      forceCheckbox.onchange = (e) => {
        this._assignForce = !!(e.target as HTMLInputElement).checked;
        if (this.editor.activeMode?.name === 'route-train-assign') {
          this.refreshAssignCandidates();
        }
      };
    }

    // Per-assignment start mode: reveal the Specific-stop picker only for 'specific'.
    const assignStartMode = document.getElementById('assign-start-mode') as HTMLSelectElement | null;
    if (assignStartMode) {
      assignStartMode.onchange = () => {
        const idxSel = document.getElementById('assign-start-task-index') as HTMLSelectElement | null;
        if (idxSel) idxSel.style.display = assignStartMode.value === 'specific' ? '' : 'none';
      };
    }

    document.querySelectorAll<HTMLElement>('.unassign-train-btn').forEach((btn) => {
      btn.onclick = (e) => {
        const trainId = (e.currentTarget as HTMLElement).dataset.trainId;
        this.unassignTrain(trainId);
      };
    });
  }

  _parseOptionalNumber(value: any): number | null {
    if (value === '' || value === null || value === undefined) return null;
    const f = parseFloat(value);
    return Number.isFinite(f) ? f : null;
  }

  /** Append a blank stop task (default platform target) and re-render. */
  addTask(): void {
    if (!Array.isArray(this.currentRoute.tasks)) this.currentRoute.tasks = [];
    const newTask = {
      task_kind: 'stop',
      task_location: { platform_id: '' },
      task_direction: 'either',
      task_time: null,
      target_time: null,
    };
    this.currentRoute.tasks.push(newTask);
    this._taskExpanded.set(newTask, true);
    this.rerenderRouteEditor();
  }

  // ===================================================================
  // Map-driven waypoint add
  // ===================================================================

  buildWaypointAddMode(): any {
    return {
      name: 'route-waypoint-add',
      cancelMessage: 'Cancelled',
      onEnter: () => {
        this._setStatus('Route: click a tile to add a waypoint — reachable tiles are highlighted. Esc to finish.');
        this.refreshWaypointFeasibility();
      },
      onClick: ({ gridPos, screenX, screenY }: { gridPos: any; screenX: any; screenY: any }) =>
        this.handleWaypointAddClick(gridPos, screenX, screenY),
      onExit: () => {
        this._dismissResourcePicker();
        this._waypointReachablePositions = null;
        this.clearLocationHighlight();
        this._setStatus('Ready');
      },
    };
  }

  _setStatus(text: string): void {
    const el = document.getElementById('status-state');
    if (el) el.textContent = text;
  }

  /** Return the last movement task's target as {type, id}, or null. */
  _lastMovementTaskLocation(): { type: string; id: string } | null {
    const tasks = this.currentRoute?.tasks || [];
    for (let i = tasks.length - 1; i >= 0; i--) {
      const t = tasks[i];
      if (t.task_kind === 'dwell') continue;
      const loc = t.task_location || {};
      if (loc.block_id) return { type: 'block', id: loc.block_id };
      if (loc.station_id) return { type: 'station', id: loc.station_id };
      if (loc.platform_id) return { type: 'platform', id: loc.platform_id };
    }
    return null;
  }

  /** Map a list of reachable endpoint ids to a Set<"x,y"> of tile positions. */
  _endpointsToPositionSet(endpoints: any): Set<string> {
    const positions = new Set<string>();
    const stationGroups = this.getStationGroups();
    const stationIds = new Set(stationGroups.map((g: any) => g.station_id));
    (endpoints || []).forEach((ep: any) => {
      if (stationIds.has(ep)) {
        const group = stationGroups.find((g: any) => g.station_id === ep);
        this.getPlatformPositionSet(group?.platform_ids || []).forEach((p) => positions.add(p));
      } else {
        this.getBlockPositionSet(ep).forEach((p) => positions.add(p));
      }
    });
    return positions;
  }

  /** Recompute and highlight the feasible tiles for the next waypoint. */
  async refreshWaypointFeasibility(): Promise<void> {
    const prev = this._lastMovementTaskLocation();
    if (!prev) {
      this._waypointReachablePositions = null;
      this.clearLocationHighlight();
      return;
    }
    try {
      const result = await this.api.getReachableFrom(prev.type, prev.id);
      const positions = this._endpointsToPositionSet(result?.reachable_endpoints || []);
      this._waypointReachablePositions = positions;
      this._applyHighlight(positions);
    } catch (err) {
      console.error('[RouteEditor] reachable-from failed:', err);
      this._waypointReachablePositions = null;
      this.clearLocationHighlight();
    }
  }

  /** Which targetable resources sit on a tile. */
  _resourceOptionsAtTile(gridPos: any, track: any): Array<{ type: string; id: string; label: string }> {
    const options = [];
    const platform = this.findPlatformAtPosition(gridPos.x, gridPos.y);
    if (platform) {
      const platformId = this.getPlatformId(platform);
      options.push({ type: 'platform', id: platformId, label: `Platform ${platform.name || platformId}` });
      const group = this.getStationGroupForPlatform(platformId);
      if (group) {
        options.push({ type: 'station', id: group.station_id, label: `Station ${group.name || group.station_id}` });
      }
    }
    if (track?.block_section_id) {
      options.push({ type: 'block', id: track.block_section_id, label: `Block ${track.block_section_id}` });
    }
    return options;
  }

  /** Handle a layout click while in waypoint-add mode. */
  async handleWaypointAddClick(gridPos: any, screenX: any, screenY: any): Promise<boolean> {
    const track = this.cachedNetwork?.tracks?.find((t: any) => t.x === gridPos.x && t.y === gridPos.y);
    const options = this._resourceOptionsAtTile(gridPos, track);

    // Empty tile: stay in mode, do nothing.
    if (options.length === 0) return true;

    // Feasibility gate (skipped for the first waypoint).
    if (this._waypointReachablePositions) {
      const key = `${gridPos.x},${gridPos.y}`;
      if (!this._waypointReachablePositions.has(key)) {
        showToast('Not reachable from the previous waypoint', 'warning', 2500);
        return true;
      }
    }

    if (options.length === 1) {
      await this._addWaypointFromOption(options[0]);
    } else {
      this._showResourcePicker(screenX, screenY, options, (opt) => this._addWaypointFromOption(opt));
    }
    return true;
  }

  /** Append a waypoint for a chosen {type, id} resource. */
  async _addWaypointFromOption(opt: any): Promise<void> {
    if (!Array.isArray(this.currentRoute.tasks)) this.currentRoute.tasks = [];
    const key = opt.type === 'block' ? 'block_id' : opt.type === 'station' ? 'station_id' : 'platform_id';
    this.currentRoute.tasks.push({
      task_kind: 'stop',
      task_location: { [key]: opt.id },
      task_direction: 'either',
      task_time: null,
      target_time: null,
    });
    await this.checkAllViability();
    this.rerenderRouteEditor();
    await this.refreshWaypointFeasibility();
    showToast(`Added stop: ${opt.label}`, 'success', 2000);
  }

  /** Floating menu to pick which resource on a tile to add. Backed by the
   *  shared resource-picker module (see ./resource-picker). */
  _showResourcePicker(
    screenX: any,
    screenY: any,
    options: Array<{ type: string; id: string; label: string }>,
    onPick: (opt: any) => void
  ): void {
    showResourcePicker(screenX, screenY, options, onPick);
  }

  _dismissResourcePicker(): void {
    dismissResourcePicker();
  }

  // ===================================================================
  // Map-driven train assignment
  // ===================================================================

  buildTrainAssignMode(): any {
    return {
      name: 'route-train-assign',
      cancelMessage: 'Cancelled',
      onEnter: () => {
        this.refreshAssignCandidates();
      },
      onClick: ({ gridPos }: { gridPos: any }) => this.handleTrainAssignClick(gridPos),
      onExit: () => {
        this._assignTileTrainMap = null;
        this.clearLocationHighlight();
        this._setStatus('Ready');
      },
    };
  }

  /** Return the id of the block currently occupied by `trainId`, or null. */
  _blockOccupiedByTrain(trainId: string): string | null {
    const blocks = this.cachedNetwork?.blocks || {};
    if (Array.isArray(blocks)) {
      const found = blocks.find((b: any) => b?.occupied_by === trainId);
      return found ? found.block_id || null : null;
    }
    for (const [blockId, b] of Object.entries(blocks)) {
      if ((b as any)?.occupied_by === trainId) return (b as any).block_id || blockId;
    }
    return null;
  }

  /** Highlight the layout tiles of trains eligible for assignment. */
  async refreshAssignCandidates(): Promise<void> {
    let assignedByTrain: any = {};
    try {
      assignedByTrain = await this.api.getAssignedRoutesByTrain();
    } catch (err) {
      console.error('[RouteEditor] getAssignedRoutesByTrain failed:', err);
    }

    const force = this._assignForce;
    const alreadyOnThisRoute = new Set(this.currentRoute.assigned_train_ids || []);
    const allTrains = Array.isArray(this.cachedNetwork?.trains)
      ? this.cachedNetwork.trains
      : Object.values(this.cachedNetwork?.trains || {});

    const positions = new Set<string>();
    const tileTrain = new Map<string, any>();
    allTrains.forEach((t: any) => {
      const trainId = t?.train_id || t?.id;
      if (!trainId || alreadyOnThisRoute.has(trainId)) return;
      const hasRoute = !!assignedByTrain[trainId];
      if (!force && hasRoute) return;
      const blockId = this._blockOccupiedByTrain(trainId);
      if (!blockId) return;
      this.getBlockPositionSet(blockId).forEach((p) => {
        positions.add(p);
        tileTrain.set(p, trainId);
      });
    });

    this._assignTileTrainMap = tileTrain;
    this._applyHighlight(positions);

    const eligible = new Set(tileTrain.values()).size;
    this._setStatus(
      force
        ? `Route: click any train to assign (force). ${eligible} eligible. Esc to finish.`
        : `Route: click an unassigned train to assign. ${eligible} eligible. Esc to finish.`
    );
  }

  /** Handle a layout click while in train-assign mode. */
  async handleTrainAssignClick(gridPos: any): Promise<boolean> {
    const trainId = this._assignTileTrainMap?.get(`${gridPos.x},${gridPos.y}`);
    if (!trainId) return true; // no eligible train here; stay in mode

    const dirtyOk = await this._promptSaveIfDirty('Assign train');
    if (!dirtyOk) return true;

    const ok = await this._confirmRuntimePreconditions('Assign train', trainId);
    if (!ok) return true;

    try {
      await this.api.assignRoute(trainId, this.currentRoute.route_id, {
        force: this._assignForce,
      });
      await this._refetchRouteAfterLifecycleOp();
      showToast(`Train ${trainId} assigned`, 'success', 2500);
      this.rerenderRouteEditor();
      await this.refreshAssignCandidates();
    } catch (err: any) {
      console.error('[RouteEditor] assign (map) failed:', err);
      showToast('Failed to assign train: ' + err.message, 'error', 5000);
    }
    return true;
  }

  /** Find platform at a grid position. */
  findPlatformAtPosition(x: number, y: number): any {
    const track = this.cachedNetwork?.tracks?.find((t: any) => t.x === x && t.y === y);
    const platformId = track?.station_id;
    if (!platformId) return null;

    return this.getPlatforms().find((p: any) => this.getPlatformId(p) === platformId) || null;
  }

  /** Move a task up or down in the list. */
  async moveTask(index: number, direction: number): Promise<void> {
    const tasks = this.currentRoute.tasks;
    const newIndex = index + direction;
    if (newIndex < 0 || newIndex >= tasks.length) return;

    [tasks[index], tasks[newIndex]] = [tasks[newIndex], tasks[index]];

    await this.checkAllViability();
    this.rerenderRouteEditor();
  }

  /** Delete a task. */
  async deleteTask(index: number): Promise<void> {
    const confirmed = await showConfirmModal({
      title: 'Delete Task',
      message: 'Delete this task?',
      detail: 'Route order and viability will be recalculated.',
      confirmText: 'Delete',
      confirmVariant: 'danger',
    });
    if (!confirmed) return;

    this.currentRoute.tasks.splice(index, 1);

    await this.checkAllViability();
    this.rerenderRouteEditor();
  }

  /** Return the canonical {key: id} location of a task, or null. */
  _taskLocationOrNull(task: any): Record<string, any> | null {
    const loc = task?.task_location || {};
    const key = 'block_id' in loc ? 'block_id' : 'station_id' in loc ? 'station_id' : 'platform_id';
    return loc[key] ? { [key]: loc[key] } : null;
  }

  /** Check viability for all consecutive movement-task pairs. */
  async checkAllViability(): Promise<void> {
    this.viabilityResults.clear();
    const tasks = this.currentRoute.tasks || [];

    const movement: Array<{ idx: number; loc: any; direction: string }> = [];
    tasks.forEach((task: any, idx: number) => {
      if (task.task_kind === 'dwell') return;
      const loc = this._taskLocationOrNull(task);
      if (loc) movement.push({ idx, loc, direction: task.task_direction || 'either' });
    });

    if (movement.length < 2) return;

    const pairs = [];
    for (let i = 1; i < movement.length; i++) {
      pairs.push({
        from: movement[i - 1].loc,
        to: movement[i].loc,
        direction: movement[i].direction,
      });
    }

    try {
      const results = await this.api.checkViability(pairs);
      (results.results || []).forEach((result: any, k: number) => {
        const rowIndex = movement[k + 1].idx;
        this.viabilityResults.set(rowIndex, result);
      });
    } catch (err) {
      console.error('[RouteEditor] Viability check failed:', err);
    }
  }

  /** Save route changes (serializes rows into the canonical `tasks` payload). */
  async saveRoute(): Promise<boolean> {
    const nameInput = document.getElementById('route-name-input') as HTMLInputElement | null;
    const modeSeg = document.getElementById('route-mode-seg');

    if (nameInput) {
      this.currentRoute.name = nameInput.value;
    }
    if (modeSeg) {
      const active = modeSeg.querySelector('.switch-btn.active') as HTMLElement | null;
      if (active) this.currentRoute.mode = active.dataset.mode;
    }

    const src = this.currentRoute.tasks || [];
    const tasks = [];
    for (let i = 0; i < src.length; i++) {
      const t = src[i];
      const kind = t.task_kind || 'waypoint';
      let location;

      if (kind === 'dwell') {
        // Runtime ignores a dwell's location, but the backend requires a
        // truthy task_location — inherit the nearest preceding target.
        let inherited = null;
        for (let j = i - 1; j >= 0; j--) {
          const prevLoc = this._taskLocationOrNull(src[j]);
          if (prevLoc) {
            inherited = prevLoc;
            break;
          }
        }
        if (!inherited) {
          showToast(
            `Dwell task ${i + 1} must follow a stop or waypoint with a target.`,
            'error',
            5000
          );
          return false;
        }
        location = inherited;
      } else {
        location = this._taskLocationOrNull(t);
        if (!location) {
          showToast(`Task ${i + 1} has no target selected.`, 'error', 5000);
          return false;
        }
      }

      tasks.push({
        task_kind: kind,
        task_location: location,
        task_direction: t.task_direction || 'either',
        task_time: t.task_time ?? null,
        target_time: kind === 'stop' ? (t.target_time ?? null) : null,
      });
    }

    // Per design_new.md "RouteRun overrides": the three policy fields each
    // carry their own push policy. Pack as `{value, push}`.
    const segValue = (segId: string) => {
      const seg = document.getElementById(segId);
      if (!seg) return null;
      const active = seg.querySelector('.switch-btn.active') as HTMLElement | null;
      return active ? active.dataset.value : null;
    };

    const acqValue = segValue('route-acq-seg');
    const acqPush = segValue('route-acq-push-seg') || 'soft';
    const priorityRaw = (document.getElementById('route-priority-input') as HTMLInputElement | null)?.value;
    const priorityPush = segValue('route-priority-push-seg') || 'soft';

    const payload: Record<string, unknown> = {
      name: this.currentRoute.name,
      mode: this.currentRoute.mode,
      tasks: tasks,
    };
    if (acqValue) {
      payload.acquisition_mode = { value: acqValue, push: acqPush };
    }
    // Priority via the shared validator (helpers#validatePriorityInput).
    const priorityCheck = validatePriorityInput(priorityRaw);
    if (priorityCheck.kind === 'error') {
      showToast(priorityCheck.message, 'error', 4000);
      return false;
    }
    if (priorityCheck.kind === 'ok') {
      payload.priority = { value: priorityCheck.value, push: priorityPush };
    }

    // Start-mode route default (per design_new.md "Run start-mode seeding").
    // Not an override — plain value, no push policy.
    const startModeValue = segValue('route-start-mode-seg') || 'first';
    payload.start_mode = startModeValue;
    if (startModeValue === 'specific') {
      const idxRaw = (
        document.getElementById('route-start-task-index-select') as HTMLSelectElement | null
      )?.value;
      payload.start_task_index = idxRaw ? parseInt(idxRaw, 10) : null;
    } else {
      payload.start_task_index = null;
    }

    // Warn if the route has no Stop tasks.
    if (tasks.length > 0 && !tasks.some((t) => t.task_kind === 'stop')) {
      const ok = await showConfirmModal({
        title: 'Route has no Stop tasks',
        message: 'Save without any Stop task?',
        detail:
          'Waypoint and dwell tasks keep the live path extended — ' +
          "the train won't halt or reset its travel direction " +
          'between them. If any task ahead requires a turnaround, ' +
          'the run will fail with "extend would reverse at ' +
          'authority end". Add at least one Stop where the train ' +
          'should be able to change direction.',
        confirmText: 'Save anyway',
        confirmVariant: 'primary',
      });
      if (!ok) return false;
    }

    try {
      await this.api.updateRoute(this.currentRoute.route_id, payload);
      showToast('Route saved', 'success', 3000);
      this._refreshSavedTasksBaseline();
      return true;
    } catch (err: any) {
      console.error('[RouteEditor] Failed to save route:', err);
      showToast('Failed to save route: ' + err.message, 'error', 5000);
      return false;
    }
  }

  /** Activate the route. */
  async activateRoute(): Promise<void> {
    const dirtyOk = await this._promptSaveIfDirty('Activate route');
    if (!dirtyOk) return;

    const ok = await this._confirmRuntimePreconditions('Activate route');
    if (!ok) return;

    try {
      const resp = await this.api.activateRoute(this.currentRoute.route_id);
      this._mergeServerRoute(resp?.route);
      showToast('Route activated', 'success', 3000);
      this.currentRouteStatus = await this.api.getRouteStatus(this.currentRoute.route_id);
      this.rerenderRouteEditor();
    } catch (err: any) {
      console.error('[RouteEditor] Failed to activate route:', err);
      showToast('Failed to activate route: ' + err.message, 'error', 5000);
    }
  }

  /** Deactivate the route. */
  async deactivateRoute(): Promise<void> {
    try {
      const resp = await this.api.deactivateRoute(this.currentRoute.route_id);
      this._mergeServerRoute(resp?.route);
      showToast('Route deactivated', 'success', 3000);
      this.currentRouteStatus = await this.api.getRouteStatus(this.currentRoute.route_id);
      this.rerenderRouteEditor();
    } catch (err: any) {
      console.error('[RouteEditor] Failed to deactivate route:', err);
      showToast('Failed to deactivate route: ' + err.message, 'error', 5000);
    }
  }

  /** Pre-flight check before a route mutation that needs the per-train phase loop. */
  async _confirmRuntimePreconditions(actionLabel: string, trainId: string | null = null): Promise<boolean> {
    const reasons = [];
    const app = window.app;
    const simPaused = !(app && app.isSimulationRunning);
    if (simPaused) {
      reasons.push('Simulation is paused.');
    }

    let trainOff = false;
    if (trainId) {
      try {
        const payload = await this.api.getTrain(trainId);
        const controller = payload?.controller;
        trainOff = controller?.state === 'off' || controller?.power_on === false;
      } catch (err) {
        console.warn(
          '[RouteEditor] getTrain pre-flight check failed; proceeding without train-state check:',
          err
        );
      }
    }
    if (trainOff) {
      reasons.push(`Train ${trainId} is powered off.`);
    }

    if (reasons.length === 0) {
      return true;
    }

    const detail =
      reasons.join(' ') +
      ` ${actionLabel} will only take effect once the simulation` +
      ` is running and the train is powered on. The change is` +
      ` recorded immediately but the train will not react until then.`;

    return await showConfirmModal({
      title: `${actionLabel}: precondition not met`,
      message: 'Continue anyway?',
      detail,
      confirmText: 'Proceed',
      confirmVariant: 'primary',
    });
  }

  /** Assign a train to this route. */
  async assignTrain(): Promise<void> {
    const select = document.getElementById('assign-train-select') as HTMLSelectElement | null;
    if (!select || !select.value) return;

    const trainId = select.value;
    const forceAssign = !!(document.getElementById('assign-train-force') as HTMLInputElement | null)?.checked;

    // Per-assignment start-mode override (empty = use the route default).
    const startMode = (document.getElementById('assign-start-mode') as HTMLSelectElement | null)?.value || '';
    const assignOpts: { force: boolean; startMode?: string; startTaskIndex?: number | null } = {
      force: forceAssign,
    };
    if (startMode) {
      assignOpts.startMode = startMode;
      if (startMode === 'specific') {
        const idxRaw = (document.getElementById('assign-start-task-index') as HTMLSelectElement | null)?.value;
        assignOpts.startTaskIndex = idxRaw ? parseInt(idxRaw, 10) : null;
      }
    }

    const dirtyOk = await this._promptSaveIfDirty('Assign train');
    if (!dirtyOk) return;

    const ok = await this._confirmRuntimePreconditions('Assign train', trainId);
    if (!ok) return;

    try {
      await this.api.assignRoute(trainId, this.currentRoute.route_id, assignOpts);

      await this._refetchRouteAfterLifecycleOp();
      showToast(`Train ${trainId} assigned`, 'success', 3000);

      this.rerenderRouteEditor();
    } catch (err: any) {
      console.error('[RouteEditor] Failed to assign train:', err);
      showToast('Failed to assign train: ' + err.message, 'error', 5000);
    }
  }

  /** Unassign a train from this route. */
  async unassignTrain(trainId: any): Promise<void> {
    try {
      await this.api.assignRoute(trainId, null);

      await this._refetchRouteAfterLifecycleOp();
      showToast(`Train ${trainId} unassigned`, 'success', 3000);

      this.rerenderRouteEditor();
    } catch (err: any) {
      console.error('[RouteEditor] Failed to unassign train:', err);
      showToast('Failed to unassign train: ' + err.message, 'error', 5000);
    }
  }

  /** Pull the server's current view of `currentRoute` after a lifecycle op. */
  async _refetchRouteAfterLifecycleOp(): Promise<void> {
    const routeId = this.currentRoute?.route_id;
    if (!routeId) return;
    try {
      const [route, status] = await Promise.all([
        this.api.getRoute(routeId),
        this.api.getRouteStatus(routeId),
      ]);
      this._mergeServerRoute(route);
      this.currentRouteStatus = status;
    } catch (err) {
      console.warn('[RouteEditor] post-op refetch failed:', err);
    }
  }

  /** Copy server-controlled fields from a freshly-fetched `route` onto currentRoute. */
  _mergeServerRoute(route: any): void {
    if (!route || !this.currentRoute) return;
    this.currentRoute.is_active = route.is_active;
    this.currentRoute.started_at = route.started_at;
    this.currentRoute.assigned_train_ids = route.assigned_train_ids;
    if ('last_edited_at' in route) {
      this.currentRoute.last_edited_at = route.last_edited_at;
    }
  }
}
