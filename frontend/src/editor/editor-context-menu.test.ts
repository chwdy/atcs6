import { beforeEach, describe, expect, it, vi } from 'vitest';

import { showContextMenu } from '../lib/context-menu';
import { Editor } from './editor';

vi.mock('../lib/context-menu', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/context-menu')>()),
  showContextMenu: vi.fn(),
}));

describe('tile context menu — trains', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('opens Train Controls without selecting the train and opening Train Monitor', () => {
    const showTrainControlPanel = vi.fn();
    const selectTrain = vi.fn();
    const editor: any = {
      cachedNetwork: { trains: [{ id: 'TRAIN_1' }], blocks: {} },
      stateManager: {
        getController: () => ({ span: { occupied_tiles: [[3, 4]] } }),
      },
      host: { selectTrain },
      showTrainControlPanel,
      selectElement: vi.fn(),
      editorMode: 'sim',
    };

    Editor.prototype.showTileContextMenu.call(
      editor,
      { x: 3, y: 4 },
      { x: 3, y: 4, signals: [], is_switch: false },
      100,
      200,
    );

    const sections = vi.mocked(showContextMenu).mock.calls[0][2];
    const trainItem = sections[0].items.find((item: any) => item.detail === 'Train');
    expect(trainItem).toBeDefined();

    trainItem!.onSelect();

    expect(showTrainControlPanel).toHaveBeenCalledWith('TRAIN_1');
    expect(selectTrain).not.toHaveBeenCalled();
  });
});
