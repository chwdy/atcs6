import { describe, expect, it } from 'vitest';

import { renderPolaritySegment } from './helpers';

// The Block panel's Track direction control. It is wiring, not the routing
// policy the neighbouring "Allowed travel direction" control writes.
//
// Assertions are on the rendered markup rather than a live DOM: this repo runs
// vitest with no DOM environment, and the render/bind split exists so the
// interesting half is a pure string.
describe('renderPolaritySegment', () => {
  const activeCell = (html: string): string | null => {
    const m = html.match(/class="switch-btn active"\s+id="[^"]*-(\w+)"/);
    return m ? m[1] : null;
  };

  it('marks Normal active when the block has no polarity flag', () => {
    expect(activeCell(renderPolaritySegment('block', null, { idBase: 'p' }))).toBe('normal');
  });

  it('marks Normal active for an explicitly normal block', () => {
    const html = renderPolaritySegment('block', { polarity_reversed: false }, { idBase: 'p' });
    expect(activeCell(html)).toBe('normal');
  });

  it('marks Reversed active for a reversed block', () => {
    const html = renderPolaritySegment('block', { polarity_reversed: true }, { idBase: 'p' });
    expect(activeCell(html)).toBe('reversed');
  });

  it('marks exactly one cell active', () => {
    for (const entity of [null, { polarity_reversed: false }, { polarity_reversed: true }]) {
      const html = renderPolaritySegment('block', entity, { idBase: 'p' });
      expect(html.match(/switch-btn active/g)?.length).toBe(1);
    }
  });

  it('emits the two ids bindPolaritySegment looks up', () => {
    // The binder resolves `${idBase}-normal` / `${idBase}-reversed`; if these
    // drift apart the control renders but silently never saves.
    const html = renderPolaritySegment('block', null, { idBase: 'block-polarity' });
    expect(html).toContain('id="block-polarity-normal"');
    expect(html).toContain('id="block-polarity-reversed"');
  });

  it('sanitizes an idBase into something safe to interpolate', () => {
    const html = renderPolaritySegment('block', null, { idBase: 'a b"><script>' });
    expect(html).not.toContain('<script>');
  });

  it('says the setting covers the whole block', () => {
    // The user-facing promise this field makes: one block, every segment.
    expect(renderPolaritySegment('block', null)).toContain('every segment in the block');
  });

  it('renders both cells disabled when the rails are not bound', () => {
    // The flag lives on the `block_track` binding, so with no binding there is
    // nothing to store it on — the panel disables the control and says why.
    const html = renderPolaritySegment('block', null, { idBase: 'p', disabled: true });
    expect(html.match(/ disabled/g)?.length).toBe(2);
  });

  it('renders enabled cells by default', () => {
    const html = renderPolaritySegment('block', null, { idBase: 'p' });
    expect(html.match(/ disabled/g)).toBeNull();
  });
});
