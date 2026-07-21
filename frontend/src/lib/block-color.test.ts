import { describe, expect, it } from 'vitest';

import { blockHue, blockColorCss } from './block-color';

describe('block-color', () => {
  it('is deterministic — same id always yields the same hue/color', () => {
    expect(blockHue('BLK_001')).toBe(blockHue('BLK_001'));
    expect(blockColorCss('yard')).toBe(blockColorCss('yard'));
  });

  it('produces a hue in [0, 359]', () => {
    for (const id of ['BLK_001', 'BLK_002', 'yard', 'a', '']) {
      const h = blockHue(id);
      expect(h).toBeGreaterThanOrEqual(0);
      expect(h).toBeLessThan(360);
    }
  });

  it('separates similar ids into distinct hues', () => {
    expect(blockHue('BLK_001')).not.toBe(blockHue('BLK_002'));
  });

  it('emits an hsla string with the given alpha', () => {
    expect(blockColorCss('BLK_001', 0.6)).toMatch(/^hsla\(\d+, 70%, 50%, 0\.6\)$/);
    expect(blockColorCss('BLK_001', 1)).toMatch(/, 1\)$/);
  });
});
