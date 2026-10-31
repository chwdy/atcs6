// @vitest-environment happy-dom
import { render } from 'preact';
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { layoutEmpty, operationEnvironment, screen } from '../state';
import { StartCard, startCardDismissed, type FirstRunActions } from './start-card';

const buttonStarting = (root: HTMLElement, text: string): HTMLButtonElement => {
  const found = [...root.querySelectorAll('button')].find((b) => b.textContent?.trim().startsWith(text));
  if (!found) throw new Error(`no button starting "${text}"`);
  return found;
};

describe('StartCard', () => {
  let root: HTMLElement;
  let actions: FirstRunActions;

  beforeEach(() => {
    root = document.createElement('div');
    document.body.appendChild(root);
    actions = { startFromLoop: vi.fn(async () => {}), openLayout: vi.fn(), drawFromScratch: vi.fn() };
    startCardDismissed.value = false;
    screen.value = 'operate';
    operationEnvironment.value = 'simulation';
    layoutEmpty.value = true;
    act(() => render(<StartCard actions={actions} />, root));
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
  });

  it('offers the three starts while the layout has no track', () => {
    expect(root.querySelector('.first-run-card')).not.toBeNull();
    buttonStarting(root, 'Start from a loop').click();
    buttonStarting(root, 'Open a layout').click();
    buttonStarting(root, 'Draw from scratch').click();
    expect(actions.startFromLoop).toHaveBeenCalledTimes(1);
    expect(actions.openLayout).toHaveBeenCalledTimes(1);
    expect(actions.drawFromScratch).toHaveBeenCalledTimes(1);
  });

  it('stays hidden before the first snapshot and once there is track', () => {
    act(() => {
      layoutEmpty.value = null;
    });
    expect(root.querySelector('.first-run-card')).toBeNull();
    act(() => {
      layoutEmpty.value = false;
    });
    expect(root.querySelector('.first-run-card')).toBeNull();
  });

  it('shows only on the screens that draw the map', () => {
    act(() => {
      screen.value = 'hardware';
    });
    expect(root.querySelector('.first-run-card')).toBeNull();
    act(() => {
      screen.value = 'editor';
    });
    expect(root.querySelector('.first-run-card')).not.toBeNull();
  });

  it('stays away after Draw from scratch', () => {
    act(() => {
      startCardDismissed.value = true;
    });
    expect(root.querySelector('.first-run-card')).toBeNull();
  });

  it('says it will switch to Simulation when another environment is active', () => {
    expect(root.textContent).not.toContain('Switches the run environment');
    act(() => {
      operationEnvironment.value = 'real_hardware';
    });
    expect(root.textContent).toContain('Switches the run environment to Simulation');
  });
});
