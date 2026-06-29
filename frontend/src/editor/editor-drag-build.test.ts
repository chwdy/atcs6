import { beforeEach, describe, expect, it, vi } from 'vitest';

import { pathToTracks, validateBlockPath } from '../lib/pathfinders';
import { showToast } from '../lib/toast';
import { DragBuildHandler } from './editor-drag-build';

// Behavior-pinning ("characterization") specs for the handler freshly extracted
// from the Editor god-class. They lock in the CURRENT dispatch — which tool maps
// to which preview shape, which API call, and which safety gate — so the next
// round of Editor decomposition can move code with a net underneath it. The
// handler takes its host via the constructor, so a hand-built fake EditorHost
// exercises it without a DOM (vitest runs in the node env). The library
// collaborators are stubbed; their own logic is covered by their own specs.
vi.mock('../lib/toast', () => ({ showToast: vi.fn() }));
vi.mock('../lib/helpers', () => ({ showConfirmModal: vi.fn(() => Promise.resolve(true)) }));
vi.mock('../lib/graph-search', () => ({ graphSearch: vi.fn(() => ({ path: null, visited: new Map() })) }));
vi.mock('../lib/pathfinders', () => ({
  pathToTracks: vi.fn(() => []),
  tileSkipsSegment: vi.fn(() => false),
  validateBlockPath: vi.fn(() => ({ valid: true, errors: [], warnings: [], signalsInPath: [], blocksInPath: [] })),
  traceNodeKey: (n: any) => `${n.x},${n.y},${n.entry}`,
}));

function makeDragEditor(overrides: any = {}): any {
  const { api: apiOverrides, renderer: rendererOverrides, ...rest } = overrides;
  return {
    dragStart: null,
    dragEnd: null,
    selectedTool: 'track',
    avoidConnectMode: false,
    forcedStartSegment: null,
    forcedEndSegment: null,
    previewPath: null,
    previewTracks: null,
    previewInvalid: false,
    cachedNetwork: { tracks: [], platforms: [] },
    pathfinder: {
      findPath: vi.fn(() => null),
      followExistingNeighbors: vi.fn(() => () => []),
    },
    _tracksByKey: vi.fn(() => new Map()),
    renderer: {
      setBuildPreview: vi.fn(),
      requestRedraw: vi.fn(),
      ...rendererOverrides,
    },
    api: {
      addTracksBulk: vi.fn().mockResolvedValue(undefined),
      createBlock: vi.fn().mockResolvedValue(undefined),
      addTracksToBlock: vi.fn().mockResolvedValue(undefined),
      extendBlock: vi.fn().mockResolvedValue({}),
      createPlatform: vi.fn().mockResolvedValue(undefined),
      extendPlatform: vi.fn().mockResolvedValue(undefined),
      removePlatformTile: vi.fn().mockResolvedValue(undefined),
      removeBlockTiles: vi.fn().mockResolvedValue({ removed: 0, errors: [] }),
      ...apiOverrides,
    },
    showBlockModal: vi.fn().mockResolvedValue(null),
    showPlatformModal: vi.fn().mockResolvedValue(null),
    refreshBlocksList: vi.fn().mockResolvedValue(undefined),
    refreshAndSelect: vi.fn().mockResolvedValue(undefined),
    ...rest,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(pathToTracks).mockReturnValue([]);
  vi.mocked(validateBlockPath).mockReturnValue({
    valid: true, errors: [], warnings: [], signalsInPath: [], blocksInPath: [],
  } as any);
});

describe('updatePreviewPath — preview dispatch by tool', () => {
  it('does nothing when the drag has no endpoints', () => {
    const ed = makeDragEditor({ dragStart: null, dragEnd: null });
    new DragBuildHandler(ed).updatePreviewPath();
    expect(ed.renderer.setBuildPreview).not.toHaveBeenCalled();
    expect(ed.renderer.requestRedraw).not.toHaveBeenCalled();
  });

  it('track tool with a route shows a "tracks" preview and clears the invalid flag', () => {
    vi.mocked(pathToTracks).mockReturnValue([{ x: 1, y: 0, track_bits: 2 }] as any);
    const ed = makeDragEditor({
      selectedTool: 'track',
      dragStart: { x: 0, y: 0 },
      dragEnd: { x: 2, y: 0 },
      previewInvalid: true,
    });
    ed.pathfinder.findPath = vi.fn(() => [{ x: 0, y: 0 }, { x: 1, y: 0 }, { x: 2, y: 0 }]);

    new DragBuildHandler(ed).updatePreviewPath();

    expect(ed.pathfinder.findPath).toHaveBeenCalled();
    expect(ed.previewInvalid).toBe(false);
    expect(ed.renderer.setBuildPreview).toHaveBeenCalledWith(expect.objectContaining({ kind: 'tracks' }));
    expect(ed.renderer.requestRedraw).toHaveBeenCalled();
  });

  it('a build-tool drag with no legal route shows the red "invalid" attempt-line', () => {
    const ed = makeDragEditor({
      selectedTool: 'block',
      dragStart: { x: 0, y: 0 },
      dragEnd: { x: 3, y: 0 },
      _tracksByKey: vi.fn(() => new Map()), // endpoints not on existing track → no route
    });

    new DragBuildHandler(ed).updatePreviewPath();

    expect(ed.previewInvalid).toBe(true);
    expect(ed.previewPath).toBeNull();
    expect(ed.renderer.setBuildPreview).toHaveBeenCalledWith(expect.objectContaining({ kind: 'invalid' }));
  });

  it('a non-drag tool just previews the single tile under the cursor', () => {
    const ed = makeDragEditor({
      selectedTool: 'signal',
      dragStart: { x: 4, y: 4 },
      dragEnd: { x: 4, y: 4 },
    });

    new DragBuildHandler(ed).updatePreviewPath();

    expect(ed.previewPath).toEqual([{ x: 4, y: 4 }]);
    expect(ed.renderer.setBuildPreview).toHaveBeenCalledWith(expect.objectContaining({ kind: 'path' }));
  });
});

describe('commitDragBuild — commit dispatch and safety gates', () => {
  it('track tool bulk-adds the routed tracks, then refreshes', async () => {
    vi.mocked(pathToTracks).mockReturnValue([{ x: 0, y: 0, track_bits: 1 }] as any);
    const ed = makeDragEditor({
      selectedTool: 'track',
      previewPath: [{ x: 0, y: 0 }, { x: 1, y: 0 }],
    });

    await new DragBuildHandler(ed).commitDragBuild();

    expect(ed.api.addTracksBulk).toHaveBeenCalledWith([{ x: 0, y: 0, track_bits: 1 }]);
    expect(ed.refreshAndSelect).toHaveBeenCalledWith(null);
  });

  it('block tool refuses an invalid block — toasts and creates nothing', async () => {
    vi.mocked(validateBlockPath).mockReturnValue({
      valid: false, errors: ['bad block'], warnings: [], signalsInPath: [], blocksInPath: [],
    } as any);
    const ed = makeDragEditor({
      selectedTool: 'block',
      previewPath: [{ x: 0, y: 0 }, { x: 1, y: 0 }],
    });

    await new DragBuildHandler(ed).commitDragBuild();

    expect(showToast).toHaveBeenCalled();
    expect(ed.api.createBlock).not.toHaveBeenCalled();
    expect(ed.refreshAndSelect).not.toHaveBeenCalled();
  });

  it('block tool creates nothing when the create dialog is cancelled', async () => {
    const ed = makeDragEditor({
      selectedTool: 'block',
      previewPath: [{ x: 0, y: 0 }, { x: 1, y: 0 }],
      showBlockModal: vi.fn().mockResolvedValue(null), // user cancelled
    });

    await new DragBuildHandler(ed).commitDragBuild();

    expect(ed.showBlockModal).toHaveBeenCalled();
    expect(ed.api.createBlock).not.toHaveBeenCalled();
  });

  it('an empty drag commits nothing', async () => {
    const ed = makeDragEditor({ selectedTool: 'track', previewPath: [] });

    await new DragBuildHandler(ed).commitDragBuild();

    expect(ed.api.addTracksBulk).not.toHaveBeenCalled();
    expect(ed.refreshAndSelect).not.toHaveBeenCalled();
  });
});
