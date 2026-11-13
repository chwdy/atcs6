/**
 * The seven steps of the Hardware Setup screen, and which of them are done.
 *
 * The steps are design_new.md "Operator workflows" → "Setting up the hardware
 * and going live", steps 1 to 7. Step 8 (Play and Pause) is the top bar's job.
 * This file decides only what the left column shows; it sends nothing.
 */
import type { SessionStateView } from '../../lib/session-controls';

export type StepId = 1 | 2 | 3 | 4 | 5 | 6 | 7;

export interface SetupStep {
  id: StepId;
  title: string;
}

export const SETUP_STEPS: readonly SetupStep[] = [
  { id: 1, title: 'Choose the environment' },
  { id: 2, title: 'Build the setup' },
  { id: 3, title: 'Enter Setup' },
  { id: 4, title: 'Prove each chip and each wire' },
  { id: 5, title: 'Close each tool' },
  { id: 6, title: 'Check readiness' },
  { id: 7, title: 'Activate' },
];

/** `done` gets a tick; `skipped` is a step Simulation does not need. */
export type StepMark = 'done' | 'open';

/** What the marks are worked out from. */
export interface StepFacts {
  /** The selected environment; null before the setup has loaded. */
  selected: string | null;
  /** The active environment, from the network snapshot. */
  active: string | null;
  session: SessionStateView | null;
  chipCount: number;
  unprovenCount: number;
  /** An edit the saved setup does not have yet. */
  unsaved: boolean;
  /** Every block and switch in the layout is bound; null when unknown. */
  coverageComplete: boolean | null;
  /** A Setup tool holds the outputs: a chip test or the occupancy bench. */
  toolOpen: boolean;
  /** The last readiness report had no blocker; null before it loaded. */
  ready: boolean | null;
}

/**
 * Whether the selected environment is the one running, and Live. Activate is
 * the only way out of Setup, so once this holds, the steps that lead to
 * activation count as done.
 */
export function selectedIsRunning(f: StepFacts): boolean {
  return f.selected != null && f.active === f.selected && f.session?.value === 'live';
}

export function stepMarks(f: StepFacts): Record<StepId, StepMark> {
  const running = selectedIsRunning(f);
  const inSetup = f.session?.value === 'setup';
  const mark = (done: boolean): StepMark => (done ? 'done' : 'open');
  return {
    1: mark(f.selected != null),
    2: mark(f.chipCount > 0 && !f.unsaved && f.coverageComplete === true),
    3: mark(inSetup || running),
    4: mark((f.chipCount > 0 && f.unprovenCount === 0) || running),
    5: mark((inSetup && !f.toolOpen) || running),
    6: mark(f.ready === true),
    7: mark(running),
  };
}

/** The first step that is not done, or 7 when all are. */
export function firstOpenStep(marks: Record<StepId, StepMark>): StepId {
  return SETUP_STEPS.find((s) => marks[s.id] === 'open')?.id ?? 7;
}
