class PlatformEditorHandler {
    constructor(editor) {
        this.editor = editor;
    }

    get api() {
        return this.editor.api;
    }

    async showEditor(platformId) {
        this.editor._currentEditorType = 'platform';
        this.editor._currentEditorId = platformId;

        const platform = await this.api.getPlatform(platformId);
        if (!platform) {
            window.showToast(`Platform ${platformId} not found`, 'error', 4000);
            return;
        }

        const sortedPositions = [...(platform.positions || [])].sort(
            (a, b) => a[0] - b[0] || a[1] - b[1]
        );

        const content = `
            <div class="platform-editor" data-platform-id="${platformId}">
                ${renderHeaderId(platformId, '#00d2ff')}
                ${renderTextInput('platform-name', 'Name', platform.name || '', 'Optional name')}
                ${renderNumberInput('platform-length-cm', 'Length (cm)', platform.length_cm || 0, 0, 50000, 10)}
                ${renderSelectInput('platform-side', 'Platform Side', [
                    { value: 'left', label: 'Left' },
                    { value: 'right', label: 'Right' },
                    { value: 'both', label: 'Both' }
                ], platform.platform_side || 'both')}
                ${renderNumberInput('platform-top-speed', 'Top Speed (km/h)', platform.top_speed_kmh ?? '', 0, 400, 1)}
                ${renderTileList(sortedPositions, {
                    entityType: 'platform',
                    extendStartBtnId: 'extend-platform-start-btn',
                    extendEndBtnId: 'extend-platform-end-btn',
                    removeBtnClass: 'remove-platform-tile-btn',
                })}
                <div class="flex gap-8 mt-12">
                    <button id="save-platform-btn" class="btn btn-primary btn-sm flex-1">Save</button>
                    <button id="delete-platform-btn" class="btn btn-danger btn-sm flex-1">Delete</button>
                </div>
                <div class="mt-8">
                    <button id="open-stations-panel-btn" class="btn btn-secondary btn-sm btn-full">Manage Logical Stations</button>
                </div>
            </div>
        `;

        this.editor.showSecondarySidebar('🚉 Platform Editor', content);
        setTimeout(() => this.initEditorHandlers(platformId), 0);
    }

    async initEditorHandlers(platformId) {
        const saveBtn = document.getElementById('save-platform-btn');
        if (saveBtn) {
            saveBtn.onclick = async () => {
                try {
                    const updates = {
                        name: document.getElementById('platform-name')?.value || '',
                        length_cm: parseFloat(document.getElementById('platform-length-cm')?.value || '0') || 0,
                        platform_side: document.getElementById('platform-side')?.value || 'both',
                    };
                    const topSpeedRaw = document.getElementById('platform-top-speed')?.value;
                    updates.top_speed_kmh = topSpeedRaw === '' ? null : (parseFloat(topSpeedRaw) || 0);

                    await this.api.updatePlatform(platformId, updates);
                    await window.app.refreshNetwork();
                    await this.showEditor(platformId);
                    window.showToast('Platform updated', 'success', 2500);
                } catch (err) {
                    window.showToast(`Failed to update platform: ${err.message}`, 'error', 5000);
                }
            };
        }

        const deleteBtn = document.getElementById('delete-platform-btn');
        if (deleteBtn) {
            deleteBtn.onclick = async () => {
                if (!confirm(`Delete platform ${platformId}?`)) {
                    return;
                }
                try {
                    await this.api.deletePlatform(platformId);
                    await window.app.refreshNetwork();
                    this.editor.hideSecondarySidebar();
                    window.showToast('Platform deleted', 'success', 2500);
                } catch (err) {
                    window.showToast(`Failed to delete platform: ${err.message}`, 'error', 5000);
                }
            };
        }

        document.querySelectorAll('.remove-platform-tile-btn').forEach((btn) => {
            btn.onclick = async () => {
                try {
                    const x = parseInt(btn.dataset.x, 10);
                    const y = parseInt(btn.dataset.y, 10);
                    await this.api.removePlatformTile(platformId, x, y);
                    await window.app.refreshNetwork();
                    await this.showEditor(platformId);
                } catch (err) {
                    window.showToast(`Failed to remove tile: ${err.message}`, 'error', 5000);
                }
            };
        });

        const openStationsBtn = document.getElementById('open-stations-panel-btn');
        if (openStationsBtn) {
            openStationsBtn.onclick = () => this.editor.showStationsPanel();
        }
    }

    showModal(options) {
        const defaultId = `PLAT_${Date.now().toString().slice(-6)}`;
        const platformId = window.prompt('Platform ID', options?.platformId || defaultId);
        if (platformId === null) {
            return null;
        }
        const name = window.prompt('Platform Name (optional)', options?.name || '') || '';
        const lengthValue = window.prompt('Platform Length (cm)', '0');
        if (lengthValue === null) {
            return null;
        }
        const sideValue = window.prompt('Platform Side (left/right/both)', 'both');
        if (sideValue === null) {
            return null;
        }
        const topSpeedValue = window.prompt('Top Speed km/h (blank for none)', '');
        if (topSpeedValue === null) {
            return null;
        }

        return {
            platformId,
            name,
            lengthCm: parseFloat(lengthValue || '0') || 0,
            platformSide: (sideValue || 'both').toLowerCase(),
            topSpeedKmh: topSpeedValue === '' ? null : (parseFloat(topSpeedValue) || 0),
        };
    }

    async showBlockAssignmentDialog(platformId, x, y) {
        const blockId = window.prompt(`Assign block to tile (${x}, ${y})`, '');
        if (!blockId) {
            return;
        }
        await this.api.assignPlatformTileToBlock(platformId, x, y, blockId);
        await window.app.refreshNetwork();
        await this.showEditor(platformId);
    }

    async enterExtendMode(platformId, _direction) {
        this.editor.stationExtendMode = { active: true, platformId };
        window.showToast(`Platform extend mode: ${platformId}`, 'info', 2500);
    }

    exitExtendMode() {
        this.editor.stationExtendMode = { active: false };
    }

    async handleExtendClick(x, y) {
        const platformId = this.editor.stationExtendMode?.platformId;
        if (!platformId) {
            return false;
        }
        await this.api.extendPlatform(platformId, [[x, y]]);
        await window.app.refreshNetwork();
        await this.showEditor(platformId);
        return true;
    }

    _cleanup() {}
}

class StationGroupEditorHandler {
    constructor(editor) {
        this.editor = editor;
    }

    get api() {
        return this.editor.api;
    }

    async showPanel() {
        this.editor._currentEditorType = 'station-group-list';
        this.editor._currentEditorId = null;

        const [stationsResp, platforms] = await Promise.all([
            this.api.listStationGroups(),
            window.app.getPlatforms(),
        ]);

        const stations = stationsResp?.stations || [];
        const platformOptions = (platforms || []).map((p) =>
            `<option value="${p.platform_id}">${p.name || p.platform_id}</option>`
        ).join('');

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
                ${stations.map((s) => `
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
                `).join('')}
            </div>
        `;

        this.editor.showSecondarySidebar('🏛️ Stations', content);
        setTimeout(() => this.initHandlers(platforms || []), 0);
    }

    initHandlers(platforms) {
        const createBtn = document.getElementById('create-station-group-btn');
        if (createBtn) {
            createBtn.onclick = async () => {
                try {
                    const stationId = document.getElementById('new-station-group-id')?.value || undefined;
                    const name = document.getElementById('new-station-group-name')?.value || '';
                    const initialPlatform = document.getElementById('new-station-group-platform')?.value || null;
                    const payload = {
                        station_id: stationId,
                        name,
                        platform_ids: initialPlatform ? [initialPlatform] : [],
                    };
                    await this.api.createStationGroup(payload);
                    await this.showPanel();
                } catch (err) {
                    window.showToast(`Failed to create station: ${err.message}`, 'error', 5000);
                }
            };
        }

        document.querySelectorAll('.delete-station-group-btn').forEach((btn) => {
            btn.onclick = async () => {
                const stationId = btn.dataset.stationId;
                if (!confirm(`Delete logical station ${stationId}?`)) {
                    return;
                }
                await this.api.deleteStationGroup(stationId);
                await this.showPanel();
            };
        });

        document.querySelectorAll('.add-platform-to-station-btn').forEach((btn) => {
            btn.onclick = async () => {
                const stationId = btn.dataset.stationId;
                const platformId = window.prompt(
                    `Platform ID to add to station ${stationId}`,
                    platforms[0]?.platform_id || ''
                );
                if (!platformId) {
                    return;
                }
                await this.api.addPlatformToStation(stationId, platformId);
                await this.showPanel();
            };
        });

        document.querySelectorAll('.remove-platform-from-station-btn').forEach((btn) => {
            btn.onclick = async () => {
                const stationId = btn.dataset.stationId;
                const platformId = window.prompt(`Platform ID to remove from station ${stationId}`, '');
                if (!platformId) {
                    return;
                }
                await this.api.removePlatformFromStation(stationId, platformId);
                await this.showPanel();
            };
        });
    }

    _cleanup() {}
}
