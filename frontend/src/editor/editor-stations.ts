// editor-stations.ts — Platform + logical-station-group editor handlers.
//
// Migrated from static/js/editor-stations.js (longterm_todo.md item #7, Wave 2).
// Behavior is unchanged. Two handler classes the Editor owns
// (`new PlatformEditorHandler(this)` / `new StationGroupEditorHandler(this)`).

import { showToast } from '../lib/toast';
import type { EditorHost } from './types';
import {
  renderHeaderId,
  renderTileList,
  renderAvailabilitySegment,
  bindAvailabilitySegment,
  showFormModal,
  showPromptModal,
  showConfirmModal,
} from '../lib/helpers';

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

    const content = `
            <div class="platform-editor" data-platform-id="${platformId}">
                ${renderHeaderId(platformId, '#00d2ff')}

                <!-- INFO SECTION (Read-only, styled like Block Editor) -->
                <div class="info-box-track mb-12">
                    <div class="flex justify-between items-start mb-2">
                        <span class="text-xs text-muted uppercase tracking-wider">Info</span>
                    </div>

                    <div class="grid grid-cols-2 gap-y-2 gap-x-4 text-sm font-mono">
                        <div>
                            <span class="text-muted text-xs block">Tile Count</span>
                            <span class="text-track font-bold">${sortedPositions.length} tiles</span>
                        </div>
                        <div>
                            <span class="text-muted text-xs block">Length</span>
                            <span class="text-info">${platform.length_cm || 0} cm</span>
                        </div>
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
                        <button id="save-platform-btn" class="btn btn-primary btn-sm flex-1">💾 Save</button>
                        <button id="delete-platform-btn" class="btn btn-danger btn-sm flex-1">🗑 Delete</button>
                    </div>
                </div>

                <div class="mt-8">
                    <button id="open-stations-panel-btn" class="btn btn-secondary btn-sm btn-full">Manage Logical Stations</button>
                </div>
            </div>
        `;

    this.editor.showSecondarySidebar('🚉 Platform Editor', content);
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

    const [stationsResp, platforms] = await Promise.all([
      this.api.listStationGroups(),
      window.app?.getPlatforms(),
    ]);

    const stations = stationsResp?.stations || [];
    const platformOptions = (platforms || [])
      .map((p: any) => `<option value="${p.platform_id}">${p.name || p.platform_id}</option>`)
      .join('');

    const content = `
            <div class="station-groups-panel">
                <div class="mb-12">
                    <div class="text-xs text-muted mb-8">Create Logical Station</div>
                    <input id="new-station-group-id" class="property-input mb-8" placeholder="Station ID (optional)">
                    <input id="new-station-group-name" class="property-input mb-8" placeholder="Name">
                    <select id="new-station-group-platform" class="property-input mb-8">
                        <option value="">No initial platform</option>
                        ${platformOptions}
                    </select>
                    <button id="create-station-group-btn" class="btn btn-primary btn-sm btn-full">Create Station</button>
                </div>
                <div class="text-xs text-muted mb-8">Existing Stations</div>
                ${stations.length === 0 ? '<div class="text-muted text-sm">No logical stations yet.</div>' : ''}
                ${stations
                  .map(
                    (s: any) => `
                    <div class="property-item mb-8" data-station-id="${s.station_id}">
                        <div class="flex justify-between items-center mb-4">
                            <span class="font-mono">${s.name || s.station_id}</span>
                            <button class="btn btn-sm btn-danger delete-station-group-btn" data-station-id="${s.station_id}">Delete</button>
                        </div>
                        <div class="text-xs text-muted mb-6">Platforms: ${(s.platform_ids || []).join(', ') || '(none)'}</div>
                        <div class="flex gap-8">
                            <button class="btn btn-sm btn-secondary add-platform-to-station-btn" data-station-id="${s.station_id}">Add Platform</button>
                            <button class="btn btn-sm btn-secondary remove-platform-from-station-btn" data-station-id="${s.station_id}">Remove Platform</button>
                        </div>
                    </div>
                `
                  )
                  .join('')}
            </div>
        `;

    this.editor.showSecondarySidebar('🏛️ Stations', content);
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
          const payload = {
            station_id: stationId,
            name,
            platform_ids: initialPlatform ? [initialPlatform] : [],
          };
          await this.api.createStationGroup(payload);
          await this.showPanel();
        } catch (err: any) {
          showToast(`Failed to create station: ${err.message}`, 'error', 5000);
        }
      };
    }

    document.querySelectorAll<HTMLElement>('.delete-station-group-btn').forEach((btn) => {
      btn.onclick = async () => {
        const stationId = btn.dataset.stationId;
        const confirmed = await showConfirmModal({
          title: 'Delete Logical Station',
          message: `Delete logical station "${stationId}"?`,
          detail: 'This removes only the grouping; platform tracks remain unchanged.',
          confirmText: 'Delete',
          confirmVariant: 'danger',
        });
        if (!confirmed) {
          return;
        }
        await this.api.deleteStationGroup(stationId);
        await this.showPanel();
      };
    });

    document.querySelectorAll<HTMLElement>('.add-platform-to-station-btn').forEach((btn) => {
      btn.onclick = async () => {
        const stationId = btn.dataset.stationId;
        const platformId = await showPromptModal({
          title: 'Add Platform to Station',
          label: `Platform ID to add to station "${stationId}"`,
          defaultValue: platforms[0]?.platform_id || '',
          required: true,
          confirmText: 'Add',
          confirmVariant: 'primary',
        });
        const normalizedPlatformId = String(platformId || '').trim();
        if (!normalizedPlatformId) {
          return;
        }
        await this.api.addPlatformToStation(stationId, normalizedPlatformId);
        await this.showPanel();
      };
    });

    document.querySelectorAll<HTMLElement>('.remove-platform-from-station-btn').forEach((btn) => {
      btn.onclick = async () => {
        const stationId = btn.dataset.stationId;
        const platformId = await showPromptModal({
          title: 'Remove Platform from Station',
          label: `Platform ID to remove from station "${stationId}"`,
          defaultValue: '',
          required: true,
          confirmText: 'Remove',
          confirmVariant: 'danger',
        });
        const normalizedPlatformId = String(platformId || '').trim();
        if (!normalizedPlatformId) {
          return;
        }
        await this.api.removePlatformFromStation(stationId, normalizedPlatformId);
        await this.showPanel();
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
    const nonMemberOptions = nonMembers
      .map((p: any) => `<option value="${p.platform_id}">${p.name || p.platform_id}</option>`)
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
                <div class="text-xs text-muted mb-4">Station ID</div>
                <div class="font-mono mb-12">${station.station_id}</div>

                <div class="text-xs text-muted mb-4">Name</div>
                <div class="flex gap-8 mb-12">
                    <input id="station-detail-name" class="property-input" value="${(station.name || '').replace(/"/g, '&quot;')}">
                    <button id="station-detail-save-name-btn" class="btn btn-sm btn-primary">Save</button>
                </div>

                ${renderAvailabilitySegment('station', station, { idBase: 'station-availability' })}

                <div class="text-xs text-muted mb-8">Member Platforms (${memberIds.length})</div>
                ${memberRows || '<div class="text-muted text-sm mb-8">No platforms in this station.</div>'}

                <div class="mt-12 mb-8">
                    <div class="text-xs text-muted mb-4">Add Platform</div>
                    <div class="flex gap-8">
                        <select id="station-detail-add-platform" class="property-input" ${nonMembers.length === 0 ? 'disabled' : ''}>
                            ${
                              nonMembers.length === 0
                                ? '<option value="">All platforms are in this station</option>'
                                : nonMemberOptions
                            }
                        </select>
                        <button id="station-detail-add-platform-btn" class="btn btn-sm btn-primary"
                                ${nonMembers.length === 0 ? 'disabled' : ''}>Add</button>
                    </div>
                </div>

                <div class="mt-16 flex justify-between">
                    <button id="station-detail-back-btn" class="btn btn-sm btn-secondary">All Stations</button>
                    <button id="station-detail-delete-btn" class="btn btn-sm btn-danger">Delete Station</button>
                </div>
            </div>
        `;

    this.editor.showSecondarySidebar(`🏛️ ${station.name || station.station_id}`, content);
    setTimeout(() => this._initStationDetailHandlers(station), 0);
  }

  _initStationDetailHandlers(station: any): void {
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
        try {
          await this.api.addPlatformToStation(stationId, platformId);
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
          await this.api.removePlatformFromStation(stationId, platformId);
          await window.app?.refreshNetwork();
          await this.showStationDetail(stationId);
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
