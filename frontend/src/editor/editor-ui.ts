// editor-ui.ts — where today's entity panels show, plus the JSON viewer and
// the tile panel's track-bit editors.
//
// Today's panels hand their HTML to Editor.showSecondarySidebar. This file
// writes it into the three nodes static/index.html keeps for them: the title,
// the "← Switches" back link and the body. The Editor screen's right panel
// shows those nodes (frontend/src/next/editor/legacy-panel.ts); the Operate
// screen takes a panel before it gets here (Editor.nextPanelHost). One panel
// shows at a time (docs/ui-layout-rewrite-plan.md §12, decision 2).

import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import { showConfirmModal } from '../lib/helpers';
import { FORBIDDEN_TRACK_BITS, isSquareCornerSwitch } from '../lib/constants';

/** The panel on show: its title, and the editor type and id its handler set
 *  on the Editor before drawing it. */
interface ShownPanel {
  title: string;
  editorType: string | null;
  editorId: string | null;
}

export class EditorUIHandler {
  editor: EditorHost;
  currentPanel: ShownPanel | null = null;

  constructor(editor: EditorHost) {
    this.editor = editor;
  }

  get selectedTool(): any {
    return this.editor.selectedTool;
  }

  setupSecondarySidebar(): void {
    this.editor.$id('secondary-nav-parent')?.addEventListener('click', () => void this.navigateToParentList());
    this.updateNavigationState();
  }

  showSecondarySidebar(title: string, content: string): void {
    const titleEl = this.editor.$id('secondary-sidebar-title');
    const contentEl = this.editor.$id('secondary-sidebar-content');
    if (!titleEl || !contentEl) return;
    titleEl.textContent = title;
    contentEl.innerHTML = content;
    const container = this.editor.$id('secondary-sidebar');
    if (container) {
      // Some panels set a width class after drawing (compact,
      // route-editor-wide); each new panel starts without one.
      container.classList.remove('compact', 'route-editor-wide');
      container.classList.add('visible');
    }
    this.currentPanel = {
      title,
      editorType: this.editor._currentEditorType,
      editorId: this.editor._currentEditorId,
    };
    this.updateNavigationState();
  }

  /** Show an entity's new name in the title, if its panel is the one showing. */
  updatePanelTitle(editorType: string, editorId: string | null, title: string): void {
    const panel = this.currentPanel;
    if (!panel || panel.editorType !== editorType || panel.editorId !== editorId) return;
    panel.title = title;
    const titleEl = this.editor.$id('secondary-sidebar-title');
    if (titleEl) titleEl.textContent = title;
  }

  async hideSecondarySidebar(): Promise<void> {
    this.editor.$id('secondary-sidebar')?.classList.remove('visible', 'compact', 'route-editor-wide');
    this.cleanupPanelSubscriptions();
    this.currentPanel = null;
    this.updateNavigationState();

    this.editor._currentEditorType = null;
    this.editor._currentEditorId = null;

    // Drop the block-direction arrows when the panel closes.
    if (this.editor.renderer?.clearBlockDirectionOverlay) {
      this.editor.renderer.clearBlockDirectionOverlay();
    }
  }

  /** Close the panel if it shows the entity with this editor type and id —
   *  used when that entity is gone, or its setup panel has finished. */
  async closeStaleSecondaryTab(editorType: string, editorId: string | null): Promise<void> {
    const panel = this.currentPanel;
    if (panel && panel.editorType === editorType && panel.editorId === editorId) {
      await this.hideSecondarySidebar();
    }
  }

  /** Stop the timers and subscriptions of whatever panel is showing. */
  cleanupPanelSubscriptions(): void {
    const editor = this.editor;
    for (const handler of [
      editor.trainHandler,
      editor.signalHandler,
      editor.switchHandler,
      editor.blockHandler,
      editor.platformHandler,
      editor.stationGroupHandler,
      editor.routeRunHandler,
      editor.routeHandler,
      editor.pathHandler,
      editor.speedProfileHandler,
    ]) {
      handler?._cleanup?.();
    }
  }

  // ---- The back link to the panel's parent list ----

  /** The list the open panel belongs to (`← Switches`), or null when there
   *  is none (list panels; detail panels with no list, such as blocks). */
  private parentList(): any | null {
    if (!this.currentPanel) return null;
    const segments = this.editor.getPanelLineage(this.currentPanel) || [];
    if (segments.length < 2) return null;
    const parent = segments[0];
    return parent && parent.editorType ? parent : null;
  }

  /** Open the open panel's parent list (the back link, Alt+Left). */
  async navigateToParentList(): Promise<void> {
    const parent = this.parentList();
    if (!parent) return;
    this.cleanupPanelSubscriptions();
    await this.editor.restoreSecondaryPanel({
      title: parent.label,
      editorType: parent.editorType,
      editorId: parent.editorId || null,
      content: '',
    });
  }

  /** Label the back link with the list it goes to, or hide it. */
  private updateNavigationState(): void {
    const parentBtn = this.editor.$id('secondary-nav-parent') as HTMLButtonElement | null;
    if (!parentBtn) return;
    const parent = this.parentList();
    parentBtn.hidden = !parent;
    parentBtn.textContent = parent ? `← ${parent.label}` : '';
    parentBtn.title = parent ? `Back to ${parent.label}` : '';
  }

  showFullJSONViewer(data: any, title = 'JSON Viewer'): void {
    const jsonString = JSON.stringify(data, null, 2);

    const content = `
            <div class="json-viewer-panel">
                <div class="json-viewer-toolbar">
                    <button id="copy-json-btn" class="btn btn-primary btn-full text-sm">
                        <i class="ti ti-copy"></i> Copy to Clipboard
                    </button>
                </div>

                <div class="json-viewer-content">
                    <pre id="json-content" class="json-content-view">${this.syntaxHighlightJSON(jsonString)}</pre>
                </div>
            </div>
        `;

    this.showSecondarySidebar(title, content);

    setTimeout(() => {
      const copyBtn = this.editor.$id('copy-json-btn') as HTMLButtonElement | null;
      if (copyBtn) {
        copyBtn.onclick = () => {
          navigator.clipboard
            .writeText(jsonString)
            .then(() => {
              showToast('JSON copied to clipboard', 'success', 2000);
              copyBtn.innerHTML = '<i class="ti ti-check"></i> Copied!';
              copyBtn.classList.remove('btn-primary');
              copyBtn.classList.add('btn-success');
              setTimeout(() => {
                copyBtn.innerHTML = '<i class="ti ti-copy"></i> Copy to Clipboard';
                copyBtn.classList.remove('btn-success');
                copyBtn.classList.add('btn-primary');
              }, 2000);
            })
            .catch((err) => {
              console.error('Failed to copy JSON:', err);
              showToast('Failed to copy JSON', 'error', 3000);
            });
        };
      }
    }, 0);
  }

  syntaxHighlightJSON(jsonString: string): string {
    return jsonString
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(
        /("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g,
        (match) => {
          let cls = 'json-number';
          if (/^"/.test(match)) {
            if (/:$/.test(match)) {
              cls = 'json-key';
            } else {
              cls = 'json-string';
            }
          } else if (/true|false/.test(match)) {
            cls = 'json-boolean';
          } else if (/null/.test(match)) {
            cls = 'json-null';
          }

          const colors: Record<string, string> = {
            'json-key': '#00d4ff',
            'json-string': '#ffd93d',
            'json-number': '#ff79c6',
            'json-boolean': '#04d361',
            'json-null': '#888',
          };

          return `<span style="color: ${colors[cls]};">${match}</span>`;
        }
      );
  }

  /** Keep the secondary inspector in step with the newly selected tile. A
   *  signal / platform / station editor that is already open follows the tile
   *  (or steps aside to the Tile Editor when the tile no longer carries that
   *  entity). When nothing more specific is open — or the Tile Editor itself is
   *  showing — the tile's own bits show in the Tile Editor. Any other entity
   *  editor (block, switch, train, path, …) is left in place; those are reached
   *  through their own Edit affordances. */
  updateSecondaryEditorForTile(track: any): void {
    const type = this.editor._currentEditorType;

    switch (type) {
      case 'signal':
        if (track.signals && track.signals.length > 0) {
          this.editor.showSignalsEditorForTile(track);
        } else {
          this.editor.showTileEditor(track);
        }
        return;

      case 'platform':
        if (track.station_id) {
          this.editor.showPlatformEditor(track.station_id);
        } else {
          this.editor.showTileEditor(track);
        }
        return;

      case 'station-group':
      case 'station-group-list':
        this.editor.showStationsPanel();
        return;
    }

    if (!type || type === 'tile') {
      this.editor.showTileEditor(track);
    }
  }

  initViewFullJSONButton(track: any): void {
    const viewJsonBtn = this.editor.$id('view-full-json-btn') as HTMLButtonElement | null;
    if (viewJsonBtn) {
      viewJsonBtn.onclick = () => {
        this.showFullJSONViewer(track, `JSON: (${track.x}, ${track.y})`);
      };
    }
  }

  initPlatformButtonHandlers(): void {
    const editPlatformBtns = document.querySelectorAll<HTMLElement>('.edit-platform-btn');
    editPlatformBtns.forEach((btn) => {
      btn.onclick = () => {
        const platformId = btn.dataset.platformId;
        if (platformId) {
          this.editor.showPlatformEditor(platformId);
        }
      };
    });
  }

  initBitEditor(track: any): void {
    // The bit grid lives in the Tile Editor (secondary sidebar) now.
    const body: HTMLElement | null = this.editor.$id('secondary-sidebar-content');
    if (!body) return;

    const checkboxes = body.querySelectorAll<HTMLInputElement>('input[type="checkbox"][data-bit]');

    const calculateBits = () => {
      let bits = 0;
      checkboxes.forEach((cb) => {
        if (cb.checked) bits |= parseInt(cb.dataset.bit ?? '0');
      });
      return bits;
    };

    // Optimistic readout update for instant feedback; the authoritative state
    // lands when _commitTrackBits re-renders the panel from the server. The
    // checked/unchecked chrome is pure CSS (.checkbox-custom :checked) now, so
    // there is nothing to repaint here beyond the numeric readout.
    const updateReadout = () => {
      const bits = calculateBits();
      const total = this.editor.$id('bit-total');
      const binary = this.editor.$id('bit-binary');
      const forbidden = this.editor.$id('bit-forbidden');
      if (total) total.textContent = String(bits);
      if (binary) binary.textContent = bits.toString(2).padStart(6, '0');
      if (forbidden) {
        forbidden.style.display =
          FORBIDDEN_TRACK_BITS.has(bits) || isSquareCornerSwitch(bits) ? 'inline' : 'none';
      }
    };

    // Every toggle auto-applies through the one validated commit path — there is
    // no Apply button. Forbidden / square-corner / destructive combinations are
    // all handled (toast, confirm-modal, or revert) inside _commitTrackBits.
    checkboxes.forEach((cb) => {
      cb.addEventListener('change', async () => {
        updateReadout();
        await this._commitTrackBits(track, calculateBits());
      });
    });
  }

  // The single validated path that writes track_bits to the server. Both the
  // checkbox grid and the visual compass route through here so the forbidden /
  // square-corner guards and the "this removes a switch/signal/platform"
  // confirmation modal apply identically to both. On any rejection or failure
  // the panel is re-rendered from server state, which reverts the optimistic UI.
  async _commitTrackBits(track: any, newBits: number): Promise<void> {
    const api: any = this.editor.api;
    const resync = () => this.editor.selectElement({ x: track.x, y: track.y });

    if (FORBIDDEN_TRACK_BITS.has(newBits)) {
      showToast('Cannot apply forbidden configuration', 'error', 3000);
      await resync();
      return;
    }
    if (isSquareCornerSwitch(newBits)) {
      showToast(
        'Invalid switch: a branch is 90° to a connecting segment ' +
          '(square corner). Add a diagonal (NS/EW) leg to ease it.',
        'error',
        5000
      );
      await resync();
      return;
    }

    const applyBits = async (force: boolean) => {
      // Removing the last segment (newBits === 0, no dead-end ports) deletes
      // the whole tile. The server reports that with a removed/empty result
      // that carries no `track` payload, so there is nothing left to re-fetch
      // — getTrack on the removed tile would just 404 to null. Track removal
      // so we skip the pointless re-fetch and still fall through to
      // refreshNetwork + reselect, which leaves the now empty tile selected.
      let tileRemoved = false;
      if (track.isVirtual) {
        await api.addTracksBulk([
          { x: track.x, y: track.y, track_bits: newBits, directed_paths: [] },
        ]);
      } else {
        const reqBody: Record<string, unknown> = { track_bits: newBits };
        if (force) reqBody.force = true;
        const updateResult = await api.updateTrack(track.x, track.y, reqBody);
        tileRemoved = !updateResult?.track;
      }

      if (!tileRemoved) {
        const updatedTrack = await api.getTrack(track.x, track.y);
        if (updatedTrack && updatedTrack.is_switch && !updatedTrack.switch_id) {
          try {
            await api.createSwitchAt(track.x, track.y, {
              main_length_cm: 0,
              diverge_length_cm: 0,
              main_radius_cm: null,
              diverge_radius_cm: null,
            });
            console.log(`[Editor] Auto-created switch entity at (${track.x}, ${track.y})`);
          } catch (switchErr) {
            console.warn('[Editor] Failed to auto-create switch:', switchErr);
          }
        }
      }

      await this.editor.host?.refreshNetwork();
      await this.editor.selectElement({ x: track.x, y: track.y });
    };

    try {
      await applyBits(false);
    } catch (err: any) {
      const data = err && err.data;
      if (data && data.requires_confirmation) {
        const proceed = await this._confirmBitChangeImpact(data.impact);
        if (!proceed) {
          await resync();
          return;
        }
        try {
          await applyBits(true);
        } catch (err2: any) {
          console.error('Failed to update track bits (forced):', err2);
          showToast('Failed to update track bits: ' + err2.message, 'error', 5000);
          await resync();
        }
        return;
      }
      console.error('Failed to update track bits:', err);
      showToast('Failed to update track bits: ' + err.message, 'error', 5000);
      await resync();
    }
  }

  async _confirmBitChangeImpact(impact: any): Promise<boolean> {
    const lines = [];
    if (impact.removes_switch) {
      lines.push(`• Switch ${impact.removes_switch} (this tile is no longer a switch)`);
    }
    for (const sid of impact.removes_signals || []) {
      lines.push(`• Signal ${sid} (its detection direction loses its segment)`);
    }
    if (impact.removes_platform) {
      lines.push(`• Platform ${impact.removes_platform} (tile would be removed)`);
    }
    const message = impact.tile_will_be_removed
      ? 'This change removes all segments and will delete the tile. The entities below will also be removed:'
      : 'This change will remove the following entities so the layout stays consistent:';
    return await showConfirmModal({
      title: 'Remove dependent entities?',
      message,
      detail: lines.join('\n'),
      confirmText: 'Remove and apply',
      cancelText: 'Cancel',
      confirmVariant: 'danger',
    });
  }

  initVisualTrackEditor(canvasId: string, track: any): void {
    const canvas = this.editor.$id(canvasId) as HTMLCanvasElement | null;
    if (!canvas) return;

    const ctx = canvas.getContext('2d')!;
    const size = 200; // logical drawing units
    // Render at device resolution so the (now larger) compass stays crisp on
    // retina. All drawing below stays in logical 0..200 coordinates.
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);
    const half = size / 2;
    const quarter = size / 4;

    // Segment color follows the editor (chrome) theme: straight = success,
    // diagonal = info. Resolved from the token at draw time in drawEditor().
    const segments: Record<number, { edges: [string, string]; label: string; token: string }> = {
      1: { edges: ['N', 'S'], label: 'NS', token: '--success-color' }, // straight
      2: { edges: ['E', 'W'], label: 'EW', token: '--success-color' },
      4: { edges: ['N', 'E'], label: 'NE', token: '--info-color' }, // diagonal
      8: { edges: ['N', 'W'], label: 'NW', token: '--info-color' },
      16: { edges: ['S', 'E'], label: 'SE', token: '--info-color' },
      32: { edges: ['S', 'W'], label: 'SW', token: '--info-color' },
    };

    const edgePositions: Record<string, { x: number; y: number }> = {
      N: { x: half + quarter - 5, y: half - quarter + 5 },
      E: { x: half + quarter - 5, y: half + quarter - 5 },
      S: { x: half - quarter + 5, y: half + quarter - 5 },
      W: { x: half - quarter + 5, y: half - quarter + 5 },
    };

    let currentBits = track.track_bits ?? 0;

    const drawEditor = () => {
      ctx.clearRect(0, 0, size, size);

      // Editor-chrome tokens, resolved at draw time so the mini-grid follows the
      // light/dark editor theme. (#000/#fff and #10101a stay literal: legibility
      // marks on saturated fills, correct in both themes.)
      const cs = getComputedStyle(document.documentElement);
      const v = (token: string, fallback: string) => cs.getPropertyValue(token).trim() || fallback;

      ctx.fillStyle = v('--bg-dark', '#121214');
      ctx.fillRect(0, 0, size, size);

      ctx.strokeStyle = v('--border-color', '#333340'); // outer diamond, the quietest line
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(half, 10);
      ctx.lineTo(size - 10, half);
      ctx.lineTo(half, size - 10);
      ctx.lineTo(10, half);
      ctx.closePath();
      ctx.stroke();

      // Ghost (inactive) segments are the click affordance, so they must read
      // clearly — a visible muted gray, a step brighter than the diamond above.
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = v('--text-tertiary', '#5a5a66');
      ctx.lineWidth = 1.5;
      for (const seg of Object.values(segments)) {
        const [e1, e2] = seg.edges;
        const start = edgePositions[e1];
        const end = edgePositions[e2];
        ctx.beginPath();
        ctx.moveTo(start.x, start.y);
        ctx.lineTo(end.x, end.y);
        ctx.stroke();
      }
      ctx.setLineDash([]);

      for (const [bit, seg] of Object.entries(segments)) {
        if (currentBits & parseInt(bit)) {
          const [e1, e2] = seg.edges;
          const start = edgePositions[e1];
          const end = edgePositions[e2];

          ctx.strokeStyle = v(seg.token, '#04d361');
          ctx.lineWidth = 3;
          ctx.beginPath();
          ctx.moveTo(start.x, start.y);
          ctx.lineTo(end.x, end.y);
          ctx.stroke();

          const midX = (start.x + end.x) / 2;
          const midY = (start.y + end.y) / 2;
          ctx.font = 'bold 12px sans-serif';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.strokeStyle = '#000';
          ctx.lineWidth = 3;
          ctx.strokeText(seg.label, midX, midY);
          ctx.fillStyle = '#fff';
          ctx.fillText(seg.label, midX, midY);
        }
      }

      for (const [name, pos] of Object.entries(edgePositions)) {
        let isConnected = false;
        for (const [bit, seg] of Object.entries(segments)) {
          if (currentBits & parseInt(bit) && seg.edges.includes(name)) {
            isConnected = true;
            break;
          }
        }

        ctx.fillStyle = isConnected ? v('--success-color', '#04d361') : v('--bg-hover', '#2a2a35');
        ctx.strokeStyle = isConnected ? v('--text-primary', '#e1e1e6') : v('--text-tertiary', '#5a5a66');
        ctx.lineWidth = isConnected ? 3 : 2;
        ctx.beginPath();
        ctx.arc(pos.x, pos.y, 12, 0, Math.PI * 2);
        ctx.fill();
        ctx.stroke();

        ctx.fillStyle = isConnected ? '#10101a' : v('--text-secondary', '#a8a8b3'); // #10101a = dark text on the saturated success dot (both themes)
        ctx.font = 'bold 10px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(name, pos.x, pos.y);
      }

      if (FORBIDDEN_TRACK_BITS.has(currentBits) || isSquareCornerSwitch(currentBits)) {
        ctx.fillStyle = v('--danger-color', '#e83f5b');
        ctx.beginPath();
        ctx.arc(half, half, 8, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#fff';
        ctx.font = 'bold 10px sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText('!', half, half);
      }
    };

    drawEditor();

    // Click the nearest segment to toggle it. The compass shares the one
    // validated commit path with the checkbox grid (_commitTrackBits): the
    // optimistic redraw is instant, then the commit re-renders both views from
    // server state — which also pops the confirmation modal when the change
    // would remove a switch / signal / platform.
    canvas.addEventListener('click', async (e) => {
      const rect = canvas.getBoundingClientRect();
      // Map the click into logical 0..200 space (independent of dpr / display size).
      const scaleX = size / rect.width;
      const scaleY = size / rect.height;
      const x = (e.clientX - rect.left) * scaleX;
      const y = (e.clientY - rect.top) * scaleY;

      let closestBit = null;
      let minDist = 12;

      for (const [bit, seg] of Object.entries(segments)) {
        const [e1, e2] = seg.edges;
        const start = edgePositions[e1];
        const end = edgePositions[e2];
        const dist = this.pointToSegmentDistance(x, y, start.x, start.y, end.x, end.y);
        if (dist < minDist) {
          minDist = dist;
          closestBit = parseInt(bit);
        }
      }

      if (closestBit !== null) {
        currentBits ^= closestBit;
        drawEditor();
        await this._commitTrackBits(track, currentBits);
      }
    });
  }

  pointToSegmentDistance(
    px: number,
    py: number,
    x1: number,
    y1: number,
    x2: number,
    y2: number
  ): number {
    const dx = x2 - x1;
    const dy = y2 - y1;
    const lenSq = dx * dx + dy * dy;

    if (lenSq === 0) return Math.sqrt((px - x1) * (px - x1) + (py - y1) * (py - y1));

    let t = ((px - x1) * dx + (py - y1) * dy) / lenSq;
    t = Math.max(0, Math.min(1, t));

    const nearX = x1 + t * dx;
    const nearY = y1 + t * dy;

    return Math.sqrt((px - nearX) * (px - nearX) + (py - nearY) * (py - nearY));
  }
}
