// @vitest-environment happy-dom
import { render } from 'preact';
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { stateManager } from '../../lib/state-manager';
import { liveVersion } from './live';
import { cardsFolded, selection, type OperateHost } from './operate-state';
import { TrainCardsRow } from './train-cards';

function fakeHost(): OperateHost {
  return {
    editor: { showAddTrainDialog: vi.fn() },
    selectTrain: vi.fn(),
    holdAll: vi.fn(async () => {}),
  } as unknown as OperateHost;
}

const NETWORK = {
  trains: [{ id: 'T_ATO' }, { id: 'T_MAN' }],
  controllers_by_train_id: {
    T_ATO: {
      train_status: 'ato',
      control_mode: 'ato',
      power_on: true,
      engaged: false,
      is_stopped: true,
      current_speed: 0,
      atc_speed_limit: 40,
    },
    T_MAN: {
      train_status: 'manual',
      control_mode: 'manual',
      manual_mode_type: 'simulation',
      power_on: true,
      is_stopped: false,
      current_speed: 31.6,
      atc_speed_limit: 45,
      handle_position: 'P2',
    },
  },
  operator_status_by_train_id: {
    T_ATO: { ato_status: 'READY', hold_reason: { code: 'no_route', question: 3, detail: null }, pending_actions: [] },
    T_MAN: { ato_status: 'OFF', hold_reason: null, pending_actions: [] },
  },
  assigned_routes_by_train_id: { T_ATO: null, T_MAN: null },
};

const buttonNamed = (root: HTMLElement, text: string): HTMLButtonElement => {
  const found = [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === text);
  if (!found) throw new Error(`no button "${text}"`);
  return found;
};

describe('TrainCardsRow', () => {
  let root: HTMLElement;
  let host: OperateHost;

  beforeEach(() => {
    stateManager.updateFromNetwork(NETWORK);
    root = document.createElement('div');
    document.body.appendChild(root);
    host = fakeHost();
    selection.value = null;
    cardsFolded.value = false;
    act(() => render(<TrainCardsRow host={host} />, root));
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
    stateManager.updateFromNetwork({ trains: [], controllers_by_train_id: {} });
  });

  const card = (id: string) => root.querySelector(`[aria-label="Train ${id}"]`) as HTMLElement;

  it('shows each train with its speed, ATC limit, train_status and ato_status', () => {
    expect(card('T_MAN').textContent).toContain('32');
    expect(card('T_MAN').textContent).toContain('ATC 45');
    expect(card('T_MAN').textContent).toContain('MANUAL');
    expect(card('T_ATO').textContent).toContain('ATO');
    expect(card('T_ATO').textContent).toContain('READY');
  });

  it('shows the hold reason only for a standing train', () => {
    expect(card('T_ATO').querySelector('.op-card-hold')!.textContent).toBe('No route is assigned.');
    expect(card('T_MAN').querySelector('.op-card-hold')).toBeNull();
  });

  it('gives an ATO train Go, unavailable until a route is assigned', () => {
    const go = buttonNamed(card('T_ATO'), '▶ Go');
    expect(go.disabled).toBe(true);
    expect(go.title).toBe('Assign a route first');
  });

  it('gives a manual train the handle at its current notch', () => {
    const handle = card('T_MAN').querySelector('input[aria-label="Handle"]') as HTMLInputElement;
    expect(handle.getAttribute('aria-valuetext')).toBe('P2');
  });

  it('selects the train when the card is clicked, but not when its control is', () => {
    card('T_MAN').click();
    expect(host.selectTrain).toHaveBeenCalledWith('T_MAN');
    buttonNamed(card('T_ATO'), '▶ Go').closest('.op-card-control')!.dispatchEvent(new Event('click', { bubbles: true }));
    expect(host.selectTrain).toHaveBeenCalledTimes(1);
  });

  it('colours each card edge by whether the train needs the operator', () => {
    // T_ATO stands with no route, so it waits on the operator; T_MAN moves.
    expect(card('T_ATO').dataset.tone).toBe('warn');
    expect(card('T_MAN').dataset.tone).toBe('ok');
  });

  it('marks the selected train\'s card', () => {
    act(() => {
      selection.value = { kind: 'train', id: 'T_ATO' };
    });
    expect(card('T_ATO').classList.contains('row-selected')).toBe(true);
    expect(card('T_MAN').classList.contains('row-selected')).toBe(false);
  });

  it('follows the next poll', async () => {
    await act(async () => {
      stateManager.updateFromNetwork({
        ...NETWORK,
        controllers_by_train_id: { ...NETWORK.controllers_by_train_id, T_MAN: { train_status: 'eb', current_speed: 0 } },
      });
      await Promise.resolve();
      liveVersion.value++;
    });
    expect(card('T_MAN').textContent).toContain('EB');
    buttonNamed(card('T_MAN'), 'Release');
  });

  it('leaves the session state off every card', async () => {
    await act(async () => {
      stateManager.updateFromNetwork({
        ...NETWORK,
        operator_status_by_train_id: {
          ...NETWORK.operator_status_by_train_id,
          T_ATO: {
            ato_status: 'READY',
            hold_reason: { code: 'halted', question: 1, detail: 'hardware_failure' },
            pending_actions: [],
          },
        },
      });
      await Promise.resolve();
      liveVersion.value++;
    });
    expect(card('T_ATO').querySelector('.op-card-hold')).toBeNull();
  });

  it('folds to one line per card, with the hold reason as hover text, and back', () => {
    const fold = root.querySelector('button[aria-label="Fold the train cards to one line"]') as HTMLButtonElement;
    act(() => fold.click());
    expect(root.querySelector('.op-cards')!.classList.contains('is-folded')).toBe(true);
    expect(card('T_ATO').querySelector('.op-card-hold')).toBeNull();
    expect(card('T_ATO').title).toBe('No route is assigned.');
    buttonNamed(card('T_ATO'), '▶ Go');
    const unfold = root.querySelector('button[aria-label="Show the full train cards"]') as HTMLButtonElement;
    act(() => unfold.click());
    expect(root.querySelector('.op-cards')!.classList.contains('is-folded')).toBe(false);
    expect(card('T_ATO').querySelector('.op-card-hold')!.textContent).toBe('No route is assigned.');
  });

  it('ends the row with Place train and Hold all', () => {
    buttonNamed(root, 'Place train').click();
    expect(host.editor.showAddTrainDialog).toHaveBeenCalled();
    buttonNamed(root, 'Hold all').click();
    expect(host.holdAll).toHaveBeenCalled();
  });
});
