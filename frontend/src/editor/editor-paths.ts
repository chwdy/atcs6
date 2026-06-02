// editor-paths.ts — Path Editor Handler (route-creation / extend / cancel tools).
//
// Migrated from static/js/editor-paths.js (longterm_todo.md item #7, Wave 2).
// Behavior is unchanged. Owned by the Editor (`new PathEditorHandler(this)`).

import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import {
  renderHeaderId,
  renderReadOnlyValue,
  formatBlockDirection,
  showConfirmModal,
} from '../lib/helpers';

/** Set the status-bar text. The #status-state element is always present in
 *  index.html; this no-ops defensively if it is ever absent. */
function setStatusState(text: string): void {
  const el = document.getElementById('status-state');
  if (el) el.textContent = text;
}

export class PathEditorHandler {
  editor: EditorHost;

  constructor(editor: EditorHost) {
    this.editor = editor;
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
  get cachedPaths(): any {
    return this.editor.cachedPaths;
  }
  set cachedPaths(value: any) {
    this.editor.cachedPaths = value;
  }
  get pathToolState(): any {
    return this.editor.pathToolState;
  }
  set pathToolState(value: any) {
    this.editor.pathToolState = value;
  }
  get extendPathState(): any {
    return this.editor.extendPathState;
  }
  set extendPathState(value: any) {
    this.editor.extendPathState = value;
  }

  enterRouteToolMode(): void {
    this.pathToolState = {
      active: false,
      startBlockId: null,
      startPosition: null,
      reachableEndpoints: [],
      reachablePositions: new Set(),
      requestedDirection: 'forward',
      trainId: null,
    };
    setStatusState('Path: Click train (start) block');
  }

  async exitRouteToolMode(): Promise<void> {
    await this.resetRouteToolState();
    setStatusState('Ready');
  }

  // EditorMode factory for the route-creation tool.
  buildRouteToolMode(): any {
    return {
      name: 'Route creation',
      toolName: 'path',
      cancelMessage: 'Route creation cancelled',
      onEnter: () => this.enterRouteToolMode(),
      onClick: async ({ gridPos }: { gridPos: any }) => {
        await this.handleRouteToolClick(gridPos);
        return true;
      },
      onExit: () => this.exitRouteToolMode(),
    };
  }

  async resetRouteToolState(): Promise<void> {
    this.pathToolState = {
      active: false,
      startBlockId: null,
      startPosition: null,
      reachableEndpoints: [],
      reachablePositions: new Set(),
      requestedDirection: 'forward',
      trainId: null,
    };
    if (this.renderer.setReachableHighlights) {
      this.renderer.setReachableHighlights(new Set());
    }
    if (this.renderer.clearRoutePreview) {
      this.renderer.clearRoutePreview();
    }
    if (this.editor._currentEditorType === 'path-setup') {
      await this.editor.goBackSecondaryPanel();
      // If history was empty (no panel before path tool), goBack does nothing.
      // In that case _currentEditorType is still 'path-setup', so close the sidebar.
      if (this.editor._currentEditorType === 'path-setup') {
        this.editor.hideSecondarySidebar();
      }
    }
    if (this.cachedNetwork) {
      this.renderer.requestRedraw();
    }
    setStatusState('Path: Click start block');
  }

  async handleRouteToolClick(gridPos: any): Promise<void> {
    const track = this.cachedNetwork?.tracks?.find((t: any) => t.x === gridPos.x && t.y === gridPos.y);

    if (!this.pathToolState.active) {
      await this.handleRouteFirstClick(gridPos, track);
    } else {
      await this.handleRouteSecondClick(gridPos, track);
    }
  }

  async handleRouteFirstClick(gridPos: any, track: any): Promise<void> {
    if (!track) {
      showToast('No track at this position', 'warning', 3000);
      return;
    }

    const blockId = track.block_section_id;
    if (!blockId) {
      showToast('Must start path from a block', 'warning', 3000);
      return;
    }

    this.pathToolState.startBlockId = blockId;
    this.pathToolState.startPosition = { x: gridPos.x, y: gridPos.y };
    this.pathToolState.active = true;

    const block = this.cachedNetwork?.blocks?.[blockId];
    if (block?.occupied_by) {
      this.pathToolState.trainId = block.occupied_by;
    } else {
      showToast('No train at start block; pick a block occupied by a train', 'warning', 3000);
      await this.resetRouteToolState();
      return;
    }

    try {
      setStatusState('Path: Loading destinations...');
      // Span-aware reachability: only tiles the train can actually reach
      // forward out of its head or in reverse out of its tail, never back
      // through its own body (e.g. switches the body is spanning in a depot).
      const result = await this.api.getReachableEndpointsForTrain(this.pathToolState.trainId);
      this.pathToolState.reachableEndpoints = result.reachable_endpoints || [];

      this.buildReachablePositionsSet();

      const count = this.pathToolState.reachableEndpoints.length;
      setStatusState(`Path: Click destination (${count} reachable)`);

      if (this.renderer.setReachableHighlights) {
        this.renderer.setReachableHighlights(this.pathToolState.reachablePositions);
      }

      this.showRoutePrioritySelector();

      if (this.cachedNetwork) {
        this.renderer.requestRedraw();
      }
    } catch (err: any) {
      console.error('[Editor] Failed to get reachable destinations:', err);
      showToast('Failed to get reachable destinations: ' + err.message, 'error', 5000);
      await this.resetRouteToolState();
    }
  }

  buildReachablePositionsSet(): void {
    const positions = new Set<string>();
    const tracks = this.cachedNetwork?.tracks || [];

    for (const endpoint of this.pathToolState.reachableEndpoints) {
      for (const track of tracks) {
        if (track.block_section_id === endpoint) {
          positions.add(`${track.x},${track.y}`);
        }
      }
    }

    this.pathToolState.reachablePositions = positions;
  }

  async handleRouteSecondClick(gridPos: any, track: any): Promise<void> {
    if (!track) {
      showToast('No track at this position', 'warning', 3000);
      return;
    }

    const endBlockId = track.block_section_id;
    if (!endBlockId) {
      showToast('Must end path on a block', 'warning', 3000);
      return;
    }

    if (!this.pathToolState.reachableEndpoints.includes(endBlockId)) {
      showToast('Cannot reach this block from start', 'warning', 3000);
      return;
    }

    const trainId = this.pathToolState.trainId;
    const direction = this.pathToolState.requestedDirection || 'forward';
    const routeId = `ROUTE_QR_${Date.now()}`;

    setStatusState('Path: Creating...');

    let routeCreated = false;
    try {
      // Quick Route is a one-off Route + RouteRun, per design_new.md:1297.
      await this.api.createRoute({
        route_id: routeId,
        name: `Quick: ${trainId} → ${endBlockId}`,
        mode: 'one_off',
        tasks: [
          {
            task_kind: 'stop',
            task_location: { block_id: endBlockId },
            task_direction: direction,
          },
        ],
        acquisition_mode: 'all_or_nothing',
        assigned_train_ids: [],
      });
      routeCreated = true;

      await this.api.activateRoute(routeId);

      const outcome = await this.assignQuickRouteWithConfirm(trainId, routeId);
      if (outcome === 'cancelled') {
        await this.deleteRouteBestEffort(routeId);
        showToast('Quick Route cancelled', 'info', 3000);
        return;
      }

      showToast(`Quick Route assigned to ${trainId}`, 'success', 5000);
      await window.app?.refreshNetwork();
      await this.resetRouteToolState();
      // The path is acquired asynchronously by the sim loop; if it can't be
      // found, surface that instead of leaving the train idle with no feedback.
      void this.watchQuickRoutePathResult(trainId);
    } catch (err: any) {
      if (routeCreated) {
        await this.deleteRouteBestEffort(routeId);
      }
      console.error('[Editor] Quick Route failed:', err);
      showToast('Quick Route failed: ' + err.message, 'error', 5000);
    }
  }

  /**
   * After a Quick Route is assigned, the path is acquired asynchronously in
   * the simulation loop. Poll the train's live controller state briefly and
   * toast if a path error appears; stop early once the train has authority.
   * Best-effort: failures to fetch are ignored, and a paused sim simply times
   * out without a toast (no tick means no acquisition attempt to report on).
   */
  async watchQuickRoutePathResult(trainId: string): Promise<void> {
    const deadline = Date.now() + 8000;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 800));
      let controller: any = null;
      try {
        const data = await this.api.getTrain(trainId);
        controller = data?.controller;
      } catch {
        continue;
      }
      if (!controller) {
        continue;
      }
      // Train acquired authority — nothing to report.
      if (controller.path_backed_run_active || controller.assigned_path_id) {
        return;
      }
      const pathError = controller.path_error;
      if (pathError && pathError.text) {
        showToast(`${trainId} could not find a path: ${pathError.text}`, 'error', 8000);
        return;
      }
    }
  }

  async assignQuickRouteWithConfirm(trainId: string, routeId: string): Promise<string> {
    try {
      await this.api.assignRoute(trainId, routeId);
      return 'ok';
    } catch (err: any) {
      if (err.status !== 409) {
        throw err;
      }
      const confirmed = await showConfirmModal({
        title: 'Train Already Has a Route',
        message: `Train ${trainId} is already assigned to another route. Replace it with the new Quick Route?`,
        detail: err.message || '',
        confirmText: 'Replace Route',
        confirmVariant: 'danger',
      });
      if (!confirmed) {
        return 'cancelled';
      }
      await this.api.assignRoute(trainId, routeId, { force: true });
      return 'ok';
    }
  }

  async deleteRouteBestEffort(routeId: string): Promise<void> {
    try {
      await this.api.deleteRoute(routeId);
    } catch (e) {
      console.warn('[Editor] Quick Route cleanup (deleteRoute) failed:', e);
    }
  }

  showRoutePrioritySelector(): void {
    this.editor._currentEditorType = 'path-setup';
    this.editor._currentEditorId = null;

    const trainInfo = this.pathToolState.trainId
      ? `
                <div class="info-display-row info-display-small">
                    <span class="info-label">Train</span>
                    <span class="info-value text-info">🚂 ${this.pathToolState.trainId}</span>
                </div>
            `
      : '<div class="text-muted text-sm py-8">No train at start block</div>';

    const direction = this.pathToolState.requestedDirection || 'forward';

    const content = `
            <div class="path-setup-panel">
                ${renderHeaderId(this.pathToolState.startBlockId, '#00d4ff')}

                <div class="property-item">
                    <label class="property-label">Direction</label>
                    <div class="switch-group">
                        <button id="path-direction-forward-btn"
                                class="switch-btn ${direction === 'forward' ? 'active' : ''}"
                                data-direction="forward">▶ Forward</button>
                        <button id="path-direction-reverse-btn"
                                class="switch-btn ${direction === 'reverse' ? 'active' : ''}"
                                data-direction="reverse">◀ Reverse</button>
                    </div>
                    <div class="text-xxs text-muted mt-4">
                        Direction the train should travel away from its current position.
                    </div>
                </div>

                ${trainInfo}

                <div class="info-box text-sm mt-16">
                    Click on an <span class="text-warning">orange</span> destination block to create the path.
                    <br><br>
                    Right-click or press ESC to cancel.
                </div>
            </div>
        `;

    this.editor.showSecondarySidebar('🛤️ Create Path', content);

    const sidebar = document.getElementById('secondary-sidebar');
    if (sidebar) {
      sidebar.classList.add('compact');
    }

    setTimeout(() => {
      const dirButtons = document.querySelectorAll<HTMLElement>('[data-direction]');
      dirButtons.forEach((btn) => {
        btn.addEventListener('click', () => {
          this.pathToolState.requestedDirection = btn.dataset.direction;
          dirButtons.forEach((b) => b.classList.toggle('active', b === btn));
        });
      });
    }, 0);
  }

  async showPathsPanel(): Promise<void> {
    this.editor._currentEditorType = 'paths-list';
    this.editor._currentEditorId = null;

    try {
      this.cachedPaths = await this.api.getPaths();
    } catch (err: any) {
      console.error('[Editor] Failed to load paths:', err);
      showToast('Failed to load paths: ' + err.message, 'error', 5000);
      return;
    }

    let content = `
            <div class="paths-panel">
                <div class="flex gap-8 mb-16">
                    <button id="refresh-paths-btn" class="btn btn-secondary btn-sm flex-1">
                        🔄 Refresh
                    </button>
                </div>
        `;

    if (this.cachedPaths.length === 0) {
      content += '<div class="text-muted text-center routes-empty-state">No active paths</div>';
    } else {
      content += '<div class="paths-list">';
      for (const path of this.cachedPaths) {
        content += this.renderPathListItem(path);
      }
      content += '</div>';
    }

    content += '</div>';

    this.editor.showSecondarySidebar('📋 Active Paths', content);

    setTimeout(() => this.initRoutesPanelHandlers(), 0);
  }

  renderPathListItem(path: any): string {
    const statusClass = this.getPathStatusClass(path.status);
    const resources = path.resource_sequence || [];
    const totalSteps = resources.length;
    const currentIndex = path.current_resource_index ?? 0;
    const progress = totalSteps > 0 ? Math.round((currentIndex / totalSteps) * 100) : 0;
    const statusText = path.status.toUpperCase();
    const direction = path.requested_destination_direction || 'forward';
    const destination =
      path.authority_end_block_id || path.requested_destination_block_id || '—';
    const dirTile = path.terminal_tile || null;
    const dirHeading = path.terminal_heading || null;
    const directionLabel = formatBlockDirection(direction, dirTile, dirHeading);
    const directionFull = formatBlockDirection(direction, dirTile, dirHeading, { form: 'full' });

    return `
            <div class="path-row ${statusClass} mb-8">
                <div class="path-row-main">
                    <div class="path-row-status">
                        <div class="train-status-dot path-status-dot"></div>
                        <div class="text-xxs font-bold uppercase tracking-wider path-status-label">
                            ${statusText}
                        </div>
                        <div class="text-xxs font-bold text-muted path-priority-tag font-mono" title="Direction: ${directionFull} · raw=${direction}">${directionLabel}</div>
                    </div>
                    <div class="train-info flex-1 path-row-content">
                        <div class="flex flex-col gap-1 mb-6">
                            <div class="flex justify-between items-start gap-2">
                                <span class="train-id font-mono font-bold text-white path-id-text">${path.path_id}</span>
                            </div>
                        </div>
                        <div class="flex justify-between items-end gap-2">
                            <div class="text-xs text-muted flex-1 path-meta-col">
                                <div class="path-endpoints-stack">
                                    <div class="font-mono text-white opacity-90">→ ${destination}</div>
                                </div>
                                <div class="mb-4">${path.train_id ? `🚂 ${path.train_id}` : 'No Train'}</div>
                                ${
                                  path.status === 'active'
                                    ? `
                                    <div class="progress-container mt-1 path-progress-compact">
                                        <progress class="path-progress-meter" value="${progress}" max="100"></progress>
                                    </div>
                                `
                                    : ''
                                }
                            </div>
                            <div class="flex gap-4 flex-shrink-0">
                                <button class="edit-btn btn-sm view-path-btn" data-path-id="${path.path_id}">View</button>
                                <button class="btn btn-sm btn-danger btn-danger-solid cancel-path-btn" data-path-id="${path.path_id}">Cancel</button>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        `;
  }

  getPathStatusColor(status: string): string {
    const colors: Record<string, string> = {
      pending: '#888',
      setting: '#ffd93d',
      set: '#04d361',
      active: '#00ff88',
      clearing: '#ff9500',
      completed: '#666',
      cancelled: '#e83f5b',
    };
    return colors[status] || '#888';
  }

  getPathStatusClass(status: string): string {
    const map: Record<string, string> = {
      pending: 'path-status-pending',
      setting: 'path-status-setting',
      set: 'path-status-set',
      active: 'path-status-active',
      clearing: 'path-status-clearing',
      completed: 'path-status-completed',
      cancelled: 'path-status-cancelled',
    };
    return map[status] || 'path-status-pending';
  }

  initRoutesPanelHandlers(): void {
    const refreshBtn = document.getElementById('refresh-paths-btn') as HTMLButtonElement | null;
    if (refreshBtn) {
      refreshBtn.onclick = async () => {
        await this.showPathsPanel();
      };
    }

    const viewBtns = document.querySelectorAll<HTMLElement>('.view-path-btn');
    viewBtns.forEach((btn) => {
      btn.onclick = async () => {
        const pathId = btn.dataset.pathId;
        await this.showPathEditor(pathId);
      };
    });

    const cancelBtns = document.querySelectorAll<HTMLElement>('.cancel-path-btn');
    cancelBtns.forEach((btn) => {
      btn.onclick = async () => {
        const pathId = btn.dataset.pathId;
        const confirmed = await showConfirmModal({
          title: 'Cancel Path',
          message: `Cancel path "${pathId}"?`,
          detail: 'Reserved blocks for this path will be released.',
          confirmText: 'Cancel Path',
          confirmVariant: 'danger',
        });
        if (!confirmed) return;

        try {
          const result = await this.api.cancelPath(pathId);
          if (result.success) {
            const queued = result.status === 'queued';
            const message = queued
              ? `Path ${pathId} cancellation queued — train is braking`
              : `Path ${pathId} cancelled`;
            showToast(message, 'success', 5000);
            await window.app?.refreshNetwork();
            await this.showPathsPanel();
            // For latched cancels, the path is not yet removed. Reconcile
            // after one tick (~200 ms) so the UI catches up with Phase-3.
            if (queued) {
              setTimeout(async () => {
                try {
                  await window.app?.refreshNetwork();
                  await this.showPathsPanel();
                } catch (err) {
                  console.warn('[Editor] Deferred path refresh failed:', err);
                }
              }, 500);
            }
          } else {
            showToast(
              `Failed to cancel: ${result.reason || result.error || 'unknown error'}`,
              'error',
              5000
            );
          }
        } catch (err: any) {
          console.error('[Editor] Failed to cancel path:', err);
          showToast('Failed to cancel path: ' + err.message, 'error', 5000);
        }
      };
    });
  }

  async showPathEditor(pathId: any): Promise<void> {
    this.editor._currentEditorType = 'path';
    this.editor._currentEditorId = pathId;

    let path;
    try {
      path = await this.api.getPath(pathId);
      if (path.error) throw new Error(path.error);
    } catch (err) {
      showToast('Path not found', 'error', 5000);
      return;
    }

    const statusClass = this.getPathStatusClass(path.status);
    const statusColor = this.getPathStatusColor(path.status);
    const resources = path.resource_sequence || [];
    const totalSteps = resources.length;
    const currentIndex = path.current_resource_index ?? 0;
    const progress = totalSteps > 0 ? Math.round((currentIndex / totalSteps) * 100) : 0;
    const direction = path.requested_destination_direction || 'forward';
    const destination =
      path.authority_end_block_id || path.requested_destination_block_id || '—';
    const detailDirTile = path.terminal_tile || null;
    const detailDirHeading = path.terminal_heading || null;
    const detailDirCompact = formatBlockDirection(direction, detailDirTile, detailDirHeading);
    const detailDirFull = formatBlockDirection(direction, detailDirTile, detailDirHeading, {
      form: 'full',
    });

    const resourceListHtml = resources
      .map((resource: any, i: number) => {
        const isCurrent = i === currentIndex;
        const isCleared = i < currentIndex;
        let cssClass = 'text-muted';
        if (isCurrent) cssClass = 'text-success font-bold';
        else if (isCleared) cssClass = 'text-muted';

        const label =
          resource.resource_id || `${resource.resource_type || '?'} #${resource.sequence_index ?? i}`;
        const kind = resource.resource_type || 'resource';

        return `<div class="block-item text-xs ${cssClass}">
                ${isCurrent ? '▶ ' : '  '}${label}<span class="text-muted ml-2">[${kind}]</span>${isCleared ? ' ✓' : ''}
            </div>`;
      })
      .join('');

    const content = `
            <div class="path-editor-panel" data-path-id="${pathId}">
                ${renderHeaderId(path.path_id, statusColor)}

                <!-- INFO SECTION (Read-only, styled like Train Control) -->
                <div class="info-box-track mb-12 info-display-large">
                    <div class="flex justify-between items-start mb-4">
                        <span class="info-label text-muted">Status</span>
                        <span class="info-meta font-mono" title="Direction: ${detailDirFull} · raw=${direction}">${detailDirCompact}</span>
                    </div>

                    <div class="text-2xl font-bold mb-6 font-mono path-status-title ${statusClass}">
                        ${path.status.toUpperCase()}
                    </div>

                    <div class="grid grid-cols-1 gap-y-4 text-sm font-mono">
                        <div>
                            <span class="info-label text-muted block mb-2">Destination</span>
                            <div class="path-route-stack">
                                <div class="info-value text-white opacity-90">→ ${destination}</div>
                            </div>
                        </div>
                        <div>
                            <span class="info-label text-muted block">Train</span>
                            <span class="info-value ${path.train_id ? 'text-info' : 'text-muted'}">${path.train_id ? `🚂 ${path.train_id}` : 'None assigned'}</span>
                        </div>
                        ${
                          path.status === 'active'
                            ? `
                        <div>
                            <span class="info-label text-muted block mb-1">Progress</span>
                            <div class="progress-container path-progress-full">
                                <progress class="path-progress-meter ${statusClass}" value="${progress}" max="100"></progress>
                            </div>
                            <span class="info-meta mt-1">${progress}% (${currentIndex}/${totalSteps})</span>
                        </div>
                        `
                            : ''
                        }
                    </div>
                </div>

                <!-- CONTROL BUTTONS -->
                <div class="control-section mt-12">
                    <h4 class="text-sm text-muted mb-8">Actions</h4>
                    <div class="flex gap-8">
                        ${
                          path.status === 'set' || path.status === 'active'
                            ? `
                            <button id="extend-path-btn" class="btn btn-primary btn-sm flex-1">
                                ➕ Extend
                            </button>
                        `
                            : ''
                        }
                        <button id="cancel-path-btn" class="btn btn-danger btn-sm flex-1 btn-danger-solid">
                            ❌ Cancel
                        </button>
                    </div>
                </div>

                <!-- RESOURCE LIST (Scrollable) -->
                <div class="control-section control-section-divider mt-12 pt-12">
                    <h4 class="text-sm text-muted mb-8">Resources (${totalSteps})</h4>
                    <div class="bg-input rounded-lg path-block-list-wrap">
                        ${resourceListHtml || '<div class="text-muted text-xs">No resources</div>'}
                    </div>
                </div>

                <button id="back-to-paths-btn" class="btn btn-secondary btn-full btn-sm mt-12">
                    ← Back to Paths List
                </button>
            </div>
        `;

    this.editor.showSecondarySidebar('🛤️ Path Details', content);

    setTimeout(() => this.initPathEditorHandlers(pathId, path), 0);

    if (this.renderer.highlightPath) {
      this.renderer.highlightPath(path);
    }
  }

  initPathEditorHandlers(pathId: any, path: any): void {
    const backBtn = document.getElementById('back-to-paths-btn') as HTMLButtonElement | null;
    if (backBtn) {
      backBtn.onclick = async () => {
        if (this.renderer.clearPathHighlight) {
          this.renderer.clearPathHighlight();
        }
        await this.showPathsPanel();
      };
    }

    const cancelBtn = document.getElementById('cancel-path-btn') as HTMLButtonElement | null;
    if (cancelBtn) {
      cancelBtn.onclick = async () => {
        const detail = 'Reserved blocks and switches for this path will be released.';

        const confirmed = await showConfirmModal({
          title: 'Cancel Path',
          message: `Cancel path "${pathId}"?`,
          detail,
          confirmText: 'Cancel Path',
          confirmVariant: 'danger',
        });
        if (!confirmed) return;

        try {
          const result = await this.api.cancelPath(pathId);
          if (result.success) {
            const queued = result.status === 'queued';
            let message;
            if (queued) {
              message = `Path ${pathId} cancellation queued — train is braking`;
            } else {
              const releasedCount =
                (result.released_block_ids?.length || 0) +
                (result.released_switch_ids?.length || 0);
              const suffix =
                releasedCount > 0
                  ? ` (${releasedCount} resource${releasedCount === 1 ? '' : 's'} released)`
                  : '';
              message = `Path ${pathId} cancelled${suffix}`;
            }
            showToast(message, 'success', 5000);
            await window.app?.refreshNetwork();
            if (this.renderer.clearPathHighlight) {
              this.renderer.clearPathHighlight();
            }
            await this.showPathsPanel();
            if (queued) {
              setTimeout(async () => {
                try {
                  await window.app?.refreshNetwork();
                  await this.showPathsPanel();
                } catch (err) {
                  console.warn('[Editor] Deferred path refresh failed:', err);
                }
              }, 500);
            }
          } else {
            showToast(
              `Failed to cancel: ${result.reason || result.message || result.error}`,
              'error',
              5000
            );
          }
        } catch (err: any) {
          console.error('[Editor] Failed to cancel path:', err);
          showToast('Failed to cancel path: ' + err.message, 'error', 5000);
        }
      };
    }

    const extendBtn = document.getElementById('extend-path-btn') as HTMLButtonElement | null;
    if (extendBtn) {
      extendBtn.onclick = async () => {
        showToast('Path extension not yet implemented', 'info', 3000);
      };
    }
  }

  enterExtendRouteMode(): void {
    this.extendPathState = {
      active: false,
      selectedPathId: null,
      selectedPath: null,
      reachableEndpoints: [],
      reachablePositions: new Set(),
    };
    setStatusState('Extend Path: Click on a path to extend');
  }

  async exitExtendRouteMode(): Promise<void> {
    await this.resetExtendRouteState();
    setStatusState('Ready');
  }

  // EditorMode factory for the extend-route tool.
  buildExtendRouteMode(): any {
    return {
      name: 'Extend route',
      toolName: 'extend-path',
      cancelMessage: 'Extend route cancelled',
      onEnter: () => this.enterExtendRouteMode(),
      onClick: async ({ gridPos }: { gridPos: any }) => {
        await this.handleExtendRouteToolClick(gridPos);
        return true;
      },
      onExit: () => this.exitExtendRouteMode(),
    };
  }

  async resetExtendRouteState(): Promise<void> {
    this.extendPathState = {
      active: false,
      selectedPathId: null,
      selectedPath: null,
      reachableEndpoints: [],
      reachablePositions: new Set(),
    };
    if (this.renderer.setReachableHighlights) {
      this.renderer.setReachableHighlights(new Set());
    }
    if (this.renderer.clearPathHighlight) {
      this.renderer.clearPathHighlight();
    }
    if (this.editor._currentEditorType === 'extend-path-setup') {
      await this.editor.goBackSecondaryPanel();
      // If history was empty, goBack does nothing - close sidebar as fallback.
      if (this.editor._currentEditorType === 'extend-path-setup') {
        this.editor.hideSecondarySidebar();
      }
    }
    if (this.cachedNetwork) {
      this.renderer.requestRedraw();
    }
  }

  async handleExtendRouteToolClick(gridPos: any): Promise<void> {
    const track = this.cachedNetwork?.tracks?.find((t: any) => t.x === gridPos.x && t.y === gridPos.y);

    if (!this.extendPathState?.active) {
      await this.handleExtendRouteFirstClick(gridPos, track);
    } else {
      await this.handleExtendRouteSecondClick(gridPos, track);
    }
  }

  async handleExtendRouteFirstClick(gridPos: any, track: any): Promise<void> {
    if (!track) {
      showToast('No track at this position', 'warning', 3000);
      return;
    }

    const blockId = track.block_section_id;
    if (!blockId) {
      showToast('No block at this position', 'warning', 3000);
      return;
    }

    try {
      const paths = await this.api.getPaths();

      const pathContainsBlock = (p: any) => {
        const resources = p.resource_sequence || [];
        return resources.some((r: any) => r.resource_type === 'block' && r.resource_id === blockId);
      };

      const matchingPaths = paths.filter(
        (p: any) =>
          p.authority_end_block_id === blockId &&
          ['pending', 'setting', 'set', 'active'].includes(p.status)
      );

      if (matchingPaths.length === 0) {
        const partOfPath = paths.find(
          (p: any) =>
            pathContainsBlock(p) && ['pending', 'setting', 'set', 'active'].includes(p.status)
        );

        if (partOfPath) {
          showToast('Can only extend from the END of a path. Click on the end block.', 'warning', 4000);
        } else {
          showToast('No active path ends at this block', 'warning', 3000);
        }
        return;
      }

      const selectedPath = matchingPaths[0];

      this.extendPathState.selectedPathId = selectedPath.path_id;
      this.extendPathState.selectedPath = selectedPath;
      this.extendPathState.active = true;

      if (this.renderer.highlightPath) {
        this.renderer.highlightPath(selectedPath);
      }

      setStatusState('Extend Path: Loading destinations...');
      const endpointsResult = await this.api.getPathReachableExtensions(selectedPath.path_id);

      if (endpointsResult.error) {
        showToast('Cannot extend path: ' + endpointsResult.error, 'error', 5000);
        await this.resetExtendRouteState();
        return;
      }

      this.extendPathState.reachableEndpoints = endpointsResult.reachable_endpoints || [];
      this.extendPathState.exitDirection = endpointsResult.exit_direction;

      this.buildExtendRoutePositionsSet();

      const count = this.extendPathState.reachableEndpoints.length;
      setStatusState(`Extend Path: Click new destination (${count} reachable)`);

      if (this.renderer.setReachableHighlights) {
        this.renderer.setReachableHighlights(this.extendPathState.reachablePositions);
      }

      this.showExtendRoutePanel();

      if (this.cachedNetwork) {
        this.renderer.requestRedraw();
      }
    } catch (err: any) {
      console.error('[Editor] Failed to get paths for extend:', err);
      showToast('Failed to load paths: ' + err.message, 'error', 5000);
      await this.resetExtendRouteState();
    }
  }

  buildExtendRoutePositionsSet(): void {
    const positions = new Set<string>();
    const tracks = this.cachedNetwork?.tracks || [];

    for (const endpoint of this.extendPathState.reachableEndpoints) {
      for (const track of tracks) {
        if (track.block_section_id === endpoint) {
          positions.add(`${track.x},${track.y}`);
        }
      }
    }

    this.extendPathState.reachablePositions = positions;
  }

  async handleExtendRouteSecondClick(gridPos: any, track: any): Promise<void> {
    if (!track) {
      showToast('No track at this position', 'warning', 3000);
      return;
    }

    const newEndBlockId = track.block_section_id;
    if (!newEndBlockId) {
      showToast('Must select a block as destination', 'warning', 3000);
      return;
    }

    if (!this.extendPathState.reachableEndpoints.includes(newEndBlockId)) {
      showToast('Cannot extend to this block', 'warning', 3000);
      return;
    }

    try {
      setStatusState('Extend Path: Extending...');

      const direction = this.extendPathState.selectedPath?.requested_destination_direction || 'forward';
      const result = await this.api.extendPath(this.extendPathState.selectedPathId, {
        requested_destination_block_id: newEndBlockId,
        requested_destination_direction: direction,
        acquisition_mode: 'all_or_nothing',
      });

      if (result.success) {
        showToast(`Path extended to ${newEndBlockId}`, 'success', 5000);
        await window.app?.refreshNetwork();
        await this.resetExtendRouteState();
        setStatusState('Extend Path: Click on a path to extend');
      } else {
        const errorMsg = result.error || result.failure_kind || result.message || 'unknown error';
        showToast(`Extend failed: ${errorMsg}`, 'error', 5000);
      }
    } catch (err: any) {
      console.error('[Editor] Failed to extend route:', err);
      showToast('Failed to extend route: ' + err.message, 'error', 5000);
    }
  }

  showExtendRoutePanel(): void {
    this.editor._currentEditorType = 'extend-path-setup';
    this.editor._currentEditorId = this.extendPathState.selectedPathId;

    const route = this.extendPathState.selectedPath;
    const exitDir = this.extendPathState.exitDirection;
    const exitDirText = exitDir ? this.getDirectionName(exitDir) : 'Unknown';
    const reachableCount = this.extendPathState.reachableEndpoints.length;

    const currentEnd = route.authority_end_block_id || route.requested_destination_block_id || '—';

    const content = `
                <div class="extend-route-panel">
                ${renderHeaderId(route.path_id, this.getPathStatusColor(route.status))}

                ${renderReadOnlyValue('Current End', currentEnd, 'value-display-mono text-sm')}
                ${renderReadOnlyValue('Direction', `${exitDirText} (${exitDir || '?'})`, 'text-info font-bold')}
                ${renderReadOnlyValue('Status', route.status.toUpperCase(), `font-bold`)}
                ${
                  route.train_id
                    ? renderReadOnlyValue('Train', `${route.train_id}`, 'text-info font-bold')
                    : ''
                }

                <div class="info-box text-sm mt-16">
                    ${
                      reachableCount > 0
                        ? `Click on an <span class="text-warning">orange</span> destination block to extend the route ${exitDirText.toLowerCase()}.`
                        : `<span class="text-danger">No forward destinations available.</span> The route may be at a dead end or all forward blocks are occupied.`
                    }
                    <br><br>
                    Right-click or press ESC to cancel.
                </div>
            </div>
        `;

    this.editor.showSecondarySidebar('+ Extend Path', content);

    const sidebar = document.getElementById('secondary-sidebar');
    if (sidebar) {
      sidebar.classList.add('compact');
    }
  }

  getDirectionName(dir: any): string {
    const names: Record<string, string> = { N: 'North', S: 'South', E: 'East', W: 'West' };
    return names[dir] || dir || 'Unknown';
  }

  enterCancelRouteMode(): void {
    setStatusState('Cancel Path: Click on a path to cancel');
  }

  exitCancelRouteMode(): void {
    setStatusState('Ready');
  }

  // EditorMode factory for the cancel-route tool.
  buildCancelRouteMode(): any {
    return {
      name: 'Cancel route',
      toolName: 'cancel-path',
      cancelMessage: 'Cancel-route mode exited',
      onEnter: () => this.enterCancelRouteMode(),
      onClick: async ({ gridPos }: { gridPos: any }) => {
        await this.handleCancelRouteToolClick(gridPos);
        return true;
      },
      onExit: () => this.exitCancelRouteMode(),
    };
  }

  async handleCancelRouteToolClick(gridPos: any): Promise<void> {
    const track = this.cachedNetwork?.tracks?.find((t: any) => t.x === gridPos.x && t.y === gridPos.y);

    if (!track) {
      showToast('No track at this position', 'warning', 3000);
      return;
    }

    const blockId = track.block_section_id;
    if (!blockId) {
      showToast('No block at this position', 'warning', 3000);
      return;
    }

    try {
      const paths = await this.api.getPaths();

      const pathContainsBlock = (p: any) => {
        const resources = p.resource_sequence || [];
        return resources.some((r: any) => r.resource_type === 'block' && r.resource_id === blockId);
      };

      const matchingPaths = paths.filter(
        (p: any) =>
          pathContainsBlock(p) && ['pending', 'setting', 'set', 'active'].includes(p.status)
      );

      if (matchingPaths.length === 0) {
        showToast('No active path found at this block', 'warning', 3000);
        return;
      }

      const pathToCancel = matchingPaths[0];
      const destination =
        pathToCancel.authority_end_block_id || pathToCancel.requested_destination_block_id || '—';
      const detail =
        `Destination: ${destination}` +
        (pathToCancel.train_id ? `\nTrain: ${pathToCancel.train_id}` : '');

      const confirmed = await showConfirmModal({
        title: 'Cancel Path',
        message: `Cancel path "${pathToCancel.path_id}"?`,
        detail,
        confirmText: 'Cancel Path',
        confirmVariant: 'danger',
      });
      if (!confirmed) {
        return;
      }

      setStatusState('Cancel Path: Cancelling...');

      const cancelResult = await this.api.cancelPath(pathToCancel.path_id);

      if (cancelResult.success) {
        const queued = cancelResult.status === 'queued';
        const message = queued
          ? `Path ${pathToCancel.path_id} cancellation queued — train is braking`
          : `Path ${pathToCancel.path_id} cancelled`;
        showToast(message, 'success', 5000);
        await window.app?.refreshNetwork();
        if (queued) {
          setTimeout(async () => {
            try {
              await window.app?.refreshNetwork();
            } catch (err) {
              console.warn('[Editor] Deferred path refresh failed:', err);
            }
          }, 500);
        }
      } else {
        showToast(
          `Failed to cancel: ${cancelResult.reason || cancelResult.error || 'unknown error'}`,
          'error',
          5000
        );
      }

      setStatusState('Cancel Path: Click on a path to cancel');
    } catch (err: any) {
      console.error('[Editor] Failed to cancel path:', err);
      showToast('Failed to cancel path: ' + err.message, 'error', 5000);
      setStatusState('Cancel Path: Click on a path to cancel');
    }
  }
}
