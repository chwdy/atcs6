import { describe, expect, it } from 'vitest';

import { districtHue, districtColorCss } from './district-color';

describe('district-color', () => {
  it('is deterministic — same id always yields the same hue/color', () => {
    expect(districtHue('D_001')).toBe(districtHue('D_001'));
    expect(districtColorCss('yard')).toBe(districtColorCss('yard'));
  });

  it('produces a hue in [0, 359]', () => {
    for (const id of ['D_001', 'D_002', 'yard', 'a', '']) {
      const h = districtHue(id);
      expect(h).toBeGreaterThanOrEqual(0);
      expect(h).toBeLessThan(360);
    }
  });

  it('separates similar ids into distinct hues', () => {
    expect(districtHue('D_001')).not.toBe(districtHue('D_002'));
  });

  it('emits an hsla string with the given alpha', () => {
    expect(districtColorCss('D_001', 0.6)).toMatch(/^hsla\(\d+, 70%, 50%, 0\.6\)$/);
    expect(districtColorCss('D_001', 1)).toMatch(/, 1\)$/);
  });
});
