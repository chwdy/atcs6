/**
 * The Hardware Setup screen (docs/ui-layout-rewrite-plan.md §7).
 *
 * A full screen in place of today's Hardware Setup dialog. The left column
 * lists design_new.md "Operator workflows" → "Setting up the hardware and
 * going live", steps 1 to 7, with a tick on each step that is done. The right
 * side shows the step picked.
 *
 * Steps 1, 3, 5, 6 and 7 are written here. Steps 2 and 4 show the dialog's
 * own sections (the bridge, the chip list, the layout tally, the idle
 * threshold and the wiring overview), which ui/hardware-panel.ts still draws
 * and whose buttons it still runs; `Hosted` below is the box each one is drawn
 * into. Step 4 also holds the Occupancy Bench and the Virtual Bench, which used
 * to be floating windows.
 */
import { signal } from '@preact/signals';
import { render, type ComponentChildren } from 'preact';
import { useEffect, useLayoutEffect, useRef, useState } from 'preact/hooks';

import { api } from '../../lib/api';
import {
  activateUnavailableReason,
  environmentLabel,
  sessionLabel,
} from '../../lib/session-controls';
import { mountOccupancyBench } from '../../panels/occupancy-bench';
import { mountVirtualBench } from '../../panels/virtual-bench';
import {
  activateFromHardwareSetup,
  attachHardwareScreen,
  enterHardwareSetup,
  hardwareSectionHtml,
  hardwareSetupFacts,
  recordChipProof,
  reloadHardwareEnvironment,
  runSelfTest,
  saveHardwareSetup,
  selectHardwareEnvironment,
  stopHardwareTests,
  type ChipListOptions,
  type HardwareSection,
  type HardwareSetupFacts,
  type ReadinessNote,
} from '../../ui/hardware-panel';
import { operationEnvironment, screen, sessionState, type Screen } from '../state';
import { runRigTest, type RigTestCalls, type RigTestReport } from './rig-test';
import { hardwareStep } from './state';
import {
  SETUP_STEPS,
  firstOpenStep,
  selectedIsRunning,
  stepMarks,
  type StepFacts,
  type StepId,
  type StepMark,
} from './steps';

/** What the screen asks App to do. */
export interface HardwareScreenActions {
  /** The top bar's Activate (`App.activateEnvironment`). */
  activate(): Promise<void>;
  showScreen(next: Screen): void;
}

// --- the dialog's sections, drawn into the screen ---------------------------

/** Bumped after hardware-panel.ts redraws, so the step list reads its facts
 *  again. */
const panelVersion = signal(0);

/** Whether the Occupancy Bench in step 4 holds the controller. */
const benchOpen = signal(false);

/** The Occupancy Bench on screen in step 4, so Test the rig can close it
 *  before it opens the bench itself. */
let benchOnScreen: { close(): Promise<void> } | null = null;

interface HostedBox {
  el: HTMLElement;
  name: HardwareSection;
  chipList?: ChipListOptions;
}

const hostedBoxes = new Set<HostedBox>();

function drawBox(box: HostedBox): void {
  box.el.innerHTML = hardwareSectionHtml(box.name, box.chipList);
}

/** hardware-panel.ts calls this after every change. */
function drawHostedSections(): void {
  hostedBoxes.forEach(drawBox);
  panelVersion.value++;
}

/**
 * A box hardware-panel.ts draws one of its sections into. Preact renders the
 * box with no children, so it never touches what the panel puts inside.
 */
function Hosted({ name, chipList }: { name: HardwareSection; chipList?: ChipListOptions }) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const box: HostedBox = { el: ref.current!, name, chipList };
    hostedBoxes.add(box);
    drawBox(box);
    return () => {
      hostedBoxes.delete(box);
    };
  }, [name, chipList?.edit, chipList?.test]);
  return <div class={`nhs-hosted nhs-hosted-${name}`} ref={ref} />;
}

// --- the facts the steps are marked from ------------------------------------

interface ScreenFacts {
  panel: HardwareSetupFacts;
  facts: StepFacts;
  marks: Record<StepId, StepMark>;
}

function readFacts(): ScreenFacts {
  void panelVersion.value; // re-read after every panel redraw
  const panel = hardwareSetupFacts();
  const facts: StepFacts = {
    selected: panel.loaded ? panel.selected : null,
    active: operationEnvironment.value,
    session: sessionState.value,
    chipCount: panel.chipCount,
    unprovenCount: panel.unprovenCount,
    unsaved: panel.unsaved,
    coverageComplete: panel.coverageComplete,
    toolOpen: panel.commissioning || benchOpen.value,
    ready: panel.loaded ? panel.readiness.ready : null,
  };
  return { panel, facts, marks: stepMarks(facts) };
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** The one line under each step's title in the left column. */
function stepStatus(id: StepId, { panel, facts, marks }: ScreenFacts): string {
  if (marks[id] === 'skipped') return 'Not needed in Simulation';
  const inSetup = facts.session?.value === 'setup';
  switch (id) {
    case 1:
      return facts.selected ? `${environmentLabel(facts.selected)} selected` : 'Loading…';
    case 2:
      if (!facts.chipCount) return 'No chips yet';
      if (facts.unsaved) return `${plural(facts.chipCount, 'chip')} · unsaved changes`;
      if (facts.coverageComplete === false) return `${plural(facts.chipCount, 'chip')} · layout not fully bound`;
      return `${plural(facts.chipCount, 'chip')} · saved`;
    case 3:
      return inSetup ? 'In Setup' : sessionLabel(facts.session);
    case 4:
      if (!facts.chipCount) return 'No chips to prove';
      // Activation loads the setup again, which starts the proof over; the
      // step is ticked because the environment it leads to is running.
      if (facts.unprovenCount && selectedIsRunning(facts)) return 'Environment active';
      return `${facts.chipCount - facts.unprovenCount} of ${plural(facts.chipCount, 'chip')} proven`;
    case 5:
      if (facts.toolOpen) return 'A tool holds the outputs';
      return inSetup || selectedIsRunning(facts) ? 'No tool open' : 'Tools run only in Setup';
    case 6:
      if (facts.ready === null) return 'Not read yet';
      return panel.readiness.issues.length
        ? plural(panel.readiness.issues.length, 'blocker')
        : 'No blockers';
    case 7:
      return selectedIsRunning(facts)
        ? sessionLabel(facts.session)
        : `${environmentLabel(facts.selected)} not active`;
  }
}

/** The step after `id` that Simulation does not skip. */
function nextStepAfter(id: StepId, marks: Record<StepId, StepMark>): StepId | null {
  const later = SETUP_STEPS.filter((s) => s.id > id && marks[s.id] !== 'skipped');
  return later[0]?.id ?? null;
}

// --- the screen ---------------------------------------------------------------

export function HardwareSetupScreen({ actions }: { actions: HardwareScreenActions }) {
  const rootRef = useRef<HTMLDivElement>(null);
  const mountVersion = useRef(panelVersion.peek());
  const seenSession = useRef<string | null>(null);

  // Show the dialog's sections here while the screen is up.
  useEffect(() => attachHardwareScreen(rootRef.current!, drawHostedSections), []);

  const sf = readFacts();
  const current: StepId = hardwareStep.value ?? firstOpenStep(sf.marks);

  // Keep the first open step once the setup has loaded, so the step on screen
  // does not move while the operator works in it.
  useEffect(() => {
    if (hardwareStep.peek() === null && panelVersion.peek() > mountVersion.current) {
      hardwareStep.value = current;
    }
  });

  // The session state also changes from the top bar (Activate, Emergency
  // stop). Read the environment again so the hosted chip tests are offered
  // only in Setup.
  const session = sessionState.value;
  const sessionKey = `${session?.value}/${session?.halt_reason}`;
  useEffect(() => {
    if (seenSession.current !== null && seenSession.current !== sessionKey) {
      void reloadHardwareEnvironment();
    }
    seenSession.current = sessionKey;
  }, [sessionKey]);

  const goTo = (id: StepId) => {
    hardwareStep.value = id;
  };

  return (
    <div class="nhs" ref={rootRef}>
      <nav class="nhs-rail" aria-label="Hardware Setup steps">
        <div class="nhs-rail-head">Hardware Setup</div>
        <ol class="nhs-steps">
          {SETUP_STEPS.map((s) => {
            const mark = sf.marks[s.id];
            const status = stepStatus(s.id, sf);
            return (
              <li key={s.id}>
                <button
                  type="button"
                  class={current === s.id ? 'nhs-step row-selected' : 'nhs-step'}
                  data-mark={mark}
                  aria-current={current === s.id ? 'step' : undefined}
                  aria-label={`Step ${s.id}, ${s.title}: ${mark === 'done' ? 'done' : status}`}
                  onClick={() => goTo(s.id)}
                >
                  <span class="nhs-step-mark" aria-hidden="true">
                    {mark === 'done' ? <i class="ti ti-check" /> : mark === 'skipped' ? '–' : s.id}
                  </span>
                  <span class="nhs-step-text">
                    <span class="nhs-step-title">{s.title}</span>
                    <span class="nhs-step-status">{status}</span>
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      </nav>
      <section class="nhs-body">
        <StepBody id={current} sf={sf} goTo={goTo} actions={actions} />
      </section>
    </div>
  );
}

interface StepProps {
  sf: ScreenFacts;
  goTo(id: StepId): void;
  actions: HardwareScreenActions;
}

function StepBody({ id, ...props }: StepProps & { id: StepId }) {
  switch (id) {
    case 1: return <ChooseEnvironment {...props} />;
    case 2: return <BuildSetup {...props} />;
    case 3: return <EnterSetup {...props} />;
    case 4: return <ProveChips {...props} />;
    case 5: return <CloseTools {...props} />;
    case 6: return <CheckReadiness {...props} />;
    case 7: return <Activate {...props} />;
  }
}

/** The frame every step shares: number and title, a plain description, the
 *  step's content, and a button to the next step. */
function StepFrame({
  id,
  sf,
  goTo,
  intro,
  children,
}: {
  id: StepId;
  sf: ScreenFacts;
  goTo(id: StepId): void;
  intro: ComponentChildren;
  children?: ComponentChildren;
}) {
  const step = SETUP_STEPS.find((s) => s.id === id)!;
  const next = nextStepAfter(id, sf.marks);
  const nextTitle = next ? SETUP_STEPS.find((s) => s.id === next)!.title : null;
  return (
    <div class={`nhs-step-body nhs-step-${id}`}>
      <header class="nhs-step-head">
        <span class="nhs-step-num">{id}</span>
        <h2>{step.title}</h2>
        {sf.marks[id] === 'done' && (
          <span class="nhs-done-chip">
            <i class="ti ti-check" aria-hidden="true" /> Done
          </span>
        )}
      </header>
      {sf.marks[id] === 'skipped' && (
        <p class="nhs-note nhs-note-info">
          Simulation never reads the Hardware Setup, so it skips this step. You can still do it
          now for Virtual Hardware or Real Hardware.
        </p>
      )}
      <div class="nhs-intro">{intro}</div>
      {children}
      {next && nextTitle && (
        <footer class="nhs-step-foot">
          <button type="button" class="btn btn-outline" onClick={() => goTo(next)}>
            Next: {nextTitle} <i class="ti ti-arrow-right" aria-hidden="true" />
          </button>
        </footer>
      )}
    </div>
  );
}

/** Run one async action at a time from a step's buttons. */
function useBusy(): [boolean, (run: () => Promise<void>) => Promise<void>] {
  const [busy, setBusy] = useState(false);
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    try {
      await fn();
    } finally {
      setBusy(false);
    }
  };
  return [busy, run];
}

// --- step 1 -------------------------------------------------------------------

const ENVIRONMENTS: { id: string; icon: string; text: string }[] = [
  {
    id: 'simulation',
    icon: 'ti ti-device-desktop',
    text: 'Trains run inside the app. It never reads the Hardware Setup, so steps 2 to 6 are skipped.',
  },
  {
    id: 'virtual_hardware',
    icon: 'ti ti-flask',
    text: 'The real chip drivers and safety checks run against a fake device, so every step can be tried without a rig.',
  },
  {
    id: 'real_hardware',
    icon: 'ti ti-cpu',
    text: 'The app drives the rig on your layout. It uses the same setup as Virtual Hardware.',
  },
];

function ChooseEnvironment(props: StepProps) {
  const { sf } = props;
  const [busy, run] = useBusy();
  const selected = sf.facts.selected;
  const blockers = sf.panel.readiness.issues.length;
  return (
    <StepFrame
      id={1}
      {...props}
      intro={
        <p>
          Pick where this session runs. Selecting does not activate anything and does not change
          the session state; Activate (step 7) starts the selected environment.
        </p>
      }
    >
      <div class="nhs-choices" role="radiogroup" aria-label="Environment">
        {ENVIRONMENTS.map((env) => (
          <button
            key={env.id}
            type="button"
            role="radio"
            aria-checked={selected === env.id}
            class="nhs-choice"
            disabled={busy || !sf.panel.loaded}
            onClick={() => selected !== env.id && void run(() => selectHardwareEnvironment(env.id))}
          >
            <i class={env.icon} aria-hidden="true" />
            <span class="nhs-choice-text">
              <span class="nhs-choice-title">{environmentLabel(env.id)}</span>
              <span class="nhs-choice-desc">{env.text}</span>
            </span>
          </button>
        ))}
      </div>
      <p class="nhs-line">
        Running now: <strong>{environmentLabel(sf.facts.active) || '—'}</strong>,{' '}
        {sessionLabel(sf.facts.session)}.
      </p>
      {selected !== 'simulation' && blockers > 0 && (
        <p class="nhs-note nhs-note-warn">
          {plural(blockers, 'readiness blocker')} would stop activation.{' '}
          <button type="button" class="nhs-link" onClick={() => props.goTo(6)}>
            Step 6 lists them.
          </button>
        </p>
      )}
    </StepFrame>
  );
}

// --- step 2 -------------------------------------------------------------------

const EDIT_CHIPS: ChipListOptions = { edit: true, test: false };
const TEST_CHIPS: ChipListOptions = { edit: false, test: true };

function BuildSetup(props: StepProps) {
  const { sf } = props;
  const [busy, run] = useBusy();
  return (
    <StepFrame
      id={2}
      {...props}
      intro={
        <p>
          Describe the rig that Virtual Hardware and Real Hardware share: the bridge, every chip
          on the board, and the idle threshold. Each block and switch is bound to a chip port in
          its own panel on the Editor screen. Saving checks each field and accepts a setup that
          is not finished yet; only activation needs it complete.
        </p>
      }
    >
      <Hosted name="controller" />
      <Hosted name="chips" chipList={EDIT_CHIPS} />
      <Hosted name="coverage" />
      <div class="nhs-save-row">
        <Hosted name="idle-threshold" />
        <span class="nhs-save-state" data-unsaved={sf.panel.unsaved ? 'true' : 'false'}>
          {sf.panel.unsaved ? 'Unsaved changes' : 'Saved'}
        </span>
        <button
          type="button"
          class="btn btn-accent"
          disabled={busy || !sf.panel.loaded}
          onClick={() => void run(saveHardwareSetup)}
        >
          <i class="ti ti-device-floppy" aria-hidden="true" /> Save Hardware Setup
        </button>
      </div>
      <Hosted name="wiring" />
    </StepFrame>
  );
}

// --- step 3 -------------------------------------------------------------------

function enterSetupRefusal(facts: StepFacts): string | null {
  if (facts.selected === 'simulation') return 'Simulation has no rig, so it has no Setup.';
  if (facts.session?.value === 'halted' && facts.session.halt_reason === 'runtime_failure') {
    return 'Clear the track conflict first.';
  }
  return null;
}

function EnterSetup(props: StepProps) {
  const { sf } = props;
  const [busy, run] = useBusy();
  const inSetup = sf.facts.session?.value === 'setup';
  const refusal = enterSetupRefusal(sf.facts);
  const live = sf.facts.session?.value === 'live' || sf.facts.session?.value === 'paused';
  return (
    <StepFrame
      id={3}
      {...props}
      intro={
        <>
          <p>
            Setup hands the outputs to the Setup tools, so each chip and wire can be tested. The
            outputs go to the safe state, the selected environment stops running, and the
            session reads Setup. No train moves in Setup unless a Setup tool moves it on purpose.
          </p>
          <p>
            The detectors keep being read when Setup was entered from a running hardware session;
            otherwise they read nothing until an activation succeeds.
          </p>
        </>
      }
    >
      {inSetup ? (
        <p class="nhs-note nhs-note-ok">
          <i class="ti ti-check" aria-hidden="true" /> The session is in Setup. The Setup tools own
          the outputs.
        </p>
      ) : (
        <div class="nhs-action-row">
          <button
            type="button"
            class="btn btn-accent"
            disabled={busy || !!refusal || !sf.panel.loaded}
            title={refusal ?? 'Stop operation and hand the outputs to the Setup tools'}
            onClick={() => void run(enterHardwareSetup)}
          >
            Enter Setup
          </button>
          {refusal && <span class="nhs-reason">{refusal}</span>}
          {!refusal && live && (
            <span class="nhs-reason">
              From {sessionLabel(sf.facts.session)}, this runs Emergency stop first: every train
              stops and gives up its reservation.
            </span>
          )}
        </div>
      )}
    </StepFrame>
  );
}

// --- step 4 -------------------------------------------------------------------

function ProveChips(props: StepProps) {
  const { sf } = props;
  const inSetup = sf.facts.session?.value === 'setup';
  const [occupancyOpen, setOccupancyOpen] = useState(false);
  const [virtualOpen, setVirtualOpen] = useState(false);

  // Moving on from this step gives the outputs back, as closing the dialog did.
  useEffect(() => () => void stopHardwareTests(), []);

  return (
    <StepFrame
      id={4}
      {...props}
      intro={
        <p>
          Test the rig runs every check that needs no one to watch it. An I²C chip proves itself
          by answering the bus scan, and an hc165 by its bits following the drive when each block
          is driven for a moment. Nothing reads an hc595 back, so watch the switches move and
          press It worked on its card. The tests that drive outputs run only in Setup.
        </p>
      }
    >
      {!inSetup && sf.marks[4] !== 'skipped' && (
        <p class="nhs-note nhs-note-warn">
          The session is not in Setup, so the tests that drive outputs are off.{' '}
          <button type="button" class="nhs-link" onClick={() => props.goTo(3)}>
            Enter Setup in step 3.
          </button>
        </p>
      )}
      <RigTest
        sf={sf}
        inSetup={inSetup}
        beforeRun={async () => {
          // The run opens the bench itself, so the one on screen closes first.
          await benchOnScreen?.close();
          setOccupancyOpen(false);
        }}
      />
      <Hosted name="controller" />
      <Hosted name="chips" chipList={TEST_CHIPS} />

      <details
        class="tc-disclosure nhs-tool"
        open={occupancyOpen}
        onToggle={(e) => setOccupancyOpen((e.currentTarget as HTMLDetailsElement).open)}
      >
        <summary
          onClick={(e) => {
            // The bench would take the outputs from the running Test the rig.
            if (rigRunning.value) e.preventDefault();
          }}
        >
          Occupancy Bench
        </summary>
        <div class="tc-disclosure-body">
          <p class="nhs-tool-desc">
            Holds every bound block at one voltage and shows both detectors for each block side by
            side, to set the detector calibration. Opening it takes the outputs; folding it closes
            it.
          </p>
          {occupancyOpen && <OccupancyBenchBox />}
        </div>
      </details>

      {sf.facts.selected === 'virtual_hardware' && (
        <details
          class="tc-disclosure nhs-tool"
          open={virtualOpen}
          onToggle={(e) => setVirtualOpen((e.currentTarget as HTMLDetailsElement).open)}
        >
          <summary>Virtual Bench</summary>
          <div class="tc-disclosure-body">
            <p class="nhs-tool-desc">
              Shows what the app drives onto each block of the fake device, and lets you tell the
              app what its detectors see.
            </p>
            {virtualOpen && <VirtualBenchBox />}
          </div>
        </details>
      )}

      <p class="nhs-aside">
        The track direction sweep, the switch throw test and the block occupancy test are in each
        block's and switch's panel on the Editor screen.
      </p>
      <Hosted name="wiring" />
    </StepFrame>
  );
}

// --- Test the rig -------------------------------------------------------------

/** What Test the rig is doing now; null when it is not running. */
const rigProgress = signal<string | null>(null);
const rigRunning = signal(false);
/** The last run's summary, kept while the app is open. */
const rigReport = signal<RigTestReport | null>(null);

/** The real calls Test the rig makes. */
function rigTestCalls(): RigTestCalls {
  // The switch throw test posts the setup it throws with: the saved one.
  let saved: unknown = null;
  return {
    async readSetup() {
      const [res, switches] = await Promise.all([api.getHardwareConfig(), api.getSwitches()]);
      const config = res?.config;
      if (!config) throw new Error('the server sent no setup');
      saved = config;
      const switchPositions: Record<string, number> = {};
      const unavailableMotors: string[] = [];
      for (const sw of Array.isArray(switches) ? switches : []) {
        switchPositions[sw.switch_id] = sw.position;
        if (sw.motor_unavailable) unavailableMotors.push(sw.switch_id);
      }
      return {
        setup: {
          chips: config.chips || [],
          block_track: config.block_track || {},
          block_occupancy: config.block_occupancy || {},
          switch_output: config.switch_output || {},
        },
        switchPositions,
        unavailableMotors,
      };
    },
    selfTest: runSelfTest,
    openBench: () => api.openOccupancyBench(),
    checkBlock: (blockId) => api.checkOccupancyBenchBlock(blockId),
    closeBench: async () => {
      await api.closeOccupancyBench();
    },
    throwSwitch: (switchId, position) => api.switchThrowTest(saved, switchId, position),
    releaseSwitches: async () => {
      await api.commissionClose();
    },
    pause: (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    progress: (text) => {
      rigProgress.value = text || null;
    },
  };
}

/** Step 4's one button: every check that needs no one watching, then one
 *  summary (design_new.md "Hardware setup", Test the rig). */
function RigTest({
  sf,
  inSetup,
  beforeRun,
}: {
  sf: ScreenFacts;
  inSetup: boolean;
  beforeRun(): Promise<void>;
}) {
  const running = rigRunning.value;
  const report = rigReport.value;
  const unsaved = sf.panel.unsaved;
  const start = async () => {
    rigRunning.value = true;
    rigReport.value = null;
    try {
      await beforeRun();
      await stopHardwareTests();
      const result = await runRigTest(rigTestCalls());
      recordChipProof(result.chipNotes);
      rigReport.value = result;
    } finally {
      rigRunning.value = false;
      rigProgress.value = null;
    }
  };
  return (
    <section class="nhs-rig-test">
      <div class="nhs-action-row">
        <button
          type="button"
          class="btn btn-accent"
          disabled={!inSetup || unsaved || running}
          onClick={() => void start()}
        >
          <i class={running ? 'ti ti-hourglass' : 'ti ti-player-play'} aria-hidden="true" />{' '}
          {running ? 'Testing…' : 'Test the rig'}
        </button>
        <span class="nhs-rig-status">
          {running
            ? rigProgress.value || 'Starting…'
            : unsaved && inSetup
              ? 'Save the setup in step 2 first: the run tests the saved setup.'
              : 'Scans the bus, drives each block for a moment to read its detectors, and throws each switch to its other position and back. It asks you nothing.'}
        </span>
      </div>
      {report && <RigSummary report={report} />}
    </section>
  );
}

function RigSummary({ report }: { report: RigTestReport }) {
  const groups: { title: string; tone: string; icon: string; items: string[] }[] = [
    { title: 'Failed', tone: 'danger', icon: 'ti ti-x', items: report.failed },
    { title: 'Still needs you', tone: 'warn', icon: 'ti ti-hand-finger', items: report.needsYou },
    { title: 'Proved', tone: 'ok', icon: 'ti ti-check', items: report.proved },
  ];
  const headline = report.failed.length
    ? `${report.failed.length} check${report.failed.length === 1 ? '' : 's'} failed.`
    : report.needsYou.length
      ? 'Nothing failed. Some checks still need you.'
      : 'Every check passed.';
  return (
    <div class="nhs-rig-summary" aria-live="polite">
      <p class="nhs-rig-headline">{headline}</p>
      {groups
        .filter((g) => g.items.length)
        .map((g) => (
          <div key={g.title} class="nhs-rig-group">
            <h3>{g.title}</h3>
            <ul class="nhs-rows">
              {g.items.map((text, i) => (
                <li key={i} class="nhs-row" data-tone={g.tone}>
                  <i class={g.icon} aria-hidden="true" />
                  <span class="nhs-row-name">{text}</span>
                </li>
              ))}
            </ul>
          </div>
        ))}
    </div>
  );
}

/** The Occupancy Bench, open while this box is on screen. */
function OccupancyBenchBox() {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let bench: ReturnType<typeof mountOccupancyBench> | null = null;
    let gone = false;
    void (async () => {
      // One Setup tool at a time: a running chip test gives the outputs back.
      await stopHardwareTests();
      if (!gone && ref.current) bench = benchOnScreen = mountOccupancyBench(ref.current);
    })();
    // The bench says nothing when it opens or times out, so step 5's mark
    // asks it twice a second. This reads a field; it sends nothing.
    const watch = setInterval(() => {
      benchOpen.value = !!bench?.isOpen();
    }, 500);
    return () => {
      gone = true;
      clearInterval(watch);
      benchOpen.value = false;
      if (benchOnScreen === bench) benchOnScreen = null;
      void bench?.close();
    };
  }, []);
  return <div class="nhs-bench nhs-occupancy-bench" ref={ref} />;
}

/** The Virtual Bench, reading the fake device while this box is on screen. */
function VirtualBenchBox() {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => mountVirtualBench(ref.current!).close, []);
  return <div class="nhs-bench nhs-virtual-bench" ref={ref} />;
}

// --- step 5 -------------------------------------------------------------------

function CloseTools(props: StepProps) {
  const { sf } = props;
  const [busy, run] = useBusy();
  // The Occupancy Bench in step 4 closes when the step is left, so a bench
  // open now was opened somewhere else; ask the server.
  const [serverBench, setServerBench] = useState<boolean | null>(null);
  const readBench = async () => {
    try {
      const res = await api.getOccupancyBenchState();
      setServerBench(!!res?.open);
    } catch {
      setServerBench(null);
    }
  };
  useEffect(() => {
    void readBench();
  }, []);

  const tools = [
    {
      name: 'Chip tests (the commissioning session)',
      open: sf.panel.commissioning,
      close: stopHardwareTests,
    },
    {
      name: 'Occupancy Bench',
      open: serverBench === true,
      close: async () => {
        await api.closeOccupancyBench();
        await readBench();
      },
    },
  ];
  return (
    <StepFrame
      id={5}
      {...props}
      intro={
        <p>
          Close each Setup tool when you are done with it. However a tool ends (you close it, it
          times out, the link drops, or it fails), the outputs go back to the safe state and the
          session stays in Setup. Moving on from step 4, or leaving Hardware Setup, closes the chip
          tests and the Occupancy Bench.
        </p>
      }
    >
      <ul class="nhs-rows">
        {tools.map((tool) => (
          <li key={tool.name} class="nhs-row" data-tone={tool.open ? 'warn' : 'ok'}>
            <span class="nhs-row-name">{tool.name}</span>
            <span class="nhs-row-value">{tool.open ? 'Holds the outputs' : 'Closed'}</span>
            {tool.open && (
              <button type="button" class="btn btn-outline" disabled={busy} onClick={() => void run(tool.close)}>
                Close
              </button>
            )}
          </li>
        ))}
      </ul>
    </StepFrame>
  );
}

// --- step 6 -------------------------------------------------------------------

const NAMED_IDS = 8;

function NoteList({ notes, tone }: { notes: ReadinessNote[]; tone: 'warn' | 'info' }) {
  return (
    <ul class="nhs-rows">
      {notes.map((note, i) => {
        const ids = note.ids || [];
        const rest = ids.length - NAMED_IDS;
        return (
          <li key={`${note.code}-${i}`} class="nhs-row" data-tone={tone}>
            <i
              class={tone === 'warn' ? 'ti ti-alert-triangle' : 'ti ti-info-circle'}
              aria-hidden="true"
            />
            <span class="nhs-row-name">
              {note.message || note.code}
              {ids.length > 0 && (
                <span class="nhs-row-ids">
                  {ids.slice(0, NAMED_IDS).join(', ')}
                  {rest > 0 ? ` and ${rest} more` : ''}
                </span>
              )}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function CheckReadiness(props: StepProps) {
  const { sf } = props;
  const [busy, run] = useBusy();
  useEffect(() => {
    void reloadHardwareEnvironment();
  }, []);
  const { issues, warnings } = sf.panel.readiness;
  return (
    <StepFrame
      id={6}
      {...props}
      intro={
        <p>
          The readiness report lists everything that would stop activation: a missing binding, a
          binding to a missing or wrong chip, a bad or reused port, a binding to a deleted block or
          switch, a device that cannot be reached, and a placed train whose lighting voltage is not
          below the current-sense floor. Reading it activates nothing.
        </p>
      }
    >
      <div class="nhs-action-row">
        <button
          type="button"
          class="btn btn-outline"
          disabled={busy}
          onClick={() => void run(reloadHardwareEnvironment)}
        >
          <i class="ti ti-refresh" aria-hidden="true" /> Check again
        </button>
        <span class="nhs-reason">For {environmentLabel(sf.facts.selected)}.</span>
      </div>
      <h3 class="nhs-subhead">Blockers</h3>
      {issues.length ? (
        <NoteList notes={issues} tone="warn" />
      ) : (
        <p class="nhs-note nhs-note-ok">
          <i class="ti ti-check" aria-hidden="true" /> No blockers. Activation can go ahead.
        </p>
      )}
      {warnings.length > 0 && (
        <>
          <h3 class="nhs-subhead">Notes</h3>
          <p class="nhs-aside">These do not stop activation.</p>
          <NoteList notes={warnings} tone="info" />
        </>
      )}
      {sf.panel.awaitsActivation && (
        <p class="nhs-note nhs-note-info">
          The saved setup has changes the running session is not using yet. Activate to use them.
        </p>
      )}
    </StepFrame>
  );
}

// --- step 7 -------------------------------------------------------------------

function Activate(props: StepProps) {
  const { sf, actions } = props;
  const [busy, run] = useBusy();
  useEffect(() => {
    void reloadHardwareEnvironment();
  }, []);
  const running = selectedIsRunning(sf.facts);
  const label = environmentLabel(sf.facts.selected);
  const unavailable = activateUnavailableReason(sf.facts.session);
  const blockers = sf.facts.selected === 'simulation' ? 0 : sf.panel.readiness.issues.length;
  // An unsaved setup is saved first, as the dialog's Activate does; otherwise
  // this is the top bar's Activate.
  const activate = () => (sf.panel.unsaved ? activateFromHardwareSetup() : actions.activate());
  return (
    <StepFrame
      id={7}
      {...props}
      intro={
        <>
          <p>
            Activate builds the interfaces, checks the environment, and starts the railway over: it
            cancels every live path, ends every running route run, and powers every train off. It
            keeps the layout, the setup, every train where it stands, and every route with its
            trains. Activate is the only way out of Setup.
          </p>
          <p>Every train is off afterwards, so nothing moves until you start a route.</p>
        </>
      }
    >
      {running ? (
        <div class="nhs-action-row">
          <p class="nhs-note nhs-note-ok">
            <i class="ti ti-check" aria-hidden="true" /> {label} is active:{' '}
            {sessionLabel(sf.facts.session)}.
          </p>
          <button type="button" class="btn btn-accent" onClick={() => actions.showScreen('operate')}>
            Go to Operate <i class="ti ti-arrow-right" aria-hidden="true" />
          </button>
        </div>
      ) : (
        <>
          {blockers > 0 && (
            <p class="nhs-note nhs-note-warn">
              The last readiness report has {plural(blockers, 'blocker')}, so activation will be
              refused.{' '}
              <button type="button" class="nhs-link" onClick={() => props.goTo(6)}>
                Step 6 lists them.
              </button>
            </p>
          )}
          <div class="nhs-action-row">
            <button
              type="button"
              class="btn btn-primary nhs-activate"
              disabled={busy || !!unavailable || !sf.panel.loaded}
              title={unavailable ?? `Activate ${label}`}
              onClick={() => void run(activate)}
            >
              <i class="ti ti-power" aria-hidden="true" /> Activate {label}
            </button>
            {unavailable && <span class="nhs-reason">{unavailable}</span>}
            {!unavailable && sf.panel.unsaved && (
              <span class="nhs-reason">Activate saves your setup changes first.</span>
            )}
          </div>
        </>
      )}
    </StepFrame>
  );
}

// --- mounting -------------------------------------------------------------------

function HardwareSetupSlot({ actions }: { actions: HardwareScreenActions }) {
  return screen.value === 'hardware' ? <HardwareSetupScreen actions={actions} /> : null;
}

/** The benches live in step 4. The floating Virtual Bench, kept for its
 *  Place on canvas (docs/ui-layout-rewrite-plan.md §13.10), is closed if it
 *  was left open, so only one bench asks for the controller. */
function closeFloatingBenches(): void {
  window.virtualBench?.hide();
}

/** Mount the Hardware Setup screen over the page body; it draws itself only
 *  while the Hardware Setup tab is picked. */
export function mountHardwareSetup(actions: HardwareScreenActions): void {
  const body = document.querySelector('.app-body');
  if (!body) return;
  const slot = document.createElement('div');
  slot.className = 'next-hardware-slot';
  body.appendChild(slot);
  render(<HardwareSetupSlot actions={actions} />, slot);
  // The floating Virtual Bench starts itself when the page loads; wait until it has.
  setTimeout(closeFloatingBenches, 0);
}
