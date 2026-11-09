/**
 * The calibration group's decisions (design_new.md "Speed calibration", The
 * panel): which trains the locomotive row lists, which controls may be
 * pressed, what the panel shows for a train that already has a pass open, and
 * how the pass's progress, points and crossings read.
 */
import { describe, it, expect } from 'vitest';
import {
  stretchEndBlocks,
  stretchEndsText,
  atoTestProgress,
  calibrationControls,
  calibrationTrainChoices,
  compareAxisTop,
  crossingRows,
  speedPassProgress,
  measuredCurve,
  pointRows,
  readinessApplies,
  speedPassEndedMessage,
  trainChoiceLabel,
  trainStoredValue,
  itMovesProblem,
  trialInstruction,
  trialStage,
  viewForOpenRun,
  volts,
} from './calibration-panel';

// Set 1 (EF66) and set 4 (DD51); instance profiles 11, 12, 14 copied from
// them; 13 is T2's own copy of set 1 too.
const PROFILES = [
  { profile_id: 1, model_id: 1, parent_profile_id: null, lighting_voltage: 1.5, voltage_speed_curve: [[2, 10], [12, 90]] },
  { profile_id: 11, model_id: null, parent_profile_id: 1, lighting_voltage: 1.8, voltage_speed_curve: [[2, 10], [12, 90]] },
  { profile_id: 12, model_id: null, parent_profile_id: 1, lighting_voltage: 1.5, voltage_speed_curve: [[3, 8], [12, 70]] },
  { profile_id: 13, model_id: null, parent_profile_id: 1, lighting_voltage: 1.5, voltage_speed_curve: [[2, 10], [12, 90]] },
  { profile_id: 4, model_id: 2, parent_profile_id: null, lighting_voltage: 2.0, voltage_speed_curve: [[2.5, 6], [12, 60]] },
  { profile_id: 14, model_id: null, parent_profile_id: 4, lighting_voltage: 2.0, voltage_speed_curve: [[2.5, 6], [12, 60]] },
];

const TRAINS = [
  { id: 'T3', car_number: 'EF66-12', speed_profile_id: 12 },
  { id: 'T1', car_number: 'EF66-2', speed_profile_id: 11 },
  { id: 'T2', car_number: '', speed_profile_id: 13 },
  { id: 'T4', car_number: 'DD51-1', speed_profile_id: 14 },
  { id: 'T5', car_number: 'X', speed_profile_id: null },
];

describe('the locomotive row lists the trains for the profile on screen', () => {
  it('lists every train copied from a set when the set is on screen', () => {
    const ids = calibrationTrainChoices(TRAINS, PROFILES, 1).map((t) => t.id);
    expect(ids.sort()).toEqual(['T1', 'T2', 'T3']);
  });

  it('lists only the owning train when its instance profile is on screen', () => {
    expect(calibrationTrainChoices(TRAINS, PROFILES, 11).map((t) => t.id)).toEqual(['T1']);
  });

  it('sorts by car number in number order, falling back to the id', () => {
    expect(calibrationTrainChoices(TRAINS, PROFILES, 1).map((t) => t.id)).toEqual([
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
  const set = PROFILES[0];

  it("reads the train's own instance profile", () => {
    expect(trainStoredValue(TRAINS[1], PROFILES, set, 'lighting_voltage')).toBe(1.8);
    expect(trainStoredValue(TRAINS[0], PROFILES, set, 'voltage_speed_curve')).toEqual([[3, 8], [12, 70]]);
  });

  it('reads the profile on screen for the train it belongs to, or for no train', () => {
    const own = PROFILES[1];
    expect(trainStoredValue(TRAINS[1], PROFILES, { ...own, lighting_voltage: 1.6 }, 'lighting_voltage')).toBe(1.6);
    expect(trainStoredValue(null, PROFILES, set, 'lighting_voltage')).toBe(1.5);
  });
});

describe('which controls may be pressed', () => {
  const base = { trainNamed: true, ready: true, view: 'none' as const, stretchResolved: false };

  it('offers nothing until a train is named', () => {
    expect(calibrationControls({ ...base, trainNamed: false })).toEqual({
      speedCalibrate: false,
      speedStart: false,
      atoTest: false,
      atoStart: false,
    });
  });

  it('does not gate the speed setup on readiness', () => {
    expect(calibrationControls({ ...base, ready: false }).speedCalibrate).toBe(true);
  });

  it('offers Start only in the setup, once the stretch resolved and the train is ready', () => {
    const setup = { ...base, view: 'speed-setup' as const };
    expect(calibrationControls({ ...setup, stretchResolved: false }).speedStart).toBe(false);
    expect(calibrationControls({ ...setup, stretchResolved: true, ready: false }).speedStart).toBe(false);
    expect(calibrationControls({ ...setup, stretchResolved: true }).speedStart).toBe(true);
    expect(calibrationControls({ ...setup, stretchResolved: true }).speedCalibrate).toBe(false);
  });

  it('offers no control while a run is open or a result is held', () => {
    for (const view of ['speed-running', 'speed-held', 'ato-running'] as const) {
      const c = calibrationControls({ ...base, view });
      expect(c.speedCalibrate || c.speedStart || c.atoTest || c.atoStart).toBe(false);
    }
  });

  it('offers the ATO minimum Test with no run open, and its Start in its setup', () => {
    expect(calibrationControls(base).atoTest).toBe(true);
    const setup = { ...base, view: 'ato-setup' as const, stretchResolved: true };
    expect(calibrationControls(setup).atoStart).toBe(true);
    expect(calibrationControls({ ...setup, ready: false }).atoStart).toBe(false);
    expect(calibrationControls(setup).speedStart).toBe(false);
  });
});

describe('the trial stages and the ATO minimum test', () => {
  it('reads which trial is running, and none once sweeping or testing', () => {
    expect(trialStage({ pass_stage: 'lighting_trial' })).toBe('lighting');
    expect(trialStage({ pass_stage: 'starting_trial' })).toBe('starting');
    expect(trialStage({ pass_stage: 'sweep' })).toBeNull();
    expect(trialStage({ kind: 'ato_minimum_test', pass_stage: 'lighting_trial' })).toBeNull();
    expect(trialInstruction('lighting')).toMatch(/Keep/);
    expect(trialInstruction('starting')).toMatch(/It moves/);
  });

  it('names the kept lighting voltage in the starting trial, so the two do not read as one', () => {
    expect(trialInstruction('lighting')).toMatch(/^Lighting voltage:/);
    expect(trialInstruction('starting', 2.2)).toMatch(/^Starting voltage:/);
    expect(trialInstruction('starting', 2.2)).toContain('The lighting voltage you kept is 2.2 V; the starting voltage must be above it.');
    expect(trialInstruction('starting', null)).toContain('No lighting voltage was kept');
  });

  it('says plainly why It moves cannot be pressed at or below the kept lighting voltage', () => {
    expect(itMovesProblem(2.2, 2.2)).toBe(
      'The starting voltage (2.2 V) must be above the lighting voltage you kept (2.2 V). Raise the voltage until the train just moves, then press It moves.'
    );
    expect(itMovesProblem(2.3, 2.2)).toBeNull();
    expect(itMovesProblem(0.5, null)).toBeNull();
  });

  it('says a run is stopping the train once the operator ended it', () => {
    expect(speedPassProgress({ ending: true, phase: 'leg' }, false)).toBe('Stopping the train, then releasing it.');
    expect(atoTestProgress({ ending: true, phase: 'leg' })).toBe('Stopping the train, then releasing it.');
  });

  it('shows an open ATO minimum test as the test', () => {
    expect(viewForOpenRun({ open: true, kind: 'ato_minimum_test', phase: 'leg' })).toBe('ato-running');
  });

  it('says how close the test voltage is to steady, and when it stalled', () => {
    const run = {
      phase: 'leg',
      current_voltage: 4,
      steady_round_trips: 1,
      steady_round_trips_target: 3,
      is_steady: false,
      leg_stalled: false,
    };
    expect(atoTestProgress(run)).toBe('Testing 4 V — 1 of 3 round trips in a row with no stall.');
    expect(atoTestProgress({ ...run, leg_stalled: true })).toMatch(/stalled/);
    expect(atoTestProgress({ ...run, is_steady: true, steady_round_trips: 3 })).toMatch(/steady/);
  });

  it('lists a stalled test crossing as counted, with its note', () => {
    const rows = crossingRows({
      crossings: [{ track_voltage: 4, attempt: 1, toward_block_id: 'C', seconds: 9, speed_kmh: 3, outcome: 'stalled' }],
    });
    expect(rows[0].note).toMatch(/stalled/);
  });
});

describe('the readiness line', () => {
  it('is asked for only while no pass is open', () => {
    expect(readinessApplies('none')).toBe(true);
    expect(readinessApplies('speed-setup')).toBe(true);
    expect(readinessApplies('speed-running')).toBe(false);
    expect(readinessApplies('speed-held')).toBe(false);
    expect(readinessApplies('ato-setup')).toBe(true);
    expect(readinessApplies('ato-running')).toBe(false);
  });
});

describe('a pass already open for the named train is shown, not hidden', () => {
  it('shows a held result and an open pass', () => {
    expect(viewForOpenRun({ open: true, phase: 'measured' })).toBe('speed-held');
    expect(viewForOpenRun({ open: true, phase: 'leg' })).toBe('speed-running');
    expect(viewForOpenRun({ open: true, phase: 'resetting' })).toBe('speed-running');
  });

  it('shows nothing when no pass is open', () => {
    expect(viewForOpenRun({ open: false })).toBe('none');
    expect(viewForOpenRun(null)).toBe('none');
  });

  it('does not pick up a run that is stopping the train after the operator ended it', () => {
    expect(viewForOpenRun({ open: true, ending: true, kind: 'ato_minimum_test', phase: 'leg' })).toBe('none');
    expect(viewForOpenRun({ open: true, ending: true, phase: 'leg' })).toBe('none');
  });
});

describe('the measured curve', () => {
  it('runs in voltage order and starts at the lowest measured voltage, with no origin point', () => {
    expect(
      measuredCurve([
        { track_voltage: 8, speed_kmh: 50, kind: 'coarse' },
        { track_voltage: 3.5, speed_kmh: 12, kind: 'narrowing' },
      ])
    ).toEqual([
      [3.5, 12],
      [8, 50],
    ]);
  });

  it("lists the failed steps among the passed ones and marks the curve's first point", () => {
    const rows = pointRows({
      samples: [
        { track_voltage: 4, speed_kmh: 15 },
        { track_voltage: 12, speed_kmh: 90 },
      ],
      failed_voltages: [2, 3],
    });
    expect(rows).toEqual([
      { voltage: 2, speed: null, first: false },
      { voltage: 3, speed: null, first: false },
      { voltage: 4, speed: 15, first: true },
      { voltage: 12, speed: 90, first: false },
    ]);
    expect(pointRows({})).toEqual([]);
  });

  it('sizes the speed axis to hold both curves', () => {
    expect(compareAxisTop([[2, 10], [12, 90]], [[3, 12], [12, 120]])).toBe(138);
    expect(compareAxisTop([], [])).toBe(30);
  });

  it('shows a voltage with at most two decimals', () => {
    expect(volts(3.5)).toBe('3.5');
    expect(volts(2.125)).toBe('2.13');
    expect(volts(12)).toBe('12');
  });
});

describe('speedPassEndedMessage', () => {
  it('names the reason a failed pass gives on the command-result stream', () => {
    expect(
      speedPassEndedMessage({ success: false, reason: 'train overran the stretch past the far end' })
    ).toBe('The pass ended: Train overran the stretch past the far end. Nothing was written.');
  });

  it('falls back to the plain line with no failure to name', () => {
    expect(speedPassEndedMessage(null)).toBe('The pass ended. Nothing was written.');
  });

  it('says what a run that ended by writing wrote', () => {
    expect(speedPassEndedMessage({ success: true, reason: 'committed 2.64 V as the ATO minimum voltage' })).toBe(
      'Committed 2.64 V as the ATO minimum voltage.'
    );
  });
});

describe('crossingRows', () => {
  // design_new.md "Speed calibration", The panel lists every crossing.
  const run = {
    block_names: { BLK_L: 'Left', BLK_R: 'Right' },
    crossings: [
      { track_voltage: 12, attempt: 1, toward_block_id: 'BLK_L', seconds: 2.62, speed_kmh: 110.4, outcome: 'used' },
      { track_voltage: 12, attempt: 1, toward_block_id: 'BLK_R', seconds: 1.99, speed_kmh: 144.7, outcome: 'discarded' },
      { track_voltage: 2, attempt: 1, toward_block_id: 'BLK_L', seconds: 3.1, speed_kmh: 5.2, outcome: 'dropped' },
      { track_voltage: 2, attempt: 1, toward_block_id: 'BLK_R', seconds: null, speed_kmh: null, outcome: 'failed_step' },
    ],
  };

  it('names the block each crossing ran toward and formats its time and speed', () => {
    const rows = crossingRows(run);
    expect(rows.map((r) => r.toward)).toEqual(['Left', 'Right', 'Left', 'Right']);
    expect(rows[0]).toMatchObject({ voltage: 12, seconds: '2.62', speed: '110.4', used: true, note: '' });
  });

  it('marks the first crossing of each step and says why a crossing does not count', () => {
    const rows = crossingRows(run);
    expect(rows.map((r) => r.firstOfStep)).toEqual([true, false, true, false]);
    expect(rows[1].used).toBe(false);
    expect(rows[1].note).toContain('discarded');
    expect(rows[2].note).toContain('dropped');
    expect(rows[3]).toMatchObject({ voltage: 2, seconds: '—', speed: '—', used: false });
    expect(rows[3].note).toContain('failed its step');
  });

  it('falls back to the block id when the run carries no name', () => {
    expect(crossingRows({ crossings: [run.crossings[0]] })[0].toward).toBe('BLK_L');
    expect(crossingRows({})).toEqual([]);
  });
});


describe('speedPassProgress', () => {
  const names = { BLK_R: 'Right', BLK_U: 'Up' };

  it('names the outer block the pass is moving the train into', () => {
    const run = { phase: 'moving', move_target_block_id: 'BLK_R', block_names: names };
    expect(speedPassProgress(run, false)).toBe(
      'Moving the train into Right before the first crossing'
    );
  });

  it('says the way is being found before the move has a target', () => {
    expect(speedPassProgress({ phase: 'moving', move_target_block_id: '' }, false)).toBe(
      'Finding the way into the nearer outer block'
    );
  });

  it('shows the step voltage, its part of the sweep, the attempt and the crossing while measuring', () => {
    const run = {
      phase: 'leg',
      current_voltage: 8,
      current_step_kind: 'coarse',
      attempt: 1,
      crossings_done: 1,
      crossings_target: 4,
    };
    expect(speedPassProgress(run, false)).toBe('Measuring 8 V (coarse step), attempt 1 — crossing 2 of 4');
    expect(
      speedPassProgress({ ...run, current_voltage: 3.25, current_step_kind: 'narrowing', attempt: 2 }, false)
    ).toBe('Measuring 3.25 V (narrowing in on the lowest voltage that runs every crossing), attempt 2 — crossing 2 of 4');
    expect(speedPassProgress({ ...run, current_step_kind: 'filled', crossings_done: 4 }, false)).toBe(
      'Measuring 8 V (in-between step, where the speed jumps), attempt 1 — crossing 4 of 4'
    );
  });

  it('says the train is being got ready after a failed step', () => {
    expect(speedPassProgress({ phase: 'resetting' }, false)).toBe(
      'Getting the train ready for the next step'
    );
  });

  it('names the measured block and the lowest crossing voltage once the result is held', () => {
    const run = {
      samples: [
        { track_voltage: 12, speed_kmh: 90 },
        { track_voltage: 3.5, speed_kmh: 11.04 },
        { track_voltage: 8, speed_kmh: 50 },
      ],
      measured_block_id: 'BLK_U',
      block_names: names,
    };
    expect(speedPassProgress(run, true)).toBe(
      'Measured 3 points across Up. The lowest voltage that ran every crossing is 3.5 V, at 11.0 km/h.'
    );
  });
});

describe('the two ends of the stretch', () => {
  const stretch = {
    outer_block_ids: ['R', 'L'],
    runoff_block_ids: [['M'], []],
    end_lengths_cm: [183.6, 99.6],
    train_length_cm: 45,
    length_cm: 84,
    block_names: { R: 'Right', L: 'Left', M: 'Main' },
  };

  it('lists every block of both ends, outer block first', () => {
    expect(stretchEndBlocks(stretch)).toEqual(['R', 'M', 'L']);
    expect(stretchEndBlocks({ outer_block_ids: ['R', 'L'] })).toEqual(['R', 'L']);
  });

  it('names each end with its length and the train length the ends were sized for', () => {
    expect(stretchEndsText(stretch)).toBe(
      'Ends: Right → Main (184 cm) ↔ Left (100 cm) · sized for a 45 cm train · measured length 84 cm'
    );
  });
});
