import { describe, it, expect } from 'vitest';

import { spineGreeting } from './spine';

describe('spineGreeting', () => {
  it('builds the spine greeting for a given name', () => {
    expect(spineGreeting('atcs6')).toBe('vite/ts spine alive for atcs6');
  });
});
