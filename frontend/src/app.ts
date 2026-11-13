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
import { Editor } from './editor/editor';
import { updateFatalBanner } from './panels/fatal-banner';
import { startRestartReconnectFlow } from './ui/restart-flow';
import {
  startConnectionMonitor,
  noteRequestFailure,
  noteRequestSuccess,
} from './ui/connection-monitor';
import { setupSettingsPanel } from './ui/settings-panel';
import { activationBlockMessage, refreshHardwarePanel } from './ui/hardware-panel';
import { updateTileDetails } from './panels/tile-debug-inspector';
import { showConfirmModal } from './lib/helpers';
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
  // controls only in some states (design_v2.md "Session state").
  sessionState: { value: string; halt_reason: string | null } | null = null;
  editorMode: 'edit' | 'sim' = 'edit';
  /** Polls the network, getting the layout only when it changed. */
  networkPoller = new NetworkPoller((layoutVersion) => api.getNetwork(layoutVersion));

  constructor() {
    // The renderer draws only the layout; the Operate screen registers the
    // train overlay (next/operate/mount-operate.tsx).
    this.renderer = new Renderer('rail-canvas');
    this.editor = new Editor(this.renderer, api);
    // Inject this App as the editor's host + the sim state store, so the editor
    // core calls back through injected refs instead of the window.app/stateManager
    // globals. (window.app is still set below for legacy inline handlers.)
    this.editor.host = this;
    this.editor.stateManager = stateManager;

    this.currentNetwork = null;
    this.hasInitiallyCentered = false;
    this.hasInitiallySelected = false;
    this.isSimulationRunning = false;
    this.editorMode = 'edit';

    this.setupControls();
    // The top bar and the four screens.
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
    // (activateEnvironment, stopAll, …). The hardware-failure banner keeps
    // its own buttons.

    // Activate starts the selected environment over with the saved Hardware
    // Setup (design_v2.md "Operating environment and activation", Activation
    // starts the railway over). The banner offers it after a hardware failure,
    // once the device is repaired (design_v2.md "Safe state, watchdog and
    // hardware failure", Recovery is reactivation).
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

  /** Stop all: stops every train at once, always accepted (design_v2.md
   *  "Running a train", Stop all). It runs at once — no confirmation —
   *  because it is the button pressed when something is about to go wrong. */
  async stopAll(): Promise<void> {
    try {
      await api.stopAllTrains();
      showToast('Every train stopped', 'warning', 3000);
    } catch (e: any) {
      console.error('[App] Stop all failed:', e);
      showToast('Stop all failed: ' + (e?.message ?? e), 'error', 6000);
    }
    await this.refreshNetwork();
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
   * The top bar names this mode "Operate" in every operating environment.
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
   *  snapshot (design_v2.md "Session state"); the top bar reads them through
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
          listEl.querySelectorAll('.layout-item').forEach(el => el.classList.remove('row-selected'));
          // Select this one (DESIGN_LANGUAGE.md §12 Selected row)
          item.classList.add('row-selected');
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
        if (stats.switches > 0) items.push(`${stats.switches} switch(es)`);
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
      // a build drag is in flight, refreshNetwork()'s fast path updates the caches but skips the
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
    // While an editor tool is active, update the caches but leave the redraw to
    // the tool, so its preview does not flicker.
    if (this.editor && this.editor.isToolActive()) {
      try {
        const { network } = pushed ?? (await this.networkPoller.poll());
        this.currentNetwork = network;

        // NEW: Update StateManager cache
        stateManager.updateFromNetwork(network);

        this.editor.setCachedNetwork(network);
        // Validate that the current editor still exists (close if deleted)
        this.editor.validateCurrentEditor();
        noteRequestSuccess();
      } catch (e: any) {
        console.error('[App] Failed to refresh network:', e);
        noteRequestFailure(e);
      }
      return;
    }

    try {
      const { network } = pushed ?? (await this.networkPoller.poll());

      this.currentNetwork = network;

      // NEW: Update StateManager cache
      stateManager.updateFromNetwork(network);

      this.editor.setCachedNetwork(network);
      if (!this.hasInitiallyCentered && network.tracks && network.tracks.length > 0) {
        this.renderer.fitToContent(network.tracks);
        this.hasInitiallyCentered = true;
      }
      // Fresh network arrived: hand it to the renderer and let the
      // rAF coalescer flush the paint.
      this.renderer.requestRedraw({ network, reason: 'network-refresh' });
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

      // Show the hardware-failure banner while that halt holds.
      updateFatalBanner(network);
      noteRequestSuccess();
    } catch (e: any) {
      console.error('[App] Failed to refresh network:', e);
      // While the network stream is down this runs every 200 ms, so it is
      // where a dead server shows up first. An HTTP error is ignored inside —
      // the server answered, so it is alive.
      noteRequestFailure(e);
    }
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
        this.renderer.fitToContent(this.currentNetwork.tracks);
        this.renderer.requestRedraw({ reason: 'fit-view' });
      });
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
    // a live-pass layer (direction arrows) stale while paused.
    canvasLayers.subscribe((id) => {
      const { redraw } = layerDef(id);
      this.renderer.requestRedraw({ world: redraw !== 'live', live: redraw !== 'world', reason: 'layer-toggle' });
    });
  }

  /**
   * Start the live network updates: the server's network stream, or a read
   * of `GET /api/network` every 200 ms while the stream is down
   * (design_v2.md "Network stream").
   */
  startSimulationPolling(): void {
    if (this.networkStream) return;
    this.networkStream = new NetworkStream({
      poller: this.networkPoller,
      onMessage: async (result) => {
        await this.refreshNetwork(result);
      },
      pollOnce: async () => {
        await this.refreshNetwork();
      },
    });
    this.networkStream.start();
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
