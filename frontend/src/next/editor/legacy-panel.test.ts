// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { adoptLegacyPanel, watchLegacyPanelOpen, watchTileBits } from './legacy-panel';

/** Today's inspector markup (static/index.html), trimmed to what is adopted. */
function legacyInspector(): HTMLElement {
  const aside = document.createElement('aside');
  aside.id = 'secondary-sidebar';
  aside.className = 'secondary-sidebar';
  aside.innerHTML = `
    <div class="secondary-sidebar-header">
      <button id="secondary-nav-parent" hidden></button>
      <h3 id="secondary-sidebar-title">Editor</h3>
      <button id="secondary-nav-tearoff"></button>
    </div>
    <div class="secondary-sidebar-tabs" id="secondary-sidebar-tabs"></div>
    <div class="secondary-sidebar-content" id="secondary-sidebar-content"></div>
    <div class="floating-window-resizer"></div>`;
  return aside;
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('hosting today’s panel nodes in the right panel', () => {
  let aside: HTMLElement;
  let header: HTMLElement;
  let body: HTMLElement;

  beforeEach(() => {
    aside = legacyInspector();
    header = document.createElement('div');
    body = document.createElement('div');
    document.body.append(aside, header, body);
  });

  afterEach(() => {
    document.body.replaceChildren();
  });

  it('moves the back link, title and body in, and back to where they were', () => {
    const before = aside.innerHTML;
    const release = adoptLegacyPanel({ header, body });
    expect([...header.children].map((c) => c.id)).toEqual(['secondary-nav-parent', 'secondary-sidebar-title']);
    expect(body.firstElementChild?.id).toBe('secondary-sidebar-content');
    release();
    expect(aside.innerHTML).toBe(before);
    expect(header.children.length).toBe(0);
  });

  it('leaves a node where it is when another screen has taken it', () => {
    const release = adoptLegacyPanel({ header, body });
    const other = document.createElement('div');
    document.body.append(other);
    other.append(document.getElementById('secondary-sidebar-content')!);
    release();
    expect(document.getElementById('secondary-sidebar-content')!.parentElement).toBe(other);
    // The others still go home.
    expect(document.getElementById('secondary-sidebar-title')!.parentElement!.className).toBe('secondary-sidebar-header');
  });

  it('reports the panel opening and closing', async () => {
    const seen: boolean[] = [];
    const stop = watchLegacyPanelOpen((open) => seen.push(open));
    aside.classList.add('visible');
    await flush();
    aside.classList.remove('visible');
    await flush();
    stop();
    aside.classList.add('visible');
    await flush();
    expect(seen).toEqual([false, true, false]);
  });
});

describe('the tile panel’s Advanced section', () => {
  let content: HTMLElement;
  let tile: string | null;

  const renderTilePanel = () => {
    content.innerHTML = `
      <div class="tile-editor">
        <div class="compact-header">(3, 5)</div>
        <div class="track-bits-editor"><input type="checkbox" data-bit="1"></div>
        <div class="tile-bits-shape">Straight</div>
        <h3 class="tile-properties-header">Properties</h3>
      </div>`;
  };
  const advanced = () => content.querySelector<HTMLDetailsElement>('.tile-editor > .next-advanced');

  beforeEach(() => {
    content = document.createElement('div');
    document.body.append(content);
    tile = '3,5';
  });

  afterEach(() => {
    document.body.replaceChildren();
  });

  it('folds the track bits into a closed section at the end of the panel', async () => {
    const stop = watchTileBits(content, () => tile);
    renderTilePanel();
    await flush();
    const details = advanced()!;
    expect(details.open).toBe(false);
    expect(details.querySelector('summary')!.textContent).toBe('Advanced');
    expect(details.querySelector('.track-bits-editor input[data-bit="1"]')).not.toBeNull();
    expect(details.querySelector('.tile-bits-shape')).not.toBeNull();
    expect(content.querySelector('.tile-editor')!.lastElementChild).toBe(details);
    stop();
  });

  it('stays open across a re-draw of the same tile, and starts closed for another', async () => {
    const stop = watchTileBits(content, () => tile);
    renderTilePanel();
    await flush();
    advanced()!.open = true;
    advanced()!.dispatchEvent(new Event('toggle'));
    renderTilePanel();
    await flush();
    expect(advanced()!.open).toBe(true);
    tile = '4,5';
    renderTilePanel();
    await flush();
    expect(advanced()!.open).toBe(false);
    stop();
  });

  it('leaves every other panel alone', async () => {
    const stop = watchTileBits(content, () => null);
    content.innerHTML = '<div class="segment-editor-panel"><div class="track-bits-editor"></div></div>';
    await flush();
    expect(content.querySelector('.next-advanced')).toBeNull();
    stop();
  });
});
