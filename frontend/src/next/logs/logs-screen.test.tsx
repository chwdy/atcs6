// @vitest-environment happy-dom
import { render } from 'preact';
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => {
  return {
    getEventLog: vi.fn(async (_query?: string): Promise<any> => ({ events: [] })),
    getRawOccupancy: vi.fn(async (): Promise<any> => ({ blocks: {} })),
    getBlockVoltage: vi.fn(async (_id?: string): Promise<any> => ({ voltage: null })),
    getScale: vi.fn(async (): Promise<any> => ({ scale: 160 })),
  };
});
vi.mock('../../lib/api', () => ({ api }));

import { stateManager } from '../../lib/state-manager';
import { screen } from '../state';
import { LogsScreen, logsPanelHost } from './logs-screen';
import { logsTab, pickedBlockId } from './state';

const event = (id: number, kind = 'info', extra: Record<string, unknown> = {}) => ({
  notification_id: id,
  event_time: 1_700_000_000 + id,
  source: 'RouteExecutor',
  operation: 'advance',
  kind,
  entities: [{ entity_type: 'train', entity_id: 'T1' }],
  message: `event ${id}`,
  error: null,
  payload: {},
  ...extra,
});

const tabNamed = (root: HTMLElement, label: string): HTMLButtonElement => {
  const found = [...root.querySelectorAll<HTMLButtonElement>('[role="tab"]')].find(
    (b) => b.textContent?.trim() === label
  );
  if (!found) throw new Error(`no tab "${label}"`);
  return found;
};

/** Move the fake clock on, letting pending reads resolve and Preact re-draw. */
const advance = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
const settle = () => advance(0);

/** The query string of the event log's latest read. */
const lastQuery = (): string => {
  const calls = api.getEventLog.mock.calls;
  return calls[calls.length - 1][0] ?? '';
};

describe('LogsScreen', () => {
  let root: HTMLElement;

  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    Object.values(api).forEach((fn) => fn.mockClear());
    screen.value = 'editor';
    logsTab.value = 'events';
    root = document.createElement('div');
    document.body.appendChild(root);
    act(() => render(<LogsScreen />, root));
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
    screen.value = 'editor';
    vi.useRealTimers();
  });

  it('draws nothing and asks the server for nothing on another screen', async () => {
    await advance(5000);
    expect(root.querySelector('.next-logs')).toBeNull();
    expect(api.getEventLog).not.toHaveBeenCalled();
    expect(api.getRawOccupancy).not.toHaveBeenCalled();
  });

  it('opens on the last tab and remembers the tab', async () => {
    act(() => {
      screen.value = 'logs';
    });
    await settle();
    expect(root.querySelector('.next-logs')).not.toBeNull();
    expect(tabNamed(root, 'Event log').getAttribute('aria-selected')).toBe('true');

    act(() => tabNamed(root, 'Detectors').click());
    const saved = JSON.parse(localStorage.getItem('atcs6.nextLogsPanel.v1')!);
    expect(saved).toMatchObject({ tab: 'detectors' });
  });

  it('polls only the tab on show, and nothing once another screen is picked', async () => {
    act(() => {
      screen.value = 'logs';
    });
    await settle();
    expect(api.getEventLog).toHaveBeenCalledTimes(1);
    await advance(1000); // the event log reads at 2 Hz
    expect(api.getEventLog).toHaveBeenCalledTimes(3);
    expect(api.getRawOccupancy).not.toHaveBeenCalled();

    act(() => tabNamed(root, 'Detectors').click());
    await settle();
    const eventLogReads = api.getEventLog.mock.calls.length;
    await advance(1000); // the detectors read at 4 Hz
    expect(api.getEventLog).toHaveBeenCalledTimes(eventLogReads);
    expect(api.getRawOccupancy.mock.calls.length).toBeGreaterThanOrEqual(4);

    act(() => {
      screen.value = 'operate';
    });
    const detectorReads = api.getRawOccupancy.mock.calls.length;
    await advance(5000);
    expect(api.getRawOccupancy).toHaveBeenCalledTimes(detectorReads);
    expect(api.getEventLog).toHaveBeenCalledTimes(eventLogReads);
  });
});

describe('A click on the canvas', () => {
  const host = logsPanelHost();

  beforeEach(() => {
    localStorage.clear();
    screen.value = 'logs';
    logsTab.value = 'events';
    pickedBlockId.value = null;
  });

  afterEach(() => {
    screen.value = 'editor';
  });

  it('on a block opens the Detectors tab on that block', () => {
    expect(host.selectTile({ x: 9, y: 9, block_section_id: 'BLK_B' })).toBe(true);
    expect(logsTab.value).toBe('detectors');
    expect(pickedBlockId.value).toBe('BLK_B');
  });

  it('is left to the other screens while Logs is not showing', () => {
    screen.value = 'operate';
    expect(host.selectTile({ x: 3, y: 4, block_section_id: 'BLK_A' })).toBe(false);
    expect(logsTab.value).toBe('events');
  });
});

describe('Event log tab', () => {
  let root: HTMLElement;

  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    Object.values(api).forEach((fn) => fn.mockClear());
    screen.value = 'logs';
    logsTab.value = 'events';
    root = document.createElement('div');
    document.body.appendChild(root);
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
    screen.value = 'editor';
    vi.useRealTimers();
  });

  it('asks only for newer events after the first read, and starts over when a filter changes', async () => {
    api.getEventLog.mockResolvedValueOnce({ events: [event(2, 'error'), event(1)] });
    act(() => render(<LogsScreen />, root));
    await settle();
    expect(root.querySelectorAll('.evl-row')).toHaveLength(2);
    expect(root.querySelector('.evl-footer')!.textContent).toContain('1 error');

    await advance(500);
    expect(lastQuery()).toContain('since_notification_id=2');

    const level = root.querySelector<HTMLSelectElement>('.evl-toolbar select')!;
    act(() => {
      level.value = 'error';
      level.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await settle();
    const query = lastQuery();
    expect(query).toContain('kind=error');
    expect(query).not.toContain('since_notification_id');
  });

  it('filters to an entity from its chip', async () => {
    api.getEventLog.mockResolvedValueOnce({ events: [event(1)] });
    act(() => render(<LogsScreen />, root));
    await settle();
    act(() => root.querySelector<HTMLButtonElement>('.evl-chip')!.click());
    await settle();
    expect(lastQuery()).toContain('entity_type=train&entity_id=T1');
    expect(root.querySelector('.evl-active-entity')!.textContent).toContain('T1');
  });
});

describe('Detectors tab', () => {
  let root: HTMLElement;

  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    Object.values(api).forEach((fn) => fn.mockClear());
    screen.value = 'logs';
    logsTab.value = 'detectors';
    root = document.createElement('div');
    document.body.appendChild(root);
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
    screen.value = 'editor';
    vi.useRealTimers();
  });

  it('shows one row per detected block with the raw read and the verdict', async () => {
    api.getRawOccupancy.mockResolvedValue({
      read_count: 7,
      on_threshold: 2,
      off_threshold: 3,
      blocks: {
        BLK_B: { raw: true, confirmed: false, on_count: 1, source: 'current', current_ma: 14.2 },
        BLK_A: { raw: false, confirmed: false },
      },
    });
    act(() => render(<LogsScreen />, root));
    await settle();
    const names = [...root.querySelectorAll('.occ-block')].map((el) => el.textContent);
    expect(names).toEqual(['BLK_A', 'BLK_B']);
    expect(root.querySelector('.occ-row-settling .occ-reading')!.textContent).toBe('14.2 mA');
    expect(root.querySelector('.occ-footer')!.textContent).toContain('read #7');
  });

  it("shows a named block's name, with its id under it", async () => {
    stateManager.updateFromNetwork({ blocks: { BLK_A: { block_id: 'BLK_A', name: 'Main' } } });
    api.getRawOccupancy.mockResolvedValue({ blocks: { BLK_A: { raw: false, confirmed: false } } });
    act(() => render(<LogsScreen />, root));
    await settle();
    const cell = root.querySelector('.occ-block')!;
    expect(cell.firstChild!.textContent).toBe('Main');
    expect(cell.querySelector('.occ-block-id')!.textContent).toBe('BLK_A');
    stateManager.updateFromNetwork({ blocks: {} });
  });

  it('reads the rail voltage only when asked, averaging several readings per block', async () => {
    api.getRawOccupancy.mockResolvedValue({ blocks: { BLK_A: { raw: false, confirmed: false }, BLK_B: { raw: false, confirmed: false } } });
    // BLK_A's sensor reads around 6 V; BLK_B has no sensor that measures voltage.
    const readings = [5, 7, 6, 6, 6];
    api.getBlockVoltage.mockImplementation(async (id?: string) => ({ voltage: id === 'BLK_A' ? readings.shift()! : null }));
    act(() => render(<LogsScreen />, root));
    await settle();
    await advance(1000);
    expect(api.getBlockVoltage).not.toHaveBeenCalled();
    const cells = () => [...root.querySelectorAll('.occ-rail')].map((el) => el.textContent);
    expect(cells()).toEqual(['—', '—']);

    const button = [...root.querySelectorAll('button')].find((b) => b.textContent === 'Read rail voltage')!;
    act(() => button.click());
    await settle();
    expect(cells()).toEqual(['6 V', '—']);
    expect(root.querySelector('.occ-rail')!.getAttribute('title')).toContain('from 5 to 7 V');
    // Five readings of BLK_A; one of BLK_B, since a block that returns nothing is not asked again.
    expect(api.getBlockVoltage.mock.calls.map((c) => c[0])).toEqual(['BLK_A', 'BLK_A', 'BLK_A', 'BLK_A', 'BLK_A', 'BLK_B']);
  });

  it('marks the block picked on the canvas, and says when it has no detector', async () => {
    api.getRawOccupancy.mockResolvedValue({ blocks: { BLK_A: { raw: false, confirmed: false }, BLK_B: { raw: false, confirmed: false } } });
    pickedBlockId.value = 'BLK_B';
    act(() => render(<LogsScreen />, root));
    await settle();
    const marked = [...root.querySelectorAll('.occ-row.row-selected .occ-block')].map((el) => el.textContent);
    expect(marked).toEqual(['BLK_B']);
    expect(root.querySelector('.occ-picked-note')).toBeNull();

    act(() => {
      pickedBlockId.value = 'BLK_Z';
    });
    expect(root.querySelector('.occ-row.row-selected')).toBeNull();
    expect(root.querySelector('.occ-picked-note')!.textContent).toBe('BLK_Z has no detector.');
    pickedBlockId.value = null;
  });
});
