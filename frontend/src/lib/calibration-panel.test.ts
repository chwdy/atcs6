/**
 * The calibration group's decisions (design_new.md "Speed calibration", The
 * panel, and "Lighting-voltage calibration", The panel): which trains the
 * locomotive row lists, which controls may be pressed, and what the panel
 * shows for a train that already has a run open.
 */
import { describe, it, expect } from 'vitest';
import {
  calibrationControls,
  calibrationTrainChoices,
  compareAxisTop,
  measuredCurve,
  readinessApplies,
  readinessKind,
  speedPassEndedMessage,
  trainChoiceLabel,
  trainStoredValue,
  viewForOpenRuns,
} from './calibration-panel';

const PROFILES = [
  { profile_id: 'EF66', derived_from: null, lighting_voltage: 1.5, power_speed_curve: [[0, 0], [100, 90]] },
  { profile_id: 'EF66__a', derived_from: 'EF66', lighting_voltage: 1.8, power_speed_curve: null },
  { profile_id: 'EF66__b', derived_from: 'EF66', lighting_voltage: null, power_speed_curve: [[0, 0], [100, 70]] },
  { profile_id: 'DD51', derived_from: null, lighting_voltage: 2.0, power_speed_curve: [[0, 0], [100, 60]] },
];

const TRAINS = [
  { id: 'T3', car_number: 'EF66-12', speed_profile_id: 'EF66__b' },
  { id: 'T1', car_number: 'EF66-2', speed_profile_id: 'EF66__a' },
  { id: 'T2', car_number: '', speed_profile_id: 'EF66' },
  { id: 'T4', car_number: 'DD51-1', speed_profile_id: 'DD51' },
  { id: 'T5', car_number: 'X', speed_profile_id: null },
];

describe('the locomotive row lists every train whose resolved profile is on screen', () => {
  it('lists trains naming the model profile and trains naming an instance of it', () => {
    const ids = calibrationTrainChoices(TRAINS, PROFILES, 'EF66').map((t) => t.id);
    expect(ids.sort()).toEqual(['T1', 'T2', 'T3']);
  });

  it('lists only the train naming an instance profile when that instance is on screen', () => {
    expect(calibrationTrainChoices(TRAINS, PROFILES, 'EF66__a').map((t) => t.id)).toEqual(['T1']);
  });

  it('sorts by car number in number order, falling back to the id', () => {
    expect(calibrationTrainChoices(TRAINS, PROFILES, 'EF66').map((t) => t.id)).toEqual([
      'T1', // EF66-2
      'T3', // EF66-12
      'T2', // no car number: T2
    ]);
  });

  it('names each train by car number and state', () => {
    expect(trainChoiceLabel(TRAINS[1], 'manual')).toBe('EF66-2 · manual');
    expect(trainChoiceLabel(TRAINS[2], 'off')).toBe('T2 · off');
    expect(trainChoiceLabel(TRAINS[1], null)).toBe('EF66-2');
  });
});

describe("the train's own stored values", () => {
  const model = PROFILES[0];

  it("reads the instance profile's value when it carries one", () => {
    expect(trainStoredValue(TRAINS[1], PROFILES, model, 'lighting_voltage')).toBe(1.8);
    expect(trainStoredValue(TRAINS[0], PROFILES, model, 'power_speed_curve')).toEqual([[0, 0], [100, 70]]);
  });

  it('reads the profile on screen when the instance leaves the field unset', () => {
    expect(trainStoredValue(TRAINS[0], PROFILES, model, 'lighting_voltage')).toBe(1.5);
    expect(trainStoredValue(TRAINS[1], PROFILES, model, 'power_speed_curve')).toEqual([[0, 0], [100, 90]]);
  });

  it('reads the profile on screen for a train naming it directly, or for no train', () => {
    expect(trainStoredValue(TRAINS[2], PROFILES, { ...model, lighting_voltage: 1.6 }, 'lighting_voltage')).toBe(1.6);
    expect(trainStoredValue(null, PROFILES, model, 'lighting_voltage')).toBe(1.5);
  });
});

describe('which controls may be pressed', () => {
  const base = { trainNamed: true, ready: true, view: 'none' as const, stretchResolved: false };

  it('offers nothing until a train is named', () => {
    expect(calibrationControls({ ...base, trainNamed: false })).toEqual({
      lightingCalibrate: false,
      speedCalibrate: false,
      speedStart: false,
    });
  });

  it('gates the lighting Calibrate on readiness, but not the speed setup', () => {
    const c = calibrationControls({ ...base, ready: false });
    expect(c.lightingCalibrate).toBe(false);
    expect(c.speedCalibrate).toBe(true);
  });

  it('offers Start only in the setup, once the stretch resolved and the train is ready', () => {
    const setup = { ...base, view: 'speed-setup' as const };
    expect(calibrationControls({ ...setup, stretchResolved: false }).speedStart).toBe(false);
    expect(calibrationControls({ ...setup, stretchResolved: true, ready: false }).speedStart).toBe(false);
    expect(calibrationControls({ ...setup, stretchResolved: true }).speedStart).toBe(true);
    expect(calibrationControls({ ...setup, stretchResolved: true }).lightingCalibrate).toBe(false);
  });

  it('offers neither Calibrate control while a run is open or a result is held', () => {
    for (const view of ['lighting', 'speed-running', 'speed-held'] as const) {
      const c = calibrationControls({ ...base, view });
      expect(c.lightingCalibrate || c.speedCalibrate || c.speedStart).toBe(false);
    }
  });
});

describe('the readiness line', () => {
  it('asks for the speed check only while the pass is being set up', () => {
    expect(readinessKind('speed-setup')).toBe('speed');
    expect(readinessKind('none')).toBe('lighting');
  });

  it('is asked for only while no run is open', () => {
    expect(readinessApplies('none')).toBe(true);
    expect(readinessApplies('speed-setup')).toBe(true);
    expect(readinessApplies('lighting')).toBe(false);
    expect(readinessApplies('speed-running')).toBe(false);
    expect(readinessApplies('speed-held')).toBe(false);
  });
});

describe('a run already open for the named train is shown, not hidden', () => {
  it('shows an open lighting run', () => {
    expect(viewForOpenRuns({ open: false }, { open: true })).toBe('lighting');
  });

  it('shows a held result and an open pass', () => {
    expect(viewForOpenRuns({ open: true, phase: 'measured' }, { open: false })).toBe('speed-held');
    expect(viewForOpenRuns({ open: true, phase: 'measuring' }, { open: false })).toBe('speed-running');
  });

  it('shows nothing when no run is open', () => {
    expect(viewForOpenRuns({ open: false }, { open: false })).toBe('none');
    expect(viewForOpenRuns(null, null)).toBe('none');
  });
});

describe('the measured curve', () => {
  it('starts at the origin and runs in power order', () => {
    expect(
      measuredCurve([
        { power_percent: 20, speed_kmh: 14 },
        { power_percent: 10, speed_kmh: 0 },
      ])
    ).toEqual([
      [0, 0],
      [10, 0],
      [20, 14],
    ]);
  });

  it('sizes the speed axis to hold both curves', () => {
    expect(compareAxisTop([[0, 0], [100, 90]], [[0, 0], [100, 120]])).toBe(138);
    expect(compareAxisTop([], [[0, 0]])).toBe(30);
  });
});

describe('speedPassEndedMessage', () => {
  it('names the reason a failed pass gives on the command-result stream', () => {
    expect(
      speedPassEndedMessage({ success: false, reason: 'train overran the stretch past the far outer block' })
    ).toBe('The pass ended: Train overran the stretch past the far outer block. Nothing was written.');
  });

  it('falls back to the plain line with no failure to name', () => {
    expect(speedPassEndedMessage(null)).toBe('The pass ended. Nothing was written.');
    expect(speedPassEndedMessage({ success: true, reason: 'speed_calibration finished' })).toBe(
      'The pass ended. Nothing was written.'
    );
  });
});
