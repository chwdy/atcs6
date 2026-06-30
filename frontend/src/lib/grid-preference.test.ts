import { describe, it, expect } from 'vitest';
import { GridPreference, memoryGridEnv } from './grid-preference';

describe('GridPreference default / restore', () => {
  it('defaults to visible when nothing is stored', () => {
    expect(new GridPreference(memoryGridEnv()).visible()).toBe(true);
  });

  it('defaults to visible for an unrecognized stored value', () => {
    expect(new GridPreference(memoryGridEnv('garbage')).visible()).toBe(true);
  });

  it('restores a hidden grid from storage', () => {
    expect(new GridPreference(memoryGridEnv('hidden')).visible()).toBe(false);
  });

  it('restores a visible grid from storage', () => {
    expect(new GridPreference(memoryGridEnv('visible')).visible()).toBe(true);
  });
});

describe('GridPreference set / toggle / persistence', () => {
  it('set() updates visibility and persists the matching sentinel', () => {
    const env = memoryGridEnv();
    const pref = new GridPreference(env);
    pref.set(false);
    expect(pref.visible()).toBe(false);
    expect(env.getStored()).toBe('hidden');
    pref.set(true);
    expect(pref.visible()).toBe(true);
    expect(env.getStored()).toBe('visible');
  });

  it('toggle() flips the current value', () => {
    const pref = new GridPreference(memoryGridEnv());
    expect(pref.visible()).toBe(true);
    pref.toggle();
    expect(pref.visible()).toBe(false);
    pref.toggle();
    expect(pref.visible()).toBe(true);
  });

  it('persists across instances sharing the same storage', () => {
    const env = memoryGridEnv();
    new GridPreference(env).set(false);
    // A fresh preference reading the same storage comes up hidden.
    expect(new GridPreference(env).visible()).toBe(false);
  });
});

describe('GridPreference subscribers', () => {
  it('notifies subscribers on change with the new value', () => {
    const pref = new GridPreference(memoryGridEnv());
    const seen: boolean[] = [];
    pref.subscribe((v) => seen.push(v));
    pref.set(false);
    pref.toggle(); // -> true
    expect(seen).toEqual([false, true]);
  });

  it('stops notifying after unsubscribe', () => {
    const pref = new GridPreference(memoryGridEnv());
    const seen: boolean[] = [];
    const unsub = pref.subscribe((v) => seen.push(v));
    pref.set(false);
    unsub();
    pref.set(true);
    expect(seen).toEqual([false]);
  });
});
