class EditorUIHandler {
    constructor(editor) {
        this.editor = editor;
    }

    get selectedTool() { return this.editor.selectedTool; }

    setupSecondarySidebar() {
        const closeBtn = document.getElementById('close-secondary-sidebar');
        if (closeBtn) {
            closeBtn.addEventListener('click', () => this.hideSecondarySidebar());
        }
    }

    setupCollapsibleCategories() {
        const categories = document.querySelectorAll('.tool-category');
        
        categories.forEach(category => {
            const label = category.querySelector('.tool-category-label');
            if (!label) return;
            
            label.addEventListener('click', (e) => {
                e.stopPropagation();
                this.toggleCategory(category);
            });
        });
        
        this.loadCategoryStates();
        
        this.expandCategoryForTool(this.selectedTool);
    }
    
    toggleCategory(category) {
        const wasCollapsed = category.classList.contains('collapsed');
        
        if (wasCollapsed) {
            category.classList.remove('collapsed');
        } else {
            category.classList.add('collapsed');
        }
        
        this.saveCategoryStates();
    }
    
    expandCategoryForTool(toolName) {
        if (!toolName) return;
        
        const toolBtn = document.querySelector(`.tool-btn[data-tool="${toolName}"]`);
        if (!toolBtn) return;
        
        const category = toolBtn.closest('.tool-category');
        if (category && category.classList.contains('collapsed')) {
            category.classList.remove('collapsed');
            this.saveCategoryStates();
        }
    }
    
    saveCategoryStates() {
        const categories = document.querySelectorAll('.tool-category');
        const states = {};
        
        categories.forEach((category, index) => {
            const label = category.querySelector('.tool-category-label');
            const categoryName = label ? label.textContent.trim() : `category-${index}`;
            states[categoryName] = category.classList.contains('collapsed');
        });
        
        try {
            localStorage.setItem('toolCategoryStates', JSON.stringify(states));
        } catch (e) {
        }
    }
    
    loadCategoryStates() {
        try {
            const saved = localStorage.getItem('toolCategoryStates');
            if (!saved) return;
            
            const states = JSON.parse(saved);
            const categories = document.querySelectorAll('.tool-category');
            
            categories.forEach((category, index) => {
                const label = category.querySelector('.tool-category-label');
                const categoryName = label ? label.textContent.trim() : `category-${index}`;
                
                if (states[categoryName] === true) {
                    category.classList.add('collapsed');
                }
            });
        } catch (e) {
        }
    }

    showSecondarySidebar(title, content) {
        const sidebar = document.getElementById('secondary-sidebar');
        const titleEl = document.getElementById('secondary-sidebar-title');
        const contentEl = document.getElementById('secondary-sidebar-content');

        if (sidebar && titleEl && contentEl) {
            titleEl.textContent = title;
            contentEl.innerHTML = content;
            
            sidebar.classList.remove('compact');
            
            const mainSidebar = document.querySelector('.sidebar');
            if (mainSidebar) {
                sidebar.style.left = mainSidebar.offsetWidth + 'px';
            }
            
            sidebar.classList.add('visible');
        }
    }

    hideSecondarySidebar() {
        const sidebar = document.getElementById('secondary-sidebar');
        if (sidebar) {
            sidebar.classList.remove('visible');
            sidebar.classList.remove('compact');
        }
        this.editor._currentEditorType = null;
        this.editor._currentEditorId = null;
    }

    showFullJSONViewer(data, title = 'JSON Viewer') {
        const jsonString = JSON.stringify(data, null, 2);
        
        const content = `
            <div class="json-viewer-panel" style="background: transparent; height: 100%; display: flex; flex-direction: column;">
                <div style="margin-bottom: 12px;">
                    <button id="copy-json-btn" class="btn btn-primary btn-full text-sm">
                        📋 Copy to Clipboard
                    </button>
                </div>

                <div style="flex: 1; overflow: auto; background: #0d1020; border: 1px solid #3a3a4e; border-radius: 6px; padding: 12px;">
                    <pre id="json-content" style="margin: 0; font-family: 'Courier New', monospace; font-size: 11px; color: #00ff88; line-height: 1.4; white-space: pre-wrap; word-wrap: break-word;">${this.syntaxHighlightJSON(jsonString)}</pre>
                </div>
            </div>
        `;

        this.showSecondarySidebar(title, content);

        setTimeout(() => {
            const copyBtn = document.getElementById('copy-json-btn');
            if (copyBtn) {
                copyBtn.onclick = () => {
                    navigator.clipboard.writeText(jsonString).then(() => {
                        window.showToast('JSON copied to clipboard', 'success', 2000);
                        copyBtn.textContent = '✓ Copied!';
                        copyBtn.classList.remove('btn-primary');
                        copyBtn.classList.add('btn-success');
                        setTimeout(() => {
                            copyBtn.textContent = '📋 Copy to Clipboard';
                            copyBtn.classList.remove('btn-success');
                            copyBtn.classList.add('btn-primary');
                        }, 2000);
                    }).catch(err => {
                        console.error('Failed to copy JSON:', err);
                        window.showToast('Failed to copy JSON', 'error', 3000);
                    });
                };
            }
        }, 0);
    }

    syntaxHighlightJSON(jsonString) {
        return jsonString
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/("(\\u[a-zA-Z0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)/g, (match) => {
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
                
                const colors = {
                    'json-key': '#00d4ff',
                    'json-string': '#ffd93d',
                    'json-number': '#ff79c6',
                    'json-boolean': '#04d361',
                    'json-null': '#888'
                };
                
                return `<span style="color: ${colors[cls]};">${match}</span>`;
            });
    }

    updateSecondaryEditorForTile(track) {
        if (!this.editor._currentEditorType) {
            return;
        }

        switch (this.editor._currentEditorType) {
            case 'signal':
                if (track.signals && track.signals.length > 0) {
                    this.editor.showSignalsEditorForTile(track);
                } else {
                    this.hideSecondarySidebar();
                }
                break;

            case 'block':
                if (track.block_section_id) {
                    this.editor.showBlockEditor(track.block_section_id);
                } else {
                    this.hideSecondarySidebar();
                }
                break;

            case 'station':
                if (track.station_id) {
                    this.editor.showStationEditor(track.station_id);
                } else {
                    this.hideSecondarySidebar();
                }
                break;
        }
    }

    initViewFullJSONButton(track) {
        const viewJsonBtn = document.getElementById('view-full-json-btn');
        if (viewJsonBtn) {
            viewJsonBtn.onclick = () => {
                this.showFullJSONViewer(track, `📄 JSON: (${track.x}, ${track.y})`);
            };
        }
    }

    initStationButtonHandlers() {
        const editStationBtns = document.querySelectorAll('.edit-station-btn');
        editStationBtns.forEach(btn => {
            btn.onclick = () => {
                const stationId = btn.dataset.stationId;
                if (stationId) {
                    this.editor.showStationEditor(stationId);
                }
            };
        });
    }

    initBitEditor(track) {
        const bitEditor = document.getElementById('bit-editor');
        if (!bitEditor) return;

        const calculateBitsFromCheckboxes = () => {
            let bits = 0;
            document.querySelectorAll('#bit-editor input[type="checkbox"]').forEach(cb => {
                if (cb.checked) {
                    bits |= parseInt(cb.dataset.bit);
                }
            });
            return bits;
        };

        const updateBitDisplay = () => {
            const bits = calculateBitsFromCheckboxes();
            const totalSpan = document.getElementById('bit-total');
            const binarySpan = document.getElementById('bit-binary');
            if (totalSpan) totalSpan.textContent = bits;
            if (binarySpan) binarySpan.textContent = bits.toString(2).padStart(6, '0');

            document.querySelectorAll('#bit-editor input[type="checkbox"]').forEach(cb => {
                const label = cb.parentElement;
                const span = label.querySelector('span');
                const isChecked = cb.checked;
                label.style.background = isChecked ? '#2a4a3e' : '#2a2a34';
                label.style.borderColor = isChecked ? '#00ff88' : 'transparent';
                if (span) span.style.color = isChecked ? '#00ff88' : '#ccc';
            });

            const warning = document.querySelector('.bit-warning');
            if (warning) {
                warning.style.display = FORBIDDEN_TRACK_BITS.has(bits) ? 'block' : 'none';
            }
        };

        const checkboxes = bitEditor.querySelectorAll('input[type="checkbox"]');
        checkboxes.forEach(cb => {
            cb.addEventListener('change', () => updateBitDisplay());
        });

        const applyButton = document.getElementById('apply-bits');
        if (applyButton) {
            applyButton.onclick = async () => {
                const newBits = calculateBitsFromCheckboxes();

                if (FORBIDDEN_TRACK_BITS.has(newBits)) {
                    alert('Cannot apply forbidden configuration');
                    return;
                }

                try {
                    if (track.isVirtual) {
                        const newTrack = {
                            x: track.x,
                            y: track.y,
                            track_bits: newBits,
                            directed_paths: []
                        };
                        await window.api.addTracksBulk([newTrack]);
                    } else {
                        await window.api.updateTrack(track.x, track.y, { track_bits: newBits });
                    }

                    await window.app.refreshNetwork();
                    await this.editor.selectElement({x: track.x, y: track.y});
                } catch (err) {
                    console.error('Failed to update track bits:', err);
                    alert('Failed to update track bits: ' + err.message);
                }
            };
        }
    }

    initVisualTrackEditor(canvasId, track) {
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;

        const ctx = canvas.getContext('2d');
        canvas.width = 200;
        canvas.height = 200;
        const size = 200;
        const half = size / 2;
        const quarter = size / 4;

        const segments = {
            1:  { edges: ['N', 'S'], label: 'NS', color: '#00ff88' },
            2:  { edges: ['E', 'W'], label: 'EW', color: '#00ff88' },
            4:  { edges: ['N', 'E'], label: 'NE', color: '#00d4ff' },
            8:  { edges: ['N', 'W'], label: 'NW', color: '#00d4ff' },
            16: { edges: ['S', 'E'], label: 'SE', color: '#00d4ff' },
            32: { edges: ['S', 'W'], label: 'SW', color: '#00d4ff' }
        };

        const edgePositions = {
            'N': { x: half + quarter - 5, y: half - quarter + 5 },
            'E': { x: half + quarter - 5, y: half + quarter - 5 },
            'S': { x: half - quarter + 5, y: half + quarter - 5 },
            'W': { x: half - quarter + 5, y: half - quarter + 5 }
        };

        let currentBits = track.track_bits ?? 0;

        const drawEditor = () => {
            ctx.clearRect(0, 0, size, size);

            ctx.fillStyle = '#111';
            ctx.fillRect(0, 0, size, size);

            ctx.strokeStyle = '#333';
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(half, 10);
            ctx.lineTo(size - 10, half);
            ctx.lineTo(half, size - 10);
            ctx.lineTo(10, half);
            ctx.closePath();
            ctx.stroke();

            ctx.setLineDash([2, 4]);
            ctx.strokeStyle = '#222';
            ctx.lineWidth = 1;
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

                    ctx.strokeStyle = seg.color;
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
                    if ((currentBits & parseInt(bit)) && seg.edges.includes(name)) {
                        isConnected = true;
                        break;
                    }
                }

                ctx.fillStyle = isConnected ? '#00ff88' : '#444';
                ctx.strokeStyle = isConnected ? '#fff' : '#666';
                ctx.lineWidth = isConnected ? 3 : 2;
                ctx.beginPath();
                ctx.arc(pos.x, pos.y, 12, 0, Math.PI * 2);
                ctx.fill();
                ctx.stroke();

                ctx.fillStyle = isConnected ? '#000' : '#ccc';
                ctx.font = 'bold 10px sans-serif';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(name, pos.x, pos.y);
            }

            if (FORBIDDEN_TRACK_BITS.has(currentBits)) {
                ctx.fillStyle = '#e83f5b';
                ctx.beginPath();
                ctx.arc(half, half, 8, 0, Math.PI * 2);
                ctx.fill();
                ctx.fillStyle = '#fff';
                ctx.font = 'bold 10px sans-serif';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText('!', half, half);
            }

            const numSegments = Object.keys(segments).filter(bit => currentBits & parseInt(bit)).length;
            ctx.fillStyle = FORBIDDEN_TRACK_BITS.has(currentBits) ? '#e83f5b' : '#aaa';
            ctx.font = 'bold 12px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText(`${numSegments} segment${numSegments !== 1 ? 's' : ''} (bits: ${currentBits})`, half, size - 10);
        };

        const saveToServer = async () => {
            if (FORBIDDEN_TRACK_BITS.has(currentBits)) {
                console.warn('Cannot save forbidden configuration');
                return;
            }

            try {
                await this.editor.api.updateTrack(track.x, track.y, { track_bits: currentBits });
                await window.app.refreshNetwork();
                await this.editor.selectElement({x: track.x, y: track.y});
            } catch (err) {
                console.error('Failed to update track bits:', err);
            }
        };

        drawEditor();

        canvas.addEventListener('click', async (e) => {
            const rect = canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;

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

                const checkbox = document.querySelector(`#bit-editor input[data-bit="${closestBit}"]`);
                if (checkbox) {
                    checkbox.checked = !!(currentBits & closestBit);
                    checkbox.dispatchEvent(new Event('change'));
                }

                drawEditor();
                await saveToServer();
            }
        });
    }

    pointToSegmentDistance(px, py, x1, y1, x2, y2) {
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

    clearPropertiesPanel() {
        const panel = document.querySelector('.properties-panel');
        if (panel) {
            panel.innerHTML = `
                <h3>Properties</h3>
                <p style="color: #666; font-size: 12px;">Select an element to view properties</p>
            `;
        }
    }
}
