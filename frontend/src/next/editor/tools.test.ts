import { describe, expect, it } from 'vitest';

import { Editor } from '../../editor/editor';
import { editorScreenTools } from './modes';
import { BAR_TOOLS, barToolFor, barToolForKey } from './tools';

// coreTools() only builds descriptor objects, so it runs against a bare `this`
// in the node environment (see editor/editor-plugin.test.ts).
const coreTools = (): any[] => Editor.prototype.coreTools.call({});

describe('Editor screen tool bar', () => {
  it('lists the things you place, in a fixed order with keys 1–6', () => {
    expect(BAR_TOOLS.map((t) => t.label)).toEqual([
      'Select', 'Track', 'Switch', 'Tunnel', 'Segment', 'Block',
    ]);
    expect(BAR_TOOLS.map((t) => t.key)).toEqual(['1', '2', '3', '4', '5', '6']);
  });

  it('shows the variants the plan names for each tool', () => {
    const labels = (id: string) => BAR_TOOLS.find((t) => t.id === id)!.variants.map((v) => v.label);
    expect(labels('select')).toEqual(['Click', 'Box', 'Lasso', 'Brush', 'Trace', 'Entity']);
    expect(labels('track')).toEqual(['Draw', 'Remove']);
    expect(labels('segment')).toEqual(['New', 'Extend', 'Remove']);
    expect(labels('block')).toEqual(['Assign']);
  });

  it('reaches every edit-mode build and select tool of the core editor, and only those', () => {
    const placeable = coreTools()
      .filter((t) => t.kind.type !== 'action' && t.availableIn.includes('edit'))
      .map((t) => t.id);
    const added = editorScreenTools({} as any).map((t) => t.id);
    const onBar = BAR_TOOLS.flatMap((t) => t.variants.map((v) => v.toolId));
    expect(new Set(onBar)).toEqual(new Set([...placeable, ...added]));
    // No tool sits under two bar tools.
    expect(onBar.length).toBe(new Set(onBar).size);
  });

  it('finds the bar tool an editor tool belongs to', () => {
    expect(barToolFor('inspect')?.id).toBe('select');
    expect(barToolFor('lasso-select')?.id).toBe('select');
    expect(barToolFor('remove-track')?.id).toBe('track');
    expect(barToolFor('extend-segment')?.id).toBe('segment');
    expect(barToolFor('block-assign')?.id).toBe('block');
    // Path, train and route tools belong to Operate.
    expect(barToolFor('path')).toBeNull();
    expect(barToolFor('place-train')).toBeNull();
  });

  it('maps number keys to bar tools', () => {
    expect(barToolForKey('1')?.id).toBe('select');
    expect(barToolForKey('6')?.id).toBe('block');
    expect(barToolForKey('7')).toBeNull();
  });
});
