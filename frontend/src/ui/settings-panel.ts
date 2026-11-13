/**
 * Settings panel wiring — extracted from app.ts.
 *
 * Wires the label-spacing selector and the Advanced section (the runtime
 * settings, design_v2.md "Runtime settings"). setupSettingsPanel takes an
 * `onLabelSpacingChange` callback (the App's refreshNetwork) — its only App
 * dependency; everything else is api + DOM. The loader is module-internal.
 */

import { showToast } from '../lib/toast';
import { setLabelSpacing } from '../lib/constants';

export function setupSettingsPanel(onLabelSpacingChange: () => void): void {
  const labelSpacingSelect = document.getElementById('label-spacing-select') as HTMLSelectElement | null;

  // Setup label spacing selector
  if (labelSpacingSelect) {
    labelSpacingSelect.addEventListener('change', () => {
      setLabelSpacing(labelSpacingSelect.value);
      onLabelSpacingChange();
    });
  }

  // Populate the Advanced (runtime operational tuning) section.
  loadAdvancedSettings();
}

async function loadAdvancedSettings(): Promise<void> {
  const container = document.getElementById('advanced-settings-controls');
  if (!container) return;
  let settings: Record<string, number> = {};
  try {
    const res = await fetch('/api/settings');
    settings = (await res.json()).settings || {};
  } catch (e) {
    container.innerHTML =
      '<span class="setting-label-compact">Failed to load settings</span>';
    return;
  }
  const humanize = (k: string): string =>
    k
      .replace(/_kmh$/, ' (km/h)')
      .replace(/_cm$/, ' (cm)')
      .replace(/_ticks$/, ' (ticks)')
      .replace(/_s$/, ' (s)')
      .replace(/_/g, ' ')
      .replace(/^./, (c) => c.toUpperCase());
  container.innerHTML = '';
  for (const [key, value] of Object.entries(settings)) {
    const row = document.createElement('div');
    row.className = 'setting-row';
    const label = document.createElement('span');
    label.className = 'setting-label-compact settings-label-inline';
    label.textContent = humanize(key);
    const input = document.createElement('input');
    input.type = 'number';
    input.step = 'any';
    input.className = 'setting-input-compact settings-select-fill';
    input.value = String(value);
    input.addEventListener('change', async () => {
      const v = parseFloat(input.value);
      if (!Number.isFinite(v)) return;
      try {
        await fetch('/api/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ settings: { [key]: v } }),
        });
        showToast(`${humanize(key)} updated`, 'success', 1500);
      } catch (e) {
        showToast('Failed to update setting', 'error');
      }
    });
    row.appendChild(label);
    row.appendChild(input);
    container.appendChild(row);
  }
}
