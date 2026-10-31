// @vitest-environment happy-dom
import { render } from 'preact';
import { act } from 'preact/test-utils';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { PolledBody, type PolledList } from './left-list';
import { routePickerPrompt } from './train-panel';

describe('PolledBody: a list read from its endpoint never hides a failed read', () => {
  let root: HTMLElement;

  beforeEach(() => {
    root = document.createElement('div');
    document.body.appendChild(root);
  });

  afterEach(() => {
    render(null, root);
    root.remove();
  });

  const show = (list: PolledList<string>) =>
    act(() => {
      render(
        <PolledBody list={list} what="routes" empty={<p class="empty-line">No routes yet.</p>}>
          {(items) => items.map((r) => <p key={r} class="row">{r}</p>)}
        </PolledBody>,
        root
      );
    });

  it('says Loading before the first answer', () => {
    show({ items: null, error: null });
    expect(root.textContent).toBe('Loading…');
  });

  it('says why when no read has worked yet, instead of looking empty', () => {
    show({ items: null, error: 'server not answering' });
    const msg = root.querySelector('.empty-state-error');
    expect(msg?.textContent).toBe('Could not read the routes: server not answering. Trying again.');
    expect(root.querySelector('.empty-line')).toBeNull();
  });

  it('shows the empty line only after a read that worked', () => {
    show({ items: [], error: null });
    expect(root.querySelector('.empty-line')).not.toBeNull();
  });

  it('keeps the last list under a note while the latest read failed', () => {
    show({ items: ['R1', 'R2'], error: 'timeout' });
    expect([...root.querySelectorAll('.row')].map((p) => p.textContent)).toEqual(['R1', 'R2']);
    expect(root.querySelector('.banner-warn')?.textContent).toContain('This is the last list read');
  });
});

describe('routePickerPrompt', () => {
  it('never says "No routes yet" before the routes were read, or after a read failed', () => {
    expect(routePickerPrompt(null, null)).toBe('Reading the routes…');
    expect(routePickerPrompt(null, 'timeout')).toBe('Could not read the routes');
    expect(routePickerPrompt([], null)).toBe('No routes yet');
    expect(routePickerPrompt([{}], 'timeout')).toBe('Choose a route…');
  });
});
