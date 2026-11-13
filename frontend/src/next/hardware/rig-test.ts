/**
 * Test the rig: the Hardware Setup screen's one-button run (design_v2.md
 * "Setup tools", Test the rig). In order it runs self-test, opens the
 * occupancy bench and runs the block check on every block with a rail
 * binding, closes the bench, and throws every bound switch to its other
 * position and back. It asks the operator nothing, a failed step does not
 * stop it, and it answers one summary: what it proved, what failed and why,
 * and what still needs the operator.
 *
 * The calls it makes are passed in (`RigTestCalls`), so it unit-tests without
 * a server; `rigTestCalls()` in screen.tsx gives the real ones.
 */
import type { ChipProofNote } from '../../ui/hardware-panel';

/** The parts of the saved setup the run reads (`GET /api/hardware/config`). */
export interface SavedSetup {
  chips: { chip_id: string; kind: string; bus?: string; i2c_address?: number | null }[];
  block_track: Record<string, unknown>;
  block_occupancy: Record<string, { chip_id: string; port: number }>;
  switch_output: Record<string, { chip_id: string }>;
}

/** What self-test found (`runSelfTest` in ui/hardware-panel.ts). */
export interface ScanAnswer {
  reachable: boolean;
  error?: string;
  /** Every I²C address that answered, as lowercase hex ("0x40"). */
  answered: string[];
}

/** The block check's answer (`POST /api/hardware/bench/check-block`). */
export interface BlockCheckAnswer {
  ok: boolean;
  error?: string;
  verdict?: 'wired' | 'crossed' | 'present_without_load' | 'no_response' | 'unbound';
  digital?: 'unbound' | 'present_at_rest' | 'followed_drive' | 'no_response';
  current?: 'unbound' | 'load' | 'none';
  others?: { block_id: string; changed: string[] }[];
}

export interface RigTestCalls {
  /** The saved setup, and each switch's position in the loaded layout. */
  readSetup(): Promise<{ setup: SavedSetup; switchPositions: Record<string, number>; unavailableMotors?: string[] }>;
  selfTest(): Promise<ScanAnswer>;
  openBench(): Promise<{ ok: boolean; error?: string }>;
  checkBlock(blockId: string): Promise<BlockCheckAnswer>;
  closeBench(): Promise<void>;
  /** Throw one switch (0 = main, 1 = diverge). */
  throwSwitch(switchId: string, position: 0 | 1): Promise<{ ok: boolean; error?: string }>;
  /** Give back the controller the switch throws took. */
  releaseSwitches(): Promise<void>;
  /** Wait between two throws, so the operator sees each one land. */
  pause(ms: number): Promise<void>;
  /** What the run is doing now, for the button's status line. */
  progress(text: string): void;
}

export interface RigTestReport {
  proved: string[];
  failed: string[];
  needsYou: string[];
  /** What the run learned about each shift register, for the chip cards. */
  chipNotes: ChipProofNote[];
}

/** How long a switch rests between its two throws. */
export const THROW_GAP_MS = 600;

const POSITION_NAME = ['main', 'diverge'];

function hex(addr: number): string {
  return '0x' + addr.toString(16).padStart(2, '0');
}

function errorText(e: unknown): string {
  return (e as { message?: string })?.message || 'The request failed.';
}

/** "A", "A and B", "A, B and C". */
export function listText(items: string[]): string {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/** One block check's line, and whether it counts against the block's
 *  `hc165`: a failure takes the proof back, an open question leaves it
 *  unproven. */
interface BlockOutcome {
  kind: 'proved' | 'failed' | 'needsYou';
  text: string;
  /** The block's bit followed the drive with no other block changing. */
  wired: boolean;
  /** The current channel had nothing to read. */
  currentUnchecked: boolean;
  /** Only the bit had anything to show, so the summary lists the block
   *  together with the others like it. */
  bitOnly?: boolean;
}

function judgeBlock(blockId: string, answer: BlockCheckAnswer, bit: string | null): BlockOutcome {
  const quiet = { wired: false, currentUnchecked: false };
  if (!answer.ok) return { kind: 'failed', text: `${blockId}: the check failed: ${answer.error || 'no reason given'}.`, ...quiet };
  const others = answer.others || [];
  switch (answer.verdict) {
    case 'wired': {
      const load = answer.current === 'load';
      const text =
        answer.digital === 'present_at_rest'
          ? `${blockId}: its bit read a train at rest, and its current channel read the train's load.`
          : load
            ? `${blockId}: its bit followed the drive, and its current channel read a load.`
            : `${blockId}: its bit followed the drive.`;
      const bitOnly = answer.digital === 'followed_drive' && !load;
      return { kind: 'proved', text, wired: true, currentUnchecked: answer.current === 'none', bitOnly };
    }
    case 'crossed': {
      const what = others.map((o) => `${o.block_id}'s ${listText(o.changed.map((c) => (c === 'digital' ? 'bit' : 'current channel')))}`);
      return {
        kind: 'failed',
        text: `${blockId}: driving it also changed ${listText(what)}, so a wire is on the wrong block's rails.`,
        ...quiet,
      };
    }
    case 'no_response':
      return {
        kind: 'failed',
        text: `${blockId}: its bit did not change while the block was driven. Check the wire from the block's detector to ${bit}.`,
        ...quiet,
      };
    case 'present_without_load':
      return {
        kind: 'needsYou',
        text: `${blockId}: its bit reads occupied at rest, but its current channel read no load. Take anything off the block, or check ${bit} for a short, and run again.`,
        ...quiet,
      };
    default:
      return { kind: 'needsYou', text: `${blockId}: it has no hc165 bit bound, so its detector was not checked.`, ...quiet };
  }
}

/** Run every check and answer the summary. Never throws: a step that fails
 *  is a line in the summary. */
export async function runRigTest(calls: RigTestCalls): Promise<RigTestReport> {
  const report: RigTestReport = { proved: [], failed: [], needsYou: [], chipNotes: [] };

  calls.progress('Reading the saved setup…');
  let setup: SavedSetup;
  let switchPositions: Record<string, number>;
  let unavailableMotors: string[];
  try {
    ({ setup, switchPositions, unavailableMotors = [] } = await calls.readSetup());
  } catch (e) {
    report.failed.push(`Could not read the saved setup: ${errorText(e)}.`);
    return report;
  }

  // 1. Self-test: every I²C chip answers the scan or it does not.
  calls.progress('Scanning the bus…');
  try {
    const scan = await calls.selfTest();
    if (!scan.reachable) {
      report.failed.push(`The controller did not answer${scan.error ? `: ${scan.error}` : ''}.`);
    } else {
      for (const chip of setup.chips) {
        if (chip.bus !== 'i2c' || chip.i2c_address == null) continue;
        const at = hex(chip.i2c_address);
        if (scan.answered.includes(at)) report.proved.push(`${chip.chip_id} answered at ${at}.`);
        else report.failed.push(`${chip.chip_id} did not answer at ${at}.`);
      }
    }
  } catch (e) {
    report.failed.push(`The bus scan failed: ${errorText(e)}.`);
  }

  // 2. The block check on every block with a rail binding.
  const railBlocks = Object.keys(setup.block_track).sort();
  const checked = new Map<string, BlockOutcome>();
  const unchecked: string[] = [];
  const bitOnly: string[] = [];
  if (railBlocks.length) {
    calls.progress('Opening the occupancy bench…');
    let open: { ok: boolean; error?: string };
    try {
      open = await calls.openBench();
    } catch (e) {
      open = { ok: false, error: errorText(e) };
    }
    if (!open.ok) {
      report.failed.push(`The occupancy bench did not open${open.error ? `: ${open.error}` : ''}, so no block was checked.`);
    } else {
      for (const [i, blockId] of railBlocks.entries()) {
        calls.progress(`Checking ${blockId} (${i + 1} of ${railBlocks.length})…`);
        const bitBinding = setup.block_occupancy[blockId];
        const bit = bitBinding ? `${bitBinding.chip_id} bit ${bitBinding.port}` : null;
        let answer: BlockCheckAnswer;
        try {
          answer = await calls.checkBlock(blockId);
        } catch (e) {
          answer = { ok: false, error: errorText(e) };
        }
        const outcome = judgeBlock(blockId, answer, bit);
        checked.set(blockId, outcome);
        if (outcome.currentUnchecked) unchecked.push(blockId);
        if (outcome.bitOnly) bitOnly.push(blockId);
        else report[outcome.kind].push(outcome.text);
      }
      calls.progress('Closing the occupancy bench…');
      try {
        await calls.closeBench();
      } catch (e) {
        report.failed.push(`The occupancy bench did not close cleanly: ${errorText(e)}.`);
      }
    }
  }
  if (bitOnly.length) {
    report.proved.push(
      bitOnly.length === 1
        ? `${bitOnly[0]}: its bit followed the drive.`
        : `${listText(bitOnly)}: each bit followed the drive.`,
    );
  }
  if (unchecked.length) {
    report.needsYou.push(
      `${listText(unchecked)}: nothing stood on ${unchecked.length === 1 ? 'it' : 'them'}, so ${unchecked.length === 1 ? 'its current channel was' : 'their current channels were'} not checked. A current channel reads only a load; put a train on the block and run again.`,
    );
  }

  // Each hc165 is proven when every block bound to it checked out.
  for (const chip of setup.chips) {
    if (chip.kind !== 'hc165') continue;
    const blocks = Object.keys(setup.block_occupancy)
      .filter((b) => setup.block_occupancy[b].chip_id === chip.chip_id)
      .sort();
    if (!blocks.length) {
      report.needsYou.push(`${chip.chip_id}: no block is bound to it, so nothing could read it back.`);
      continue;
    }
    const failedBlock = blocks.find((b) => checked.get(b)?.kind === 'failed');
    if (failedBlock) {
      report.chipNotes.push({ chip_id: chip.chip_id, result: 'failed', reason: checked.get(failedBlock)!.text });
      continue;
    }
    const noRails = blocks.filter((b) => !(b in setup.block_track));
    if (noRails.length) {
      report.needsYou.push(
        `${chip.chip_id}: ${listText(noRails)} ${noRails.length === 1 ? 'has' : 'have'} no rail binding, so ${noRails.length === 1 ? 'its bit' : 'their bits'} could not be checked.`,
      );
      continue;
    }
    if (blocks.every((b) => checked.get(b)?.wired)) {
      report.proved.push(`${chip.chip_id}: every bit followed the drive (${blocks.length} block${blocks.length === 1 ? '' : 's'}).`);
      report.chipNotes.push({ chip_id: chip.chip_id, result: 'proven' });
    }
  }

  // 3. The switch throw test: each bound switch to its other position and back.
  const switches = Object.keys(setup.switch_output).filter(id => !unavailableMotors.includes(id)).sort();
  const throwFailures = new Map<string, string>();
  const thrown = new Map<string, string[]>();
  let tookController = false;
  for (const switchId of switches) {
    const chipId = setup.switch_output[switchId].chip_id;
    const at = switchPositions[switchId];
    if (at !== 0 && at !== 1) {
      report.needsYou.push(`${switchId}: it is bound but not in the loaded layout, so it was not thrown.`);
      continue;
    }
    const other = (1 - at) as 0 | 1;
    let failure: string | null = null;
    for (const [n, position] of [other, at as 0 | 1].entries()) {
      if (n) await calls.pause(THROW_GAP_MS);
      calls.progress(`Throwing ${switchId} to ${POSITION_NAME[position]}…`);
      tookController = true;
      let res: { ok: boolean; error?: string };
      try {
        res = await calls.throwSwitch(switchId, position);
      } catch (e) {
        res = { ok: false, error: errorText(e) };
      }
      if (!res.ok) {
        failure = `${switchId}: the throw to ${POSITION_NAME[position]} failed: ${res.error || 'no reason given'}.`;
        break;
      }
    }
    if (failure) {
      report.failed.push(failure);
      if (!throwFailures.has(chipId)) throwFailures.set(chipId, failure);
    } else {
      thrown.set(chipId, [...(thrown.get(chipId) || []), switchId]);
    }
  }
  if (tookController) {
    try {
      await calls.releaseSwitches();
    } catch {
      /* best effort: the commissioning session's idle timeout closes it too */
    }
  }

  // Nothing reads a coil back, so an hc595 waits for the operator.
  for (const chip of setup.chips) {
    if (chip.bus === 'i2c' || chip.kind === 'hc165') continue;
    const failure = throwFailures.get(chip.chip_id);
    const ok = thrown.get(chip.chip_id);
    if (failure) {
      report.chipNotes.push({ chip_id: chip.chip_id, result: 'failed', reason: failure });
    } else if (ok) {
      report.chipNotes.push({ chip_id: chip.chip_id, result: 'ran' });
      report.needsYou.push(
        `${chip.chip_id}: ${listText(ok)} ${ok.length === 1 ? 'was' : 'were'} thrown to the other position and back. If the points moved each time, press It worked on its card.`,
      );
    } else {
      report.needsYou.push(`${chip.chip_id}: no bound switch drives it. Run its sweep, watch the board, and press It worked.`);
    }
  }

  calls.progress('');
  return report;
}
