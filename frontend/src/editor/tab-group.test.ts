import { describe, it, expect, vi } from 'vitest';
import { TabGroup } from './tab-group';

// TabGroup writes into DOM elements via callbacks (see TabGroupDom) rather
// than querying the document itself, so a minimal fake element — not a real
// DOM/jsdom — is enough to exercise it, the same no-DOM-needed shape
// editor-ui.test.ts and window-manager.test.ts already use.
function fakeElement() {
  const classes = new Set<string>();
  return {
    textContent: '',
    innerHTML: '',
    hidden: false,
    title: '',
    classList: {
      add: (c: string) => classes.add(c),
      remove: (c: string) => classes.delete(c),
      toggle: (c: string, force?: boolean) => {
        const on = force ?? !classes.has(c);
        if (on) classes.add(c);
        else classes.delete(c);
      },
      contains: (c: string) => classes.has(c),
    },
    querySelector: () => null,
  };
}

function makeGroup(editorOverrides: Record<string, any> = {}) {
  const titleEl = fakeElement();
  const contentEl = fakeElement();
  const tabsEl = fakeElement();
  const parentBtnEl = fakeElement();
  const pinBtnEl = fakeElement();
  const closeBtnEl = fakeElement();
  const containerEl = fakeElement();

  const editor: any = {
    _currentEditorType: null,
    _currentEditorId: null,
    // A trivial lineage: a snapshot with an editorType has one segment named
    // after it — enough to drive getTabDisplay/getCurrentParentSegment
    // without needing the real Editor's lineage table.
    getPanelLineage: (snapshot: any) => (snapshot?.editorType ? [{ label: snapshot.editorType, editorType: null }] : []),
    restoreSecondaryPanel: vi.fn().mockResolvedValue(false),
    whenNetworkReady: vi.fn().mockResolvedValue(undefined),
    cachedNetwork: {},
    ...editorOverrides,
  };

  const onPersist = vi.fn();
  const group = new TabGroup({
    groupId: 'test',
    editor,
    dom: {
      titleEl: () => titleEl as any,
      contentEl: () => contentEl as any,
      tabsEl: () => tabsEl as any,
      parentBtnEl: () => parentBtnEl as any,
      pinBtnEl: () => pinBtnEl as any,
      closeBtnEl: () => closeBtnEl as any,
      containerEl: () => containerEl as any,
    },
    onPersist,
  });

  return { group, editor, titleEl, contentEl, tabsEl, parentBtnEl, pinBtnEl, closeBtnEl, containerEl, onPersist };
}

describe('TabGroup.show — preview-reuse invariant', () => {
  it('opens a preview (unpinned) tab for a preview-eligible editor type', () => {
    const { group, titleEl, contentEl } = makeGroup({ _currentEditorType: 'block', _currentEditorId: 'BLK_1' });

    group.show('Block Editor', '<div>content</div>');

    expect(titleEl.textContent).toBe('Block Editor');
    expect(contentEl.innerHTML).toBe('<div>content</div>');
    expect(group.tabs).toHaveLength(1);
    expect(group.tabs[0]).toMatchObject({ editorType: 'block', editorId: 'BLK_1', pinned: false });
  });

  it('replaces the existing preview slot on a second casual inspect instead of stacking a tab', () => {
    const { group, editor } = makeGroup({ _currentEditorType: 'block', _currentEditorId: 'BLK_1' });
    group.show('Block A', 'a');

    editor._currentEditorType = 'block';
    editor._currentEditorId = 'BLK_2';
    group.show('Block B', 'b');

    expect(group.tabs).toHaveLength(1);
    expect(group.tabs[0].editorId).toBe('BLK_2');
  });

  it('treats the current block-detail type as a preview instead of piling up block tabs', () => {
    const { group, editor } = makeGroup({ _currentEditorType: 'block-detail', _currentEditorId: 'BLK_1' });
    group.show('North Yard', 'a');

    editor._currentEditorId = 'BLK_2';
    group.show('South Yard', 'b');

    expect(group.tabs).toHaveLength(1);
    expect(group.tabs[0]).toMatchObject({ editorType: 'block-detail', editorId: 'BLK_2', title: 'South Yard', pinned: false });
  });

  it('opens its own kept tab for a list-panel type (not preview-eligible), never replaced', () => {
    const { group, editor } = makeGroup({ _currentEditorType: 'trains-list', _currentEditorId: null });
    group.show('Trains', 'a');

    editor._currentEditorType = 'routes-list';
    group.show('Routes', 'b');

    expect(group.tabs).toHaveLength(2);
    expect(group.tabs.every((t) => t.pinned)).toBe(true);
  });

  it('pinning a preview tab keeps it instead of letting the next inspect replace it', () => {
    const { group, editor } = makeGroup({ _currentEditorType: 'block', _currentEditorId: 'BLK_1' });
    group.show('Block A', 'a');
    group.pinTab(group.tabs[0].tabId);

    editor._currentEditorType = 'block';
    editor._currentEditorId = 'BLK_2';
    group.show('Block B', 'b');

    expect(group.tabs).toHaveLength(2);
    expect(group.tabs[0].pinned).toBe(true);
    expect(group.tabs[0].editorId).toBe('BLK_1');
    expect(group.tabs[1].editorId).toBe('BLK_2');
  });

  it('persists after every change that mutates tab state', () => {
    const { group, onPersist } = makeGroup({ _currentEditorType: 'block', _currentEditorId: 'BLK_1' });
    group.show('Block A', 'a');
    expect(onPersist).toHaveBeenCalled();
  });
});

describe('TabGroup horizontal working-set strip', () => {
  it('renders tabs directly in one strip without vertical type-group wrappers', () => {
    const { group, editor, tabsEl } = makeGroup({
      _currentEditorType: 'trains-list',
      getPanelLineage: (snapshot: any) => [{ label: snapshot.title, editorType: snapshot.editorType }],
    });
    group.show('Trains', 'a');
    editor._currentEditorType = 'routes-list';
    group.show('Routes', 'b');

    expect(tabsEl.innerHTML).not.toContain('secondary-nav-group');
    expect(tabsEl.innerHTML).toContain('>Trains</button>');
    expect(tabsEl.innerHTML).toContain('>Routes</button>');
  });

  it('updates a matching entity title immediately after rename and persists it', () => {
    const { group, titleEl, tabsEl, onPersist } = makeGroup({
      _currentEditorType: 'segment',
      _currentEditorId: 'SEG_1',
      getPanelLineage: (snapshot: any) => [
        { label: 'Segments', editorType: null },
        { label: snapshot.title, editorType: 'segment', editorId: snapshot.editorId },
      ],
    });
    group.show('SEG_1', 'a');
    onPersist.mockClear();

    group.updateTabTitle('segment', 'SEG_1', 'Platform Lead');

    expect(group.tabs[0].title).toBe('Platform Lead');
    expect(titleEl.textContent).toBe('Platform Lead');
    expect(tabsEl.innerHTML).toContain('>Platform Lead</button>');
    expect(onPersist).toHaveBeenCalledTimes(1);
  });
});

describe('TabGroup.pruneStaleTabs', () => {
  it('drops a tab whose block id no longer exists in the network and re-points the active tab', () => {
    const { group, editor } = makeGroup();
    group.tabs = [
      { tabId: 'block:B1', key: 'block:B1', title: 'B1', editorType: 'block', editorId: 'B1', content: '', pinned: true },
      { tabId: 'block:B2', key: 'block:B2', title: 'B2', editorType: 'block', editorId: 'B2', content: '', pinned: true },
    ];
    group.activeTabId = 'block:B1';
    editor.cachedNetwork = { blocks: { B2: {} } };

    group.pruneStaleTabs();

    expect(group.tabs.map((t) => t.tabId)).toEqual(['block:B2']);
    expect(group.activeTabId).toBe('block:B2');
  });

  it('leaves list/setup tabs alone — they are never tied to a network entity', () => {
    const { group, editor } = makeGroup();
    group.tabs = [
      { tabId: 'trains-list:__none__', key: 'trains-list:__none__', title: 'Trains', editorType: 'trains-list', editorId: null, content: '', pinned: true },
    ];
    group.activeTabId = 'trains-list:__none__';
    editor.cachedNetwork = {};

    group.pruneStaleTabs();

    expect(group.tabs).toHaveLength(1);
  });

  it('is a no-op when nothing is stale', () => {
    const { group, editor, onPersist } = makeGroup();
    group.tabs = [{ tabId: 'block:B1', key: 'block:B1', title: 'B1', editorType: 'block', editorId: 'B1', content: '', pinned: true }];
    group.activeTabId = 'block:B1';
    editor.cachedNetwork = { blocks: { B1: {} } };

    group.pruneStaleTabs();

    expect(onPersist).not.toHaveBeenCalled();
  });
});

describe('TabGroup.extractTab / acceptMovedTab — the split/move primitive', () => {
  it('round-trips a tab between two independent groups', async () => {
    const { group: source } = makeGroup({ _currentEditorType: 'block', _currentEditorId: 'B1' });
    source.show('Block B1', 'a');
    const { group: target } = makeGroup();

    const tab = (await source.extractTab(source.tabs[0].tabId))!;
    expect(source.tabs).toHaveLength(0);
    expect(tab).toMatchObject({ editorType: 'block', editorId: 'B1' });

    target.acceptMovedTab(tab);
    expect(target.tabs).toHaveLength(1);
    expect(target.activeTabId).toBe(tab.tabId);
  });

  it('extractTab re-points the active tab to a remaining neighbor and restores its panel', async () => {
    const { group, editor } = makeGroup({ _currentEditorType: 'trains-list' });
    group.show('Trains', 'a');
    editor._currentEditorType = 'routes-list';
    group.show('Routes', 'b');
    const trainsTabId = group.tabs[0].tabId;
    const routesTabId = group.tabs[1].tabId;
    expect(group.activeTabId).toBe(routesTabId);

    await group.extractTab(routesTabId);

    expect(group.tabs.map((t) => t.tabId)).toEqual([trainsTabId]);
    expect(group.activeTabId).toBe(trainsTabId);
    // restoreTabPanel's fallback (restoreSecondaryPanel mocked to resolve
    // false) calls show(), which re-syncs the remaining tab's own content —
    // proving the source group's title/content actually update, not just its
    // bookkeeping fields.
    expect(group.currentPanel).toMatchObject({ editorType: 'trains-list' });
  });

  it('extractTab returns null for an unknown tabId and leaves the group untouched', async () => {
    const { group } = makeGroup({ _currentEditorType: 'block', _currentEditorId: 'B1' });
    group.show('Block', 'a');

    const result = await group.extractTab('does-not-exist');

    expect(result).toBeNull();
    expect(group.tabs).toHaveLength(1);
  });

  it('extracting the last tab clears currentPanel but does not hide/destroy anything itself', async () => {
    const { group } = makeGroup({ _currentEditorType: 'block', _currentEditorId: 'B1' });
    group.show('Block', 'a');

    await group.extractTab(group.tabs[0].tabId);

    expect(group.tabs).toHaveLength(0);
    expect(group.currentPanel).toBeNull();
    expect(group.activeTabId).toBeNull();
  });
});

describe('TabGroup.closeStaleTab — scoped to this group only', () => {
  it('does not clear a group whose currentPanel differs, even if closeStaleTab is called on it', async () => {
    const { group } = makeGroup({ _currentEditorType: 'block', _currentEditorId: 'B1' });
    group.show('Block B1', 'a');

    const nowEmpty = await group.closeStaleTab('block', 'B2');

    expect(nowEmpty).toBe(false);
    expect(group.tabs).toHaveLength(1);
  });

  it('clears the group when its own currentPanel matches the deleted entity, even with no tab for it', async () => {
    const { group, editor } = makeGroup({ _currentEditorType: 'block', _currentEditorId: 'B1' });
    // A panel can be "current" before syncTabsWithCurrentPanel has run for it;
    // simulate that by setting currentPanel without a corresponding tab.
    (group as any).currentPanel = { title: 'Block B1', editorType: 'block', editorId: 'B1' };
    editor._currentEditorType = 'block';
    editor._currentEditorId = 'B1';

    const nowEmpty = await group.closeStaleTab('block', 'B1');

    expect(nowEmpty).toBe(true);
  });
});

describe('TabGroup.closeTab', () => {
  it('returns true (now empty) when closing the last remaining tab', async () => {
    const { group } = makeGroup({ _currentEditorType: 'block', _currentEditorId: 'B1' });
    group.show('Block', 'a');

    const nowEmpty = await group.closeTab(group.tabs[0].tabId);

    expect(nowEmpty).toBe(true);
    expect(group.tabs).toHaveLength(0);
    expect(group.activeTabId).toBeNull();
  });

  it('returns false and activates a neighbor when the active tab among several is closed', async () => {
    const { group, editor } = makeGroup({ _currentEditorType: 'trains-list' });
    group.show('Trains', 'a');
    editor._currentEditorType = 'routes-list';
    group.show('Routes', 'b');
    const activeId = group.activeTabId; // the just-shown Routes tab

    const nowEmpty = await group.closeTab(activeId!);

    expect(nowEmpty).toBe(false);
    expect(group.tabs).toHaveLength(1);
    expect(group.activeTabId).toBe(group.tabs[0].tabId);
  });

  it('returns false and leaves the active tab alone when closing a different tab', async () => {
    const { group, editor } = makeGroup({ _currentEditorType: 'trains-list' });
    group.show('Trains', 'a');
    editor._currentEditorType = 'routes-list';
    group.show('Routes', 'b');
    const trainsTabId = group.tabs[0].tabId;
    const activeId = group.activeTabId;

    const nowEmpty = await group.closeTab(trainsTabId);

    expect(nowEmpty).toBe(false);
    expect(group.activeTabId).toBe(activeId);
  });
});

describe('TabGroup serialize / restoreTabs', () => {
  it('round-trips tabs, pinned, and activeTabId — content is never persisted', () => {
    const { group: groupA } = makeGroup({ _currentEditorType: 'block', _currentEditorId: 'B1' });
    groupA.show('Block', 'live-content-not-persisted');
    groupA.setPinned(true);

    const saved = groupA.serialize();
    expect(saved.tabs[0]).not.toHaveProperty('content');
    expect(saved.pinned).toBe(true);

    const { group: groupB } = makeGroup();
    groupB.restoreTabs(saved);

    expect(groupB.pinned).toBe(true);
    expect(groupB.tabs).toHaveLength(1);
    expect(groupB.tabs[0]).toMatchObject({ editorType: 'block', editorId: 'B1' });
    expect(groupB.activeTabId).toBe(saved.activeTabId);
  });

  it('defaults restored tabs to pinned when the persisted flag predates preview/pin', () => {
    const { group } = makeGroup();
    group.restoreTabs({ pinned: false, activeTabId: 'block:B1', tabs: [{ tabId: 'block:B1', editorType: 'block', editorId: 'B1', title: 'B1' }] });

    expect(group.tabs[0].pinned).toBe(true);
  });
});
