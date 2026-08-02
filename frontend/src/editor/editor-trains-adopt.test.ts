/**
 * The unidentified-adoption form (design_new.md "Unidentified trains", Adoption).
 *
 * An unidentified train claims whole blocks because nobody knows where inside them it sits.
 * The form's job is to not invent a position on top of that: it asks the
 * runtime to centre the consist unless the operator says otherwise, and it
 * offers only the facings the detected track can actually be left through.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { TrainEditorHandler } from './editor-trains';
import { TRACK_EW } from '../lib/constants';

/** An unidentified train standing on a straight west-to-east run of `length` tiles from x=3. */
function handlerOn(length: number): any {
  const tracks = [];
  for (let x = 0; x < 3 + length + 2; x++) tracks.push({ x, y: 0, track_bits: TRACK_EW });
  const handler: any = new TrainEditorHandler({ cachedNetwork: { tracks } } as any);
  const tiles: Array<[number, number]> = [];
  for (let i = 0; i < length; i++) tiles.push([3 + i, 0]);
  handler.renderUnidentifiedTrainPanel('UNIDENT_1', {
    span: { occupied_blocks: ['BLK_1'], occupied_tiles: tiles },
  });
  return handler;
}

describe('unidentified train adoption form', () => {
  let handler: any;

  beforeEach(() => {
    handler = handlerOn(3);
  });

  it('starts centred, with no head tile chosen', () => {
    expect(handler._adoptHeadTile).toBeNull();
    expect(handler._adoptClaimedTiles).toEqual([
      [3, 0],
      [4, 0],
      [5, 0],
    ]);
  });

  it('offers the centred option first and every claimed tile after it', () => {
    const html = handler.renderUnidentifiedTrainPanel('UNIDENT_1', {
      span: { occupied_blocks: ['BLK_1'], occupied_tiles: [[3, 0], [4, 0], [5, 0]] },
    });
    expect(html).toContain('<option value="" selected>Centred in the blocks</option>');
    expect(html).toContain('<option value="3,0">Front at (3, 0)</option>');
    expect(html).toContain('<option value="5,0">Front at (5, 0)</option>');
  });

  it('offers only the facings that leave the detected track', () => {
    // A straight run can only be left westward off its first tile or eastward
    // off its last — never through a neighbour it also stands on.
    expect(handler._adoptExits().sort()).toEqual(['E', 'W']);
  });

  it('offers the chosen tile\'s own exits once a head tile is named', () => {
    handler._adoptHeadTile = { x: 4, y: 0 };
    // The middle tile: the front goes there, so it may face either way along it.
    expect(handler._adoptExits().sort()).toEqual(['E', 'W']);
  });

  it('leaves nothing to face when the unidentified train is in the detector gap', () => {
    const empty: any = new TrainEditorHandler({ cachedNetwork: { tracks: [] } } as any);
    empty.renderUnidentifiedTrainPanel('UNIDENT_1', { span: { occupied_blocks: [], occupied_tiles: [] } });
    expect(empty._adoptExits()).toEqual([]);
  });

  it('renders a facing button per available exit, none preselected', () => {
    const slot = handler._renderAdoptFacingOptions();
    expect(slot).toContain('data-adopt-facing="W"');
    expect(slot).toContain('data-adopt-facing="E"');
    // Facing is the operator's call, so nothing is chosen for them and the
    // submit gate (see _refreshAdoptUI) stays shut until they pick one.
    expect(slot).not.toContain('active');
    expect(handler._adoptFacing).toBeNull();
  });
});
