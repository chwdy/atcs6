import { describe, it, expect } from 'vitest';
import {
  memoryWindowPersistenceEnv,
  loadWindowState,
  saveWindowState,
  type WindowState,
} from './window-persistence';

describe('window state round-trip', () => {
  const geometry = { left: 200, top: 100, width: 400, height: 300 };

  it('returns null for a window with no stored state', () => {
    expect(loadWindowState(memoryWindowPersistenceEnv(), 'operate-panel')).toBeNull();
  });

  it('round-trips a pop-out panel WindowState', () => {
    const env = memoryWindowPersistenceEnv();
    const state: WindowState = { geometry, floating: true };

    saveWindowState(env, 'operate-panel', state);

    expect(loadWindowState(env, 'operate-panel')).toEqual(state);
  });

  it('round-trips a Virtual Bench WindowState with its settings', () => {
    const env = memoryWindowPersistenceEnv();
    const state: WindowState = {
      geometry,
      floating: true,
      hidden: true,
      virtualBench: { refreshHz: 4, filter: 'all', showRaw: false },
    };

    saveWindowState(env, 'virtual-bench', state);

    expect(loadWindowState(env, 'virtual-bench')).toEqual(state);
  });

  it('keeps each window under its own key — saving one does not disturb another', () => {
    const env = memoryWindowPersistenceEnv();
    saveWindowState(env, 'next-editor-panel', { geometry, floating: false });
    saveWindowState(env, 'operate-panel', { geometry, floating: true });

    expect(loadWindowState(env, 'next-editor-panel')?.floating).toBe(false);
    expect(loadWindowState(env, 'operate-panel')?.floating).toBe(true);
  });

  it('tolerates malformed JSON, returning null', () => {
    const env = memoryWindowPersistenceEnv({ 'window:operate-panel': '{not json' });
    expect(loadWindowState(env, 'operate-panel')).toBeNull();
  });
});
