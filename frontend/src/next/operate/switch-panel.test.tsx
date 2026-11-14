// @vitest-environment happy-dom
import { render } from 'preact';
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../../lib/api';
import { stateManager } from '../../lib/state-manager';
import { SwitchPanel } from './switch-panel';

const train = (over: Record<string, unknown>) => ({
  train_id: 'T1',
  name: 'T1',
  direction: 'forward',
  knob_volts: 0,
  lost: false,
  assigned_legs: {},
  blocks: [{ block_id: 'B_RIGHT', reads_occupied: true }],
  state: 'stopped',
  waiting_reason: null,
  next_block_id: null,
  claimed_block_id: null,
  ...over,
});

const network = (sw: Record<string, unknown> = {}) => ({
  operation_mode: 'virtual_hardware',
  blocks: { B_RIGHT: { block_id: 'B_RIGHT', name: 'Right' } },
  switches: { SW_1: { switch_id: 'SW_1', x: 1, y: 1, position: 0, position_known: true, held: false, ...sw } },
  trains: {
    T1: train({}),
    T2: train({ train_id: 'T2', name: 'T2', assigned_legs: { SW_1: 1 } }),
    T3: train({ train_id: 'T3', name: 'T3', state: 'lost', lost: true }),
  },
});

describe('SwitchPanel assigned legs', () => {
  let root: HTMLElement;
  const host: any = { refreshNetwork: vi.fn(async () => {}) };

  const draw = async (sw: Record<string, unknown> = {}) => {
    stateManager.updateFromNetwork(network(sw));
    await act(() => render(<SwitchPanel host={host} switchId="SW_1" />, root));
  };
  const picked = (name: string) =>
    root.querySelector(`[role="radiogroup"][aria-label="${name}"] [aria-checked="true"]`)?.textContent?.trim();

  beforeEach(() => {
    root = document.createElement('div');
    document.body.append(root);
  });

  afterEach(() => {
    render(null, root);
    root.remove();
    vi.restoreAllMocks();
  });

  it('shows each train that is not lost with its assigned leg', async () => {
    await draw();
    expect(picked('T1')).toBe('As set');
    expect(picked('T2')).toBe('Diverge');
    expect(root.querySelector('[role="radiogroup"][aria-label="T3"]')).toBeNull();
  });

  it('sends the leg the operator picks, and null for As set', async () => {
    const send = vi.spyOn(api, 'setTrainAssignedLeg').mockResolvedValue({ success: true, reason: null });
    await draw();
    const pick = async (name: string, label: string) => {
      const group = root.querySelector(`[role="radiogroup"][aria-label="${name}"]`)!;
      const button = [...group.querySelectorAll('button')].find((b) => b.textContent?.trim() === label)!;
      await act(async () => button.click());
    };
    await pick('T1', 'Main');
    await pick('T2', 'As set');
    expect(send.mock.calls).toEqual([
      ['T1', 'SW_1', 0],
      ['T2', 'SW_1', null],
    ]);
  });

  it('offers no assigned leg at a manual switch or one with no motor', async () => {
    await draw({ manual: true });
    expect(root.querySelector('[role="radiogroup"][aria-label="T1"]')).toBeNull();
    await draw({ motor_unavailable: true });
    expect(root.querySelector('[role="radiogroup"][aria-label="T1"]')).toBeNull();
  });
});
