import { describe, expect, it } from 'vitest';

import {
  containerPaths,
  containerSummary,
  nextSortMode,
  shortValue,
  sortItems,
  valueChanged,
} from './diagnostics-model';

describe('diagnostics model', () => {
  it('names every object and array inside a value, outermost first', () => {
    expect(containerPaths({ a: [1, { b: 2 }], c: 3 }, 'live::span')).toEqual([
      'live::span::root',
      'live::span::root.a',
      'live::span::root.a[1]',
    ]);
    expect(containerPaths(5, 'live::speed')).toEqual([]);
  });

  it('sums up a folded value in one line', () => {
    expect(containerSummary([1, 2, 3])).toBe('[3]');
    expect(containerSummary({})).toBe('{}');
    expect(containerSummary({ a: 1, b: 2, c: 3, d: 4 })).toBe('{4} a, b, c, …');
  });

  it('cuts a long plain value to 80 characters', () => {
    expect(shortValue('x'.repeat(100))).toHaveLength(79);
    expect(shortValue(12)).toBe('12');
  });

  it('sorts A→Z, then Z→A, then back to the server order', () => {
    const items = [{ name: 'b', value: 1 }, { name: 'A', value: 2 }, { name: 'c', value: 3 }];
    expect(sortItems(items, 'asc').map((i) => i.name)).toEqual(['A', 'b', 'c']);
    expect(sortItems(items, 'desc').map((i) => i.name)).toEqual(['c', 'b', 'A']);
    expect(sortItems(items, null)).toBe(items);
    expect([nextSortMode(null), nextSortMode('asc'), nextSortMode('desc')]).toEqual(['asc', 'desc', null]);
  });

  it('counts a row as changed only when its value differs from the last read', () => {
    expect(valueChanged({ name: 'v', value: [1, 2] }, { name: 'v', value: [1, 2] })).toBe(false);
    expect(valueChanged({ name: 'v', value: 3 }, { name: 'v', value: 2 })).toBe(true);
    expect(valueChanged({ name: 'v', value: 3 }, undefined)).toBe(false);
    expect(valueChanged({ name: 'v', value: null, error: 'boom' }, { name: 'v', value: 2 })).toBe(false);
  });
});
