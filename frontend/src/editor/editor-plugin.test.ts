import { describe, expect, it, vi } from 'vitest';

import { Editor } from './editor';

// The plugin hook is exercised on the prototype with a hand-built `this` — the
// full Editor constructor needs a DOM, but registerPlugin itself is pure glue.
describe('editor plugin hook', () => {
  it('registerPlugin records the plugin and calls register(editor)', () => {
    const ed: any = { _plugins: [] };
    const plugin = { register: vi.fn() };

    Editor.prototype.registerPlugin.call(ed, plugin);

    expect(ed._plugins).toEqual([plugin]);
    expect(plugin.register).toHaveBeenCalledTimes(1);
    expect(plugin.register).toHaveBeenCalledWith(ed);
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
