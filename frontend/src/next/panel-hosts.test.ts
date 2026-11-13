import { describe, expect, it, vi } from 'vitest';

import type { NextPanelHost } from '../editor/editor';
import { combinePanelHosts } from './panel-hosts';

function fakeHost(takes: boolean): NextPanelHost {
  return {
    selectTile: vi.fn(() => takes),
    showPanel: vi.fn(() => takes),
    hidePanel: vi.fn(() => takes),
    closePanel: vi.fn(),
  };
}

describe('combinePanelHosts', () => {
  it('gives a request to the first screen that takes it', () => {
    const logs = fakeHost(false);
    const operate = fakeHost(true);
    const both = combinePanelHosts(null, logs, operate);
    expect(both.selectTile({ x: 1, y: 2 })).toBe(true);
    expect(logs.selectTile).toHaveBeenCalledTimes(1);
    expect(operate.selectTile).toHaveBeenCalledTimes(1);

    const first = fakeHost(true);
    const second = fakeHost(true);
    combinePanelHosts(first, second).selectTile({ x: 0, y: 0 });
    expect(second.selectTile).not.toHaveBeenCalled();
  });

  it('leaves a request no screen takes to the panel host, and tells every screen to close', () => {
    const a = fakeHost(false);
    const b = fakeHost(false);
    const both = combinePanelHosts(a, b);
    expect(both.showPanel('Blocks', '<div></div>')).toBe(false);
    expect(both.hidePanel()).toBe(false);
    both.closePanel('switch', 'SW1');
    expect(a.closePanel).toHaveBeenCalledWith('switch', 'SW1');
    expect(b.closePanel).toHaveBeenCalledWith('switch', 'SW1');
  });
});
