// editor-stations.ts — Platform + logical-station-group editor handlers.
//
// Migrated from static/js/editor-stations.js (longterm_todo.md item #7, Wave 2).
// Behavior is unchanged. Two handler classes the Editor owns
// (`new PlatformEditorHandler(this)` / `new StationGroupEditorHandler(this)`).

import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import {
  renderTileList,
  renderAvailabilitySegment,
  bindAvailabilitySegment,
  showFormModal,
  showPromptModal,
  showConfirmModal,
} from '../lib/helpers';

/** Escape a string for safe use inside an HTML attribute value. */
function escAttr(s: unknown): string {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
}

/** Read the value of a text/number/select input by id. */
function inputValue(id: string): string | undefined {
  return (document.getElementById(id) as HTMLInputElement | null)?.value;
}

export class PlatformEditorHandler {
  editor: EditorHost;

  constructor(editor: EditorHost) {
    this.editor = editor;
  }

  get api(): any {
    return this.editor.api;
  }

  async showEditor(platformId: string): Promise<void> {
    this.editor._currentEditorType = 'platform';
    this.editor._currentEditorId = platformId;

    const platform = await this.api.getPlatform(platformId);
    if (!platform) {
      showToast(`Platform ${platformId} not found`, 'error', 4000);
      return;
    }

    const sortedPositions = [...(platform.positions || [])].sort(
      (a, b) => a[0] - b[0] || a[1] - b[1]
    );

    // §11 status-header chrome: platform identity is the static --info-color
    // (§1), dropping to muted when disabled. The availability state surfaces as
    // a state chip (§7) only when it deviates from the normal "Available"; the
    // segmented control below is the editor for it.
    const isDisabled = !!platform.disabled;
    const isManual = !!platform.manual;
    const tileCount = sortedPositions.length;
    const side = platform.platform_side || 'both';
    const accentClass = isDisabled ? 'accent-muted' : 'accent-info';
    const stateChip = isDisabled
      ? '<span class="chip chip-state chip-muted">DISABLED</span>'
      : isManual
        ? '<span class="chip chip-state chip-warn">MANUAL</span>'
        : '';

    const content = `
            <div class="platform-editor" data-platform-id="${platformId}">
                <!-- §11 Status header — identity + availability pill, dynamic accent. -->
                <div class="status-header ${accentClass}">
                    <div class="status-header-row">
                        <div class="status-header-id font-mono" title="${escAttr(platformId)}">${platformId}</div>
                        ${stateChip}
                    </div>
                    <div class="status-header-ident">
                        <span class="status-header-role">Platform</span>
                        <span class="status-sep">·</span>
                        <span class="font-mono">${tileCount} tile${tileCount === 1 ? '' : 's'}</span>
                        <span class="status-sep">·</span>
                        <span>${side}</span>
                    </div>
                </div>

                <!-- TILES LIST (Scrollable with extend buttons) -->
                <div class="control-section mt-12">
                    <h4 class="control-section-header">Tiles</h4>
                    ${renderTileList(sortedPositions, {
                      entityType: 'platform',
                      extendStartBtnId: 'extend-platform-start-btn',
                      extendEndBtnId: 'extend-platform-end-btn',
                      removeBtnClass: 'remove-platform-tile-btn',
                    })}
                </div>

                <!-- EDITABLE PROPERTIES SECTION -->
                <div class="control-section control-section-divider mt-12 pt-12">
                    <h4 class="control-section-header">Properties</h4>

                    <!-- Name field - Large/Medium size -->
                    <div class="input-large-container mb-8">
                        <label class="input-large-label">Platform Name</label>
                        <input type="text" id="platform-name-input" class="input-large-field" value="${platform.name || ''}" placeholder="Enter platform name...">
                    </div>

                    <!-- Length - Small size -->
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="platform-length-input">Length (cm)</label>
                        <input type="number" id="platform-length-input" class="input-small-field input-small-field-compact" value="${platform.length_cm || 0}" min="0" step="1">
                    </div>

                    <!-- Platform Side - Small size -->
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="platform-side-input">Platform Side</label>
                        <select id="platform-side-input" class="input-small-field input-small-field-compact">
                            <option value="left" ${platform.platform_side === 'left' ? 'selected' : ''}>Left</option>
                            <option value="right" ${platform.platform_side === 'right' ? 'selected' : ''}>Right</option>
                            <option value="both" ${platform.platform_side === 'both' || !platform.platform_side ? 'selected' : ''}>Both</option>
                        </select>
                    </div>

                    <!-- Top Speed - Small size -->
                    <div class="input-small-row mb-8">
                        <label class="input-small-label input-small-label-inline" for="platform-speed-input">Max Speed (km/h)</label>
                        <input type="number" id="platform-speed-input" class="input-small-field input-small-field-compact" value="${platform.top_speed_kmh || ''}" min="0" step="1" placeholder="Not set">
                    </div>

                    ${renderAvailabilitySegment('platform', platform, { idBase: 'platform-availability' })}

                    <div class="flex gap-8">
                        <button id="save-platform-btn" class="btn btn-accent btn-sm flex-1"><i class="ti ti-device-floppy"></i> Save</button>
                        <button id="delete-platform-btn" class="btn btn-danger btn-sm flex-1"><i class="ti ti-trash"></i> Delete</button>
                    </div>
                </div>

                <div class="mt-8">
                    <button id="open-stations-panel-btn" class="btn btn-secondary btn-sm btn-full">Manage Logical Stations</button>
                </div>
            </div>
        `;

    this.editor.showSecondarySidebar('Platform Editor', content);
    setTimeout(() => this.initEditorHandlers(platformId), 0);
  }

  async initEditorHandlers(platformId: string): Promise<void> {
    // Availability segment (disabled / manual). Per docs/DESIGN_LANGUAGE.md
    // §3 segmented control. Both toggles succeed unconditionally per
    // design_new.md "Availability flag REST API surface" for platforms.
    bindAvailabilitySegment('platform-availability', async (_state, flags) => {
      try {
        const result = await this.api.updatePlatform(platformId, flags);
        if (result && result.error) {
          showToast(`Cannot change availability: ${result.error}`, 'error', 4000);
          this.showEditor(platformId);
        } else {
          showToast(`Platform ${platformId} availability updated`, 'info', 1500);
          await window.app?.refreshNetwork();
        }
      } catch (err: any) {
        console.error('Failed to update platform availability:', err);
        showToast(`Cannot change availability: ${err.message || err}`, 'error', 4000);
        this.showEditor(platformId);
      }
    });

    const saveBtn = document.getElementById('save-platform-btn') as HTMLButtonElement | null;
    if (saveBtn) {
      saveBtn.onclick = async () => {
        try {
          const updates: Record<string, unknown> = {
            name: inputValue('platform-name-input') || '',
            length_cm: parseFloat(inputValue('platform-length-input') || '0') || 0,
            platform_side: inputValue('platform-side-input') || 'both',
          };
          const topSpeedRaw = inputValue('platform-speed-input');
          updates.top_speed_kmh = topSpeedRaw === '' ? null : parseFloat(topSpeedRaw ?? '') || 0;

          await this.api.updatePlatform(platformId, updates);
          await window.app?.refreshNetwork();
          await this.showEditor(platformId);
          showToast('Platform updated', 'success', 2500);
        } catch (err: any) {
          showToast(`Failed to update platform: ${err.message}`, 'error', 5000);
        }
      };
    }

    const deleteBtn = document.getElementById('delete-platform-btn') as HTMLButtonElement | null;
    if (deleteBtn) {
      deleteBtn.onclick = async () => {
        const confirmed = await showConfirmModal({
          title: 'Delete Platform',
          message: `Delete platform "${platformId}"?`,
          detail: 'Platform assignments on its track tiles will be removed.',
          confirmText: 'Delete',
          confirmVariant: 'danger',
        });
        if (!confirmed) {
          return;
        }
        try {
          await this.api.deletePlatform(platformId);
          await window.app?.refreshNetwork();
          this.editor.hideSecondarySidebar();
          showToast('Platform deleted', 'success', 2500);
        } catch (err: any) {
          showToast(`Failed to delete platform: ${err.message}`, 'error', 5000);
        }
      };
    }

    document.querySelectorAll<HTMLElement>('.remove-platform-tile-btn').forEach((btn) => {
      btn.onclick = async () => {
        try {
          const x = parseInt(btn.dataset.x ?? '', 10);
          const y = parseInt(btn.dataset.y ?? '', 10);
          await this.api.removePlatformTile(platformId, x, y);
          await window.app?.refreshNetwork();
          await this.showEditor(platformId);
        } catch (err: any) {
          showToast(`Failed to remove tile: ${err.message}`, 'error', 5000);
        }
      };
    });

    const openStationsBtn = document.getElementById('open-stations-panel-btn') as HTMLButtonElement | null;
    if (openStationsBtn) {
      openStationsBtn.onclick = () => this.editor.showStationsPanel();
    }
  }

  async showModal(options?: { platformId?: string; name?: string }): Promise<any> {
    const defaultId = `PLAT_${Date.now().toString().slice(-6)}`;
    const values = await showFormModal({
      title: 'Create Platform',
      message: 'Enter platform properties.',
      fields: [
        {
          name: 'platformId',
          label: 'Platform ID',
          type: 'text',
          defaultValue: options?.platformId || defaultId,
          required: true,
          validate: (value: any) => (String(value || '').trim() ? '' : 'Platform ID is required.'),
        },
        {
          name: 'name',
          label: 'Platform Name (optional)',
          type: 'text',
          defaultValue: options?.name || '',
        },
        {
          name: 'lengthCm',
          label: 'Platform Length (cm)',
          type: 'number',
          defaultValue: 0,
          min: 0,
          step: 1,
          validate: (value: any) =>
            value === '' || value < 0 ? 'Length must be 0 or greater.' : '',
        },
        {
          name: 'platformSide',
          label: 'Platform Side',
          type: 'select',
          defaultValue: 'both',
          options: [
            { value: 'left', label: 'Left' },
            { value: 'right', label: 'Right' },
            { value: 'both', label: 'Both' },
          ],
        },
        {
          name: 'topSpeedKmh',
          label: 'Top Speed (km/h, optional)',
          type: 'number',
          defaultValue: '',
          min: 0,
          step: 1,
          hint: 'Leave blank for no speed limit.',
          validate: (value: any) =>
            value !== '' && value < 0 ? 'Top speed must be 0 or greater.' : '',
        },
      ],
      confirmText: 'Create Platform',
      confirmVariant: 'primary',
    });

    if (!values) {
      return null;
    }

    return {
      platformId: String(values.platformId || '').trim() || defaultId,
      name: String(values.name || ''),
      lengthCm: Number(values.lengthCm || 0),
      platformSide: String(values.platformSide || 'both').toLowerCase(),
      topSpeedKmh: values.topSpeedKmh === '' ? null : Number(values.topSpeedKmh),
    };
  }

  async showBlockAssignmentDialog(platformId: string, x: number, y: number): Promise<void> {
    const blockId = await showPromptModal({
      title: 'Assign Block to Platform Tile',
      label: `Block ID for tile (${x}, ${y})`,
      defaultValue: '',
      required: true,
      confirmText: 'Assign',
      confirmVariant: 'primary',
    });
    const normalizedBlockId = String(blockId || '').trim();
    if (!normalizedBlockId) {
      return;
    }
    await this.api.assignPlatformTileToBlock(platformId, x, y, normalizedBlockId);
    await window.app?.refreshNetwork();
    await this.showEditor(platformId);
  }

  _cleanup(): void {}
}

export class StationGroupEditorHandler {
  editor: EditorHost;

  constructor(editor: EditorHost) {
    this.editor = editor;
  }

  get api(): any {
    return this.editor.api;
  }

  async showPanel(): Promise<void> {
    this.editor._currentEditorType = 'station-group-list';
    this.editor._currentEditorId = null;

    const stationsResp = await this.api.listStationGroups();
    const platforms = window.app?.currentNetwork?.platforms || [];

    const stations = stationsResp?.stations || [];
    const platformOptions = (platforms || [])
      .map((p: any) => `<option value="${p.platform_id}">${p.name || p.platform_id}</option>`)
      .join('');

    // Each station is a card on the shared .rr-card list skeleton (§11/§12),
    // the same shell the switches list consumes — station identity carries the
    // --info-color left accent (§1/§6). The whole card is click-to-open; Delete
    // lives inside the station editor, mirroring the switches list.
    const stationCards = stations
      .map((s: any) => {
        const platformIds = s.platform_ids || [];
        const platformChips = platformIds.length
          ? platformIds.map((pid: string) => `<span class="chip">${escAttr(pid)}</span>`).join('')
          : '<span class="text-muted text-xs">No platforms</span>';
        const name = s.name && String(s.name).trim() ? String(s.name).trim() : '';
        const title = name || s.station_id;
        // Display name is the title (wraps per §5); the opaque id drops to a
        // mono caption only when a name exists.
        return `
                    <div class="rr-card station-row accent-info" data-station-id="${escAttr(s.station_id)}" role="button" tabindex="0" title="Open ${escAttr(title)}">
                        <div class="rr-card-head">
                            <div class="rr-card-title-col">
                                <div class="station-row-name">${title}</div>
                                ${name ? `<div class="rr-card-route font-mono">${escAttr(s.station_id)}</div>` : ''}
                            </div>
                        </div>
                        <div class="chip-row">${platformChips}</div>
                    </div>`;
      })
      .join('');

    const content = `
            <div class="station-groups-panel">
                <div class="station-create-form">
                    <div class="control-section-header">Create Logical Station</div>
                    <input id="new-station-group-id" class="property-input" placeholder="Station ID (optional)">
                    <input id="new-station-group-name" class="property-input" placeholder="Name">
                    <select id="new-station-group-platform" class="property-input">
                        <option value="">No initial platform</option>
                        ${platformOptions}
                    </select>
                    <button id="create-station-group-btn" class="btn btn-outline btn-full">Create Station</button>
                </div>
                <div class="control-section-header">Existing Stations</div>
                ${stations.length === 0 ? '<div class="empty-state">No logical stations yet.</div>' : ''}
                <div class="station-list">${stationCards}</div>
            </div>
        `;

    this.editor.showSecondarySidebar('Stations', content);
    setTimeout(() => this.initHandlers(platforms || []), 0);
  }

  initHandlers(platforms: any[]): void {
    const createBtn = document.getElementById('create-station-group-btn') as HTMLButtonElement | null;
    if (createBtn) {
      createBtn.onclick = async () => {
        try {
          const stationId = inputValue('new-station-group-id') || undefined;
          const name = inputValue('new-station-group-name') || '';
          const initialPlatform = inputValue('new-station-group-platform') || null;
          // A station group must contain at least one platform (Non-empty
          // parent rule), so the first platform is required at creation.
          if (!initialPlatform) {
            showToast(
              'Pick an initial platform — a station group must have at least one platform.',
              'error',
              5000
            );
            return;
          }
          const payload = {
            station_id: stationId,
            name,
            platform_ids: [initialPlatform],
          };
          await this.api.createStationGroup(payload);
          await this.showPanel();
        } catch (err: any) {
          showToast(`Failed to create station: ${err.message}`, 'error', 5000);
        }
      };
    }

    // Whole card opens the station editor (where Delete lives) — same
    // click-to-open affordance as the switches list.
    document.querySelectorAll<HTMLElement>('.station-row').forEach((row) => {
      const open = () => {
        const id = row.dataset.stationId;
        if (id) this.showStationDetail(id);
      };
      row.onclick = open;
      row.onkeydown = (e: KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          open();
        }
      };
    });
  }

  _cleanup(): void {}

  /**
   * Focused panel for a single station — opened when the user clicks a
   * station label on the canvas. Shows name, member platforms, and the
   * usual add / remove / delete actions for just this station.
   */
  async showStationDetail(stationId: string): Promise<void> {
    this.editor._currentEditorType = 'station-group-detail';
    this.editor._currentEditorId = stationId;

    const stationsResp = await this.api.listStationGroups();
    const platforms = window.app?.currentNetwork?.platforms || [];
    const station = (stationsResp?.stations || []).find((s: any) => s.station_id === stationId);
    if (!station) {
      showToast(`Station ${stationId} not found`, 'error', 4000);
      return;
    }

    const memberIds = station.platform_ids || [];
    const nonMembers = (platforms || []).filter((p: any) => !memberIds.includes(p.platform_id));
    // Owner index so the add-select can tag a platform that already belongs to
    // another station, and the Force flow can warn about cascade deletes.
    const ownerByPlatform: Record<string, { id: string; name: string; count: number }> = {};
    for (const s of stationsResp?.stations || []) {
      for (const pid of s.platform_ids || []) {
        ownerByPlatform[pid] = {
          id: s.station_id,
          name: s.name || s.station_id,
          count: (s.platform_ids || []).length,
        };
      }
    }
    const nonMemberOptions = nonMembers
      .map((p: any) => {
        const owner = ownerByPlatform[p.platform_id];
        const tag = owner && owner.id !== stationId ? ` — in ${owner.name}` : '';
        return `<option value="${p.platform_id}">${p.name || p.platform_id}${tag}</option>`;
      })
      .join('');

    const memberRows = (station.platforms || [])
      .map(
        (pf: any) => `
            <div class="flex justify-between items-center mb-4" data-platform-id="${pf.platform_id}">
                <span class="font-mono text-sm">${pf.name || pf.platform_id}</span>
                <button class="btn btn-sm btn-secondary station-detail-remove-platform-btn"
                        data-station-id="${stationId}" data-platform-id="${pf.platform_id}">Remove</button>
            </div>
        `
      )
      .join('');

    const content = `
            <div class="station-detail-panel">
                <div class="entity-card" style="--card-accent: var(--info-color)">
                    <div class="entity-card-dot" style="background-color: var(--info-color)"></div>
                    <div class="entity-card-body">
                        <span class="entity-card-id">${station.station_id}</span>
                        <span class="entity-card-meta" style="color: var(--info-color)">${memberIds.length} platform${memberIds.length === 1 ? '' : 's'}</span>
                    </div>
                </div>

                <label class="property-label">Name</label>
                <div class="flex gap-8 mb-12">
                    <input id="station-detail-name" class="property-input" value="${(station.name || '').replace(/"/g, '&quot;')}">
                    <button id="station-detail-save-name-btn" class="btn btn-sm btn-accent">Save</button>
                </div>

                ${renderAvailabilitySegment('station', station, { idBase: 'station-availability' })}

                <div class="control-section-header">Member Platforms (${memberIds.length})</div>
                ${memberRows || '<div class="text-muted text-sm mb-8">No platforms in this station.</div>'}

                <div class="mt-12 mb-8">
                    <div class="control-section-header">Add Platform</div>
                    <div class="flex gap-8 items-center">
                        <select id="station-detail-add-platform" class="property-input" ${nonMembers.length === 0 ? 'disabled' : ''}>
                            ${
                              nonMembers.length === 0
                                ? '<option value="">All platforms are in this station</option>'
                                : nonMemberOptions
                            }
                        </select>
                        <label class="checkbox-wrapper" title="Reassign the platform from its current station. If that empties the other station, it is deleted.">
                            <input type="checkbox" id="station-detail-add-force" class="checkbox-input">
                            <span class="checkbox-custom"></span>
                            <span class="checkbox-label">Force</span>
                        </label>
                        <button id="station-detail-add-platform-btn" class="btn btn-sm btn-accent"
                                ${nonMembers.length === 0 ? 'disabled' : ''}>Add</button>
                    </div>
                </div>

                <div class="mt-16 flex justify-between">
                    <button id="station-detail-back-btn" class="btn btn-sm btn-secondary">All Stations</button>
                    <button id="station-detail-delete-btn" class="btn btn-sm btn-danger">Delete Station</button>
                </div>
            </div>
        `;

    this.editor.showSecondarySidebar('Station Editor', content);
    setTimeout(() => this._initStationDetailHandlers(station, ownerByPlatform), 0);
  }

  _initStationDetailHandlers(
    station: any,
    ownerByPlatform: Record<string, { id: string; name: string; count: number }> = {}
  ): void {
    const stationId = station.station_id;

    // Availability segment (disabled / manual). Per docs/DESIGN_LANGUAGE.md
    // §3 segmented control. Both succeed unconditionally per design_new.md
    // "Availability flag REST API surface" for stations.
    bindAvailabilitySegment('station-availability', async (_state, flags) => {
      try {
        const result = await this.api.updateStationGroup(stationId, flags);
        if (result && result.error) {
          showToast(`Cannot change availability: ${result.error}`, 'error', 4000);
          this.showStationDetail(stationId);
        } else {
          showToast(`Station ${stationId} availability updated`, 'info', 1500);
          await window.app?.refreshNetwork();
        }
      } catch (err: any) {
        console.error('Failed to update station availability:', err);
        showToast(`Cannot change availability: ${err.message || err}`, 'error', 4000);
        this.showStationDetail(stationId);
      }
    });

    const saveBtn = document.getElementById('station-detail-save-name-btn') as HTMLButtonElement | null;
    if (saveBtn) {
      saveBtn.onclick = async () => {
        const name = inputValue('station-detail-name') || '';
        try {
          await this.api.updateStationGroup(stationId, { name });
          await window.app?.refreshNetwork();
          await this.showStationDetail(stationId);
          showToast('Station name updated', 'success', 2000);
        } catch (err: any) {
          showToast(`Failed to save: ${err.message}`, 'error', 5000);
        }
      };
    }

    const addBtn = document.getElementById('station-detail-add-platform-btn') as HTMLButtonElement | null;
    if (addBtn) {
      addBtn.onclick = async () => {
        const platformId = inputValue('station-detail-add-platform');
        if (!platformId) return;
        const force =
          (document.getElementById('station-detail-add-force') as HTMLInputElement | null)?.checked ||
          false;
        const owner = ownerByPlatform[platformId];
        const conflict = !!owner && owner.id !== stationId;
        if (conflict && !force) {
          showToast(`Platform is in "${owner.name}". Tick Force to reassign it.`, 'error', 5000);
          return;
        }
        // Cascade warn: forcing the last platform out of its station deletes
        // that station; name any routes whose StationTarget tasks would break
        // (design_new.md "Unreachable and broken task handling").
        if (conflict && force && owner.count === 1) {
          const affected: string[] = [];
          try {
            const routesResp = await this.api.getRoutes();
            const routes = routesResp?.routes || routesResp?.train_routes || [];
            for (const r of routes) {
              if (
                (r.tasks || []).some(
                  (t: any) => t.task_location && t.task_location.station_id === owner.id
                )
              ) {
                affected.push(r.name || r.route_id);
              }
            }
          } catch {
            /* best-effort cascade detection */
          }
          const confirmed = await showConfirmModal({
            title: 'Reassign will delete a station',
            message: `Moving this platform empties and deletes "${owner.name}".`,
            detail:
              affected.length > 0
                ? `Routes that stop there will break (tasks become "broken" and get skipped): ${affected.join(', ')}.`
                : 'No routes reference the deleted station.',
            confirmText: 'Delete and reassign',
            confirmVariant: 'danger',
          });
          if (!confirmed) return;
        }
        try {
          await this.api.addPlatformToStation(stationId, platformId, force);
          await window.app?.refreshNetwork();
          await this.showStationDetail(stationId);
        } catch (err: any) {
          showToast(`Failed to add: ${err.message}`, 'error', 5000);
        }
      };
    }

    document.querySelectorAll<HTMLElement>('.station-detail-remove-platform-btn').forEach((btn) => {
      btn.onclick = async () => {
        const platformId = btn.dataset.platformId;
        try {
          const resp = await this.api.removePlatformFromStation(stationId, platformId);
          await window.app?.refreshNetwork();
          if (resp?.station_deleted) {
            // Removing the last platform auto-deleted the group; its detail
            // view no longer exists, so fall back to the station list.
            await this.showPanel();
          } else {
            await this.showStationDetail(stationId);
          }
        } catch (err: any) {
          showToast(`Failed to remove: ${err.message}`, 'error', 5000);
        }
      };
    });

    const deleteBtn = document.getElementById('station-detail-delete-btn') as HTMLButtonElement | null;
    if (deleteBtn) {
      deleteBtn.onclick = async () => {
        const confirmed = await showConfirmModal({
          title: 'Delete Logical Station',
          message: `Delete logical station "${station.name || stationId}"?`,
          detail: 'This removes only the grouping; platform tracks remain unchanged.',
          confirmText: 'Delete',
          confirmVariant: 'danger',
        });
        if (!confirmed) return;
        await this.api.deleteStationGroup(stationId);
        await window.app?.refreshNetwork();
        await this.showPanel();
      };
    }

    const backBtn = document.getElementById('station-detail-back-btn') as HTMLButtonElement | null;
    if (backBtn) {
      backBtn.onclick = () => this.showPanel();
    }
  }


}
