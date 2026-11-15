// @vitest-environment happy-dom
import { signal } from '@preact/signals';
import { render } from 'preact';
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../../lib/api';
import { stateManager } from '../../lib/state-manager';
import { liveVersion, runningTrains, usesRunningTrains } from './live';
import { cardLine, RunningTrainsRow } from './running-trains';

const train = (over: Record<string, unknown>) => ({
  train_id: 'T1',
  name: 'T1',
  direction: 'forward',
  knob_volts: 0,
  lost: false,
  blocks: [{ block_id: 'B_LEFT', reads_occupied: true }],
  state: 'stopped',
  waiting_reason: null,
  next_block_id: 'B_MAIN',
  claimed_block_id: null,
  ...over,
});

const NETWORK = {
  operation_mode: 'real_hardware',
  blocks: { B_LEFT: { block_id: 'B_LEFT', name: 'Left' }, B_MAIN: { block_id: 'B_MAIN', name: 'Main' } },
  trains: {
    T2: train({ train_id: 'T2', name: 'T2', state: 'waiting', knob_volts: 6, waiting_reason: 'block_taken' }),
    T10: train({ train_id: 'T10', name: 'T10', state: 'lost', lost: true }),
    T1: train({ state: 'moving', knob_volts: 6, claimed_block_id: 'B_MAIN' }),
  },
};

const buttonNamed = (root: HTMLElement, text: string): HTMLButtonElement[] =>
  [...root.querySelectorAll('button')].filter((b) => b.textContent?.trim() === text);

describe('RunningTrainsRow', () => {
  let root: HTMLElement;

  beforeEach(async () => {
    stateManager.updateFromNetwork(NETWORK);
    root = document.createElement('div');
    document.body.append(root);
    (window as any).app = { refreshNetwork: vi.fn(async () => {}) };
    await act(() => render(<RunningTrainsRow />, root));
  });

  afterEach(() => {
    render(null, root);
    root.remove();
    stateManager.updateFromNetwork({ operation_mode: 'simulation', trains: {} });
    vi.restoreAllMocks();
  });

  it('shows one card per train, oldest first', () => {
    expect(usesRunningTrains()).toBe(true);
    expect(runningTrains().map((t) => t.train_id)).toEqual(['T1', 'T2', 'T10']);
    const names = [...root.querySelectorAll('.rt-tag')].map((n) => n.textContent?.trim());
    expect(names).toEqual(['T1', 'T2', 'T10']);
  });

  it('shows each train\'s state on its card, and the knob\'s voltage as a number', () => {
    const cards = [...root.querySelectorAll('.rt-card')] as HTMLElement[];
    expect(cards.map((c) => c.dataset.state)).toEqual(['moving', 'waiting', 'lost']);
    expect(cards.map((c) => c.querySelector('.rt-card-state')?.textContent)).toEqual(['Moving', 'Waiting', 'Lost']);
    expect(cards.map((c) => c.querySelector('.rt-card-volts')?.textContent)).toEqual(['6.0 V', '6.0 V', '0.0 V']);
  });

  it('keeps the train emphasized when focus moves between its controls', async () => {
    const selected = signal<string | null>(null);
    const focus = vi.fn();
    await act(() => render(<RunningTrainsRow interaction={{
      selected, focus, hover: vi.fn(), select: (id) => { selected.value = id; },
    }} />, root));
    const identity = buttonNamed(root, 'T1')[0];
    const knob = root.querySelector('input[type=range]') as HTMLInputElement;
    identity.dispatchEvent(new FocusEvent('focusin', { bubbles: true }));
    expect(focus).toHaveBeenLastCalledWith('T1');
    identity.dispatchEvent(new FocusEvent('focusout', { bubbles: true, relatedTarget: knob }));
    expect(focus).toHaveBeenCalledTimes(1);
    knob.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
    expect(focus).toHaveBeenLastCalledWith(null);
    await act(() => identity.click());
    expect(identity.getAttribute('aria-pressed')).toBe('true');
  });

  it('says where a train is and what it is doing', () => {
    const [t1, t2, t10] = runningTrains();
    expect(cardLine(t1)).toBe('Left → Main');
    expect(cardLine(t2)).toBe('Left · waiting: the next block is taken');
    expect(cardLine(t10)).toBe('Lost on Left');
  });

  it('sends the knob and Stop, and leaves Stop all to the top bar', async () => {
    const ok = { success: true, reason: null };
    const knob = vi.spyOn(api, 'setTrainKnob').mockResolvedValue(ok);
    const stop = vi.spyOn(api, 'stopRunningTrain').mockResolvedValue(ok);

    const slider = root.querySelector('input[type=range]') as HTMLInputElement;
    slider.value = '8';
    await act(async () => {
      slider.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(knob).toHaveBeenCalledWith('T1', 8);

    await act(async () => buttonNamed(root, 'Stop')[0].click());
    expect(stop).toHaveBeenCalledWith('T1');

    expect([...root.querySelectorAll('button')].some((b) => /stop all/i.test(b.textContent ?? ''))).toBe(false);
  });

  it('offers Reverse only to a stopped train and locks a lost train', async () => {
    // A lost train's card has Remove and nothing else to press.
    const reverse = buttonNamed(root, 'Reverse');
    expect(reverse.map((b) => b.disabled)).toEqual([true, true]);
    expect(buttonNamed(root, 'Remove')).toHaveLength(1);
    const sliders = [...root.querySelectorAll('input[type=range]')] as HTMLInputElement[];
    expect(sliders.map((s) => s.disabled)).toEqual([false, false, true]);

    stateManager.updateFromNetwork({ ...NETWORK, trains: { T1: train({}) } });
    await act(async () => {
      liveVersion.value++;
    });
    expect(buttonNamed(root, 'Reverse').map((b) => b.disabled)).toEqual([false]);
  });
});
