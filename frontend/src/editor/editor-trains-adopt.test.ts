// @vitest-environment happy-dom
/**
 * The unidentified-adoption form (design_new.md "Unidentified trains", Adoption).
 *
 * An unidentified train claims whole blocks because nobody knows where inside them it sits.
 * The form's job is to not invent a position on top of that: it asks the
 * runtime to centre the consist unless the operator says otherwise, and it
 * offers only the facings the detected track can actually be left through.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TrainEditorHandler } from './editor-trains';
import { TRACK_EW, TRACK_NS, TRACK_NW, TRACK_SW } from '../lib/constants';

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

  it('offers centring on one block, with a map pick to override it', () => {
    const html = handler.renderUnidentifiedTrainPanel('UNIDENT_1', {
      span: { occupied_blocks: ['BLK_1'], occupied_tiles: [[3, 0], [4, 0], [5, 0]] },
    });
    expect(html).toContain('Centred in the block');
    expect(html).toContain('id="unidentified-adopt-pick"');
    expect(html).not.toContain('unidentified-adopt-multiblock');
  });

  it('offers only the facings that leave the detected track', () => {
    // A straight run can only be left westward off its first tile or eastward
    // off its last — never through a neighbour it also stands on.
    const choices = handler._adoptChoices();
    expect(choices.map((c: any) => `${c.tile.x},${c.tile.y}:${c.edge}`).sort()).toEqual(['3,0:W', '5,0:E']);
  });

  it('offers the chosen tile\'s own exits once a head tile is named', () => {
    handler._adoptHeadTile = { x: 4, y: 0 };
    // The middle tile: the front goes there, so it may face either way along it.
    expect(handler._adoptChoices().map((c: any) => c.edge).sort()).toEqual(['E', 'W']);
  });

  it('leaves nothing to face when the unidentified train is in the detector gap', () => {
    const empty: any = new TrainEditorHandler({ cachedNetwork: { tracks: [] } } as any);
    empty.renderUnidentifiedTrainPanel('UNIDENT_1', { span: { occupied_blocks: [], occupied_tiles: [] } });
    expect(empty._adoptChoices()).toEqual([]);
  });

  it('renders a numbered button per available end, none preselected', () => {
    const slot = handler._renderAdoptFacingOptions();
    expect(slot).toContain('data-adopt-choice="0"');
    expect(slot).toContain('data-adopt-choice="1"');
    expect(slot).toContain('<span class="front-choice-num">1</span>');
    expect(slot).toContain('<span class="front-choice-num">2</span>');
    // Facing is the operator's call, so nothing is chosen for them and the
    // submit gate (see _refreshAdoptUI) stays shut until they pick one.
    expect(slot).not.toContain('active');
    expect(handler._adoptFacing).toBeNull();
  });
});

/**
 * A horseshoe block whose two ends both exit WEST (design_new.md "Unidentified
 * trains", Choosing the front end). North is y + 1.
 *
 *   (0,2) Yard --- (1,2) --- (2,2)
 *                              |
 *                            (2,1)
 *                              |
 *   (0,0) Up ----- (1,0) --- (2,0)
 */
function horseshoe(): any {
  const tracks = [
    { x: 0, y: 0, track_bits: TRACK_EW, block_section_id: 'BLK_UP' },
    { x: 0, y: 2, track_bits: TRACK_EW, block_section_id: 'BLK_YARD' },
    { x: 1, y: 0, track_bits: TRACK_EW, block_section_id: 'BLK_U' },
    { x: 2, y: 0, track_bits: TRACK_NW, block_section_id: 'BLK_U' },
    { x: 2, y: 1, track_bits: TRACK_NS, block_section_id: 'BLK_U' },
    { x: 2, y: 2, track_bits: TRACK_SW, block_section_id: 'BLK_U' },
    { x: 1, y: 2, track_bits: TRACK_EW, block_section_id: 'BLK_U' },
  ];
  const blocks = { BLK_UP: { name: 'Up' }, BLK_YARD: { name: 'Yard' }, BLK_U: { name: 'Loop end' } };
  return { tracks, blocks };
}

describe('two ends that share a compass edge', () => {
  it('are two choices, each named by where it leads', () => {
    const handler: any = new TrainEditorHandler({ cachedNetwork: horseshoe() } as any);
    handler.renderUnidentifiedTrainPanel('UNIDENT_1', {
      span: { occupied_blocks: ['BLK_U'], occupied_tiles: [[1, 0], [2, 0], [2, 1], [2, 2], [1, 2]] },
    });
    const choices = handler._adoptChoices();
    expect(choices).toHaveLength(2);
    expect(choices.map((c: any) => c.edge)).toEqual(['W', 'W']);
    expect(choices.map((c: any) => c.toward).sort()).toEqual(['Up', 'Yard']);
    const slot = handler._renderAdoptFacingOptions();
    expect(slot).toContain('toward Up');
    expect(slot).toContain('toward Yard');
  });

  it('sends the chosen end with a centred adoption', async () => {
    const adoptUnidentifiedTrain = vi.fn().mockResolvedValue({});
    const handler: any = new TrainEditorHandler({ cachedNetwork: horseshoe(), api: { adoptUnidentifiedTrain } } as any);
    handler.showTrainControlPanel = vi.fn();
    document.body.innerHTML = handler.renderUnidentifiedTrainPanel('UNIDENT_1', {
      span: { occupied_blocks: ['BLK_U'], occupied_tiles: [[1, 0], [2, 0], [2, 1], [2, 2], [1, 2]] },
    });
    handler._refreshAdoptUI();
    const yard = handler._adoptChoices().findIndex((c: any) => c.toward === 'Yard');
    (document.querySelector(`[data-adopt-choice="${yard}"]`) as HTMLElement).click();
    (document.getElementById('unidentified-adopt-id') as HTMLInputElement).value = 'T1';

    await handler._submitAdoptUnidentified('UNIDENT_1');

    const body = adoptUnidentifiedTrain.mock.calls[0][1];
    expect(body.direction).toBe('WEST');
    expect(body.front_end_tile).toEqual({ x: 1, y: 2 });
    expect(body.head_tile).toBeUndefined();
  });
});

describe('placement preview', () => {
  it('walks the body back along a corner tile\'s own rail', () => {
    // A corner head tile exits N and W. Facing N, the body leaves westward —
    // the compass opposite (S) is not an edge this tile has.
    const tracks = [
      { x: 5, y: 4, track_bits: TRACK_EW, block_section_id: 'BLK_L' },
      { x: 6, y: 4, track_bits: TRACK_EW, block_section_id: 'BLK_L' },
      { x: 7, y: 4, track_bits: TRACK_NW, block_section_id: 'BLK_L' },
      { x: 7, y: 5, track_bits: TRACK_NS, block_section_id: 'BLK_UP' },
    ];
    const handler: any = new TrainEditorHandler({
      cachedNetwork: { tracks, blocks: { BLK_L: { length_cm: 150, name: 'Left' }, BLK_UP: { name: 'Up' } } },
    } as any);
    const { tiles, fits } = handler._computeGhostTiles({ x: 7, y: 4 }, 'N', 120);
    expect(tiles).toEqual([
      { x: 7, y: 4 },
      { x: 6, y: 4 },
      { x: 5, y: 4 },
    ]);
    expect(fits).toBe(true);
  });

  it('names each way the front can point by what lies past the block', () => {
    const tracks = [
      { x: 4, y: 4, track_bits: TRACK_EW, block_section_id: 'BLK_W' },
      { x: 5, y: 4, track_bits: TRACK_EW, block_section_id: 'BLK_L' },
      { x: 6, y: 4, track_bits: TRACK_EW, block_section_id: 'BLK_L' },
      { x: 7, y: 4, track_bits: TRACK_NW, block_section_id: 'BLK_L' },
      { x: 7, y: 5, track_bits: TRACK_NS, is_switch: true, switch_id: 'SW_7_5' },
    ];
    const handler: any = new TrainEditorHandler({
      cachedNetwork: { tracks, blocks: { BLK_L: { name: 'Left' }, BLK_W: { name: 'West yard' } }, switches: { SW_7_5: { name: '' } } },
    } as any);
    handler._placePos = { x: 6, y: 4 };
    handler._placeExits = ['E', 'W'];
    const toward = handler._placeChoices().map((c: any) => `${c.edge}:${c.toward}`);
    expect(toward).toEqual(['E:switch SW_7_5', 'W:West yard']);
  });
});

/**
 * Two blocks joined by an undetected switch tile, as on the rig where a loco on
 * Main and a loco on Right became one unidentified train (design_new.md "The
 * form asks where the front is when more than one block is detected").
 *
 *   (0,0) (1,0) Main  |  (2,0) switch  |  (3,0) (4,0) Right
 */
function twoBlocks(api: any = {}): any {
  const tracks = [
    { x: 0, y: 0, track_bits: TRACK_EW, block_section_id: 'BLK_MAIN' },
    { x: 1, y: 0, track_bits: TRACK_EW, block_section_id: 'BLK_MAIN' },
    { x: 2, y: 0, track_bits: TRACK_EW, is_switch: true, switch_id: 'SW_1' },
    { x: 3, y: 0, track_bits: TRACK_EW, block_section_id: 'BLK_RIGHT' },
    { x: 4, y: 0, track_bits: TRACK_EW, block_section_id: 'BLK_RIGHT' },
    { x: 5, y: 0, track_bits: TRACK_EW, block_section_id: 'BLK_UP' },
  ];
  const blocks = {
    BLK_MAIN: { name: 'Main', length_cm: 100 },
    BLK_RIGHT: { name: 'Right', length_cm: 100 },
    BLK_UP: { name: 'Up', length_cm: 100 },
  };
  const switches = { SW_1: { name: '', main_length_cm: 10, diverge_length_cm: 10 } };
  const handler: any = new TrainEditorHandler({ cachedNetwork: { tracks, blocks, switches }, api } as any);
  handler.showTrainControlPanel = vi.fn();
  document.body.innerHTML = handler.renderUnidentifiedTrainPanel('UNIDENT_1', {
    span: { occupied_blocks: ['BLK_MAIN', 'BLK_RIGHT'], occupied_tiles: [[0, 0], [1, 0], [3, 0], [4, 0]] },
  });
  handler._refreshAdoptUI();
  return handler;
}

describe('an unidentified train detected on more than one block', () => {
  it('offers no centred placement and says why', () => {
    const handler = twoBlocks();
    expect(document.getElementById('unidentified-adopt-multiblock')?.textContent).toContain('more than one');
    expect(document.getElementById('unidentified-adopt-head-value')?.textContent).toBe('Not picked');
    // No centred ends are offered to face toward; the front tile comes first.
    expect(handler._adoptChoices()).toEqual([]);
    expect(document.getElementById('unidentified-adopt-facing-slot')?.textContent).toContain('Pick the front tile first');
    handler._adoptFacing = 'E';
    expect((document.getElementById('btn-adopt-unidentified') as HTMLButtonElement).disabled).toBe(true);
    expect(handler._adoptReady()).toBe(false);
  });

  it('previews the body from the picked front and names the block it hands back', () => {
    const handler = twoBlocks();
    handler._setAdoptHeadTile({ x: 4, y: 0 });
    // Right is 100 cm over two tiles, so 80 cm covers both of its tiles.
    (document.getElementById('unidentified-adopt-length') as HTMLInputElement).value = '80';
    const east = handler._adoptChoices().findIndex((c: any) => c.edge === 'E');
    (document.querySelector(`[data-adopt-choice="${east}"]`) as HTMLElement).click();

    const preview = handler._adoptPreview('E');
    expect(preview.tiles).toEqual([
      { x: 4, y: 0 },
      { x: 3, y: 0 },
    ]);
    expect(preview.givenBack).toEqual(['BLK_MAIN']);
    expect(document.getElementById('unidentified-adopt-note')?.textContent).toContain('Main stays unidentified');
    expect((document.getElementById('btn-adopt-unidentified') as HTMLButtonElement).disabled).toBe(false);
  });

  it('refuses a body that would reach a block reading no current', () => {
    const handler = twoBlocks();
    // Front on (3,0) facing W: the body runs back east over Right's two tiles
    // and on into Up, which reads no current.
    handler._setAdoptHeadTile({ x: 3, y: 0 });
    (document.getElementById('unidentified-adopt-length') as HTMLInputElement).value = '120';
    const preview = handler._adoptPreview('W');
    expect(preview.outside).toBe(true);
    handler._adoptFacing = 'W';
    expect(handler._adoptReady()).toBe(false);
  });

  it('sends the picked tile as the head tile', async () => {
    const adoptUnidentifiedTrain = vi.fn().mockResolvedValue({});
    const handler = twoBlocks({ adoptUnidentifiedTrain });
    handler._setAdoptHeadTile({ x: 1, y: 0 });
    (document.getElementById('unidentified-adopt-length') as HTMLInputElement).value = '40';
    const west = handler._adoptChoices().findIndex((c: any) => c.edge === 'W');
    (document.querySelector(`[data-adopt-choice="${west}"]`) as HTMLElement).click();
    (document.getElementById('unidentified-adopt-id') as HTMLInputElement).value = 'T1';

    await handler._submitAdoptUnidentified('UNIDENT_1');

    const body = adoptUnidentifiedTrain.mock.calls[0][1];
    expect(body.head_tile).toEqual({ x: 1, y: 0 });
    expect(body.front_end_tile).toBeUndefined();
    expect(body.direction).toBe('WEST');
  });
});
