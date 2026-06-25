import { describe, it, expect } from 'vitest';
import { ThemeChannel, ThemeManager, memoryEnv, type ThemeEnv, type ChannelOptions } from './theme';

const DARK = { name: 'dark', label: 'Dark', icon: 'ti ti-moon' };
const LIGHT = { name: 'light', label: 'Light', icon: 'ti ti-sun' };
const BLUEPRINT = { name: 'blueprint', label: 'Blueprint', icon: 'ti ti-ruler-2' };

/** A chrome-like channel (dark + light, follows OS). */
function chromeChannel(env: ThemeEnv = memoryEnv(), opts: ChannelOptions = { followOsPref: true, defaultTheme: 'dark' }): ThemeChannel {
  return new ThemeChannel(env, opts).register(DARK).register(LIGHT);
}

/** A canvas-like channel (dark + light + blueprint, ignores OS). */
function canvasChannel(env: ThemeEnv = memoryEnv()): ThemeChannel {
  return new ThemeChannel(env, { followOsPref: false, defaultTheme: 'dark' })
    .register(DARK)
    .register(LIGHT)
    .register(BLUEPRINT);
}

describe('ThemeChannel registry', () => {
  it('lists registered themes in registration order', () => {
    expect(canvasChannel().names()).toEqual(['dark', 'light', 'blueprint']);
    expect(chromeChannel().themes().map((t) => t.label)).toEqual(['Dark', 'Light']);
  });

  it('re-registering a name updates the definition without reordering', () => {
    const ch = chromeChannel().register({ ...DARK, label: 'Midnight' });
    expect(ch.names()).toEqual(['dark', 'light']);
    expect(ch.get('dark')?.label).toBe('Midnight');
  });

  it('reports membership via has()', () => {
    const ch = canvasChannel();
    expect(ch.has('blueprint')).toBe(true);
    expect(ch.has('sepia')).toBe(false);
  });
});

describe('ThemeChannel current / preferred', () => {
  it('current() reflects a registered attribute', () => {
    expect(canvasChannel(memoryEnv({ attribute: 'blueprint' })).current()).toBe('blueprint');
  });

  it('current() falls back to preferred() when the attribute is unregistered', () => {
    expect(chromeChannel(memoryEnv({ attribute: 'bogus' })).current()).toBe('dark');
  });

  it('preferred() honors a saved registered theme', () => {
    expect(canvasChannel(memoryEnv({ stored: 'blueprint' })).preferred()).toBe('blueprint');
  });

  it('chrome preferred() follows OS when nothing saved', () => {
    expect(chromeChannel(memoryEnv({ prefersLight: true })).preferred()).toBe('light');
    expect(chromeChannel(memoryEnv({ prefersLight: false })).preferred()).toBe('dark');
  });

  it('canvas preferred() ignores OS and uses the channel default', () => {
    // followOsPref is false, so an OS light preference must NOT flip the canvas.
    expect(canvasChannel(memoryEnv({ prefersLight: true })).preferred()).toBe('dark');
  });

  it('preferred() ignores a saved unregistered theme', () => {
    expect(canvasChannel(memoryEnv({ stored: 'bogus' })).preferred()).toBe('dark');
  });
});

describe('ThemeChannel set / toggle', () => {
  it('set() updates the attribute and persists', () => {
    const env = memoryEnv();
    canvasChannel(env).set('blueprint');
    expect(env.getAttribute()).toBe('blueprint');
    expect(env.getStored()).toBe('blueprint');
  });

  it('set() ignores an unregistered theme', () => {
    const env = memoryEnv({ attribute: 'dark' });
    chromeChannel(env).set('sepia');
    expect(env.getAttribute()).toBe('dark');
    expect(env.getStored()).toBeNull();
  });

  it('toggle() cycles through all registered themes and wraps', () => {
    const env = memoryEnv({ attribute: 'dark' });
    const ch = canvasChannel(env);
    ch.toggle();
    expect(env.getAttribute()).toBe('light');
    ch.toggle();
    expect(env.getAttribute()).toBe('blueprint');
    ch.toggle();
    expect(env.getAttribute()).toBe('dark'); // wraps
  });

  it('next() returns the theme toggle() would switch to', () => {
    expect(chromeChannel(memoryEnv({ attribute: 'dark' })).next().name).toBe('light');
  });
});

describe('ThemeChannel subscribe / reconcile', () => {
  it('notifies subscribers on set() and stops after unsubscribe', () => {
    const ch = canvasChannel();
    const seen: string[] = [];
    const off = ch.subscribe((name) => seen.push(name));
    ch.set('light');
    ch.set('blueprint');
    off();
    ch.set('dark');
    expect(seen).toEqual(['light', 'blueprint']);
  });

  it('reconcile() corrects an unregistered attribute', () => {
    const env = memoryEnv({ attribute: 'bogus' });
    canvasChannel(env).reconcile();
    expect(env.getAttribute()).toBe('dark');
  });

  it('reconcile() leaves a registered attribute untouched', () => {
    const env = memoryEnv({ attribute: 'blueprint' });
    canvasChannel(env).reconcile();
    expect(env.getAttribute()).toBe('blueprint');
  });
});

describe('ThemeManager — two independent channels', () => {
  it('switching the canvas axis does not affect the chrome axis', () => {
    const chromeEnv = memoryEnv({ attribute: 'dark' });
    const canvasEnv = memoryEnv({ attribute: 'dark' });
    const mgr = new ThemeManager(chromeChannel(chromeEnv), canvasChannel(canvasEnv));

    mgr.canvas.set('blueprint');
    expect(canvasEnv.getAttribute()).toBe('blueprint');
    expect(chromeEnv.getAttribute()).toBe('dark'); // chrome untouched

    mgr.chrome.set('light');
    expect(chromeEnv.getAttribute()).toBe('light');
    expect(canvasEnv.getAttribute()).toBe('blueprint'); // canvas untouched
  });

  it('exposes both channels with their own registries', () => {
    const mgr = new ThemeManager(chromeChannel(), canvasChannel());
    expect(mgr.chrome.names()).toEqual(['dark', 'light']);
    expect(mgr.canvas.names()).toEqual(['dark', 'light', 'blueprint']);
  });
});
