/**
 * Settings panel wiring — extracted from app.ts.
 *
 * Wires the scale selector (preset + custom), the label-spacing selector, and
 * the Advanced (runtime operational tuning) section. setupSettingsPanel takes an
 * `onLabelSpacingChange` callback (the App's refreshNetwork) — its only App
 * dependency; everything else is api + DOM. The two loaders are module-internal.
 */
import { api } from '../lib/api';
import { showToast } from '../lib/toast';
import { setLabelSpacing } from '../lib/constants';

export function setupSettingsPanel(onLabelSpacingChange: () => void): void {
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
      onLabelSpacingChange();
    });
  }

  // Load current scale on init
  loadCurrentScale();
  // Populate the Advanced (runtime operational tuning) section.
  loadAdvancedSettings();

  const applyScale = async (scaleValue: string) => {
    if (!scaleValue) return;

    const scale = parseFloat(scaleValue);
    if (isNaN(scale) || scale <= 0) {
      showToast('Invalid scale value', 'error');
      return;
    }

    try {
      await api.setScale(scale);
      await loadCurrentScale();
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

async function loadCurrentScale(): Promise<void> {
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
