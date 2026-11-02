import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../lib/toast', () => ({ showToast: vi.fn() }));

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

describe('Quick Route success', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('keeps the window open: shows the routed train, then closes only the Create Path tab', async () => {
    vi.stubGlobal('document', { getElementById: () => null });
    vi.stubGlobal('window', { app: { refreshNetwork: vi.fn(async () => {}) } });
    const calls: string[] = [];
    const endTrack = { x: 5, y: 0, block_section_id: 'BLK_END' };
    const editor: any = {
      _currentEditorType: 'path-setup',
      api: { createRoute: vi.fn(async () => ({})), assignRoute: vi.fn(async () => ({})) },
      renderer: { requestRedraw: vi.fn() },
      cachedNetwork: { tracks: [endTrack] },
      highlighter: { clear: vi.fn() },
      pathToolState: {
        active: true,
        trainId: 'T1',
        reachableEndpoints: ['BLK_END'],
        reachablePositions: new Set(),
      },
      showTrainControlPanel: vi.fn(async (id: string) => {
        calls.push(`show ${id}`);
        editor._currentEditorType = 'train-control';
      }),
      closeEditorTab: vi.fn(async (type: string, id: string | null) => {
        calls.push(`close ${type} ${id}`);
      }),
      hideSecondarySidebar: vi.fn(async () => {
        calls.push('hide');
      }),
    };
    const handler = new PathEditorHandler(editor);
    handler.watchQuickRoutePathResult = vi.fn(async () => {});

    await handler.handleRouteSecondClick({ x: 5, y: 0 }, endTrack);

    // The train panel opens first, in the window the Create Path tab sits in;
    // closing that tab afterwards leaves the window up. The whole window is
    // never hidden.
    expect(calls).toEqual(['show T1', 'close path-setup null']);
    expect(editor.api.assignRoute).toHaveBeenCalledWith('T1', expect.stringMatching(/^ROUTE_QR_/));
    expect(handler.pathToolState.active).toBe(false);
  });
});

describe('Quick Route cancel', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('closes only the Create Path tab and never hides the whole window', async () => {
    vi.stubGlobal('document', { getElementById: () => null });
    const calls: string[] = [];
    const editor: any = {
      _currentEditorType: 'path-setup',
      renderer: { requestRedraw: vi.fn(), clearRoutePreview: vi.fn() },
      cachedNetwork: { tracks: [] },
      highlighter: { clear: vi.fn() },
      pathToolState: { active: true, trainId: 'T1', reachableEndpoints: [], reachablePositions: new Set() },
      closeEditorTab: vi.fn(async (type: string, id: string | null) => {
        calls.push(`close ${type} ${id}`);
      }),
      hideSecondarySidebar: vi.fn(async () => {
        calls.push('hide');
      }),
    };
    const handler = new PathEditorHandler(editor);

    // Esc (leaving the tool) and right-click (cancelling a pick) both end here.
    await handler.exitRouteToolMode();

    expect(calls).toEqual(['close path-setup null']);
    expect(handler.pathToolState.active).toBe(false);
  });
});

describe('Quick Route from a chosen train', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('lights up the reachable blocks at once, and one click sends the train and ends the mode', async () => {
    vi.stubGlobal('document', { getElementById: () => null });
    vi.stubGlobal('window', { app: { refreshNetwork: vi.fn(async () => {}) } });
    const endTrack = { x: 5, y: 0, block_section_id: 'BLK_END' };
    const editor: any = {
      api: {
        getReachableEndpointsForTrain: vi.fn(async () => ({ reachable_endpoints: ['BLK_END'] })),
        createRoute: vi.fn(async () => ({})),
        assignRoute: vi.fn(async () => ({})),
      },
      renderer: { requestRedraw: vi.fn(), clearRoutePreview: vi.fn() },
      cachedNetwork: {
        tracks: [endTrack],
        controllers_by_train_id: { T1: { span: { occupied_blocks: ['BLK_START'] } } },
      },
      highlighter: { show: vi.fn(), clear: vi.fn() },
      pathToolState: {},
      showSecondarySidebar: vi.fn(),
      showTrainControlPanel: vi.fn(async () => {}),
      closeEditorTab: vi.fn(async () => {}),
      _setSelectedTool: vi.fn(),
    };
    editor.exitMode = vi.fn(async () => {
      const m = editor.activeMode;
      editor.activeMode = null;
      await m?.onExit();
    });
    const handler = new PathEditorHandler(editor);
    handler.watchQuickRoutePathResult = vi.fn(async () => {});

    const mode = handler.buildQuickRouteMode('T1');
    editor.activeMode = mode;
    await mode.onEnter();

    expect(editor.api.getReachableEndpointsForTrain).toHaveBeenCalledWith('T1');
    expect(handler.pathToolState.active).toBe(true);
    expect(handler.pathToolState.startBlockId).toBe('BLK_START');
    expect(editor.highlighter.show).toHaveBeenCalledWith(
      { keys: new Set(['5,0']) },
      { channel: 'reachable' }
    );

    await mode.onClick({ gridPos: { x: 5, y: 0 } });

    expect(editor.api.assignRoute).toHaveBeenCalledWith('T1', expect.stringMatching(/^ROUTE_QR_/));
    expect(editor.exitMode).toHaveBeenCalled();
    expect(editor._setSelectedTool).toHaveBeenCalledWith('inspect');
  });

  it('stays in the mode when the click is not on a reachable block', async () => {
    vi.stubGlobal('document', { getElementById: () => null });
    const editor: any = {
      api: {
        getReachableEndpointsForTrain: vi.fn(async () => ({ reachable_endpoints: ['BLK_END'] })),
        createRoute: vi.fn(async () => ({})),
      },
      renderer: { requestRedraw: vi.fn() },
      cachedNetwork: { tracks: [{ x: 1, y: 1, block_section_id: 'BLK_OTHER' }] },
      highlighter: { show: vi.fn(), clear: vi.fn() },
      pathToolState: {},
      showSecondarySidebar: vi.fn(),
      exitMode: vi.fn(async () => {}),
    };
    const handler = new PathEditorHandler(editor);
    const mode = handler.buildQuickRouteMode('T1');
    editor.activeMode = mode;
    await mode.onEnter();

    await mode.onClick({ gridPos: { x: 1, y: 1 } });

    expect(editor.api.createRoute).not.toHaveBeenCalled();
    expect(editor.exitMode).not.toHaveBeenCalled();
    expect(handler.pathToolState.active).toBe(true);
  });
});
