/* eslint-env jest */

import PinSelection from '@/components/bulk/PinSelection';

describe('PinSelection', () => {
  it('adds a Shift range in current list order and updates the anchor', () => {
    const selection = new PinSelection();
    selection.setLoadedRows([9, 8, 7, 6].map(id => ({ id, owned: true })));
    selection.toggle(8, {});
    selection.toggle(6, { shiftKey: true });

    expect(selection.snapshot()).toEqual({
      selectedIds: [8, 7, 6],
      anchorId: 6,
      ownershipById: {
        9: true, 8: true, 7: true, 6: true,
      },
      scope: 'loaded',
    });
  });

  it.each([
    ['Ctrl', { ctrlKey: true }],
    ['Command', { metaKey: true }],
  ])('toggles an id and refreshes the anchor with %s', (name, modifiers) => {
    const selection = new PinSelection();
    selection.setLoadedRows([
      { id: 3, owned: true },
      { id: 2, owned: true },
    ]);

    selection.toggle(3, {});
    selection.toggle(2, modifiers);
    selection.toggle(2, modifiers);

    expect(selection.snapshot()).toMatchObject({
      selectedIds: [3],
      anchorId: 2,
    });
  });

  it('selects only requested loaded ids in current order', () => {
    const selection = new PinSelection();
    selection.setLoadedRows([
      { id: 5, owned: true },
      { id: 4, owned: false },
      { id: 3, owned: true },
    ]);

    selection.selectLoaded([3, 5]);

    expect(selection.snapshot()).toMatchObject({
      selectedIds: [5, 3],
      anchorId: null,
      scope: 'loaded',
    });
  });

  it('keeps the existing selection when a later loaded block is added', () => {
    const selection = new PinSelection();
    selection.setLoadedRows([
      { id: 9, owned: true },
      { id: 8, owned: true },
    ]);
    selection.toggle(8, {});
    selection.setLoadedRows([
      { id: 9, owned: true },
      { id: 8, owned: true },
      { id: 7, owned: true },
      { id: 6, owned: true },
    ]);

    expect(selection.snapshot().selectedIds).toEqual([8]);
  });

  it('applies a server scope without selecting later loaded ids', () => {
    const selection = new PinSelection();
    selection.setLoadedRows([
      { id: 9, owned: true },
      { id: 8, owned: true },
    ]);
    selection.applyScope([
      { id: 9, owned: true },
      { id: 7, owned: false },
    ]);
    selection.setLoadedRows([
      { id: 9, owned: true },
      { id: 8, owned: true },
      { id: 10, owned: true },
    ]);

    expect(selection.snapshot().selectedIds).toEqual([9, 7]);
    expect(selection.snapshot().ownershipById[7]).toBe(false);
  });

  it('preserves the server result order for ids that are not loaded', () => {
    const selection = new PinSelection();
    selection.setLoadedRows([{ id: 9, owned: true }]);
    selection.applyScope([
      { id: 7, owned: true },
      { id: 9, owned: true },
      { id: 8, owned: false },
    ]);

    expect(selection.snapshot()).toMatchObject({
      selectedIds: [9, 7, 8],
      scope: 'all',
      ownershipById: { 9: true, 7: true, 8: false },
    });
  });

  it('rejects duplicate and unknown ids without changing selection', () => {
    const selection = new PinSelection();
    selection.setLoadedRows([
      { id: 2, owned: true },
      { id: 1, owned: false },
    ]);

    selection.selectLoaded([2]);
    selection.selectLoaded([2, 2, 99]);
    selection.toggle(99, {});
    selection.applyScope([
      { id: 2, owned: true },
      { id: 2, owned: true },
    ]);

    expect(selection.snapshot()).toEqual({
      selectedIds: [2],
      anchorId: null,
      ownershipById: { 2: true, 1: false },
      scope: 'loaded',
    });
  });

  it('reports an invalid scope explicitly without changing an existing all scope', () => {
    const selection = new PinSelection();
    selection.setLoadedRows([
      { id: 3, owned: true },
      { id: 2, owned: true },
    ]);
    selection.applyScope([
      { id: 3, owned: true },
      { id: 1, owned: false },
    ]);
    const before = selection.snapshot();

    const result = selection.tryApplyScope([
      { id: 3, owned: true },
      { id: 3, owned: false },
    ]);

    expect(result).toEqual({ applied: false, snapshot: before });
    expect(selection.snapshot()).toEqual(before);
  });

  it('reports a valid scope application explicitly', () => {
    const selection = new PinSelection();
    selection.setLoadedRows([{ id: 3, owned: true }]);

    const result = selection.tryApplyScope([
      { id: 3, owned: true },
      { id: 1, owned: false },
    ]);

    expect(result).toEqual({
      applied: true,
      snapshot: {
        selectedIds: [3, 1],
        anchorId: null,
        ownershipById: { 3: true, 1: false },
        scope: 'all',
      },
    });
  });

  it('stores false ownership for loaded non-owned rows', () => {
    const selection = new PinSelection();
    selection.setLoadedRows([{ id: 1, owned: false }]);

    expect(selection.snapshot().ownershipById).toEqual({ 1: false });
  });

  it('clears selection, anchor, scope, and ownership', () => {
    const selection = new PinSelection();
    selection.setLoadedRows([{ id: 1, owned: true }]);
    selection.toggle(1, {});
    selection.applyScope([{ id: 1, owned: true }, { id: 2, owned: false }]);

    selection.clear();

    expect(selection.snapshot()).toEqual({
      selectedIds: [],
      anchorId: null,
      ownershipById: {},
      scope: 'loaded',
    });
  });

  it('returns a stable snapshot for 50,000 selected ids', () => {
    const ids = Array.from({ length: 50000 }, (_, index) => index + 1);
    const selection = new PinSelection(ids);
    selection.selectLoaded(ids);

    const snapshot = selection.snapshot();

    expect(snapshot.selectedIds).toHaveLength(50000);
    expect(snapshot.selectedIds[0]).toBe(1);
    expect(snapshot.selectedIds[49999]).toBe(50000);
    expect(snapshot.ownershipById[1]).toBe(false);
  });
});
