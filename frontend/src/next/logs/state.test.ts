import { describe, expect, it } from 'vitest';

import { logsTab, pickedBlockId, readPrefs, showBlockInLogs, writePrefs } from './state';

function memoryStorage() {
  const items = new Map<string, string>();
  return {
    getItem: (key: string) => items.get(key) ?? null,
    setItem: (key: string, value: string) => void items.set(key, value),
    items,
  };
}

describe('Logs screen prefs', () => {
  it('reads back what was written, merging each tab field by field', () => {
    const storage = memoryStorage();
    writePrefs({ tab: 'detectors' }, storage);
    writePrefs({ events: { level: 'error' } }, storage);
    writePrefs({ events: { refreshHz: 4 } }, storage);
    expect(readPrefs(storage)).toEqual({
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
    expect(() => writePrefs({ tab: 'events' }, throwing)).not.toThrow();
  });
});

describe('picking from the canvas', () => {
  it('a block opens Detectors on it', () => {
    showBlockInLogs('BLK_C');
    expect(logsTab.value).toBe('detectors');
    expect(pickedBlockId.value).toBe('BLK_C');
  });
});
