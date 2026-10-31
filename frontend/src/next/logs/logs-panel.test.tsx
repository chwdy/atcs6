// @vitest-environment happy-dom
import { render } from 'preact';
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => {
  return {
    getEventLog: vi.fn(async (_query?: string): Promise<any> => ({ events: [] })),
    getRawOccupancy: vi.fn(async (): Promise<any> => ({ blocks: {} })),
    getInspectorEntityTrain: vi.fn(async (_id?: string): Promise<any> => null),
  };
});
vi.mock('../../lib/api', () => ({ api }));

import { stateManager } from '../../lib/state-manager';
import { LogsPanel } from './logs-panel';
import { DEFAULT_HEIGHT, logsHeight, logsOpen, logsTab, toggleLogs } from './state';

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

describe('LogsPanel', () => {
  let root: HTMLElement;

  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    Object.values(api).forEach((fn) => fn.mockClear());
    logsOpen.value = false;
    logsTab.value = 'events';
    logsHeight.value = DEFAULT_HEIGHT;
    root = document.createElement('div');
    document.body.appendChild(root);
    act(() => render(<LogsPanel />, root));
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
    vi.useRealTimers();
  });

  it('draws nothing and asks the server for nothing while closed', async () => {
    await advance(5000);
    expect(root.querySelector('.next-logs')).toBeNull();
    expect(api.getEventLog).not.toHaveBeenCalled();
    expect(api.getRawOccupancy).not.toHaveBeenCalled();
    expect(document.documentElement.dataset.logs).toBeUndefined();
  });

  it('opens on the last tab and remembers open state, tab and height', async () => {
    act(() => toggleLogs());
    await settle();
    expect(root.querySelector('.next-logs')).not.toBeNull();
    expect(document.documentElement.dataset.logs).toBe('open');
    expect(tabNamed(root, 'Event log').getAttribute('aria-selected')).toBe('true');

    act(() => tabNamed(root, 'Detectors').click());
    const saved = JSON.parse(localStorage.getItem('atcs6.nextLogsPanel.v1')!);
    expect(saved).toMatchObject({ open: true, tab: 'detectors' });

    act(() => toggleLogs());
    expect(root.querySelector('.next-logs')).toBeNull();
    expect(document.documentElement.dataset.logs).toBeUndefined();
  });

  it('polls only the tab on show, and nothing once closed', async () => {
    act(() => toggleLogs());
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

    act(() => toggleLogs());
    const detectorReads = api.getRawOccupancy.mock.calls.length;
    await advance(5000);
    expect(api.getRawOccupancy).toHaveBeenCalledTimes(detectorReads);
    expect(api.getEventLog).toHaveBeenCalledTimes(eventLogReads);
  });

  it('resizes from the drag handle and saves the height when the drag ends', async () => {
    act(() => toggleLogs());
    await settle();
    const handle = root.querySelector<HTMLElement>('.nlp-resizer')!;
    act(() => {
      handle.dispatchEvent(new PointerEvent('pointerdown', { button: 0, clientY: 600, bubbles: true }));
      handle.dispatchEvent(new PointerEvent('pointermove', { clientY: 500, bubbles: true }));
    });
    expect(root.querySelector<HTMLElement>('.next-logs')!.style.height).toBe(`${DEFAULT_HEIGHT + 100}px`);
    act(() => {
      handle.dispatchEvent(new PointerEvent('pointerup', { clientY: 500, bubbles: true }));
    });
    const saved = JSON.parse(localStorage.getItem('atcs6.nextLogsPanel.v1')!);
    expect(saved.height).toBe(DEFAULT_HEIGHT + 100);
  });
});

describe('Event log tab', () => {
  let root: HTMLElement;

  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    Object.values(api).forEach((fn) => fn.mockClear());
    logsOpen.value = true;
    logsTab.value = 'events';
    root = document.createElement('div');
    document.body.appendChild(root);
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
    logsOpen.value = false;
    vi.useRealTimers();
  });

  it('asks only for newer events after the first read, and starts over when a filter changes', async () => {
    api.getEventLog.mockResolvedValueOnce({ events: [event(2, 'error'), event(1)] });
    act(() => render(<LogsPanel />, root));
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
    act(() => render(<LogsPanel />, root));
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
    logsOpen.value = true;
    logsTab.value = 'detectors';
    root = document.createElement('div');
    document.body.appendChild(root);
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
    logsOpen.value = false;
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
    act(() => render(<LogsPanel />, root));
    await settle();
    const names = [...root.querySelectorAll('.occ-block')].map((el) => el.textContent);
    expect(names).toEqual(['BLK_A', 'BLK_B']);
    expect(root.querySelector('.occ-row-settling .occ-reading')!.textContent).toBe('14.2 mA');
    expect(root.querySelector('.occ-footer')!.textContent).toContain('read #7');
  });
});

describe('Diagnostics tab', () => {
  let root: HTMLElement;

  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    Object.values(api).forEach((fn) => fn.mockClear());
    stateManager.updateFromNetwork({ trains: [{ id: 'T1' }, { id: 'T2' }] });
    logsOpen.value = true;
    logsTab.value = 'diagnostics';
    root = document.createElement('div');
    document.body.appendChild(root);
  });

  afterEach(() => {
    act(() => render(null, root));
    root.remove();
    logsOpen.value = false;
    stateManager.updateFromNetwork({ trains: [] });
    vi.useRealTimers();
  });

  it('lists the trains, asks nothing until one is picked, then shows its groups', async () => {
    api.getInspectorEntityTrain.mockResolvedValue({
      id: 'T1',
      label: 'T1',
      fetched_at: 1_700_000_000,
      groups: [{ name: 'live', items: [{ name: 'speed', value: 12, source: 'property' }] }],
    });
    act(() => render(<LogsPanel />, root));
    await settle();
    const select = root.querySelector<HTMLSelectElement>('.diagnostics-panel-train-select')!;
    expect([...select.options].map((o) => o.textContent)).toEqual(['(select a train)', 'T1', 'T2']);
    expect(api.getInspectorEntityTrain).not.toHaveBeenCalled();

    act(() => {
      select.value = 'T1';
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await settle();
    expect(api.getInspectorEntityTrain).toHaveBeenCalledWith('T1');
    expect(root.querySelector('.diagnostics-group-name')!.textContent).toBe('live');
    expect(root.querySelector('.diagnostics-row-name')!.textContent).toBe('speed');
  });

  it('stops asking about a train the server says no longer exists', async () => {
    localStorage.setItem('atcs6.nextLogsPanel.v1', JSON.stringify({ diagnostics: { trainId: 'T9' } }));
    api.getInspectorEntityTrain.mockResolvedValue(null);
    act(() => render(<LogsPanel />, root));
    await settle();
    expect(root.querySelector('.diagnostics-panel-body')!.textContent).toContain('Train no longer exists.');
    const reads = api.getInspectorEntityTrain.mock.calls.length;
    await advance(5000);
    expect(api.getInspectorEntityTrain.mock.calls.length).toBeLessThanOrEqual(reads + 1);
  });
});
