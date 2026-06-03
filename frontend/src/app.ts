// app.ts — top-level application bootstrap (header controls, simulation
// polling, network refresh, layout save/load, settings, train selection,
// the toast system, and sidebar resize).
//
// Migrated from static/js/app.js (longterm_todo.md item #7, Wave 4 — the last
// classic script). Behavior is unchanged. main.ts imports this module last;
// because module scripts are deferred, this runs before DOMContentLoaded, so
// the `new App()` bootstrap at the bottom fires once the DOM is ready.
//
// api / stateManager / showToast are imported from lib/. window.app is set on
// DOMContentLoaded (the App instance) for the editor handlers and panels that
// call app methods.

import { Renderer } from './renderer';
import { Editor } from './editor/editor';
import { TrainMonitor } from './panels/train-monitor';
import { showConfirmModal, showFormModal } from './lib/helpers';
import { setLabelSpacing } from './lib/constants';
import { api } from './lib/api';
import { stateManager } from './lib/state-manager';
import { showToast } from './lib/toast';

class App {
  renderer: any;
  editor: any;
  trainMonitor: any;

  simulationInterval: ReturnType<typeof setInterval> | null;
  currentNetwork: any;
  hasInitiallyCentered: boolean;
  hasInitiallySelected: boolean;
  isSimulationRunning: boolean;
  editorMode: 'edit' | 'sim' = 'edit';
  selectedTrainId: string | null;
  lastLookaheadFetch: number;
  lookaheadThrottleMs: number;
  lastLookaheadData: any;
  lastLookaheadPayload: any;
  trainMonitorUnsub: (() => void) | null;
  followTrainEnabled: boolean;

  constructor() {
    this.renderer = new Renderer('rail-canvas');
    this.editor = new Editor(this.renderer, api);
    this.trainMonitor = new TrainMonitor();

    this.simulationInterval = null;
    this.currentNetwork = null;
    this.hasInitiallyCentered = false;
    this.hasInitiallySelected = false;
    this.isSimulationRunning = false;
    this.editorMode = 'edit';
    this.selectedTrainId = null;
    this.lastLookaheadFetch = 0;
    this.lookaheadThrottleMs = 100;
    this.lastLookaheadData = null;  // Store last lookahead for panel rebuilds
    this.lastLookaheadPayload = null;
    this.trainMonitorUnsub = null;
    // Viewport follow toggle. Off by default — clicking a train no longer
    // pans the canvas. Users opt in via the Follow Train checkbox.
    this.followTrainEnabled = false;

    this.setupControls();

    this.setupDebugPanel();
    this.setupSettingsPanel();
    this.setupViewControls();
    this.refreshNetwork();
  }


  setupControls(): void {
    const playPauseBtn = document.getElementById('btn-play-pause')!;
    playPauseBtn.addEventListener('click', () => {
      if (this.isSimulationRunning) {
        // Pause
        api.controlSimulation('stop').catch((e: any) => console.error('[App] Pause error:', e));
        this.stopSimulationPolling();
        this.updatePlaybackUI(false);
      } else {
        // Play
        api.controlSimulation('start').catch((e: any) => console.error('[App] Play error:', e));
        this.startSimulationPolling();
        this.updatePlaybackUI(true);
      }
    });

    const clearFatalBtn = document.getElementById('btn-clear-fatal') as HTMLButtonElement | null;
    if (clearFatalBtn) {
      clearFatalBtn.addEventListener('click', async () => {
        clearFatalBtn.disabled = true;
        try {
          const res = await api.releaseFailedRuntime();
          if (res && res.released) {
            showToast('Conflict cleared — simulation resumed', 'success', 3000);
          } else {
            showToast('Conflict still present — resolve it on the layout, then clear again', 'warning', 6000);
          }
        } catch (e: any) {
          console.error('[App] Clear-fatal error:', e);
          showToast(`Could not clear halt: ${e.message}`, 'error', 5000);
        } finally {
          clearFatalBtn.disabled = false;
          this.refreshNetwork();
        }
      });
    }

    document.getElementById('btn-restart-server')!.addEventListener('click', async () => {
      const confirmed = await showConfirmModal({
        title: 'Restart Server?',
        message: 'The server process will exit and relaunch.',
        detail: 'Unsaved layout edits will be flushed to autosave first. The page will reload automatically when the server is back (~3–5s).',
        confirmText: 'Restart',
        cancelText: 'Cancel',
        confirmVariant: 'danger'
      });
      if (!confirmed) return;

      try {
        await api.restartServer();
      } catch (e: any) {
        console.error('[App] Restart request failed:', e);
        showToast('Could not reach server to restart.', 'error', 4000);
        return;
      }

      this.stopSimulationPolling();
      this.updatePlaybackUI(false);
      this.startRestartReconnectFlow();
    });

    document.querySelectorAll<HTMLElement>('.speed-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const speed = parseFloat((e.currentTarget as HTMLElement).dataset.speed ?? '');
        if (!Number.isFinite(speed)) return;
        try {
          await api.controlSimulation('set_speed', speed);
        } catch (err: any) {
          console.error('[App] set_speed error:', err);
          showToast(`Failed to set speed: ${err.message}`, 'error', 2000);
          return;
        }
        this.syncSpeedPresetUI(speed);
        showToast(`Speed set to ${speed}x`, 'info', 1000);
      });
    });

    document.getElementById('mode-edit')!.addEventListener('click', () => {
      this.setEditorMode('edit');
    });

    document.getElementById('mode-sim')!.addEventListener('click', () => {
      this.setEditorMode('sim');
    });

    // Apply the initial (Editor) tool-availability state.
    this.setEditorMode('edit');

    // Layout controls
    this.setupLayoutControls();
  }

  /**
   * Switch the header Editor/Simulation toggle and gate the tool palette.
   *
   * In Simulation mode the layout must not change, so every tool that builds
   * or deletes track, blocks, paths, platforms, trains or routes is disabled.
   * Only the plain Select tool (to pick/inspect entities, including trains)
   * and the read-only viewer panels (data-tool ending in `-panel`) stay live.
   * The rule keys off the tool name so new `-panel` viewers stay enabled and
   * any new build tool is disabled by default.
   */
  setEditorMode(mode: 'edit' | 'sim'): void {
    const isSim = mode === 'sim';
    this.editorMode = mode;

    document.getElementById('mode-edit')!.classList.toggle('active', !isSim);
    document.getElementById('mode-sim')!.classList.toggle('active', isSim);

    let activeToolDisabled = false;
    document.querySelectorAll<HTMLButtonElement>('.tool-btn').forEach((btn) => {
      const tool = btn.dataset.tool || '';
      const allowedInSim = tool === 'inspect' || tool.endsWith('-panel');
      const disabled = isSim && !allowedInSim;
      btn.disabled = disabled;
      if (disabled && btn.classList.contains('active')) activeToolDisabled = true;
    });

    // If the tool we were holding just got disabled, fall back to Select and
    // leave any active build mode so the editor is not stuck mid-build.
    if (activeToolDisabled && this.editor) {
      const revert = () => this.editor._setSelectedTool('select');
      if (this.editor.activeMode) {
        Promise.resolve(this.editor.exitMode()).then(revert);
      } else {
        revert();
      }
    }
  }


  updatePlaybackUI(isPlaying: boolean): void {
    const btn = document.getElementById('btn-play-pause');
    if (!btn) return;

    if (isPlaying) {
      btn.innerHTML = '<span class="icon">⏸</span> Pause';
      btn.classList.remove('btn-primary');
      btn.classList.add('btn-secondary');
    } else {
      btn.innerHTML = '<span class="icon">▶</span> Play';
      btn.classList.remove('btn-secondary');
      btn.classList.add('btn-primary');
    }
  }

  syncSpeedPresetUI(serverSpeed: number): void {
    const buttons = document.querySelectorAll<HTMLElement>('.speed-btn');
    if (!buttons.length) return;
    let match: HTMLElement | null = null;
    for (const btn of buttons) {
      if (Math.abs(parseFloat(btn.dataset.speed ?? '') - serverSpeed) < 1e-6) {
        match = btn;
        break;
      }
    }
    if (!match) return;
    if (match.classList.contains('active')) return;
    buttons.forEach(b => b.classList.remove('active'));
    match.classList.add('active');
  }

  setupLayoutControls(): void {
    const newBtn = document.getElementById('btn-new-layout');
    const saveBtn = document.getElementById('btn-save-layout');
    const loadBtn = document.getElementById('btn-load-layout');
    const saveModal = document.getElementById('save-layout-modal');
    const loadModal = document.getElementById('load-layout-modal');

    // New layout button
    if (newBtn) {
      newBtn.addEventListener('click', async () => {
        await this.newLayout();
      });
    }

    // Open save modal
    if (saveBtn) {
      saveBtn.addEventListener('click', async () => {
        this.openModal(saveModal);
        (document.getElementById('layout-name') as HTMLInputElement).value = '';
        (document.getElementById('layout-description') as HTMLTextAreaElement).value = '';
        document.getElementById('layout-name')!.focus();
        // Load existing layouts for overwrite selection
        await this.loadSaveLayoutsList();
      });
    }

    // Open load modal
    if (loadBtn) {
      loadBtn.addEventListener('click', async () => {
        this.openModal(loadModal);
        await this.loadLayoutsList();
      });
    }

    // Open settings modal (hosts the Display + Debug Info controls). Closing is
    // handled by the generic modal machinery below (close button, backdrop,
    // Escape), so only the open handler is wired here.
    const settingsBtn = document.getElementById('btn-settings');
    const settingsModal = document.getElementById('settings-modal');
    if (settingsBtn) {
      settingsBtn.addEventListener('click', () => {
        this.openModal(settingsModal);
      });
    }

    // Save layout confirm
    const confirmSaveBtn = document.getElementById('btn-confirm-save');
    if (confirmSaveBtn) {
      confirmSaveBtn.addEventListener('click', async () => {
        await this.saveCurrentLayout();
      });
    }

    // Allow Enter key to save
    const layoutNameInput = document.getElementById('layout-name');
    if (layoutNameInput) {
      layoutNameInput.addEventListener('keydown', async (e) => {
        if (e.key === 'Enter') {
          await this.saveCurrentLayout();
        }
      });
    }

    // Close modal buttons
    document.querySelectorAll('[data-close-modal]').forEach(btn => {
      btn.addEventListener('click', (e) => {
        const modal = (e.target as HTMLElement).closest('.modal');
        if (modal) this.closeModal(modal);
      });
    });

    // Close modal on backdrop click
    document.querySelectorAll('.modal').forEach(modal => {
      modal.addEventListener('click', (e) => {
        if (e.target === modal) {
          this.closeModal(modal);
        }
      });
    });

    // Close modal on Escape key
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        document.querySelectorAll('.modal.active').forEach(modal => {
          this.closeModal(modal);
        });
      }
    });
  }

  openModal(modal: Element | null): void {
    if (modal) modal.classList.add('active');
  }

  closeModal(modal: Element | null): void {
    if (modal) modal.classList.remove('active');
  }

  async saveCurrentLayout(): Promise<void> {
    const nameInput = document.getElementById('layout-name') as HTMLInputElement;
    const descInput = document.getElementById('layout-description') as HTMLTextAreaElement;
    const name = nameInput.value.trim();
    const description = descInput.value.trim();

    if (!name) {
      showToast('Please enter a layout name', 'error');
      nameInput.focus();
      return;
    }

    try {
      const result = await api.saveLayout(name, description);

      // Show success message with step count
      let message = `Layout "${name}" saved`;
      if (result.steps_count > 0) {
        message += ` (${result.steps_count} steps)`;
      }
      showToast(message, 'success');

      // Show warning if steps failed
      if (result.warning) {
        showToast(`Warning: ${result.warning}`, 'warning');
      }

      this.closeModal(document.getElementById('save-layout-modal'));
    } catch (e: any) {
      showToast(`Failed to save layout: ${e.message}`, 'error');
    }
  }

  async loadSaveLayoutsList(): Promise<void> {
    const listEl = document.getElementById('save-layouts-list')!;
    listEl.innerHTML = '<p class="loading-text">Loading...</p>';

    try {
      const layouts = await api.getLayouts();
      // Filter out old _steps suffix files (now merged into main file)
      const filteredLayouts = layouts.filter((l: any) => !l.name.endsWith('_steps'));

      if (filteredLayouts.length === 0) {
        listEl.innerHTML = '<p class="empty-text">No existing layouts</p>';
        return;
      }

      listEl.innerHTML = filteredLayouts.map((layout: any) => `
                <div class="layout-item" data-name="${layout.name}">
                    <div class="layout-item-info">
                        <span class="layout-item-name">${this.escapeHtml(layout.name)}</span>
                    </div>
                </div>
            `).join('');

      // Click to select and populate name field
      listEl.querySelectorAll<HTMLElement>('.layout-item').forEach(item => {
        item.addEventListener('click', () => {
          // Clear previous selection
          listEl.querySelectorAll('.layout-item').forEach(el => el.classList.remove('selected'));
          // Select this one
          item.classList.add('selected');
          // Populate name field
          (document.getElementById('layout-name') as HTMLInputElement).value = item.dataset.name ?? '';
        });
      });
    } catch (e: any) {
      listEl.innerHTML = '<p class="empty-text">Failed to load layouts</p>';
    }
  }

  async loadLayoutsList(): Promise<void> {
    const listEl = document.getElementById('layouts-list')!;
    listEl.innerHTML = '<p class="loading-text">Loading layouts...</p>';

    try {
      const layouts = await api.getLayouts();
      // Filter out _steps suffix files (old format, now merged)
      const filteredLayouts = layouts.filter((l: any) => !l.name.endsWith('_steps'));

      if (filteredLayouts.length === 0) {
        listEl.innerHTML = '<p class="empty-text">No saved layouts yet</p>';
        return;
      }

      listEl.innerHTML = filteredLayouts.map((layout: any) => {
        const isAutosave = layout.filename === '_autosave';
        const displayName = isAutosave ? 'Auto-save (Recovery)' : layout.name;

        // Build independent badges for snapshot and steps
        let badges = '';
        if (layout.has_snapshot) {
          badges += '<span class="layout-format-badge snapshot">snapshot</span>';
        }
        if (layout.has_steps) {
          badges += '<span class="layout-format-badge steps">steps</span>';
        }

        const itemClass = isAutosave ? 'layout-item autosave' : 'layout-item';

        return `
                <div class="${itemClass}" data-filename="${layout.filename}">
                    <div class="layout-item-info">
                        <div class="layout-item-name">${this.escapeHtml(displayName)}${badges}</div>
                        <div class="layout-item-meta">
                            ${layout.description ? this.escapeHtml(layout.description) + ' · ' : ''}
                            ${this.formatDate(layout.modified)}
                        </div>
                    </div>
                    <div class="layout-item-actions">
                        <button class="btn btn-primary btn-load-item">Load</button>
                        <button class="btn btn-danger btn-delete-item">Delete</button>
                    </div>
                </div>
            `}).join('');

      // Add event listeners
      listEl.querySelectorAll('.btn-load-item').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const item = (e.target as HTMLElement).closest('.layout-item') as HTMLElement;
          const filename = item.dataset.filename ?? '';
          await this.loadSelectedLayout(filename);
        });
      });

      listEl.querySelectorAll('.btn-delete-item').forEach(btn => {
        btn.addEventListener('click', async (e) => {
          e.stopPropagation();
          const item = (e.target as HTMLElement).closest('.layout-item') as HTMLElement;
          const filename = item.dataset.filename ?? '';
          const name = item.querySelector('.layout-item-name')!.textContent ?? '';
          await this.deleteSelectedLayout(filename, name);
        });
      });

    } catch (e: any) {
      listEl.innerHTML = `<p class="empty-text">Error loading layouts: ${e.message}</p>`;
    }
  }

  async loadSelectedLayout(filename: string): Promise<void> {
    try {
      await api.loadLayout(filename);
      showToast('Layout loaded successfully', 'success');
      this.closeModal(document.getElementById('load-layout-modal'));
      await this.refreshNetwork();
    } catch (e: any) {
      showToast(`Failed to load layout: ${e.message}`, 'error');
    }
  }

  async deleteSelectedLayout(filename: string, name: string): Promise<void> {
    const confirmed = await showConfirmModal({
      title: 'Delete Layout',
      message: `Delete layout "${name}"?`,
      detail: 'This action cannot be undone.',
      confirmText: 'Delete',
      confirmVariant: 'danger'
    });
    if (!confirmed) {
      return;
    }

    try {
      await api.deleteLayout(filename);
      showToast(`Layout "${name}" deleted`, 'success');
      await this.loadLayoutsList();
    } catch (e: any) {
      showToast(`Failed to delete layout: ${e.message}`, 'error');
    }
  }

  async newLayout(): Promise<void> {
    try {
      // Check if there's anything on the layout
      const stats = await api.getNetworkStats();

      if (!stats.is_empty) {
        // Build a summary of what will be cleared
        const items = [];
        if (stats.tracks > 0) items.push(`${stats.tracks} track(s)`);
        if (stats.blocks > 0) items.push(`${stats.blocks} block(s)`);
        if (stats.signals > 0) items.push(`${stats.signals} signal(s)`);
        if (stats.stations > 0) items.push(`${stats.stations} station(s)`);
        if (stats.trains > 0) items.push(`${stats.trains} train(s)`);

        const summary = items.join(', ');
        const confirmed = await showConfirmModal({
          title: 'Create New Layout',
          message: 'Create a new blank layout?',
          detail: `This will clear the current layout:\n${summary}`,
          confirmText: 'Clear Layout',
          confirmVariant: 'danger'
        });

        if (!confirmed) {
          return;
        }
      }

      // Clear the network
      await api.clearNetwork();
      showToast('New layout created', 'success');
      await this.refreshNetwork();

    } catch (e: any) {
      showToast(`Failed to create new layout: ${e.message}`, 'error');
    }
  }

  escapeHtml(str: string): string {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  formatDate(isoString: string): string {
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'Just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;

    return date.toLocaleDateString();
  }

  async refreshNetwork(): Promise<void> {
    // Skip refresh if editor is actively using a tool that needs hover preview
    // This prevents flickering when moving the mouse with signal/station tools
    if (this.editor && this.editor.isToolActive()) {
      // Still update the cache and trains panel, but don't redraw directly
      // The setCachedNetwork call will trigger re-validation of hover state
      try {
        const network = await api.getNetwork();
        this.currentNetwork = network;

        // NEW: Update StateManager cache
        stateManager.updateFromNetwork(network);

        this.editor.setCachedNetwork(network);
        // Validate that the current editor still exists (close if deleted)
        this.editor.validateCurrentEditor();
        // Don't call drawWithHoverPreview here - setCachedNetwork triggers updateHoverPreview
        // which will call drawWithHoverPreview after re-validating the signal state
      } catch (e: any) {
        console.error('[App] Failed to refresh network:', e);
      }
      return;
    }

    try {
      const network = await api.getNetwork();

      // Pull station groups alongside so the renderer can draw station
      // labels at the centroid of each station's platforms. Failure here
      // shouldn't block the rest of the render.
      try {
        const stationResp = await api.listStationGroups();
        network.stations = stationResp?.stations || [];
      } catch (_) {
        network.stations = [];
      }

      this.currentNetwork = network;

      // NEW: Update StateManager cache
      stateManager.updateFromNetwork(network);

      this.editor.setCachedNetwork(network);
      if (!this.hasInitiallyCentered && network.tracks && network.tracks.length > 0) {
        this.renderer.centerOnContent(network.tracks);
        this.hasInitiallyCentered = true;
      }
      // Fresh network arrived: hand it to the renderer and let the
      // rAF coalescer flush the paint. _maybeFollowSelectedTrain (if
      // enabled) also calls requestRedraw — both collapse to one paint.
      this.renderer.requestRedraw({ network, reason: 'network-refresh' });
      this._maybeFollowSelectedTrain();
      // Validate that the current editor still exists (close if deleted)
      this.editor.validateCurrentEditor();

      // On first successful load, pre-select tile (0,0) so the Track Bits
      // sidebar section has a track to show by default. selectElement
      // handles the missing-tile case by creating a virtual empty tile.
      if (!this.hasInitiallySelected && network.tracks) {
        this.hasInitiallySelected = true;
        this.editor.selectElement({ x: 0, y: 0 }).catch((e: any) =>
          console.warn('[App] Initial select(0,0) failed:', e)
        );
      }

      // Sync play/pause button with server state
      if (typeof network.simulation_running === 'boolean') {
        this.isSimulationRunning = network.simulation_running;
        this.updatePlaybackUI(this.isSimulationRunning);
      }

      // Sync speed-preset active state with server state
      if (typeof network.simulation_speed === 'number') {
        this.syncSpeedPresetUI(network.simulation_speed);
      }

      // Surface a fatal-conflict halt (failed_runtime) if one is active.
      this.updateFatalBanner(network);
    } catch (e: any) {
      console.error('[App] Failed to refresh network:', e);
    }
  }

  /**
   * Show/hide the fatal-conflict halt banner. When the runtime is halted
   * (failed_runtime), update() skips every train tick — trains freeze at
   * their last state (often RUNNING_MOVING / handle Px) while the clock
   * keeps advancing, and play/pause cannot recover it. This banner is the
   * only surface for that state and the only path to recovery (the Clear
   * button calls release-failed-runtime).
   */
  updateFatalBanner(network: any): void {
    const banner = document.getElementById('fatal-banner');
    if (!banner) return;

    if (!network || !network.failed_runtime) {
      banner.hidden = true;
      return;
    }

    const detailEl = document.getElementById('fatal-banner-detail');
    const metaEl = document.getElementById('fatal-banner-meta');
    const c = network.failed_runtime_conflict;
    const tail = ' All trains are halted — resolve the conflict on the layout, then clear to resume.';

    if (detailEl) {
      detailEl.textContent = c
        ? this._describeFatalConflict(c) + tail
        : 'A fatal track conflict halted the simulation.' + tail;
    }
    if (metaEl) {
      metaEl.textContent = c ? this._fatalConflictMeta(c) : '';
    }
    banner.hidden = false;
  }

  /** Plain-English description of a fatal physical-entry conflict. */
  _describeFatalConflict(c: any): string {
    const res = `${c.resource_type} ${c.resource_id}`;
    const incoming = `Train ${c.incoming_train_id}`;
    const prior = c.prior_owner_train_id;
    switch (c.conflict_kind) {
      case 'switch_occupied_by_other_train':
        return `${incoming} tried to enter switch ${c.resource_id}, but train ${prior} already holds it.`;
      case 'occupied_by_other_train':
        return `${incoming} tried to enter block ${c.resource_id}, but train ${prior} already occupies it.`;
      case 'reserved_for_other_train':
        return `${incoming} tried to enter block ${c.resource_id}, but it is reserved for train ${prior}.`;
      case 'disabled_resource_entry':
        return `${incoming} tried to enter ${res}, which is disabled.`;
      case 'malformed_route_key':
        return `${incoming} entered switch ${c.resource_id} without a valid route key — an internal path/geometry error.`;
      default:
        return `${incoming} hit a fatal conflict at ${res}${prior ? ` (held by ${prior})` : ''}.`;
    }
  }

  /** Mono meta line: path ids, halt time, raw kind. */
  _fatalConflictMeta(c: any): string {
    const parts = [];
    if (c.incoming_path_id) parts.push(`${c.incoming_train_id} path: ${c.incoming_path_id}`);
    if (c.prior_owner_path_id && c.prior_owner_train_id) {
      parts.push(`${c.prior_owner_train_id} path: ${c.prior_owner_path_id}`);
    }
    if (typeof c.halted_at_sim_time === 'number') {
      parts.push(`halted at t=${c.halted_at_sim_time.toFixed(1)}s`);
    }
    parts.push(`kind: ${c.conflict_kind}`);
    return parts.join('  ·  ');
  }

  selectTrain(trainId: string): void {
    if (!this.currentNetwork || !this.currentNetwork.trains) {
      return;
    }

    const train = this.currentNetwork.trains.find((t: any) => t.id === trainId);
    if (train) {
      this.selectedTrainId = trainId;
      this.lastLookaheadData = null;
      this.lastLookaheadPayload = null;
      // No auto-pan on selection. The Follow Train toggle opts in to
      // centring on the selected train at each refresh tick via
      // _maybeFollowSelectedTrain.
      if (this.followTrainEnabled) {
        this._maybeFollowSelectedTrain();
      }
      this.editor.showTrainControlPanel(trainId);
      this.trainMonitor.show(trainId, train.name || trainId);
      this._subscribeTrainMonitor(trainId);
      this.fetchTrainLookahead(trainId, train);
      this.updateTrainDebugPanel(train);
    }
  }

  _maybeFollowSelectedTrain(): void {
    if (!this.followTrainEnabled) return;
    if (!this.selectedTrainId) return;
    const controller = stateManager?.getController(this.selectedTrainId);
    const span = controller?.span;
    if (!span?.end_a || !span?.end_b) return;
    const headEnd = span.leading_end === 'a' ? span.end_a : span.end_b;
    if (!headEnd?.tile) return;
    const [gx, gy] = headEnd.tile;
    // Camera.panToWorld handles the isometric math. The previous in-line
    // formula (panX = -gx * tileSize) was flat-cartesian and landed the
    // train off-center.
    this.renderer.camera.panToWorld(gx, gy);
    this.renderer.requestRedraw({ reason: 'follow-train' });
  }

  setupViewControls(): void {
    // Multiplicative zoom step for + / − buttons. Wheel zoom keeps its
    // existing additive feel; the buttons want even relative steps.
    const ZOOM_STEP = 1.25;
    const canvasCenter = () => {
      const canvas = document.getElementById('rail-canvas') as HTMLCanvasElement | null;
      if (!canvas) return { x: 0, y: 0 };
      return { x: canvas.width / 2, y: canvas.height / 2 };
    };

    const zoomInBtn = document.getElementById('btn-zoom-in');
    if (zoomInBtn) {
      zoomInBtn.addEventListener('click', () => {
        const { x, y } = canvasCenter();
        this.editor._zoomAt(x, y, this.renderer.camera.scale * ZOOM_STEP);
      });
    }

    const zoomOutBtn = document.getElementById('btn-zoom-out');
    if (zoomOutBtn) {
      zoomOutBtn.addEventListener('click', () => {
        const { x, y } = canvasCenter();
        this.editor._zoomAt(x, y, this.renderer.camera.scale / ZOOM_STEP);
      });
    }

    const zoomResetBtn = document.getElementById('btn-zoom-reset');
    if (zoomResetBtn) {
      zoomResetBtn.addEventListener('click', () => {
        this.editor._resetZoom();
      });
    }

    const fitBtn = document.getElementById('btn-fit-view');
    if (fitBtn) {
      fitBtn.addEventListener('click', () => {
        if (!this.currentNetwork?.tracks?.length) return;
        this.renderer.centerOnContent(this.currentNetwork.tracks);
        this.renderer.requestRedraw({ reason: 'fit-view' });
      });
    }

    const followToggle = document.getElementById('toggle-follow-train') as HTMLInputElement | null;
    if (followToggle) {
      followToggle.checked = this.followTrainEnabled;
      followToggle.addEventListener('change', (e) => {
        this.followTrainEnabled = !!(e.target as HTMLInputElement).checked;
        if (this.followTrainEnabled) {
          this._maybeFollowSelectedTrain();
        }
      });
    }
  }

  deselectTrain(): void {
    this.selectedTrainId = null;
    this.lastLookaheadData = null;  // Clear stored lookahead
    this.lastLookaheadPayload = null;
    if (this.trainMonitorUnsub) {
      this.trainMonitorUnsub();
      this.trainMonitorUnsub = null;
    }
    this.trainMonitor.hide();
    this.hideTrainDebugPanel();
  }

  _subscribeTrainMonitor(trainId: string): void {
    if (!stateManager) return;
    if (this.trainMonitorUnsub) {
      this.trainMonitorUnsub();
      this.trainMonitorUnsub = null;
    }

    this.trainMonitorUnsub = stateManager.subscribe(
      'trains',
      trainId,
      (newTrain: any) => {
        if (!newTrain) {
          this.deselectTrain();
          return;
        }
        // Only fetch fresh lookahead data -- do NOT render with
        // stale lastLookaheadPayload here.  The async fetch will
        // call updateFromController with the fresh payload once it
        // arrives, preventing the flicker caused by rendering
        // stale-then-fresh on every poll cycle.
        this.fetchTrainLookahead(trainId, newTrain);
      },
      { immediate: true }
    );
  }

  updateTrainDebugPanel(train: any): void {
    const checkbox = document.getElementById('debug-show-train-position') as HTMLInputElement | null;
    const debugGeometry = document.getElementById('train-debug-geometry');

    // If debug is disabled, hide both debug sub-blocks
    if (checkbox && !checkbox.checked) {
      const controlDebug = document.getElementById('train-control-debug-section');
      if (controlDebug) controlDebug.classList.add('hidden');
      if (debugGeometry) debugGeometry.classList.add('hidden');
      return;
    }

    if (!train) return;

    // Target the Train Control panel debug section
    const controlPanelDebug = document.getElementById('train-control-debug-section');
    if (!controlPanelDebug) return;

    // Reveal the geometry + read-only properties block (Status fields moved here).
    if (debugGeometry) debugGeometry.classList.remove('hidden');

    const controller = stateManager.getController(train.id);
    const dispatcher = stateManager.getDispatcherProgress(train.id);
    const span = controller?.span || null;

    // Build the skeleton once. Subsequent calls reuse the same DOM nodes so
    // the Resource Sequence list's scroll position is preserved across the
    // 200ms sim poll + every controller/dispatcher subscription tick.
    let panel = controlPanelDebug.querySelector('.train-debug-panel');
    if (!panel) {
      controlPanelDebug.innerHTML = `
                <div class="train-debug-panel info-display-compact" style="background: transparent; border: none; padding: 0;">
                    <div class="train-debug-row"><span class="train-debug-label">Train ID:</span><span class="train-debug-value" data-debug-field="train-id"></span></div>
                    <div class="train-debug-row"><span class="train-debug-label">State:</span><span class="train-debug-value" data-debug-field="state"></span></div>
                    <div data-debug-block="resource-sequence"></div>
                    <div data-debug-block="occupied-tiles"></div>
                    <div data-debug-block="occupied-blocks"></div>
                </div>
            `;
      panel = controlPanelDebug.querySelector('.train-debug-panel');
    }

    panel!.querySelector('[data-debug-field="train-id"]')!.textContent = train.id;
    panel!.querySelector('[data-debug-field="state"]')!.textContent = controller?.state || 'unknown';

    // Resource sequence: rebuild children only when the sequence identity
    // changes (path reassigned / extended). Otherwise just toggle the
    // `train-debug-path-current` class so scroll position survives.
    const seqBlock = panel!.querySelector('[data-debug-block="resource-sequence"]') as HTMLElement;
    const seq = dispatcher?.resource_sequence;
    if (seq && seq.length > 0) {
      const first = seq[0];
      const last = seq[seq.length - 1];
      const sig = `${dispatcher.path_id || ''}|${seq.length}|${first.resource_id}|${last.resource_id}`;
      let pathContainer = seqBlock.querySelector('.train-debug-path');
      if (!pathContainer || seqBlock.dataset.sig !== sig) {
        let html = `<div class="train-debug-subsection">Resource Sequence (${seq.length})</div>`;
        html += `<div class="train-debug-path">`;
        seq.forEach((entry: any) => {
          html += `<span class="train-debug-path-step">${entry.resource_type}: ${entry.resource_id} (${entry.entry_edge || '?'}→${entry.exit_edge || '?'})</span>`;
        });
        html += `</div>`;
        seqBlock.innerHTML = html;
        seqBlock.dataset.sig = sig;
        pathContainer = seqBlock.querySelector('.train-debug-path');
      }
      const steps = pathContainer!.children;
      const currentIdx = dispatcher.current_resource_index;
      for (let i = 0; i < steps.length; i++) {
        steps[i].classList.toggle('train-debug-path-current', i === currentIdx);
      }
    } else if (seqBlock.dataset.sig) {
      seqBlock.innerHTML = '';
      delete seqBlock.dataset.sig;
    }

    const tilesBlock = panel!.querySelector('[data-debug-block="occupied-tiles"]') as HTMLElement;
    const tiles = span?.occupied_tiles;
    if (tiles && tiles.length > 0) {
      const sig = `${tiles.length}|${tiles.map((t: any) => `${t[0]},${t[1]}`).join('|')}`;
      if (tilesBlock.dataset.sig !== sig) {
        let html = `<div class="train-debug-subsection">`;
        html += `<div class="train-debug-subsection-title">Occupied Tiles (${tiles.length})</div>`;
        html += `<div class="train-debug-tiles">`;
        tiles.forEach((t: any) => {
          html += `<span class="train-debug-tile">(${t[0]}, ${t[1]})</span>`;
        });
        html += `</div></div>`;
        tilesBlock.innerHTML = html;
        tilesBlock.dataset.sig = sig;
      }
    } else if (tilesBlock.dataset.sig) {
      tilesBlock.innerHTML = '';
      delete tilesBlock.dataset.sig;
    }

    const blocksBlock = panel!.querySelector('[data-debug-block="occupied-blocks"]') as HTMLElement;
    const blocks = span?.occupied_blocks;
    if (blocks && blocks.length > 0) {
      const sig = `${blocks.length}|${blocks.join('|')}`;
      if (blocksBlock.dataset.sig !== sig) {
        let html = `<div class="train-debug-subsection">`;
        html += `<div class="train-debug-subsection-title">Occupied Blocks (${blocks.length})</div>`;
        html += `<div class="train-debug-tiles">`;
        blocks.forEach((b: any) => {
          html += `<span class="train-debug-tile" style="color: var(--accent-purple);">${b}</span>`;
        });
        html += `</div></div>`;
        blocksBlock.innerHTML = html;
        blocksBlock.dataset.sig = sig;
      }
    } else if (blocksBlock.dataset.sig) {
      blocksBlock.innerHTML = '';
      delete blocksBlock.dataset.sig;
    }

    controlPanelDebug.classList.remove('hidden');
  }

  hideTrainDebugPanel(): void {
    const controlDebug = document.getElementById('train-control-debug-section');
    if (controlDebug) controlDebug.classList.add('hidden');
    const debugGeometry = document.getElementById('train-debug-geometry');
    if (debugGeometry) debugGeometry.classList.add('hidden');
  }

  async fetchTrainLookahead(trainId: string, train: any): Promise<void> {
    if (!trainId) {
      return;
    }
    // Check if debug is enabled, otherwise we don't need to fetch
    const debugCheckbox = document.getElementById('debug-show-train-position') as HTMLInputElement | null;
    const monitorVisible = this.trainMonitor.isVisible();
    if (debugCheckbox && !debugCheckbox.checked && !monitorVisible) {
      return;
    }

    const now = Date.now();
    if (now - this.lastLookaheadFetch < this.lookaheadThrottleMs) {
      return;
    }
    this.lastLookaheadFetch = now;

    try {
      if (this.selectedTrainId === trainId) {
        const trainSnapshot = train || this.currentNetwork?.trains?.find((t: any) => t.id === trainId);
        if (!trainSnapshot) {
          return;
        }
        const lookaheadPayload = await api.getTrainLookahead(trainId);
        this.lastLookaheadPayload = lookaheadPayload;
        if (this.trainMonitor.isVisible()) {
          this.trainMonitor.updateFromController(trainSnapshot, lookaheadPayload);
        }
        // Lookahead detail lives in the Train Monitor (bottom panel),
        // not in this debug strip. Cache the data only — the monitor
        // is the canonical surface for it.
        const lookaheadData = this.trainMonitor.lookaheadData;
        if (lookaheadData) {
          this.lastLookaheadData = lookaheadData;
          lookaheadData.speed_limit_kmh = trainSnapshot.max_speed || 0;
        }
      }
    } catch (e: any) {
      console.error('[App] fetchTrainLookahead failed:', e);
    }
  }

  async showAddTrainDialog(): Promise<void> {
    const defaultTrainId = `TRN_${Date.now().toString().slice(-6)}`;
    const values = await showFormModal({
      title: 'Add Train',
      message: 'Enter train placement and startup settings.',
      fields: [
        {
          name: 'trainId',
          label: 'Train ID',
          type: 'text',
          defaultValue: defaultTrainId,
          required: true,
          validate: (value: any) => String(value || '').trim() ? '' : 'Train ID is required.'
        },
        {
          name: 'posX',
          label: 'X Position',
          type: 'number',
          defaultValue: 0,
          step: 1,
          validate: (value: any) => Number.isFinite(value) ? '' : 'X position must be a number.'
        },
        {
          name: 'posY',
          label: 'Y Position',
          type: 'number',
          defaultValue: 0,
          step: 1,
          validate: (value: any) => Number.isFinite(value) ? '' : 'Y position must be a number.'
        },
        {
          name: 'direction',
          label: 'Direction',
          type: 'select',
          defaultValue: 'EAST',
          options: [
            { value: 'NORTH', label: 'North' },
            { value: 'EAST', label: 'East' },
            { value: 'SOUTH', label: 'South' },
            { value: 'WEST', label: 'West' }
          ]
        },
        {
          name: 'maxSpeed',
          label: 'Max Speed (km/h)',
          type: 'number',
          defaultValue: 100,
          min: 1,
          step: 1,
          validate: (value: any) => Number.isFinite(value) && value > 0 ? '' : 'Max speed must be greater than 0.'
        }
      ],
      confirmText: 'Create Train',
      confirmVariant: 'primary'
    });

    if (!values) {
      return;
    }

    try {
      await api.createTrain({
        id: String(values.trainId || '').trim(),
        position: { x: Number(values.posX), y: Number(values.posY) },
        direction: values.direction || 'EAST',
        max_speed: Number(values.maxSpeed)
      });
      await this.refreshNetwork();
    } catch (err: any) {
      console.error('Failed to create train:', err);
      showToast('Failed to create train: ' + err.message, 'error', 4000);
    }
  }

  setupDebugPanel(): void {
    const canvas = this.renderer.canvas;
    if (!canvas) return;

    // Add mousemove handler for tile hover
    canvas.addEventListener('mousemove', (e: MouseEvent) => {
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;

      const gridX = this.renderer.screenToGridX(x);
      const gridY = this.renderer.screenToGridY(y);

      // Update debug panel with tile details
      this.updateTileDetails(gridX, gridY);
    });

    // Add change listeners to debug checkboxes to trigger re-render
    const checkboxIds = ['debug-show-bits', 'debug-show-coords', 'debug-show-switches', 'debug-show-edges', 'debug-show-blocks', 'debug-show-signals', 'debug-show-block-boundaries', 'debug-show-tunnel-links'];
    checkboxIds.forEach(id => {
      const checkbox = document.getElementById(id);
      if (checkbox) {
        checkbox.addEventListener('change', () => {
          // Re-render network with debug overlays
          this.renderer.requestRedraw({ world: true, reason: 'debug-toggle' });
        });
      }
    });

    // "Show stuck" draws on the live (train) layer, so it must request a
    // live redraw — a world-only redraw would not repaint trains, leaving
    // the toggle with no visible effect while the sim is paused.
    const stuckCheckbox = document.getElementById('debug-show-stuck');
    if (stuckCheckbox) {
      stuckCheckbox.addEventListener('change', () => {
        this.renderer.requestRedraw({ world: true, live: true, reason: 'debug-toggle' });
      });
    }

    // Train position debug checkbox
    const trainPosCheckbox = document.getElementById('debug-show-train-position') as HTMLInputElement | null;
    if (trainPosCheckbox) {
      trainPosCheckbox.addEventListener('change', () => {
        if (this.selectedTrainId && this.currentNetwork) {
          const train = this.currentNetwork.trains?.find((t: any) => t.id === this.selectedTrainId);
          if (train) {
            this.updateTrainDebugPanel(train);
            // Also trigger lookahead fetch immediately if checkbox is enabled
            if (trainPosCheckbox.checked) {
              this.fetchTrainLookahead(this.selectedTrainId, train);
            }
          }
        } else {
          this.hideTrainDebugPanel();
        }
      });
    }
  }

  setupSettingsPanel(): void {
    const scaleSelect = document.getElementById('scale-select') as HTMLSelectElement | null;
    const scaleApplyBtn = document.getElementById('scale-apply-custom');
    const scaleDisplay = document.getElementById('scale-current-display');
    const labelSpacingSelect = document.getElementById('label-spacing-select') as HTMLSelectElement | null;

    if (!scaleSelect || !scaleApplyBtn || !scaleDisplay) {
      console.warn('[App] Settings panel elements not found');
      return;
    }

    // Setup label spacing selector
    if (labelSpacingSelect) {
      labelSpacingSelect.addEventListener('change', () => {
        setLabelSpacing(labelSpacingSelect.value);
        this.refreshNetwork();
      });
    }

    // Load current scale on init
    this.loadCurrentScale();

    const applyScale = async (scaleValue: string) => {
      if (!scaleValue) return;

      const scale = parseFloat(scaleValue);
      if (isNaN(scale) || scale <= 0) {
        showToast('Invalid scale value', 'error');
        return;
      }

      try {
        await api.setScale(scale);
        await this.loadCurrentScale();
        showToast(`Scale set to 1:${scale}`, 'success');
      } catch (e: any) {
        console.error('[App] Failed to set scale:', e);
        showToast('Failed to set scale', 'error');
      }
    };

    // Handle preset selection
    scaleSelect.addEventListener('change', () => {
      applyScale(scaleSelect.value);
    });

    // Handle apply button click
    scaleApplyBtn.addEventListener('click', () => {
      applyScale(scaleSelect.value);
    });
  }

  async loadCurrentScale(): Promise<void> {
    const scaleDisplay = document.getElementById('scale-current-display')!;
    const scaleSelect = document.getElementById('scale-select') as HTMLSelectElement;

    try {
      const data = await api.getScale();
      const scale = data.scale;
      const presets = data.presets || {};

      // Update display
      if (scale === 1.0) {
        scaleDisplay.textContent = 'Current: 1:1 (Full scale)';
      } else {
        scaleDisplay.textContent = `Current: 1:${scale}`;
      }

      // Match dropdown value to current scale
      // Dropdown values are numeric strings (e.g., "160")
      const scaleOptions = Array.from(scaleSelect.options);
      const matchingOption = scaleOptions.find(opt => parseFloat(opt.value) === scale);
      if (matchingOption) {
        scaleSelect.value = matchingOption.value;
      } else {
        scaleSelect.value = '';
      }
    } catch (e: any) {
      console.error('[App] Failed to load scale:', e);
      scaleDisplay.textContent = 'Current: Unknown';
    }
  }

  updateTileDetails(gridX: number, gridY: number): void {
    const detailsDiv = document.getElementById('tile-details');
    if (!detailsDiv) return;

    if (!this.currentNetwork || !this.currentNetwork.tracks) {
      detailsDiv.innerHTML = '<p class="debug-hint">No network loaded</p>';
      return;
    }

    const track = this.currentNetwork.tracks.find((t: any) => t.x === gridX && t.y === gridY);

    if (!track) {
      detailsDiv.innerHTML = '<p class="debug-hint">No track at this position</p>';
      return;
    }

    // Calculate ribi for display
    const RIBI: Record<string, number> = { NORTH: 1, EAST: 2, SOUTH: 4, WEST: 8 };
    let ribi = 0;
    if (track.directed_paths) {
      for (const p of track.directed_paths) {
        if (p.port1 === 'NORTH' || p.port2 === 'NORTH') ribi |= RIBI.NORTH;
        if (p.port1 === 'EAST' || p.port2 === 'EAST') ribi |= RIBI.EAST;
        if (p.port1 === 'SOUTH' || p.port2 === 'SOUTH') ribi |= RIBI.SOUTH;
        if (p.port1 === 'WEST' || p.port2 === 'WEST') ribi |= RIBI.WEST;
      }
    }

    let html = `
            <h4 class="debug-tile-header">Tile (${gridX}, ${gridY})</h4>
            <table class="debug-table">
                <tr><td class="debug-label">Type:</td><td class="debug-value">${track.type || 'TRACK'}</td></tr>
                <tr><td class="debug-label">Track Bits:</td><td class="debug-value">${track.track_bits || 0} (0b${(track.track_bits || 0).toString(2).padStart(6, '0')})</td></tr>
                <tr><td class="debug-label">Ribi:</td><td class="debug-value">${ribi}</td></tr>
        `;

    // Show active bits
    if (track.track_bits) {
      const bits = [];
      if (track.track_bits & 1) bits.push('NS');
      if (track.track_bits & 2) bits.push('EW');
      if (track.track_bits & 4) bits.push('NE');
      if (track.track_bits & 8) bits.push('NW');
      if (track.track_bits & 16) bits.push('SE');
      if (track.track_bits & 32) bits.push('SW');
      html += `<tr><td class="debug-label">Active:</td><td class="debug-value">${bits.join(', ') || 'None'}</td></tr>`;
    }

    // Dead-ends
    if (track.dead_end_ports && track.dead_end_ports.length > 0) {
      html += `<tr><td class="debug-label">Dead-Ends:</td><td class="debug-value-deadend">${track.dead_end_ports.join(', ')}</td></tr>`;
    }

    // Switch states
    if (track.switch_states && Object.keys(track.switch_states).length > 0) {
      const switches = Object.entries(track.switch_states)
        .map(([edge, pos]) => `${edge}:${pos}`)
        .join(', ');
      html += `<tr><td class="debug-label">Switches:</td><td class="debug-value-switches">${switches}</td></tr>`;
    }

    // Validation - check if forbidden
    const FORBIDDEN_TRACK_BITS = new Set([
      13, 22, 42, 49, 15, 23, 43, 51, 29, 30, 45, 46, 53, 54, 57, 58,
      31, 47, 55, 59, 61, 62, 63
    ]);
    const isForbidden = FORBIDDEN_TRACK_BITS.has(track.track_bits);
    html += `<tr><td class="debug-label">Valid:</td><td class="${isForbidden ? 'debug-value-invalid' : 'debug-value-valid'}">${isForbidden ? 'FORBIDDEN' : 'Valid'}</td></tr>`;

    // Occupation
    if (track.occupied_by) {
      html += `<tr><td class="debug-label">Occupied:</td><td class="debug-value-occupied">${track.occupied_by}</td></tr>`;
    }
    if (track.reserved_by) {
      html += `<tr><td class="debug-label">Reserved:</td><td class="debug-value-reserved">${track.reserved_by}</td></tr>`;
    }

    html += '</table>';
    detailsDiv.innerHTML = html;
  }

  /**
   * Start polling for simulation updates (train movement, signal changes)
   * Only runs when simulation is active
   */
  startSimulationPolling(): void {
    if (this.simulationInterval) return;
    this.isSimulationRunning = true;
    this.simulationInterval = setInterval(async () => {
      await this.refreshNetwork();

      // Phase 4 COMPLETE: All panels now use StateManager subscriptions
      // No need for refreshCurrentEditor() - subscriptions handle all updates

      if (this.selectedTrainId) {
        const train = stateManager.get('trains', this.selectedTrainId);
        if (train) {
          // Train Control Panel updates via subscription
          // Only update debug panel here
          this.updateTrainDebugPanel(train);

          // Always refresh the lookahead for the selected train.
          // The StateManager subscription only fires on train-data
          // changes, so a parked train (e.g. EB latched, speed=0,
          // position frozen) would leave the monitor stale even
          // though signals, switches, and clearance ahead can still
          // be evolving on the server. The throttle inside
          // fetchTrainLookahead caps the actual fetch rate.
          this.fetchTrainLookahead(this.selectedTrainId, train);
        }
      }
    }, 200);
  }

  /**
   * Stop simulation polling
   */
  stopSimulationPolling(): void {
    if (this.simulationInterval) {
      clearInterval(this.simulationInterval);
      this.simulationInterval = null;
    }
    this.isSimulationRunning = false;
    // One final refresh to show stopped state
    this.refreshNetwork();
  }

  startRestartReconnectFlow({ timeoutMs = 60000, pollIntervalMs = 500 }: { timeoutMs?: number; pollIntervalMs?: number } = {}): void {
    const existing = document.querySelector('.restart-overlay');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.className = 'app-modal-overlay restart-overlay';
    overlay.innerHTML = `
            <div class="app-modal restart-card">
                <div class="restart-spinner" aria-hidden="true"></div>
                <div class="restart-title">Restarting server…</div>
                <div class="restart-sub">Reconnecting when the new process is ready.</div>
            </div>
        `;
    document.body.appendChild(overlay);

    const startedAt = Date.now();
    let stopped = false;

    const finishSuccess = () => {
      stopped = true;
      setTimeout(() => window.location.reload(), 300);
    };

    const finishTimeout = () => {
      stopped = true;
      const card = overlay.querySelector('.restart-card')!;
      card.innerHTML = `
                <div class="restart-title">Server did not come back</div>
                <div class="restart-sub">Check the terminal where the server was launched, then refresh manually.</div>
                <div class="restart-actions">
                    <button class="btn btn-secondary btn-sm" id="restart-reload-btn">Reload</button>
                </div>
            `;
      card.querySelector('#restart-reload-btn')!.addEventListener('click', () => {
        window.location.reload();
      });
    };

    const tick = async () => {
      if (stopped) return;
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 800);
        const res = await fetch('/api/health', { signal: controller.signal });
        clearTimeout(timer);
        if (res.ok) {
          finishSuccess();
          return;
        }
      } catch (_e) {
        // Expected during exec — keep polling.
      }
      if (Date.now() - startedAt >= timeoutMs) {
        finishTimeout();
        return;
      }
      setTimeout(tick, pollIntervalMs);
    };

    setTimeout(tick, pollIntervalMs);
  }
}


// Sidebar resize functionality
(function() {
  let isResizing = false;
  let currentSidebar: HTMLElement | null = null;

  function initSidebarResize(sidebar: HTMLElement | null, _minWidth: number, _maxWidth: number): void {
    if (!sidebar) return;

    sidebar.addEventListener('mousedown', (e: MouseEvent) => {
      const rect = sidebar.getBoundingClientRect();
      const isNearRightEdge = e.clientX > rect.right - 8;

      // Both sidebars: resize on right edge
      if (isNearRightEdge) {
        isResizing = true;
        currentSidebar = sidebar;
        sidebar.classList.add('resizing');
        document.body.style.cursor = 'ew-resize';
        document.body.style.userSelect = 'none';
        e.preventDefault();
      }
    });
  }

  document.addEventListener('mousemove', (e: MouseEvent) => {
    if (!isResizing || !currentSidebar) return;

    const rect = currentSidebar.getBoundingClientRect();
    const isSecondarySidebar = currentSidebar.id === 'secondary-sidebar';

    let newWidth;
    if (isSecondarySidebar) {
      // For secondary sidebar, resize from right edge (normal behavior)
      newWidth = e.clientX - rect.left;
    } else {
      // For main sidebar, resize from right edge
      newWidth = e.clientX - rect.left;
    }

    const minWidth = parseInt(getComputedStyle(currentSidebar).minWidth) || 200;
    const maxWidth = parseInt(getComputedStyle(currentSidebar).maxWidth) || 600;

    if (newWidth >= minWidth && newWidth <= maxWidth) {
      currentSidebar.style.width = newWidth + 'px';

      // Secondary sidebar position is now handled by flexbox layout
    }
  });

  document.addEventListener('mouseup', () => {
    if (isResizing && currentSidebar) {
      currentSidebar.classList.remove('resizing');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      isResizing = false;
      currentSidebar = null;
    }
  });

  // Initialize both sidebars
  document.addEventListener('DOMContentLoaded', () => {
    const mainSidebar = document.querySelector('.sidebar') as HTMLElement | null;
    const secondarySidebar = document.getElementById('secondary-sidebar');

    if (mainSidebar) {
      initSidebarResize(mainSidebar, 200, 600);
    }
    if (secondarySidebar) {
      initSidebarResize(secondarySidebar, 280, 500);
    }
  });
})();

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  window.app = new App() as unknown as Window['app'];
});
