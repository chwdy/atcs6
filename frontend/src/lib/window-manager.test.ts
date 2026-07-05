import { describe, it, expect, vi } from 'vitest';
import { WindowManager, type FloatingWindow } from './window-manager';

// FloatingWindow builds real DOM (document.createElement, pointer events),
// which these tests deliberately avoid — there's no jsdom in this project's
// vitest setup (see grid-preference.test.ts / theme.test.ts for the same
// no-DOM constraint). WindowManager's z-order/registry bookkeeping only needs
// `id` + `setZIndex` from a window, so a minimal fake stands in, the same way
// editor-ui.test.ts casts a bare fake editor rather than constructing a real
// one.
function fakeWindow(id: string): { id: string; setZIndex: ReturnType<typeof vi.fn> } {
  return { id, setZIndex: vi.fn() };
}

function asWindow(fake: ReturnType<typeof fakeWindow>): FloatingWindow {
  return fake as unknown as FloatingWindow;
}

describe('WindowManager registration + z-order', () => {
  it('assigns increasing z-index in registration order', () => {
    const manager = new WindowManager();
    const a = fakeWindow('a');
    const b = fakeWindow('b');
    manager.register(asWindow(a));
    manager.register(asWindow(b));

    expect(a.setZIndex).toHaveBeenLastCalledWith(900);
    expect(b.setZIndex).toHaveBeenLastCalledWith(901);
  });

  it('all() returns windows back-to-front in z-order (last = topmost)', () => {
    const manager = new WindowManager();
    manager.register(asWindow(fakeWindow('a')));
    manager.register(asWindow(fakeWindow('b')));

    expect(manager.all().map((w) => w.id)).toEqual(['a', 'b']);
  });

  it('focus() moves a window to the top and reassigns the whole band', () => {
    const manager = new WindowManager();
    const a = fakeWindow('a');
    const b = fakeWindow('b');
    const c = fakeWindow('c');
    manager.register(asWindow(a)); // a=900
    manager.register(asWindow(b)); // b=901
    manager.register(asWindow(c)); // c=902

    manager.focus(asWindow(a));

    expect(manager.all().map((w) => w.id)).toEqual(['b', 'c', 'a']);
    expect(a.setZIndex).toHaveBeenLastCalledWith(902);
    expect(b.setZIndex).toHaveBeenLastCalledWith(900);
    expect(c.setZIndex).toHaveBeenLastCalledWith(901);
  });

  it('focus() on the already-topmost window leaves order unchanged', () => {
    const manager = new WindowManager();
    manager.register(asWindow(fakeWindow('a')));
    const b = fakeWindow('b');
    manager.register(asWindow(b));

    manager.focus(asWindow(b));

    expect(manager.all().map((w) => w.id)).toEqual(['a', 'b']);
  });

  it('ignores focus() for a window that was never registered', () => {
    const manager = new WindowManager();
    manager.register(asWindow(fakeWindow('a')));
    const stray = fakeWindow('stray');

    expect(() => manager.focus(asWindow(stray))).not.toThrow();
    expect(manager.all().map((w) => w.id)).toEqual(['a']);
  });

  it('unregister() removes a window from the registry and z-order', () => {
    const manager = new WindowManager();
    manager.register(asWindow(fakeWindow('a')));
    manager.register(asWindow(fakeWindow('b')));

    manager.unregister(asWindow(fakeWindow('a')));

    expect(manager.all().map((w) => w.id)).toEqual(['b']);
    expect(manager.get('a')).toBeUndefined();
  });
});

describe('WindowManager subscribe', () => {
  it('notifies subscribers on register, focus, and unregister', () => {
    const manager = new WindowManager();
    const cb = vi.fn();
    manager.subscribe(cb);

    const a = fakeWindow('a');
    manager.register(asWindow(a));
    expect(cb).toHaveBeenCalledTimes(1);

    manager.register(asWindow(fakeWindow('b')));
    manager.focus(asWindow(a));
    expect(cb).toHaveBeenCalledTimes(3);

    manager.unregister(asWindow(a));
    expect(cb).toHaveBeenCalledTimes(4);
  });

  it('stops notifying after unsubscribe', () => {
    const manager = new WindowManager();
    const cb = vi.fn();
    const unsub = manager.subscribe(cb);
    unsub();

    manager.register(asWindow(fakeWindow('a')));
    expect(cb).not.toHaveBeenCalled();
  });
});
