import { beforeEach, describe, expect, it, vi } from 'vitest';

import { showContextMenu } from '../lib/context-menu';
import { showToast } from '../lib/toast';
import { Editor } from './editor';

// Stub the two side-effecting UI helpers so the click logic can run in the node
// test env without a DOM. importOriginal keeps every other export intact.
vi.mock('../lib/context-menu', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/context-menu')>()),
  showContextMenu: vi.fn(),
}));
vi.mock('../lib/toast', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/toast')>()),
  showToast: vi.fn(),
}));

// handleTunnelClick is glue: it reads dead_end_ports off the clicked track and
// either commits the single end directly or pops a menu to disambiguate two
// ends. Tested against a hand-built `this` (the full constructor needs a DOM),
// the same way editor-plugin.test.ts exercises prototype methods.
function makeEditor(overrides: any = {}): any {
  return {
    _pendingTunnelEndpoint: null,
    _commitTunnelEndpoint: vi.fn(),
    renderer: {
      canvas: { getBoundingClientRect: () => ({ left: 0, top: 0 }) },
      gridToScreenX: () => 0,
      gridToScreenY: () => 0,
    },
    ...overrides,
  };
}

const click = (ed: any, track: any, ev?: any) =>
  Editor.prototype.handleTunnelClick.call(ed, { x: 3, y: 4 }, track, ev);

describe('tunnel tool — choosing which end', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('a tile with no dead-end edge is rejected and commits nothing', async () => {
    const ed = makeEditor();
    await click(ed, { dead_end_ports: [] });
    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining('no dead-end'),
      'error',
      expect.anything(),
    );
    expect(ed._commitTunnelEndpoint).not.toHaveBeenCalled();
    expect(showContextMenu).not.toHaveBeenCalled();
  });

  it('a single dead-end is committed directly (uppercased), with no menu', async () => {
    const ed = makeEditor();
    await click(ed, { dead_end_ports: ['e'] });
    expect(ed._commitTunnelEndpoint).toHaveBeenCalledTimes(1);
    expect(ed._commitTunnelEndpoint).toHaveBeenCalledWith({ x: 3, y: 4, edge: 'E' });
    expect(showContextMenu).not.toHaveBeenCalled();
  });

  it('two dead-ends pop a menu (top end first) and commit nothing until a pick', async () => {
    const ed = makeEditor();
    await click(ed, { dead_end_ports: ['E', 'W'] }, { clientX: 100, clientY: 200 });

    expect(ed._commitTunnelEndpoint).not.toHaveBeenCalled();
    expect(showContextMenu).toHaveBeenCalledTimes(1);
    const [x, y, sections] = (showContextMenu as any).mock.calls[0];
    expect([x, y]).toEqual([100, 200]); // anchored at the cursor
    const items = sections[0].items;
    // W is the top-left end, E the bottom-right — the top end reads first.
    expect(items.map((i: any) => i.label)).toEqual(['Top-left end', 'Bottom-right end']);
    expect(items.map((i: any) => i.detail)).toEqual(['W', 'E']);

    // Choosing an item commits exactly that edge.
    items[1].onSelect();
    expect(ed._commitTunnelEndpoint).toHaveBeenCalledWith({ x: 3, y: 4, edge: 'E' });
  });
});

describe('tunnel tool — pairing two endpoints', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('arms the first endpoint, then pairs the second into a tunnel', async () => {
    const setPending = vi.fn();
    const createTunnel = vi.fn().mockResolvedValue({});
    const refreshAndSelect = vi.fn().mockResolvedValue(undefined);
    const ed = makeEditor({
      renderer: { setPendingTunnelEndpoint: setPending },
      api: { createTunnel },
      refreshAndSelect,
    });

    const a = { x: 1, y: 1, edge: 'W' };
    const b = { x: 5, y: 5, edge: 'E' };

    await Editor.prototype._commitTunnelEndpoint.call(ed, a);
    expect(ed._pendingTunnelEndpoint).toEqual(a);
    expect(setPending).toHaveBeenLastCalledWith(a);
    expect(createTunnel).not.toHaveBeenCalled();

    await Editor.prototype._commitTunnelEndpoint.call(ed, b);
    expect(createTunnel).toHaveBeenCalledWith(a, b);
    expect(ed._pendingTunnelEndpoint).toBeNull();
    expect(setPending).toHaveBeenLastCalledWith(null);
    expect(refreshAndSelect).toHaveBeenCalledTimes(1);
  });
});
