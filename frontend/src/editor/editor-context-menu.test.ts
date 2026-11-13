import { beforeEach, describe, expect, it, vi } from 'vitest';

import { showContextMenu } from '../lib/context-menu';
import { Editor } from './editor';

vi.mock('../lib/context-menu', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/context-menu')>()),
  showContextMenu: vi.fn(),
}));

describe('tile context menu', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  function makeEditor(overrides: Record<string, any> = {}) {
    return {
      cachedNetwork: { blocks: {} },
      selectElement: vi.fn(),
      editorMode: 'sim',
      ...overrides,
    } as any;
  }

  function openMenu(editor: any, track: Record<string, any> = {}) {
    Editor.prototype.showTileContextMenu.call(
      editor,
      { x: 3, y: 4 },
      { x: 3, y: 4, is_switch: false, ...track },
      100,
      200,
    );
    return vi.mocked(showContextMenu).mock.calls[0][2];
  }

  it('Inspect tile opens the Tile Editor whatever the panel shows', () => {
    const editor = makeEditor();
    const inspectItem = openMenu(editor)[1].items.find((item: any) => item.label === 'Inspect tile');
    inspectItem!.onSelect();
    expect(editor.selectElement).toHaveBeenCalledWith({ x: 3, y: 4 }, { force: true });
  });

  describe('entity rows carry their own name and id', () => {
    /** Editor stub holding one block whose single member
     *  segment shares its name — the shape the backend always produces for an
     *  un-grouped segment (create_segment names the owning block after it). */
    function makeEntityEditor(network: Record<string, any> = {}) {
      return makeEditor({
        cachedNetwork: {
          blocks: {
            BLK_C37589A5: {
              name: 'Left',
              segments: [{ segment_id: 'SEG_4F110A', name: 'Left' }],
            },
          },
          ...network,
        },
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

    it('names the switch row from the network record', () => {
      const editor = makeEntityEditor({
        switches: { SW_7_5: { switch_id: 'SW_7_5', name: 'Yard Throat' } },
      });
      const sections = openMenu(editor, { is_switch: true, switch_id: 'SW_7_5' });

      expect(rowFor(sections, 'Switch')).toMatchObject({
        label: 'Yard Throat',
        sublabel: 'SW_7_5',
      });
    });
  });
});
