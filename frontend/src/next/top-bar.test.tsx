// @vitest-environment happy-dom
import { render } from 'preact';
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { logsOpen } from './logs/state';
import { canvasLayers } from '../lib/canvas-layers';
import { operationEnvironment, screen, sessionState } from './state';
import { TopBar, type TopBarActions } from './top-bar';

function fakeActions(): TopBarActions {
  return {
    newLayout: vi.fn(),
    openLayout: vi.fn(),
    saveLayout: vi.fn(),
    showScreen: vi.fn(),
    openHardwareSetup: vi.fn(),
    activate: vi.fn(async () => {}),
    togglePlayPause: vi.fn(async () => {}),
    setSpeed: vi.fn(async () => {}),
    resetLayout: vi.fn(async () => {}),
    clearConflict: vi.fn(async () => {}),
    emergencyStop: vi.fn(async () => {}),
    toggleLogs: vi.fn(),
    openSettings: vi.fn(),
    restartServer: vi.fn(async () => {}),
  };
}

const buttonNamed = (root: HTMLElement, text: string): HTMLButtonElement => {
  const found = [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  if (!found) throw new Error(`no button "${text}"`);
  return found;
};

describe('TopBar', () => {
  let root: HTMLElement;
  let actions: TopBarActions;

  beforeEach(() => {
    root = document.createElement('div');
    document.body.appendChild(root);
    actions = fakeActions();
    screen.value = 'editor';
    sessionState.value = { value: 'halted', halt_reason: 'not_activated' };
    operationEnvironment.value = 'real_hardware';
    act(() => render(<TopBar actions={actions} />, root));
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
  });

  it('shows the environment, the session state, and Activate as the next step', () => {
    const chip = root.querySelector('.ntb-chip')!;
    expect(chip.textContent).toContain('Real Hardware');
    expect(chip.textContent).toContain('Halted — not activated');
    buttonNamed(root, 'Activate').click();
    expect(actions.activate).toHaveBeenCalledTimes(1);
  });

  it('swaps the next-step button when the session state changes', () => {
    act(() => {
      sessionState.value = { value: 'paused', halt_reason: null };
      operationEnvironment.value = 'simulation';
    });
    expect(root.textContent).not.toContain('Activate');
    buttonNamed(root, 'Play').click();
    expect(actions.togglePlayPause).toHaveBeenCalledTimes(1);
  });

  it('has the four screen tabs and marks the picked one', () => {
    const tabs = [...root.querySelectorAll('[role="tab"]')].map((t) => t.textContent);
    expect(tabs).toEqual(['Editor', 'Hardware Setup', 'Operate', 'Locomotives']);
    expect(root.querySelector('[role="tab"][aria-selected="true"]')!.textContent).toBe('Editor');
    buttonNamed(root, 'Operate').click();
    expect(actions.showScreen).toHaveBeenCalledWith('operate');
  });

  it('runs Emergency stop from the top bar', () => {
    buttonNamed(root, 'Emergency stop').click();
    expect(actions.emergencyStop).toHaveBeenCalledTimes(1);
  });

  it('toggles the Logs panel from Logs, and shows it pressed while open', () => {
    buttonNamed(root, 'Logs').click();
    expect(actions.toggleLogs).toHaveBeenCalledTimes(1);
    expect(buttonNamed(root, 'Logs').getAttribute('aria-pressed')).toBe('false');
    act(() => {
      logsOpen.value = true;
    });
    expect(buttonNamed(root, 'Logs').getAttribute('aria-pressed')).toBe('true');
    act(() => {
      logsOpen.value = false;
    });
  });

  it('turns map layers on from View and keeps the menu open for the next one', () => {
    canvasLayers.set('coords', false);
    act(() => buttonNamed(root, 'View').click());
    const row = [...root.querySelectorAll('.ntb-menu [role="menuitemcheckbox"]')].find(
      (b) => b.textContent?.trim() === 'Coordinates'
    ) as HTMLButtonElement;
    act(() => row.click());
    expect(canvasLayers.isOn('coords')).toBe(true);
    expect(root.querySelector('.ntb-menu')).not.toBeNull();
    canvasLayers.set('coords', false);
  });
});
