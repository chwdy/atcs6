import { describe, expect, it, vi } from 'vitest';

import { StateManager } from './state-manager';

describe('StateManager.updateEntity', () => {
  it('keeps the newer object when it equals the stored one, without notifying', () => {
    // design_new.md "Layout version": the network poll hands back the object it
    // cached from the last full reply while an entity is unchanged, so the
    // manager must hold that same object for later polls to skip it on identity.
    const manager = new StateManager();
    const first = { block_id: 'B1', state: 'free' };
    const equal = { block_id: 'B1', state: 'free' };
    expect(manager.updateEntity('blocks', 'B1', first)).toBe(true);
    const listener = vi.fn();
    manager.subscribe('blocks', 'B1', listener, { immediate: false });

    expect(manager.updateEntity('blocks', 'B1', equal)).toBe(false);

    expect(manager.get('blocks', 'B1')).toBe(equal);
    expect(listener).not.toHaveBeenCalled();
  });

  it('skips a repeat of the same object without a deep compare', () => {
    const manager = new StateManager();
    const block = { block_id: 'B1', state: 'free' };
    manager.updateEntity('blocks', 'B1', block);
    const isEqual = vi.spyOn(JSON, 'stringify');

    expect(manager.updateEntity('blocks', 'B1', block)).toBe(false);

    expect(isEqual).not.toHaveBeenCalled();
    isEqual.mockRestore();
  });
});
