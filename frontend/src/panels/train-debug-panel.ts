/**
 * Train debug panel (Resource Sequence / Occupied Tiles / Occupied Blocks) —
 * extracted from app.ts. Pure DOM builders driven by the train + the
 * stateManager's controller/dispatcher snapshot; no App state. The App keeps
 * thin `updateTrainDebugPanel` / `hideTrainDebugPanel` delegators so existing
 * callers (incl. editor-trains via window.app) are unchanged.
 */
import { stateManager } from '../lib/state-manager';

export function updateTrainDebugPanel(train: any): void {
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
        html += `<span class="train-debug-tile" style="color: var(--accent-color);">${b}</span>`;
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

export function hideTrainDebugPanel(): void {
  const controlDebug = document.getElementById('train-control-debug-section');
  if (controlDebug) controlDebug.classList.add('hidden');
  const debugGeometry = document.getElementById('train-debug-geometry');
  if (debugGeometry) debugGeometry.classList.add('hidden');
}
