import { describe, expect, it } from 'vitest';

import { firstOpenStep, stepMarks, type StepFacts } from './steps';

function facts(over: Partial<StepFacts> = {}): StepFacts {
  return {
    selected: 'virtual_hardware',
    active: 'simulation',
    session: { value: 'halted', halt_reason: 'not_activated' },
    chipCount: 2,
    unprovenCount: 2,
    unsaved: false,
    coverageComplete: true,
    toolOpen: false,
    ready: false,
    ...over,
  };
}

describe('stepMarks', () => {
  it('skips steps 2 to 6 in Simulation', () => {
    const marks = stepMarks(facts({ selected: 'simulation', session: { value: 'paused', halt_reason: null } }));
    expect(marks).toEqual({
      1: 'done',
      2: 'skipped',
      3: 'skipped',
      4: 'skipped',
      5: 'skipped',
      6: 'skipped',
      7: 'done',
    });
  });

  it('counts the setup as built only when it is saved and binds the whole layout', () => {
    expect(stepMarks(facts())[2]).toBe('done');
    expect(stepMarks(facts({ unsaved: true }))[2]).toBe('open');
    expect(stepMarks(facts({ coverageComplete: false }))[2]).toBe('open');
    expect(stepMarks(facts({ coverageComplete: null }))[2]).toBe('open');
    expect(stepMarks(facts({ chipCount: 0 }))[2]).toBe('open');
  });

  it('in Setup, marks the steps that are true now', () => {
    const inSetup = { session: { value: 'setup', halt_reason: null } };
    expect(stepMarks(facts(inSetup))).toMatchObject({ 3: 'done', 4: 'open', 5: 'done', 6: 'open', 7: 'open' });
    expect(stepMarks(facts({ ...inSetup, toolOpen: true }))[5]).toBe('open');
    expect(stepMarks(facts({ ...inSetup, unprovenCount: 0 }))[4]).toBe('done');
    expect(stepMarks(facts({ ...inSetup, ready: true }))[6]).toBe('done');
  });

  it('once the selected environment runs, the steps that lead to it are done', () => {
    const marks = stepMarks(
      facts({ active: 'virtual_hardware', session: { value: 'live', halt_reason: null }, ready: true })
    );
    expect(marks).toMatchObject({ 3: 'done', 4: 'done', 5: 'done', 6: 'done', 7: 'done' });
  });

  it('does not count another environment running as activated', () => {
    expect(stepMarks(facts({ session: { value: 'paused', halt_reason: null } }))[7]).toBe('open');
  });
});

describe('firstOpenStep', () => {
  it('is the first step neither done nor skipped', () => {
    expect(firstOpenStep(stepMarks(facts({ chipCount: 0 })))).toBe(2);
    expect(firstOpenStep(stepMarks(facts()))).toBe(3);
  });

  it('is Activate when every step is done', () => {
    const marks = stepMarks(facts({ selected: 'simulation', active: 'simulation', session: { value: 'live', halt_reason: null } }));
    expect(firstOpenStep(marks)).toBe(7);
  });
});
