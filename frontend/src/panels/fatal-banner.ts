/**
 * The hardware-failure banner (design_v2.md "Safe state, watchdog and
 * hardware failure"; "Session state").
 *
 * While the session is Halted for `hardware_failure`, every train is halted
 * and the outputs are in their safe state. The way back is repairing the
 * device and activating again, so the banner names the failure and offers
 * Activate; restarting the server stays available as the heavier option.
 * The failure's detail comes from the snapshot's `hardware_failure_detail`,
 * which the server reads from the last `enter_hardware_failure` event.
 */

/** Show or hide the banner for this snapshot. */
export function updateFatalBanner(network: any): void {
  const banner = document.getElementById('fatal-banner');
  if (!banner) return;

  const haltReason = network?.session_state?.value === 'halted'
    ? network.session_state.halt_reason
    : null;
  if (haltReason !== 'hardware_failure') {
    banner.hidden = true;
    return;
  }

  const titleEl = document.querySelector('.fatal-banner-title');
  const detailEl = document.getElementById('fatal-banner-detail');
  const metaEl = document.getElementById('fatal-banner-meta');
  const hw = network.hardware_failure_detail;
  if (titleEl) titleEl.textContent = 'Halted — hardware failure';
  if (detailEl) {
    detailEl.textContent =
      (hw?.message || 'A hardware output or detector failed.') +
      ' Every train is halted and the outputs are in their safe state.' +
      ' Repair or replace the device, then activate the environment again.';
  }
  if (metaEl) {
    metaEl.textContent = hw
      ? [hw.component, hw.operation, hw.resource_id].filter(Boolean).join(' · ')
      : '';
  }
  banner.hidden = false;
}
