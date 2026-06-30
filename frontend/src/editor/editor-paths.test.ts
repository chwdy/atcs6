import { describe, expect, it } from 'vitest';

import { PathEditorHandler, orderPathsForList } from './editor-paths';

// Unit specs for the paths-list view controls (sort + filter) and the path
// status → color/accent mapping. `orderPathsForList` is a pure function, and
// the status helpers read no `this.editor` state, so a handler built with a
// null host exercises them without a DOM.
function path(path_id: string, status: string): any {
  return { path_id, status };
}

describe('orderPathsForList', () => {
  const paths = [
    path('P3', 'cancelled'),
    path('P1', 'completed'),
    path('P2', 'active'),
    path('P4', 'active'),
  ];

  it('active-first ranks active above terminal paths, ties by path_id', () => {
    const out = orderPathsForList(paths, 'active-first', 'all').map((p) => p.path_id);
    // active (P2, P4) → completed (P1) → cancelled (P3)
    expect(out).toEqual(['P2', 'P4', 'P1', 'P3']);
  });

  it('path-id mode sorts purely by id regardless of status', () => {
    const out = orderPathsForList(paths, 'path-id', 'all').map((p) => p.path_id);
    expect(out).toEqual(['P1', 'P2', 'P3', 'P4']);
  });

  it('"active" filter keeps only live paths', () => {
    const out = orderPathsForList(paths, 'active-first', 'active').map((p) => p.status);
    expect(out).toEqual(['active', 'active']);
  });

  it('"hide-cancelled" filter drops cancelled but keeps completed', () => {
    const out = orderPathsForList(paths, 'active-first', 'hide-cancelled').map((p) => p.path_id);
    expect(out).toEqual(['P2', 'P4', 'P1']);
    expect(out).not.toContain('P3');
  });

  it('does not mutate the input array', () => {
    const input = [path('B', 'cancelled'), path('A', 'active')];
    orderPathsForList(input, 'active-first', 'all');
    expect(input.map((p) => p.path_id)).toEqual(['B', 'A']);
  });

  it('tolerates null/empty input', () => {
    expect(orderPathsForList(null as any, 'active-first', 'all')).toEqual([]);
    expect(orderPathsForList([], 'path-id', 'hide-cancelled')).toEqual([]);
  });
});

describe('path status color semantics', () => {
  const h = new PathEditorHandler(null as any);

  it('completed reads as blue (info), not muted', () => {
    expect(h.getPathAccent('completed')).toBe('info');
    expect(h.getPathStatusColor('completed')).toBe('var(--info-color)');
  });

  it('cancelled reads as grey (muted), never the danger/critical color', () => {
    expect(h.getPathAccent('cancelled')).toBe('muted');
    expect(h.getPathStatusColor('cancelled')).toBe('var(--text-tertiary)');
    expect(h.getPathStatusColor('cancelled')).not.toBe('var(--danger-color)');
  });

  it('active stays green (success)', () => {
    expect(h.getPathAccent('active')).toBe('success');
    expect(h.getPathStatusColor('active')).toBe('var(--success-color)');
  });
});
