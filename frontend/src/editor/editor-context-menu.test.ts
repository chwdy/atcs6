import { beforeEach, describe, expect, it, vi } from 'vitest';

import { showContextMenu } from '../lib/context-menu';
import { Editor } from './editor';

vi.mock('../lib/context-menu', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/context-menu')>()),
  showContextMenu: vi.fn(),
}));

describe('tile context menu — trains', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  /** Editor stub with the one train sitting on tile (3, 4). `openAsOwnTab`
   *  stands in for the real one: it just runs the render, and records that the
   *  menu routed through it (see EditorUIHandler.openAsOwnTab). */
  function makeEditor(overrides: Record<string, any> = {}) {
    const openAsOwnTab = vi.fn(async (render: () => any) => render());
    return {
      cachedNetwork: { trains: [{ id: 'TRAIN_1' }], blocks: {} },
      stateManager: {
        getController: () => ({ span: { occupied_tiles: [[3, 4]] } }),
      },
      showTrainControlPanel: vi.fn(),
      selectElement: vi.fn(),
      uiHandler: { openAsOwnTab },
      editorMode: 'sim',
      ...overrides,
    } as any;
  }

  function openMenu(editor: any, track: Record<string, any> = {}) {
    Editor.prototype.showTileContextMenu.call(
      editor,
      { x: 3, y: 4 },
      { x: 3, y: 4, signals: [], is_switch: false, ...track },
      100,
      200,
    );
    return vi.mocked(showContextMenu).mock.calls[0][2];
  }

  it('opens Train Controls without selecting the train and opening Train Monitor', () => {
    const selectTrain = vi.fn();
    const editor = makeEditor({ host: { selectTrain } });

    const trainItem = openMenu(editor)[0].items.find((item: any) => item.detail === 'Train');
    expect(trainItem).toBeDefined();

    trainItem!.onSelect();

    expect(editor.showTrainControlPanel).toHaveBeenCalledWith('TRAIN_1');
    expect(selectTrain).not.toHaveBeenCalled();
  });

  it('opens every menu pick as its own tab, not into the shared preview slot', () => {
    const editor = makeEditor();
    const sections = openMenu(editor);

    const trainItem = sections[0].items.find((item: any) => item.detail === 'Train');
    trainItem!.onSelect();
    expect(editor.uiHandler.openAsOwnTab).toHaveBeenCalledTimes(1);

    const inspectItem = sections[1].items.find((item: any) => item.label === 'Inspect tile');
    inspectItem!.onSelect();
    expect(editor.uiHandler.openAsOwnTab).toHaveBeenCalledTimes(2);
    expect(editor.selectElement).toHaveBeenCalledWith({ x: 3, y: 4 }, { force: true });
  });

  describe('entity rows carry their own name and id', () => {
    /** Editor stub with no trains, holding one block whose single member
     *  segment shares its name — the shape the backend always produces for an
     *  un-grouped segment (create_segment names the owning block after it). */
    function makeEntityEditor(network: Record<string, any> = {}) {
      return makeEditor({
        cachedNetwork: {
          trains: [],
          blocks: {
            BLK_C37589A5: {
              name: 'Left',
              segments: [{ segment_id: 'SEG_4F110A', name: 'Left' }],
            },
          },
          ...network,
        },
        stateManager: { getController: () => null },
      });
    }

    function rowFor(sections: any[], detail: string) {
      return sections[0].items.find((item: any) => item.detail === detail);
    }

    it('separates a block from its like-named segment by their ids', () => {
      const sections = openMenu(makeEntityEditor(), {
        segment_id: 'SEG_4F110A',
        block_section_id: 'BLK_C37589A5',
      });

      expect(rowFor(sections, 'Segment')).toMatchObject({
        label: 'Left',
        sublabel: 'SEG_4F110A',
      });
      expect(rowFor(sections, 'Block')).toMatchObject({
        label: 'Left',
        sublabel: 'BLK_C37589A5',
      });
    });

    it('labels an unnamed entity with its id and no second line', () => {
      const editor = makeEntityEditor({
        blocks: { BLK_1: { name: '', segments: [{ segment_id: 'SEG_1', name: '  ' }] } },
      });
      const sections = openMenu(editor, { segment_id: 'SEG_1', block_section_id: 'BLK_1' });

      expect(rowFor(sections, 'Segment')).toMatchObject({ label: 'SEG_1' });
      expect(rowFor(sections, 'Segment').sublabel).toBeUndefined();
      expect(rowFor(sections, 'Block')).toMatchObject({ label: 'BLK_1' });
      expect(rowFor(sections, 'Block').sublabel).toBeUndefined();
    });

    it('names the switch and platform rows from the network records', () => {
      const editor = makeEntityEditor({
        switches: { SW_7_5: { switch_id: 'SW_7_5', name: 'Yard Throat' } },
        platforms: [{ platform_id: 'PLT_1', name: 'Platform 1' }],
      });
      const sections = openMenu(editor, {
        is_switch: true,
        switch_id: 'SW_7_5',
        station_id: 'PLT_1',
      });

      expect(rowFor(sections, 'Switch')).toMatchObject({
        label: 'Yard Throat',
        sublabel: 'SW_7_5',
      });
      expect(rowFor(sections, 'Platform')).toMatchObject({
        label: 'Platform 1',
        sublabel: 'PLT_1',
      });
    });

    it('lists signals from signal_ids — GET /api/track sends no enriched array', () => {
      const editor = makeEntityEditor({
        signals: { SIG_1: { signal_id: 'SIG_1', name: 'Home' } },
      });
      const sections = openMenu(editor, { signals: undefined, signal_ids: ['SIG_1', 'SIG_2'] });

      const signalRows = sections[0].items.filter((item: any) => item.detail === 'Signal');
      expect(signalRows).toHaveLength(2);
      expect(signalRows[0]).toMatchObject({ label: 'Home', sublabel: 'SIG_1' });
      // Unknown to the network payload: the row still opens, labelled by id.
      expect(signalRows[1]).toMatchObject({ label: 'SIG_2' });
    });
  });
});
