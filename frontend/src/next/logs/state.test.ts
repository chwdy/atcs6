import { describe, expect, it } from 'vitest';

import { DEFAULT_HEIGHT, MIN_HEIGHT, clampHeight, readPrefs, writePrefs } from './state';

function memoryStorage() {
  const items = new Map<string, string>();
  return {
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => void items.set(key, value),
    items,
  };
}

describe('Logs panel prefs', () => {
  it('reads back what was written, merging each tab field by field', () => {
    const storage = memoryStorage();
    writePrefs({ height: 320, tab: 'detectors' }, storage);
    writePrefs({ events: { level: 'error' } }, storage);
    writePrefs({ events: { refreshHz: 4 } }, storage);
    expect(readPrefs(storage)).toEqual({
      height: 320,
      tab: 'detectors',
      events: { level: 'error', refreshHz: 4 },
    });
  });

  it('treats missing or malformed storage as nothing saved', () => {
    expect(readPrefs(null)).toEqual({});
    const storage = memoryStorage();
    storage.items.set('atcs6.nextLogsPanel.v1', '{not json');
    expect(readPrefs(storage)).toEqual({});
    storage.items.set('atcs6.nextLogsPanel.v1', '[1, 2]');
    expect(readPrefs(storage)).toEqual({});
  });

  it('keeps working when storage throws on every call', () => {
    const throwing = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(readPrefs(throwing)).toEqual({});
    expect(() => writePrefs({ open: true }, throwing)).not.toThrow();
  });
});

describe('clampHeight', () => {
  it('keeps the height between the minimum and 70% of the window', () => {
    expect(clampHeight(50, 1000)).toBe(MIN_HEIGHT);
    expect(clampHeight(300, 1000)).toBe(300);
    expect(clampHeight(900, 1000)).toBe(700);
  });

  it('falls back to the default for a height that is not a number', () => {
    expect(clampHeight(Number.NaN, 1000)).toBe(DEFAULT_HEIGHT);
  });
});
