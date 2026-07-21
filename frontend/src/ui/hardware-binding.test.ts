import { describe, expect, it } from 'vitest';

import {
  BLOCK_HW_SLOTS,
  hardwareSectionHtml,
  occupancyResultHtml,
  switchHardwareSectionHtml,
} from './hardware-binding';
import type { HwConfig } from './hardware-binding';

// These cover the two HTML builders, which is where every user-facing string in
// the block Hardware section is decided. They are pure string functions, so no
// DOM environment is needed (this suite has none).

const BLOCK = 'BLK_1';

/** A block wired to track power plus both occupancy detectors. */
function cfg(overrides: Partial<HwConfig> = {}): HwConfig {
  return {
    enabled: true,
    chips: [
      { chip_id: 'track0', kind: 'pca9685', role: 'track_output', port_kind: 'bipolar_pair', port_count: 8, label: 'Track PWM' },
      { chip_id: 'det0', kind: 'hc165', role: 'occupancy_input', port_kind: 'digital_in', port_count: 8, label: 'Detectors' },
      { chip_id: 'cur0', kind: 'ina3221', role: 'occupancy_input', port_kind: 'current_sense', port_count: 3, label: 'INA3221' },
      { chip_id: 'cur1', kind: 'ina219', role: 'occupancy_input', port_kind: 'current_sense', port_count: 1, label: 'INA219' },
    ],
    block_track: { [BLOCK]: { chip_id: 'track0', port: 0 } },
    block_occupancy: { [BLOCK]: { chip_id: 'det0', port: 3 } },
    block_occupancy_current: {
      [BLOCK]: { chip_id: 'cur0', port: 1, baseline_ma: 0, threshold_ma: 15 },
    },
    switch_output: {},
    ...overrides,
  } as HwConfig;
}

const section = (config: HwConfig | null) => hardwareSectionHtml(config, BLOCK, BLOCK_HW_SLOTS);

/** The <option> texts of one <select>, identified by a class on it. */
function optionsOf(html: string, selectClass: string): string[] {
  const open = html.indexOf(selectClass);
  if (open === -1) return [];
  const start = html.indexOf('>', open);
  const end = html.indexOf('</select>', start);
  return [...html.slice(start, end).matchAll(/<option[^>]*>([^<]*)<\/option>/g)].map((m) => m[1]);
}

describe('block hardware section', () => {
  it('renders nothing when no chip is configured, so a simulated layout never sees it', () => {
    // Gated on chips, not on the enabled flag: a pure-sim layout has no chips.
    expect(section(null)).toBe('');
    expect(section(cfg({ chips: [] }))).toBe('');
    expect(section(cfg({ chips: [], enabled: true }))).toBe('');
  });

  it('shows the section for binding before hardware is enabled', () => {
    // The port bindings are bench configuration, so they must not wait on the
    // real-hardware enable gate — the section renders with enabled off.
    const html = section(cfg({ enabled: false }));
    expect(html).toContain('hw-bind-section');
    expect(html).toContain('Track power');
    expect(html).toContain('Test occupancy');
  });

  it('warns that hardware is not live only while it is disabled', () => {
    expect(section(cfg({ enabled: false }))).toContain('Hardware is not live yet');
    expect(section(cfg({ enabled: true }))).not.toContain('Hardware is not live yet');
  });

  it('names the two rails each track port drives', () => {
    // Port p is the H-bridge pair (ch2p, ch2p+1) — the wires to the block's rails.
    const opts = optionsOf(section(cfg()), 'hw-bind-select" data-hw-map="block_track');
    expect(opts).toContain('Track PWM · P0 · rails ch0/ch1');
    expect(opts).toContain('Track PWM · P3 · rails ch6/ch7');
  });

  it('splits the two occupancy sources by port kind and offers both sensors', () => {
    const html = section(cfg());
    const digital = optionsOf(html, 'hw-bind-select" data-hw-map="block_occupancy');
    const current = optionsOf(html, 'hw-cur-select');
    // The '165 slot takes only digital detectors...
    expect(digital.some((o) => o.startsWith('Detectors'))).toBe(true);
    expect(digital.some((o) => /INA/.test(o))).toBe(false);
    // ...and the current slot takes both current sensors, channel-numbered.
    expect(current).toContain('INA3221 · CH2');
    expect(current).toContain('INA219 · CH1');
    expect(current.some((o) => o.startsWith('Detectors'))).toBe(false);
  });

  it('gives the occupancy pair one shared test, and track power its own sweep', () => {
    const html = section(cfg());
    expect(html).toContain('hw-occ-test');
    // Exactly one per-row Test button, on the track row, running the sweep.
    expect(html.match(/hw-bind-test/g)).toHaveLength(1);
    expect(html).toContain('data-hw-map="block_track" data-hw-action="drive_sequence"');
    // The occupancy rows carry no button of their own.
    expect(html).not.toContain('hw-cur-read');
  });

  it('orders the occupancy pair and its test above track power', () => {
    const html = section(cfg());
    expect(html.indexOf('Occupancy (idle)')).toBeLessThan(html.indexOf('Occupancy (driving)'));
    expect(html.indexOf('Occupancy (driving)')).toBeLessThan(html.indexOf('hw-occ-test'));
    expect(html.indexOf('hw-occ-test')).toBeLessThan(html.indexOf('Track power'));
  });

  it('drops a slot with no chip rather than offering an empty dropdown', () => {
    const noCurrent = cfg({
      chips: cfg().chips.filter((c) => c.port_kind !== 'current_sense'),
    });
    const html = section(noCurrent);
    expect(html).toContain('Occupancy (idle)');
    expect(html).not.toContain('Occupancy (driving)');
    // The shared occupancy test still shows: one detector is enough to test.
    expect(html).toContain('hw-occ-test');
  });
});

describe('occupancy test report', () => {
  /** A full report from `/api/hardware/occupancy-test`. */
  const report = (over: Record<string, any> = {}) => ({
    ok: true,
    block_id: BLOCK,
    track: { bound: true, chip_id: 'track0', port: 0, volts: 0 },
    driven: false,
    deadband_v: 0.25,
    digital: { bound: true, chip_id: 'det0', port: 3, value: true, occupied: true },
    current: {
      bound: true, chip_id: 'cur0', port: 1, baseline_ma: 0, threshold_ma: 15,
      current_ma: 0.8, bus_v: 0, occupied: false,
    },
    source: 'digital',
    occupied: true,
    ...over,
  });

  /** The three detector rows, tag-stripped, in render order. */
  function rowsOf(html: string): string[] {
    return [...html.matchAll(/<div class="hw-occ-row[^"]*">([\s\S]*?)<\/div>/g)]
      .map((m) => m[1].replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim());
  }

  const verdict = (html: string) =>
    html.match(/<div class="hw-occ-verdict[^"]*">\s*([^<]*?)\s*<\/div>/)?.[1] ?? '';

  it('shows both detectors and marks the idle one as deciding when the block is idle', () => {
    const html = occupancyResultHtml(report());
    const rows = rowsOf(html);
    expect(rows[0]).toContain('0.0 V · idle');
    expect(rows[1]).toContain('P3 · occupied');
    // The current detector disagrees, and is still shown rather than hidden.
    expect(rows[2]).toContain('0.8 mA');
    expect(rows[2]).toContain('trips at ±15.0');
    expect(rows[2]).toContain('clear');
    // Exactly one row is marked as the one that decides, and it is the '165.
    expect(html.match(/hw-occ-row is-source/g)).toHaveLength(1);
    expect(rows[1]).toContain('deciding');
    expect(verdict(html)).toBe('Occupied — the block is idle, so the idle detector decides');
    expect(html).toContain('hw-occ-verdict is-occupied');
  });

  it('hands the verdict to the current detector once the block is driven', () => {
    const html = occupancyResultHtml(report({
      track: { bound: true, chip_id: 'track0', port: 0, volts: 12 },
      driven: true,
      source: 'current',
      occupied: false,
    }));
    const rows = rowsOf(html);
    expect(rows[0]).toContain('12.0 V · driving');
    expect(html.match(/hw-occ-row is-source/g)).toHaveLength(1);
    expect(rows[2]).toContain('deciding');
    expect(verdict(html)).toBe('Clear — the block is driving, so the driving detector decides');
    expect(html).toContain('hw-occ-verdict is-clear');
  });

  it('reads a reverse-polarity block as driven', () => {
    const html = occupancyResultHtml(report({
      track: { bound: true, chip_id: 'track0', port: 0, volts: -9 },
      driven: true, source: 'current', occupied: true,
    }));
    expect(rowsOf(html)[0]).toContain('-9.0 V · driving');
  });

  it('reports an unreadable detector without pretending it read clear', () => {
    const html = occupancyResultHtml(report({
      current: {
        bound: true, chip_id: 'cur0', port: 1, baseline_ma: 0, threshold_ma: 15,
        error: 'no ACK',
      },
    }));
    const rows = rowsOf(html);
    expect(rows[2]).toContain('could not read (no ACK)');
    expect(rows[2]).not.toContain('clear');
  });

  it('says an unbound detector is unbound rather than clear', () => {
    const html = occupancyResultHtml(report({
      digital: { bound: false },
      source: 'current',
      occupied: false,
    }));
    expect(rowsOf(html)[1]).toContain('not bound');
  });

  it('says so plainly when the block has no detector wired', () => {
    const html = occupancyResultHtml(report({
      digital: { bound: false },
      current: { bound: false },
      source: 'none',
      occupied: false,
    }));
    expect(html.match(/hw-occ-row is-source/g)).toBeNull();
    expect(verdict(html)).toBe('No detector bound to this block');
    expect(html).toContain('hw-occ-verdict is-unbound');
  });

  it('treats a block with no track port as idle', () => {
    const html = occupancyResultHtml(report({ track: { bound: false, volts: 0 } }));
    expect(rowsOf(html)[0]).toContain('no port bound — read as idle');
    expect(verdict(html)).toContain('the block is idle');
  });
});

describe('switch hardware section', () => {
  const SWITCH = 'SW_1';

  /** A layout with one '595 chain for switches plus a track chip to ignore. */
  function swCfg(overrides: Partial<HwConfig> = {}): HwConfig {
    return {
      enabled: true,
      chips: [
        { chip_id: 'sw0', kind: 'hc595', role: 'switch_output', port_kind: 'digital_out', port_count: 8, label: 'Points' },
        { chip_id: 'track0', kind: 'pca9685', role: 'track_output', port_kind: 'bipolar_pair', port_count: 8, label: 'Track PWM' },
      ],
      block_track: {},
      block_occupancy: {},
      block_occupancy_current: {},
      switch_output: {},
      ...overrides,
    } as HwConfig;
  }

  /** The value of every <option> carrying `selected`, in render order. */
  function selectedValues(html: string): string[] {
    return [...html.matchAll(/<option value="([^"]*)" selected>/g)].map((m) => m[1]);
  }

  const bound = (extra: Record<string, number> = {}) => ({
    [SWITCH]: { chip_id: 'sw0', port_main: 2, port_diverge: 3, ...extra },
  });

  it('renders nothing when no chip can drive a coil, so a simulated layout never sees it', () => {
    expect(switchHardwareSectionHtml(null, SWITCH)).toBe('');
    const trackOnly = swCfg({ chips: swCfg().chips.filter((c) => c.role !== 'switch_output') });
    expect(switchHardwareSectionHtml(trackOnly, SWITCH)).toBe('');
  });

  it('shows the section for coil wiring before hardware is enabled', () => {
    // Coil wiring is bench configuration, so — like the block section — it does
    // not wait on the real-hardware enable gate.
    const html = switchHardwareSectionHtml(swCfg({ enabled: false }), SWITCH);
    expect(html).toContain('hw-switch-section');
    expect(html).toContain('data-side="main"');
    expect(html).toContain('Hardware is not live yet');
    expect(switchHardwareSectionHtml(swCfg({ enabled: true }), SWITCH))
      .not.toContain('Hardware is not live yet');
  });

  it('offers a coil row per side, listing only switch-role ports', () => {
    const html = switchHardwareSectionHtml(swCfg(), SWITCH);
    expect(html).toContain('data-side="main"');
    expect(html).toContain('data-side="diverge"');
    // A coil cannot be driven from a PWM track port, so that chip is not offered.
    expect(html).toContain('sw0::0');
    expect(html).not.toContain('track0::0');
  });

  it('leaves every coil unset and Test disabled while the switch is unwired', () => {
    const html = switchHardwareSectionHtml(swCfg(), SWITCH);
    expect(selectedValues(html)).toEqual([]);
    expect(html).toMatch(/hw-sw-test" disabled/);
  });

  it('preselects both coils and enables Test once the switch is wired', () => {
    const html = switchHardwareSectionHtml(swCfg({ switch_output: bound() }), SWITCH);
    expect(selectedValues(html)).toEqual(['sw0::2', 'sw0::3']);
    expect(html).not.toMatch(/hw-sw-test" disabled/);
  });

  it('hides the standby row when the driver needs no standby pin', () => {
    const html = switchHardwareSectionHtml(swCfg({ switch_output: bound() }), SWITCH);
    expect(html).toContain('class="hw-sw-standby-row" hidden');
    expect(html).not.toContain('hw-sw-standby-on" checked');
  });

  it('shows the standby row, checked and preselected, when one is wired', () => {
    const html = switchHardwareSectionHtml(
      swCfg({ switch_output: bound({ port_standby: 7 }) }), SWITCH
    );
    expect(html).toContain('hw-sw-standby-on" checked');
    expect(html).not.toContain('class="hw-sw-standby-row" hidden');
    expect(selectedValues(html)).toEqual(['sw0::2', 'sw0::3', 'sw0::7']);
  });

  it('reads the binding for its own switch only', () => {
    const other = { SW_OTHER: { chip_id: 'sw0', port_main: 2, port_diverge: 3 } };
    expect(selectedValues(switchHardwareSectionHtml(swCfg({ switch_output: other }), SWITCH)))
      .toEqual([]);
  });

  it('spans a cascaded chain, so a coil may sit on any chip in it', () => {
    const chained = swCfg({
      chips: [
        { chip_id: 'sw0', kind: 'hc595', role: 'switch_output', port_kind: 'digital_out', port_count: 16, label: 'Points' },
      ],
      switch_output: { [SWITCH]: { chip_id: 'sw0', port_main: 0, port_diverge: 15 } },
    });
    expect(selectedValues(switchHardwareSectionHtml(chained, SWITCH))).toEqual(['sw0::0', 'sw0::15']);
  });

  it('tells the user the throw is a short pulse, which is why it is safe', () => {
    const html = switchHardwareSectionHtml(swCfg(), SWITCH);
    expect(html).toContain('10&nbsp;ms');
  });
});
