import { describe, it, expect, vi } from 'vitest';
import { currentDpr, isCoarsePointer, coarsePointerScale, watchDprChange, memoryDpiEnv } from './dpi';

describe('currentDpr', () => {
  it('reads the ratio from the env, defaulting to 1', () => {
    expect(currentDpr(memoryDpiEnv())).toBe(1);
    expect(currentDpr(memoryDpiEnv({ dpr: 2 }))).toBe(2);
  });
});

describe('isCoarsePointer', () => {
  it('is false by default', () => {
    expect(isCoarsePointer(memoryDpiEnv())).toBe(false);
  });

  it('reflects the env coarse-pointer flag', () => {
    expect(isCoarsePointer(memoryDpiEnv({ coarsePointer: true }))).toBe(true);
  });

  it('falls back to false when matchMedia is unavailable', () => {
    const env = { devicePixelRatio: () => 1, matchMedia: () => null };
    expect(isCoarsePointer(env)).toBe(false);
  });
});

describe('coarsePointerScale', () => {
  it('is 1 for a fine pointer and doubles for a coarse one', () => {
    expect(coarsePointerScale(false)).toBe(1);
    expect(coarsePointerScale(true)).toBe(2);
  });
});

describe('watchDprChange', () => {
  it('fires with the new ratio when the env DPR changes', () => {
    const env = memoryDpiEnv({ dpr: 1 });
    const onChange = vi.fn();
    watchDprChange(onChange, env);

    env.setDpr(2);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(2);
  });

  it('keeps firing on every subsequent change (re-subscribes each time)', () => {
    const env = memoryDpiEnv({ dpr: 1 });
    const onChange = vi.fn();
    watchDprChange(onChange, env);

    env.setDpr(2);
    env.setDpr(3);
    env.setDpr(1);
    expect(onChange).toHaveBeenCalledTimes(3);
    expect(onChange.mock.calls.map((c) => c[0])).toEqual([2, 3, 1]);
  });

  it('does not fire when the ratio is set to its current value', () => {
    const env = memoryDpiEnv({ dpr: 1 });
    const onChange = vi.fn();
    watchDprChange(onChange, env);

    env.setDpr(1);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('stops firing after the returned unsubscribe is called', () => {
    const env = memoryDpiEnv({ dpr: 1 });
    const onChange = vi.fn();
    const unsubscribe = watchDprChange(onChange, env);

    unsubscribe();
    env.setDpr(2);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('is a no-op when matchMedia is unavailable', () => {
    const env = { devicePixelRatio: () => 1, matchMedia: () => null };
    const onChange = vi.fn();
    expect(() => watchDprChange(onChange, env)()).not.toThrow();
  });
});
