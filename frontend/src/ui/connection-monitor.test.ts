// Wording tests for the shutdown summary the UI shows after a reconnect.
//
// The sentence IS the feature here — an operator who was not watching the
// terminal learns from this line alone whether the track was left energized.
// Pinning the classification (and the severity that colours it) is worth more
// than asserting on DOM.

import { describe, it, expect } from 'vitest';
import { describeShutdown, formatRunFor } from './connection-monitor';

describe('formatRunFor', () => {
  it('renders seconds, minutes and hours', () => {
    expect(formatRunFor(20)).toBe('20s');
    expect(formatRunFor(90)).toBe('1m');
    expect(formatRunFor(3600 * 2 + 60 * 14)).toBe('2h 14m');
  });

  it('does not invent a duration it was not given', () => {
    expect(formatRunFor(null)).toBe('an unknown time');
    expect(formatRunFor(undefined)).toBe('an unknown time');
  });
});

describe('describeShutdown', () => {
  it('flags a killed process as an error and warns the outputs were never safed', () => {
    const { message, type } = describeShutdown({
      clean: false,
      safe_state_applied: false,
      pid: 52223,
      ran_for_seconds: 20,
    });
    expect(type).toBe('error');
    expect(message).toContain('52223');
    expect(message).toContain('20s');
    expect(message).toContain('never driven to the safe state');
  });

  it('flags a failed safe-state call as an error even though the exit was clean', () => {
    // The process shut down properly but the hardware write threw — the track
    // can still be live, so this must not be softened to a warning.
    const { message, type } = describeShutdown({
      clean: true,
      safe_state_applied: false,
      ran_for_seconds: 600,
    });
    expect(type).toBe('error');
    expect(message).toContain('FAILED');
    expect(message).toContain('may still be energized');
  });

  it('reports a signalled-but-clean shutdown as a warning', () => {
    const { message, type } = describeShutdown({
      clean: true,
      safe_state_applied: true,
      signal_name: 'SIGTERM',
      ran_for_seconds: 840,
    });
    expect(type).toBe('warning');
    expect(message).toContain('SIGTERM');
    expect(message).toContain('14m');
    expect(message).toContain('driven to the safe state');
  });

  it('treats an ordinary clean exit as info', () => {
    const { type } = describeShutdown({
      clean: true,
      safe_state_applied: true,
      ran_for_seconds: 5,
    });
    expect(type).toBe('info');
  });
});
