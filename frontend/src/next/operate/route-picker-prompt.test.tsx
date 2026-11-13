import { describe, expect, it } from 'vitest';

import { routePickerPrompt } from './train-panel';

describe('routePickerPrompt', () => {
  it('never says "No routes yet" before the routes were read, or after a read failed', () => {
    expect(routePickerPrompt(null, null)).toBe('Reading the routes…');
    expect(routePickerPrompt(null, 'timeout')).toBe('Could not read the routes');
    expect(routePickerPrompt([], null)).toBe('No routes yet');
    expect(routePickerPrompt([{}], 'timeout')).toBe('Choose a route…');
  });
});
