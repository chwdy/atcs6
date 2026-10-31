import { describe, expect, it, vi } from 'vitest';

vi.mock('../../lib/api', () => ({
  api: {
    getNetwork: vi.fn(async () => ({ trains: [] })),
    getRoutes: vi.fn(async () => ({ routes: [] })),
    selectOperationEnvironment: vi.fn(async () => {}),
    activateOperationEnvironment: vi.fn(async () => {}),
  },
}));
vi.mock('../../lib/toast', () => ({ showToast: vi.fn() }));
vi.mock('./starter-loop', () => ({
  buildStarterLoop: vi.fn(async () => ({ trainId: 'LOOP_1', routeId: 'ROUTE-1' })),
}));

import { firstRunActions, type FirstRunHost } from './mount';

describe('Start from a loop', () => {
  it('frames the view on the new loop, even after an earlier layout was framed', async () => {
    const loopTracks = [{ x: 0, y: 0 }, { x: 1, y: 0 }];
    const host: FirstRunHost = {
      editor: { toolbarHandler: { selectTool: vi.fn() } },
      openLoadLayoutDialog: vi.fn(async () => {}),
      // The refresh is what brings the new loop's track into the app.
      refreshNetwork: vi.fn(async () => {
        host.currentNetwork = { tracks: loopTracks };
      }),
      selectTrain: vi.fn(),
      renderer: { fitToContent: vi.fn(), requestRedraw: vi.fn() },
      currentNetwork: { tracks: [] },
      _canvasChromeInsets: () => ({ left: 40, bottom: 120 }),
    };
    const showScreen = vi.fn();

    await firstRunActions(host, showScreen).startFromLoop();

    expect(showScreen).toHaveBeenCalledWith('operate');
    expect(host.renderer!.fitToContent).toHaveBeenCalledWith(loopTracks, undefined, { left: 40, bottom: 120 });
    expect(host.selectTrain).toHaveBeenCalledWith('LOOP_1');
  });
});
