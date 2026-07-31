import { describe, expect, it } from 'vitest';

import { Editor } from './editor';

const getLineage = (snapshot: Record<string, unknown>) =>
  Editor.prototype.getPanelLineage.call({}, snapshot);

describe('Editor.getPanelLineage — entity tab identity', () => {
  it('uses a block display name and keeps the Blocks collection as its parent', () => {
    expect(
      getLineage({ editorType: 'block-detail', editorId: 'BLK_1', title: 'North Yard' })
    ).toEqual([
      { label: 'Blocks', editorType: 'block-list', editorId: null },
      { label: 'North Yard', editorType: 'block-detail', editorId: 'BLK_1' },
    ]);
  });

  it('falls back to the stable id for old generic block and segment titles', () => {
    expect(getLineage({ editorType: 'block-detail', editorId: 'BLK_1', title: 'Block' })[1].label).toBe('BLK_1');
    expect(getLineage({ editorType: 'segment', editorId: 'SEG_1', title: 'Segment Editor' })[1].label).toBe(
      'SEG_1'
    );
  });

  it('uses a segment display name when one is available', () => {
    expect(getLineage({ editorType: 'segment', editorId: 'SEG_1', title: 'Platform Lead' })[1].label).toBe(
      'Platform Lead'
    );
  });
});
