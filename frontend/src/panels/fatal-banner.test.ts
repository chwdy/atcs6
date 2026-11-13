// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest';

import { updateFatalBanner } from './fatal-banner';

function banner(): HTMLElement {
  return document.getElementById('fatal-banner')!;
}

describe('updateFatalBanner', () => {
  beforeEach(() => {
    document.body.innerHTML = `
      <div id="fatal-banner" hidden>
        <div class="fatal-banner-title"></div>
        <div id="fatal-banner-detail"></div>
        <div id="fatal-banner-meta"></div>
      </div>`;
  });

  it('shows the hardware failure and its detail while that halt holds', () => {
    updateFatalBanner({
      session_state: { value: 'halted', halt_reason: 'hardware_failure' },
      hardware_failure_detail: {
        component: 'block_track_output',
        operation: 'set_voltage',
        resource_id: 'BLK_1',
        message: 'pwm1: bus error',
      },
    });
    expect(banner().hidden).toBe(false);
    expect(document.getElementById('fatal-banner-detail')!.textContent).toContain('pwm1: bus error');
    expect(document.getElementById('fatal-banner-meta')!.textContent).toBe(
      'block_track_output · set_voltage · BLK_1'
    );
  });

  it('stays hidden in every other session state', () => {
    for (const session of [
      { value: 'live', halt_reason: null },
      { value: 'setup', halt_reason: null },
      { value: 'halted', halt_reason: 'not_activated' },
    ]) {
      updateFatalBanner({ session_state: session });
      expect(banner().hidden).toBe(true);
    }
  });
});
