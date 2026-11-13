import { describe, expect, it, vi } from 'vitest';

import { Editor } from './editor';
import { SimEditorPlugin } from './sim-plugin';

// The plugin hook is exercised on the prototype with a hand-built `this` — the
// full Editor constructor needs a DOM, but registerPlugin itself is pure glue.
// Likewise coreTools()/tools() only build descriptor objects (their factory/run
// thunks close over `this`/`editor` but are not invoked here), so they are safe
// to call against a bare object in the node test environment.
describe('editor plugin hook', () => {
  it('registerPlugin records the plugin, calls register(editor), refreshes the tools', () => {
    const ed: any = { _plugins: [], refreshTools: vi.fn() };
    const plugin = { register: vi.fn() };

    Editor.prototype.registerPlugin.call(ed, plugin);

    expect(ed._plugins).toEqual([plugin]);
    expect(plugin.register).toHaveBeenCalledTimes(1);
    expect(plugin.register).toHaveBeenCalledWith(ed);
    // A plugin may contribute tools, so the lookup is rebuilt after register.
    expect(ed.refreshTools).toHaveBeenCalledTimes(1);
  });

  it('a layout-only editor (no plugin) has no sim handlers', () => {
    // With no SimEditorPlugin registered, the sim handler fields are undefined —
    // the editor is a layout-only editor.
    const ed: any = { _plugins: [] };
    expect(ed.trainHandler).toBeUndefined();
    expect(ed.pathHandler).toBeUndefined();
    expect(ed.routeHandler).toBeUndefined();
    expect(ed.routeRunHandler).toBeUndefined();
    expect(ed.speedProfileHandler).toBeUndefined();
  });
});

describe('tool registry', () => {
  const coreIds = (): string[] => Editor.prototype.coreTools.call({}).map((t: any) => t.id);

  it('core tools are layout-only — no sim (path / train / route) tools', () => {
    const ids = coreIds();
    expect(ids).toContain('inspect');
    expect(ids).toContain('track');
    expect(ids).toContain('segment');
    for (const simId of ['path', 'add-train', 'new-route', 'route-runs-panel', 'paths-panel']) {
      expect(ids).not.toContain(simId);
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

  it('the sim plugin contributes path / train / route tools, all available in sim', () => {
    const simTools = new SimEditorPlugin().tools({} as any);
    const ids = simTools.map((t) => t.id);
    expect(ids).toEqual([
      'path', 'extend-path', 'cancel-path', 'paths-panel',
      'add-train', 'train-panel', 'speed-profiles-panel',
      'new-route', 'routes-panel', 'route-runs-panel',
    ]);
    // Full dispatcher freedom: every sim tool is usable in operation mode.
    for (const t of simTools) expect(t.availableIn).toContain('sim');
  });

  it('_collectToolContributions merges core tools with every plugin that has tools()', () => {
    const ctx: any = {
      coreTools: () => [{ id: 'inspect' }],
      _plugins: [
        { tools: () => [{ id: 'path' }] }, // a tool-contributing plugin
        {}, // a behavior-only plugin (no tools()) must not break the merge
      ],
    };
    const ids = Editor.prototype._collectToolContributions.call(ctx).map((t: any) => t.id);
    expect(ids).toEqual(['inspect', 'path']);
  });
});
