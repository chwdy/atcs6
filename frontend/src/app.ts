// app.ts — top-level application bootstrap: the session and layout actions
// the top bar calls, simulation polling, network refresh, layout save/load,
// settings, and train selection.
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
import { SimRendererOverlay } from './renderer/sim-overlay';
import { Editor } from './editor/editor';
import { SimEditorPlugin } from './editor/sim-plugin';
import { TrainMonitor } from './panels/train-monitor';
import { updateFatalBanner } from './panels/fatal-banner';
import { startRestartReconnectFlow } from './ui/restart-flow';
import {
  startConnectionMonitor,
  noteRequestFailure,
  noteRequestSuccess,
} from './ui/connection-monitor';
import { setupSettingsPanel } from './ui/settings-panel';
import { activationBlockMessage, refreshHardwarePanel } from './ui/hardware-panel';
import { updateTrainDebugPanel as renderTrainDebugPanel, hideTrainDebugPanel as renderHideTrainDebugPanel } from './panels/train-debug-panel';
import { updateTileDetails } from './panels/tile-debug-inspector';
import { showConfirmModal, showFormModal } from './lib/helpers';
import { setLabelSpacing } from './lib/constants';
import { api } from './lib/api';
import { canvasLayers, layerDef } from './lib/canvas-layers';
import { NetworkPoller, type NetworkPollResult } from './lib/network-poll';
import { NetworkStream } from './lib/network-stream';
import { stateManager } from './lib/state-manager';
import { showToast } from './lib/toast';
import { mountNextUI } from './next/mount';
import { mountOperateScreen } from './next/operate/mount-operate';
import { followEditorMode, updateFromNetwork } from './next/state';

class App {
  renderer: any;
  editor: any;
  trainMonitor: any;

  /** The server's network stream, which polls only while it is down. */
  networkStream: NetworkStream | null = null;
  currentNetwork: any;
  hasInitiallyCentered: boolean;
  hasInitiallySelected: boolean;
  isSimulationRunning: boolean;
  // The active operating environment (`simulation`, `virtual_hardware` or
  // `real_hardware`), from the network snapshot.
  operationEnvironment: string | null = null;
  // The session state from the network snapshot, for panels that offer
  // controls only in some states (design_new.md "Session state").
  sessionState: { value: string; halt_reason: string | null } | null = null;
  editorMode: 'edit' | 'sim' = 'edit';
  selectedTrainId: string | null;
  lastLookaheadFetch: number;
  lookaheadThrottleMs: number;
  lastLookaheadData: any;
  lastLookaheadPayload: any;
  trainMonitorUnsub: (() => void) | null;
  followTrainEnabled: boolean;
  /** Polls the network, getting the layout only when it changed. */
  networkPoller = new NetworkPoller((layoutVersion) => api.getNetwork(layoutVersion));
  /** Station groups for the renderer's labels. They are layout, so they are
   *  fetched again only when the layout changed (or the last fetch failed). */
  stationGroups: any[] = [];
  stationGroupsStale = true;

  constructor() {
    this.renderer = new Renderer('rail-canvas');
    // The renderer draws only the layout; register the sim overlay so this app
    // draws trains, routes, and the train-placement ghost on top.
    this.renderer.registerOverlay(new SimRendererOverlay(stateManager));
    this.editor = new Editor(this.renderer, api);
    // The core editor is layout-only; register the sim plugin so this app also
    // edits trains, paths, routes, route-runs, and speed profiles.
    this.editor.registerPlugin(new SimEditorPlugin());
    // Inject this App as the editor's host + the sim state store, so the editor
    // core calls back through injected refs instead of the window.app/stateManager
    // globals. (window.app is still set below for legacy inline handlers.)
    this.editor.host = this;
    this.editor.stateManager = stateManager;
    this.trainMonitor = new TrainMonitor();

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
    // The top bar, the four screens and the Logs panel
    // (docs/ui-layout-rewrite-plan.md).
    mountNextUI(this);
    mountOperateScreen(this);

    this.setupDebugPanel();
    setupSettingsPanel(() => this.refreshNetwork());
    this.setupViewControls();
    // Learns this process's boot id (so a reconnect can tell "same server" from
    // "restarted server") and reports a previous run that died without safing
    // its outputs. Fire-and-forget: nothing else waits on it.
    startConnectionMonitor().catch((e: any) =>
      console.warn('[App] Connection monitor init failed:', e)
    );
    this.refreshNetwork();
    // The network updates (block/switch state, incl. hardware occupancy) run
    // continuously regardless of play/pause: the backend keeps committing
    // hardware occupancy while stopped (real mode), so the client must keep
    // listening or the layout freezes at whatever it looked like at the last
    // Pause. Play/Pause only toggles simulation ticking on the server.
    this.startSimulationPolling();
  }


  setupControls(): void {
    // The top bar's controls call the session action methods below
    // (togglePlayPause, emergencyStop, …). The fatal banner keeps its own
    // three buttons.
    const clearFatalBtn = document.getElementById('btn-clear-fatal') as HTMLButtonElement | null;
    clearFatalBtn?.addEventListener('click', () => void this.clearFatalConflict(clearFatalBtn));

    // Activate starts the selected environment over with the saved Hardware
    // Setup (design_new.md "Activation starts the railway over"). The banner
    // offers it after a hardware failure, once the device is repaired
    // ("Power", Recovery is reactivation).
    const activateFatalBtn = document.getElementById('btn-activate-fatal') as HTMLButtonElement | null;
    activateFatalBtn?.addEventListener('click', () => void this.activateEnvironment(activateFatalBtn));

    const restartFatalBtn = document.getElementById('btn-restart-fatal') as HTMLButtonElement | null;
    if (restartFatalBtn) {
      restartFatalBtn.addEventListener('click', async () => {
        const confirmed = await showConfirmModal({
          title: 'Restart Server?',
          message: 'The server process will exit and relaunch, throwing away everything since the last save.',
          detail: 'Unsaved layout edits will be flushed to autosave first. The page will reload automatically when the server is back (~3–5s).',
          confirmText: 'Restart',
          cancelText: 'Cancel',
          confirmVariant: 'danger'
        });
        if (!confirmed) return;

        restartFatalBtn.disabled = true;
        try {
          await api.restartServer();
        } catch (e: any) {
          console.error('[App] Restart request failed:', e);
          showToast('Could not reach server to restart.', 'error', 4000);
          restartFatalBtn.disabled = false;
          return;
        }

        this.stopSimulationPolling();
        startRestartReconnectFlow();
      });
    }

    // Apply the initial (Editor) tool-availability state.
    this.setEditorMode('edit');

    // The Save and Load dialogs.
    this.setupLayoutControls();
  }

  // ── Session and layout actions ────────────────────────────────────────
  // The top bar (frontend/src/next/top-bar.tsx) calls these.

  /** Play from Paused, Pause from Live; Play/Pause exists only in Simulation
   *  (design_new.md "Session state"). The server decides; the top bar follows
   *  the session state the next snapshot carries rather than guessing. */
  async togglePlayPause(): Promise<void> {
    const action = this.isSimulationRunning ? 'stop' : 'start';
    try {
      await api.controlSimulation(action);
    } catch (e: any) {
      console.error('[App] Play/Pause error:', e);
      showToast(e?.data?.message || e?.message || 'Play/Pause was refused', 'warning', 4000);
    }
    await this.refreshNetwork();
  }

  /** Set the Simulation speed multiplier. */
  async setSimulationSpeed(speed: number): Promise<void> {
    try {
      await api.controlSimulation('set_speed', speed);
    } catch (err: any) {
      console.error('[App] set_speed error:', err);
      showToast(`Failed to set speed: ${err.message}`, 'error', 2000);
      return;
    }
    showToast(`Speed set to ${speed}x`, 'info', 1000);
  }

  /** Emergency stop: the one kill switch, in every environment. It runs at
   *  once — no confirmation — because it is the button pressed when
   *  something is about to go wrong. */
  async emergencyStop(): Promise<void> {
    try {
      const result = await api.layoutEmergencyStop();
      showToast(result?.message || 'Emergency stop', 'warning', 4000);
    } catch (e: any) {
      console.error('[App] Emergency stop failed:', e);
      showToast('Emergency stop failed: ' + (e?.message ?? e), 'error', 6000);
    }
    await this.refreshNetwork();
  }

  /** Reset releases every train after Emergency stop; each ATO train then
   *  waits for Go and each manual train for driver input. */
  async resetLayout(): Promise<void> {
    try {
      const result = await api.resetLayoutCutOut();
      showToast(result?.message || 'Reset', 'success', 4000);
    } catch (e: any) {
      showToast('Reset was refused: ' + (e?.message ?? e), 'warning', 5000);
    }
    await this.refreshNetwork();
  }

  /** Hold all: ATO stop on every ATO train, keeping its reservation. */
  async holdAll(): Promise<void> {
    try {
      const result = await api.holdAll();
      const n = (result?.stopped_train_ids || []).length;
      showToast(`Held ${n} ATO train${n === 1 ? '' : 's'}. Press Go on each to resume.`, 'info', 4000);
    } catch (e: any) {
      showToast('Hold all failed: ' + (e?.message ?? e), 'error', 5000);
    }
    await this.refreshNetwork();
  }

  /** Clearing is the ordinary way out of a fatal conflict: the operator has
   *  gone and sorted it out on the layout, and the session carries on rather
   *  than losing the whole layout state to a process restart. Every train
   *  comes back OFF, so nothing moves until it is powered up deliberately.
   *  `btn`, when given, is disabled while it runs. */
  async clearFatalConflict(btn?: HTMLButtonElement): Promise<void> {
    const confirmed = await showConfirmModal({
      title: 'Clear the conflict?',
      message: 'Only clear this once the track is actually sorted out — two trains claimed the same place, and the app cannot see that you have fixed it.',
      detail: 'Every train comes back powered off and stopped. Nothing moves until you power it up again.',
      confirmText: 'Clear',
      cancelText: 'Cancel',
      confirmVariant: 'danger'
    });
    if (!confirmed) return;

    if (btn) btn.disabled = true;
    try {
      await api.clearFatalConflict();
      showToast('Conflict cleared — every train is off.', 'success', 4000);
      await this.refreshNetwork();
    } catch (e: any) {
      console.error('[App] Clear conflict failed:', e);
      showToast('Could not clear the conflict: ' + (e?.message ?? e), 'error', 5000);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /** Ask, then restart the server process; the page reloads once it is back. */
  async restartServer(): Promise<void> {
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
    startRestartReconnectFlow();
  }

  /** Activate the selected environment; `btn`, when given, is disabled while
   *  it runs. */
  async activateEnvironment(btn?: HTMLButtonElement): Promise<void> {
    if (btn) btn.disabled = true;
    try {
      await api.activateOperationEnvironment();
      showToast('Activated — every train is off. Press Start route to run a train.', 'success', 5000);
      await this.refreshNetwork();
      await refreshHardwarePanel();
    } catch (e: any) {
      console.error('[App] Activate failed:', e);
      showToast(activationBlockMessage(e) || 'Could not activate: ' + (e?.message ?? e), 'error', 8000);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  /**
   * Switch between Editor mode and Operate mode (the Editor screen, and the
   * Operate and Locomotives screens).
   *
   * The top bar names the operation session mode "Operate" in every operating
   * environment (design_new.md "Session mode and operating environment"), so
   * the word "Simulation" means only the environment.
   *
   * In Operate mode the layout must not change, so a tool that builds or
   * deletes layout (its `availableIn` lacks 'sim') stops being the active
   * tool.
   */
  setEditorMode(mode: 'edit' | 'sim'): void {
    this.editorMode = mode;
    followEditorMode(mode);

    if (!this.editor) return;

    // If the tool we were holding is not available in the new mode, fall back
    // to Inspect and leave any active build mode so the editor is not stuck
    // mid-build.
    const current = this.editor._toolsById.get(this.editor.selectedTool);
    const stillAvailable = current ? current.availableIn.includes(mode) : true;
    this.editor.editorMode = mode;

    // If an entity inspector is open, re-freeze (or release) its structural
    // fields for the new mode — the panel itself only locks on render.
    this.editor.reapplyStructuralLock();

    if (!stillAvailable) {
      const revert = () => this.editor._setSelectedTool('inspect');
      if (this.editor.activeMode) {
        Promise.resolve(this.editor.exitMode()).then(revert);
      } else {
        revert();
      }
    }
  }

  /** Keep the session state and operating environment from the network
   *  snapshot (design_new.md "Session state"); the top bar reads them through
   *  next/state.ts. `isSimulationRunning` means the session is Live. */
  recordSessionState(network: any): void {
    const state = network?.session_state ?? null;
    this.sessionState = state;
    this.isSimulationRunning = state?.value === 'live';
    this.operationEnvironment = network?.operation_environment ?? null;
  }

  /** Open the Save Layout dialog, with the existing layouts listed for
   *  overwriting. */
  async openSaveLayoutDialog(): Promise<void> {
    this.openModal(document.getElementById('save-layout-modal'));
    (document.getElementById('layout-name') as HTMLInputElement).value = '';
    (document.getElementById('layout-description') as HTMLTextAreaElement).value = '';
    document.getElementById('layout-name')!.focus();
    await this.loadSaveLayoutsList();
  }

  /** Open the Load Layout dialog. */
  async openLoadLayoutDialog(): Promise<void> {
    this.openModal(document.getElementById('load-layout-modal'));
    await this.loadLayoutsList();
  }

  /** Open the Settings dialog (Display and Advanced). Closing is
   *  handled by the generic modal machinery in setupLayoutControls. */
  openSettings(): void {
    this.openModal(document.getElementById('settings-modal'));
  }

  setupLayoutControls(): void {
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
        listEl.innerHTML = '<p class="empty-state">No existing layouts</p>';
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
      listEl.innerHTML = '<p class="empty-state empty-state-error">Failed to load layouts</p>';
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
        listEl.innerHTML = '<p class="empty-state">No saved layouts yet</p>';
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
      listEl.innerHTML = `<p class="empty-state empty-state-error">Error loading layouts: ${e.message}</p>`;
    }
  }

  async loadSelectedLayout(filename: string): Promise<void> {
    try {
      const result = await api.loadLayout(filename);
      showToast('Layout loaded successfully', 'success');

      // The layout carries its own hardware configuration. Adopting it changes
      // the wiring the NEXT session builds on, so say so rather than let the
      // chip inventory and bindings change silently.
      if (result?.warning) {
        showToast(`Warning: ${result.warning}`, 'warning');
      } else if (result?.hardware === 'applied') {
        showToast("This layout's hardware configuration was applied — press Activate to run on it", 'info');
      }

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
        if (stats.switches > 0) items.push(`${stats.switches} switch(es)`);
        if (stats.stations > 0) items.push(`${stats.stations} station(s)`);
        if (stats.tunnels > 0) items.push(`${stats.tunnels} tunnel(s)`);
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

      // A new blank layout is a hard reset. Drop any in-progress tool, build /
      // hover preview, and block-color overlay first: otherwise, if a tool with
      // a live hover preview is active (signal/platform) or a build drag is in
      // flight, refreshNetwork()'s fast path updates the caches but skips the
      // world redraw, so the renderer keeps painting the stale tracks / blocks /
      // switches over the now-empty layout.
      await this.editor.resetForBlankLayout();

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

  /** Show the network: `pushed`, a message from the network stream, or
   *  else one read of `GET /api/network`. */
  async refreshNetwork(pushed?: NetworkPollResult): Promise<void> {
    // Skip refresh if editor is actively using a tool that needs hover preview
    // This prevents flickering when moving the mouse with signal/station tools
    if (this.editor && this.editor.isToolActive()) {
      // Still update the cache and trains panel, but don't redraw directly
      // The setCachedNetwork call will trigger re-validation of hover state
      try {
        const { network, layoutChanged } = pushed ?? (await this.networkPoller.poll());
        if (layoutChanged) this.stationGroupsStale = true;
        this.currentNetwork = network;

        // NEW: Update StateManager cache
        stateManager.updateFromNetwork(network);

        this.editor.setCachedNetwork(network);
        // Validate that the current editor still exists (close if deleted)
        this.editor.validateCurrentEditor();
        // Don't call drawWithHoverPreview here - setCachedNetwork triggers updateHoverPreview
        // which will call drawWithHoverPreview after re-validating the signal state
        noteRequestSuccess();
      } catch (e: any) {
        console.error('[App] Failed to refresh network:', e);
        noteRequestFailure(e);
      }
      return;
    }

    try {
      const { network, layoutChanged } = pushed ?? (await this.networkPoller.poll());

      // Pull station groups so the renderer can draw station labels at the
      // centroid of each station's platforms. They are layout, so fetch them
      // only when the layout changed. Failure here shouldn't block the rest
      // of the render; the next poll tries again.
      if (layoutChanged || this.stationGroupsStale) {
        try {
          const stationResp = await api.listStationGroups();
          this.stationGroups = stationResp?.stations || [];
          this.stationGroupsStale = false;
        } catch (_) {
          this.stationGroups = [];
          this.stationGroupsStale = true;
        }
      }
      network.stations = this.stationGroups;

      this.currentNetwork = network;

      // NEW: Update StateManager cache
      stateManager.updateFromNetwork(network);

      this.editor.setCachedNetwork(network);
      if (!this.hasInitiallyCentered && network.tracks && network.tracks.length > 0) {
        this.renderer.fitToContent(network.tracks, undefined, this._canvasChromeInsets());
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

      // Keep the session state; the top bar reads it through next/state.ts.
      this.recordSessionState(network);
      updateFromNetwork(network);

      // Surface a fatal-conflict halt (failed_runtime) if one is active.
      updateFatalBanner(network, this.renderer);
      noteRequestSuccess();
    } catch (e: any) {
      console.error('[App] Failed to refresh network:', e);
      // While the network stream is down this runs every 200 ms, so it is
      // where a dead server shows up first. An HTTP error is ignored inside —
      // the server answered, so it is alive.
      noteRequestFailure(e);
    }
  }

  /** Make `trainId` the selected train: its controls open in the inspector and
   *  the live subscription follows it.
   *
   *  The Train Monitor (the lookahead chart) is deliberately NOT part of that.
   *  It is a large panel that covers the canvas, so it stays opt-in: it opens
   *  only when asked for by name — `showMonitor`, which the control panel's
   *  "Monitor →" button passes — and otherwise merely retargets if it is
   *  already on screen. */
  selectTrain(trainId: string, options: { showMonitor?: boolean } = {}): void {
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
      if (options.showMonitor || this.trainMonitor.isVisible()) {
        this.trainMonitor.show(trainId, train.name || trainId);
      }
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

  /**
   * Pixels of each canvas edge currently hidden behind floating chrome, for
   * fitToContent to frame the layout into the visible region instead of behind
   * it. The canvas backing store is sized in CSS pixels (Renderer.resize uses
   * parent.clientWidth), so getBoundingClientRect values are already in the
   * same units fitToContent works in.
   */
  _canvasChromeInsets(): { left: number; bottom: number } {
    const canvas = this.renderer.canvas;
    if (!canvas) return { left: 0, bottom: 0 };
    const canvasRect = canvas.getBoundingClientRect();
    const GAP = 12; // breathing room between the chrome and the framed layout

    // The Train Monitor window hovers over the bottom of the canvas when it is
    // open, so Fit View insets past it or it frames the layout under it.
    let bottom = 0;
    const monitor = document.getElementById('train-monitor-bar');
    if (monitor && !monitor.classList.contains('hidden')) {
      const monitorRect = monitor.getBoundingClientRect();
      if (monitorRect.height > 0) {
        bottom = Math.max(0, canvasRect.bottom - monitorRect.top + GAP);
      }
    }

    return { left: 0, bottom };
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
        this.renderer.fitToContent(this.currentNetwork.tracks, undefined, this._canvasChromeInsets());
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
    renderTrainDebugPanel(train);
  }

  hideTrainDebugPanel(): void {
    renderHideTrainDebugPanel();
  }

  async fetchTrainLookahead(trainId: string, train: any): Promise<void> {
    if (!trainId) {
      return;
    }
    // Nothing shows the look-ahead unless the position details or the monitor are.
    const monitorVisible = this.trainMonitor.isVisible();
    if (!canvasLayers.isOn('train-position') && !monitorVisible) {
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
      updateTileDetails(gridX, gridY, this.currentNetwork);
    });

    // A canvas layer turned on or off in the View menu repaints the canvas
    // pass it draws on (lib/canvas-layers.ts). A world-only repaint would leave
    // a live-pass layer (direction arrows, stuck trains) stale while paused.
    canvasLayers.subscribe((id, on) => {
      const { redraw } = layerDef(id);
      if (redraw !== 'none') {
        this.renderer.requestRedraw({ world: redraw !== 'live', live: redraw !== 'world', reason: 'layer-toggle' });
      }
      if (id === 'train-position') this.showTrainPositionDetails(on);
    });
  }

  /** Show or hide the selected train's position details after the View menu
   *  turned them on or off, fetching its look-ahead when they come on. */
  showTrainPositionDetails(on: boolean): void {
    const train = this.selectedTrainId
      ? this.currentNetwork?.trains?.find((t: any) => t.id === this.selectedTrainId)
      : null;
    if (!train) {
      this.hideTrainDebugPanel();
      return;
    }
    this.updateTrainDebugPanel(train);
    if (on) this.fetchTrainLookahead(this.selectedTrainId!, train);
  }

  /**
   * Start the live network updates: the server's network stream, or a read
   * of `GET /api/network` every 200 ms while the stream is down
   * (design_new.md "Data Flow To UI", Network stream).
   */
  startSimulationPolling(): void {
    if (this.networkStream) return;
    this.networkStream = new NetworkStream({
      poller: this.networkPoller,
      onMessage: async (result) => {
        await this.refreshNetwork(result);
        this.refreshSelectedTrain();
      },
      pollOnce: async () => {
        await this.refreshNetwork();
        this.refreshSelectedTrain();
      },
    });
    this.networkStream.start();
  }

  /** After each network update: the selected train's debug panel and its
   *  lookahead. */
  refreshSelectedTrain(): void {
    if (!this.selectedTrainId) return;
    const train = stateManager.get('trains', this.selectedTrainId);
    if (!train) return;
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

  /**
   * Stop the live network updates
   */
  stopSimulationPolling(): void {
    this.networkStream?.stop();
    this.networkStream = null;
    // One final refresh to show stopped state
    this.refreshNetwork();
  }
}


// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  window.app = new App() as unknown as Window['app'];
});
