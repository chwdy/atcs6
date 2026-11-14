import { describe, expect, it } from 'vitest';

import { setupToolRefusalMessage } from './setup-tool-refusals';

describe('setupToolRefusalMessage', () => {
  it('names the two device-handle refusals plainly', () => {
    expect(setupToolRefusalMessage({ reason: 'outputs_owned', error: 'x' })).toBe(
      'Another tool is using the outputs — close it first',
    );
    expect(setupToolRefusalMessage({ reason: 'no_device' })).toBe('No controller connected');
  });

  it('leaves any other reply to its own error text', () => {
    expect(setupToolRefusalMessage({ error: 'runs only in Setup' })).toBeNull();
    expect(setupToolRefusalMessage({ reason: 'something_else' })).toBeNull();
    expect(setupToolRefusalMessage(null)).toBeNull();
  });
});
