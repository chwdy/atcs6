// @vitest-environment happy-dom
import { render } from 'preact';
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { HardwareSetupFacts } from '../../ui/hardware-panel';

// The screen hosts hardware-panel.ts and the two benches; here they are
// stand-ins, so the test checks only what the screen itself decides.
const panel = vi.hoisted(() => {
  const facts: HardwareSetupFacts = {
    loaded: true,
    selected: 'virtual_hardware',
    awaitsActivation: false,
    chipCount: 0,
    unprovenCount: 0,
    unsaved: false,
    coverageComplete: true,
    readiness: { ready: true, issues: [], warnings: [] },
    commissioning: false,
  };
  return {
    facts,
    detach: vi.fn(),
    attachHardwareScreen: vi.fn(),
    selectHardwareEnvironment: vi.fn(async () => {}),
    activateFromHardwareSetup: vi.fn(async () => {}),
  };
});

vi.mock('../../ui/hardware-panel', () => ({
  attachHardwareScreen: panel.attachHardwareScreen,
  hardwareSetupFacts: () => panel.facts,
  hardwareSectionHtml: (name: string) => `<section data-section="${name}"></section>`,
  selectHardwareEnvironment: panel.selectHardwareEnvironment,
  activateFromHardwareSetup: panel.activateFromHardwareSetup,
  enterHardwareSetup: vi.fn(async () => {}),
  saveHardwareSetup: vi.fn(async () => {}),
  stopHardwareTests: vi.fn(async () => {}),
  reloadHardwareEnvironment: vi.fn(async () => {}),
}));
vi.mock('../../panels/occupancy-bench', () => ({ mountOccupancyBench: vi.fn() }));
vi.mock('../../panels/virtual-bench', () => ({ mountVirtualBench: vi.fn() }));
vi.mock('../../lib/api', () => ({ api: {} }));

import { operationEnvironment, screen, sessionState } from '../state';
import { HardwareSetupScreen, type HardwareScreenActions } from './screen';
import { hardwareStep, showHardwareSetup } from './state';

const buttonNamed = (root: HTMLElement, text: string): HTMLButtonElement => {
  const found = [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  if (!found) throw new Error(`no button "${text}"`);
  return found;
};

describe('HardwareSetupScreen', () => {
  let root: HTMLElement;
  let actions: HardwareScreenActions;

  const show = () => act(() => render(<HardwareSetupScreen actions={actions} />, root));

  beforeEach(() => {
    root = document.createElement('div');
    document.body.appendChild(root);
    actions = { activate: vi.fn(async () => {}), showScreen: vi.fn() };
    panel.detach.mockReset();
    panel.attachHardwareScreen.mockReset().mockReturnValue(panel.detach);
    panel.facts.selected = 'virtual_hardware';
    panel.facts.unsaved = false;
    sessionState.value = { value: 'halted', halt_reason: 'not_activated' };
    operationEnvironment.value = 'virtual_hardware';
    screen.value = 'editor';
    hardwareStep.value = null;
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
  });

  it('lists the seven steps and marks the ones already done', () => {
    show();
    const steps = [...root.querySelectorAll('.nhs-step')];
    expect(steps.map((s) => s.querySelector('.nhs-step-title')!.textContent)).toEqual([
      'Choose the environment',
      'Build the setup',
      'Enter Setup',
      'Prove each chip and each wire',
      'Close each tool',
      'Check readiness',
      'Activate',
    ]);
    expect(steps.map((s) => s.getAttribute('data-mark'))).toEqual([
      'done', 'open', 'open', 'open', 'open', 'done', 'open',
    ]);
  });

  it('shows the dialog in its own root while it is up, and gives it back', () => {
    show();
    expect(panel.attachHardwareScreen).toHaveBeenCalledWith(root.querySelector('.nhs'), expect.any(Function));
    act(() => render(null, root));
    expect(panel.detach).toHaveBeenCalledTimes(1);
  });

  it('opens at step 1 from the session-state chip, and selects an environment', () => {
    showHardwareSetup(1);
    expect(screen.value).toBe('hardware');
    show();
    expect(root.querySelector('.nhs-step[aria-current="step"] .nhs-step-title')!.textContent).toBe(
      'Choose the environment'
    );
    act(() => root.querySelector<HTMLButtonElement>('.nhs-choice[aria-checked="false"]')!.click());
    expect(panel.selectHardwareEnvironment).toHaveBeenCalledWith('real_hardware');
  });

  it('Activate is the top bar Activate, or saves the setup first when it has unsaved edits', async () => {
    showHardwareSetup(7);
    show();
    await act(async () => buttonNamed(root, 'Activate Virtual Hardware').click());
    expect(actions.activate).toHaveBeenCalledTimes(1);

    panel.facts.unsaved = true;
    act(() => {
      hardwareStep.value = 6;
    });
    act(() => {
      hardwareStep.value = 7;
    });
    await act(async () => buttonNamed(root, 'Activate Virtual Hardware').click());
    expect(panel.activateFromHardwareSetup).toHaveBeenCalledTimes(1);
  });

  it('offers no Activate while the session is Live, and says why', () => {
    sessionState.value = { value: 'live', halt_reason: null };
    panel.facts.selected = 'virtual_hardware';
    operationEnvironment.value = 'real_hardware';
    showHardwareSetup(7);
    show();
    expect(buttonNamed(root, 'Activate Virtual Hardware').disabled).toBe(true);
    expect(root.textContent).toContain('The session is Live.');
  });
});
