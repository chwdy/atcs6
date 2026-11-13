import { describe, expect, it, vi } from 'vitest';

import { CanvasLayers, LAYERS, layerDef, memoryLayersEnv } from './canvas-layers';

describe('CanvasLayers', () => {
  it('starts with every layer off', () => {
    const layers = new CanvasLayers(memoryLayersEnv());
    expect(LAYERS.filter((l) => layers.isOn(l.id)).map((l) => l.id)).toEqual([]);
    // The renderer used to treat a missing tile-direction checkbox as on; the
    // checkbox itself started unchecked, so the arrows were off. They stay off.
    expect(layers.isOn('tile-directions')).toBe(false);
  });

  it('remembers what the viewer turned on and off', () => {
    const env = memoryLayersEnv();
    const first = new CanvasLayers(env);
    first.set('coords', true);
    first.set('bits', true);
    first.set('bits', false);
    const reloaded = new CanvasLayers(env);
    expect(reloaded.isOn('coords')).toBe(true);
    expect(reloaded.isOn('bits')).toBe(false);
  });

  it('falls back to the defaults when the stored value is unreadable, and drops unknown layers', () => {
    expect(new CanvasLayers(memoryLayersEnv('not json')).isOn('coords')).toBe(false);
    expect(new CanvasLayers(memoryLayersEnv('{"coords":true}')).isOn('coords')).toBe(false);
    const layers = new CanvasLayers(memoryLayersEnv('["coords","gone-layer"]'));
    expect(layers.isOn('coords')).toBe(true);
    expect(layers.isOn('bits')).toBe(false);
  });

  it('tells subscribers about each change, and only real changes', () => {
    const layers = new CanvasLayers(memoryLayersEnv());
    const cb = vi.fn();
    const unsubscribe = layers.subscribe(cb);
    layers.toggle('bits');
    layers.set('bits', true); // already on: no call
    expect(cb.mock.calls).toEqual([['bits', true]]);
    unsubscribe();
    layers.toggle('bits');
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('defines every layer once, with the canvas pass it draws on', () => {
    const ids = LAYERS.map((l) => l.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(layerDef('tile-directions').redraw).toBe('live');
    expect(layerDef('coords').redraw).toBe('world');
  });
});
