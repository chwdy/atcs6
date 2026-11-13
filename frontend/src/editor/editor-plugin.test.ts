import { describe, expect, it, vi } from 'vitest';

import { Editor } from './editor';

// Exercised on the prototype with a hand-built `this` — the full Editor
// constructor needs a DOM. coreTools() only builds descriptor objects (their
// factory/run thunks close over `this` but are not invoked here), so it is safe
// to call against a bare object in the node test environment.
describe('editor tools', () => {
  it('addTools records the tools and refreshes the tool lookup', () => {
    const ed: any = { _extraTools: [], refreshTools: vi.fn() };
    const tool = { id: 'switch-pick' };

    Editor.prototype.addTools.call(ed, [tool as any]);

    expect(ed._extraTools).toEqual([tool]);
    expect(ed.refreshTools).toHaveBeenCalledTimes(1);
  });
});

describe('tool registry', () => {
  const coreIds = (): string[] => Editor.prototype.coreTools.call({}).map((t: any) => t.id);

  it('core tools are layout tools — no path / train / route tools', () => {
    const ids = coreIds();
    expect(ids).toContain('inspect');
    expect(ids).toContain('track');
    expect(ids).toContain('segment');
    for (const id of ['path', 'add-train', 'new-route', 'route-runs-panel', 'paths-panel']) {
      expect(ids).not.toContain(id);
    }
  });

  it('structural tools are edit-only; inspect and viewers are available in sim', () => {
    const core: any[] = Editor.prototype.coreTools.call({});
    const byId = new Map(core.map((t) => [t.id, t]));
    // Laying/reshaping the layout is frozen in operation mode.
    expect(byId.get('track').availableIn).toEqual(['edit']);
    expect(byId.get('segment').availableIn).toEqual(['edit']);
    // Inspect and the read-only viewers stay available while operating.
    expect(byId.get('inspect').availableIn).toContain('sim');
    expect(byId.get('switches-panel').availableIn).toContain('sim');
  });

  it('_collectToolContributions puts the added tools after the core tools', () => {
    const ctx: any = {
      coreTools: () => [{ id: 'inspect' }],
      _extraTools: [{ id: 'switch-pick' }],
    };
    const ids = Editor.prototype._collectToolContributions.call(ctx).map((t: any) => t.id);
    expect(ids).toEqual(['inspect', 'switch-pick']);
  });
});
