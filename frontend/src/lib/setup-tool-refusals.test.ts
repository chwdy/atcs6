import { describe, expect, it } from 'vitest';

import { setupToolRefusalMessage } from './setup-tool-refusals';

describe('setupToolRefusalMessage', () => {
  it('names the two device-handle refusals plainly', () => {
    expect(setupToolRefusalMessage({ reason: 'outputs_owned' })).toBe(
      'Another tool is using the outputs — close it first',
    );
    expect(setupToolRefusalMessage({ reason: 'no_device' })).toBe('No controller connected');
  });

  it('keeps the server text that names the tool holding the outputs', () => {
    const error = 'Another tool is using the outputs (the occupancy bench) — close it first';
    expect(setupToolRefusalMessage({ reason: 'outputs_owned', error })).toBe(error);
  });

  it('leaves any other reply to its own error text', () => {
    expect(setupToolRefusalMessage({ error: 'runs only in Setup' })).toBeNull();
    expect(setupToolRefusalMessage({ reason: 'something_else' })).toBeNull();
    expect(setupToolRefusalMessage(null)).toBeNull();
  });
});
