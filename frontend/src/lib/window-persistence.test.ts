import { describe, it, expect } from 'vitest';
import {
  memoryWindowPersistenceEnv,
  loadRegistry,
  saveRegistry,
  loadWindowState,
  saveWindowState,
  ensureRegistered,
  unregister,
  type WindowState,
} from './window-persistence';

describe('registry round-trip', () => {
  it('defaults to an empty registry when nothing is stored', () => {
    expect(loadRegistry(memoryWindowPersistenceEnv())).toEqual({ windows: [] });
  });

  it('round-trips a saved registry', () => {
    const env = memoryWindowPersistenceEnv();
    saveRegistry(env, {
      windows: [
        { id: 'home', kind: 'home' },
        { id: 'diagnostics', kind: 'diagnostics' },
        { id: 'tabgroup-1', kind: 'tab-group' },
      ],
    });

    expect(loadRegistry(env).windows).toEqual([
      { id: 'home', kind: 'home' },
      { id: 'diagnostics', kind: 'diagnostics' },
      { id: 'tabgroup-1', kind: 'tab-group' },
    ]);
  });

  it('tolerates malformed JSON, defaulting to empty', () => {
    const env = memoryWindowPersistenceEnv({ windowRegistryV1: '{not json' });
    expect(loadRegistry(env)).toEqual({ windows: [] });
  });

  it('drops malformed entries (missing id/kind) but keeps well-formed ones', () => {
    const env = memoryWindowPersistenceEnv({
      windowRegistryV1: JSON.stringify({ windows: [{ id: 'home', kind: 'home' }, { id: 'bad' }, 'garbage', null] }),
    });
    expect(loadRegistry(env).windows).toEqual([{ id: 'home', kind: 'home' }]);
  });
});

describe('window state round-trip', () => {
  const geometry = { left: 200, top: 100, width: 400, height: 300 };

  it('returns null for a window with no stored state', () => {
    expect(loadWindowState(memoryWindowPersistenceEnv(), 'home')).toBeNull();
  });

  it('round-trips a full WindowState for a tab-group window', () => {
    const env = memoryWindowPersistenceEnv();
    const state: WindowState = {
      geometry,
      floating: true,
      tabGroup: { pinned: false, activeTabId: 'block:B1', tabs: [{ tabId: 'block:B1', key: 'block:B1', title: 'B1', editorType: 'block', editorId: 'B1', pinned: true }] },
    };

    saveWindowState(env, 'tabgroup-1', state);

    expect(loadWindowState(env, 'tabgroup-1')).toEqual(state);
  });

  it('round-trips a diagnostics WindowState', () => {
    const env = memoryWindowPersistenceEnv();
    const state: WindowState = {
      geometry,
      floating: true,
      hidden: true,
      diagnostics: { trainId: 'T1', refreshHz: 1, sortMode: 'asc', collapsedGroups: [] },
    };

    saveWindowState(env, 'diagnostics', state);

    expect(loadWindowState(env, 'diagnostics')).toEqual(state);
  });

  it('keeps each window under its own key — saving one does not disturb another', () => {
    const env = memoryWindowPersistenceEnv();
    saveWindowState(env, 'home', { geometry, floating: false });
    saveWindowState(env, 'tabgroup-1', { geometry, floating: true });

    expect(loadWindowState(env, 'home')?.floating).toBe(false);
    expect(loadWindowState(env, 'tabgroup-1')?.floating).toBe(true);
  });

  it('tolerates malformed JSON, returning null', () => {
    const env = memoryWindowPersistenceEnv({ 'window:home': '{not json' });
    expect(loadWindowState(env, 'home')).toBeNull();
  });
});

describe('ensureRegistered', () => {
  it('adds an entry when not already present', () => {
    const env = memoryWindowPersistenceEnv();
    ensureRegistered(env, 'diagnostics', 'diagnostics');
    expect(loadRegistry(env).windows).toEqual([{ id: 'diagnostics', kind: 'diagnostics' }]);
  });

  it('is idempotent — calling it again does not duplicate the entry', () => {
    const env = memoryWindowPersistenceEnv();
    ensureRegistered(env, 'diagnostics', 'diagnostics');
    ensureRegistered(env, 'diagnostics', 'diagnostics');
    expect(loadRegistry(env).windows).toHaveLength(1);
  });

  it('leaves other entries untouched', () => {
    const env = memoryWindowPersistenceEnv();
    saveRegistry(env, { windows: [{ id: 'home', kind: 'home' }] });
    ensureRegistered(env, 'diagnostics', 'diagnostics');
    expect(loadRegistry(env).windows).toEqual([
      { id: 'home', kind: 'home' },
      { id: 'diagnostics', kind: 'diagnostics' },
    ]);
  });
});

describe('unregister', () => {
  it('removes the matching entry', () => {
    const env = memoryWindowPersistenceEnv();
    saveRegistry(env, {
      windows: [
        { id: 'home', kind: 'home' },
        { id: 'tabgroup-1', kind: 'tab-group' },
      ],
    });

    unregister(env, 'tabgroup-1');

    expect(loadRegistry(env).windows).toEqual([{ id: 'home', kind: 'home' }]);
  });

  it('is a no-op when the id is not present', () => {
    const env = memoryWindowPersistenceEnv();
    saveRegistry(env, { windows: [{ id: 'home', kind: 'home' }] });

    unregister(env, 'does-not-exist');

    expect(loadRegistry(env).windows).toEqual([{ id: 'home', kind: 'home' }]);
  });
});
