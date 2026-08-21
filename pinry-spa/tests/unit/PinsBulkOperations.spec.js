/* eslint-env jest */

import flushPromises from 'flush-promises';
import { createLocalVue, mount, shallowMount } from '@vue/test-utils';

import API from '@/components/api';
import PinBulkBoardDialog from '@/components/bulk/PinBulkBoardDialog.vue';
import PinBulkEdit, { buildChanges } from '@/components/bulk/PinBulkEdit.vue';
import { openPinBulkBoard, openPinBulkEdit } from '@/components/modals';
import Pins from '@/components/Pins.vue';

let mountedWrappers = [];

function deferred() {
  const request = {};
  request.promise = new Promise((resolve, reject) => {
    request.resolve = resolve;
    request.reject = reject;
  });
  request.promise.catch(() => {});
  return request;
}

function defaultStatus(operation) {
  if (operation === 'delete' || operation === 'delete_if_exclusive_to_board') {
    return 'deleted';
  }
  if (operation === 'move_between_boards') return 'moved';
  return 'updated';
}

function bulkResponse(ids, statuses = {}, operation = 'update') {
  return Promise.resolve({
    data: {
      operation,
      results: ids.map(id => ({ id, status: statuses[id] || defaultStatus(operation) })),
    },
  });
}

function pin(id, author = 'owner') {
  return {
    id,
    private: false,
    description: `Pin ${id}`,
    tags: [],
    referer: '',
    url: `https://example.test/original-${id}.jpg`,
    submitter: {
      id: author === 'owner' ? 1 : 2,
      username: author,
      gravatar: '',
    },
    image: {
      image: `https://example.test/image-${id}.jpg`,
      width: 240,
      thumbnail: {
        image: `https://example.test/thumb-${id}.jpg`,
        width: 240,
        height: 180,
      },
    },
  };
}

function mountBoardDialog(propsData) {
  const wrapper = shallowMount(PinBulkBoardDialog, {
    propsData: {
      mode: 'add',
      sourceBoardId: null,
      selectedIds: [41, 42],
      username: 'owner',
      ...propsData,
    },
    mocks: { $t: (key, values) => (values ? `${key}:${values.count}` : key) },
  });
  mountedWrappers.push(wrapper);
  return wrapper;
}

function mountBulkEdit() {
  const wrapper = shallowMount(PinBulkEdit, {
    propsData: { selectedIds: [41, 42] },
    mocks: { $t: (key, values) => (values ? `${key}:${values.count}` : key) },
    stubs: ['b-taginput'],
  });
  mountedWrappers.push(wrapper);
  return wrapper;
}

function mountPins({
  pinFilters = { userFilter: 'owner' },
  pins = [pin(41), pin(40), pin(39)],
} = {}) {
  API.fetchPins.mockResolvedValue({ data: { results: pins, next: null } });
  API.Board.get.mockResolvedValue({
    data: { id: Number(pinFilters.boardFilter), submitter: { username: 'owner' } },
  });
  const localVue = createLocalVue();
  localVue.directive('masonry', {});
  localVue.directive('masonry-tile', {});
  const dialog = { confirm: jest.fn() };
  const modal = { open: jest.fn() };
  const wrapper = mount(Pins, {
    localVue,
    propsData: { pinFilters },
    mocks: {
      $buefy: { dialog, modal },
      $t: (key, values) => {
        if (!values) return key;
        if (values.completed !== undefined) return `${values.completed}/${values.total}`;
        return `${key}:${values.count}`;
      },
    },
    stubs: {
      EditorUI: true,
      loadingSpinner: true,
      noMore: true,
      'router-link': { template: '<a href="#"><slot /></a>' },
    },
  });
  wrapper.dialog = dialog;
  wrapper.modal = modal;
  mountedWrappers.push(wrapper);
  return wrapper;
}

async function settle() {
  await flushPromises();
  await flushPromises();
}

function selectScope(wrapper, ids, owned = true) {
  const rows = ids.map(id => ({ id, owned }));
  wrapper.vm.updateSelection(wrapper.vm.selectionModel.applyScope(rows), {
    active: true,
    allCount: ids.length,
    result: null,
  });
}

function dispatchKey(target, key) {
  const event = new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
  });
  target.dispatchEvent(event);
  return event;
}

describe('bulk operation dialogs', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    API.Board.fetchFullList = jest.fn().mockResolvedValue({
      data: [
        { id: 3, name: 'Source' },
        { id: 7, name: 'Target' },
      ],
    });
    API.Pin.bulk = jest.fn(
      payload => bulkResponse(payload.pin_ids, {}, payload.operation),
    );
  });

  afterEach(() => {
    mountedWrappers.forEach(wrapper => wrapper.destroy());
    mountedWrappers = [];
  });

  it.each([
    ['add', null, { operation: 'add_to_board', board_id: 7 }],
    [
      'move',
      3,
      { operation: 'move_between_boards', source_board_id: 3, target_board_id: 7 },
    ],
  ])('submits the exact %s board payload', async (mode, sourceBoardId, expected) => {
    const wrapper = mountBoardDialog({ mode, sourceBoardId });
    await settle();
    await wrapper.find('[data-test="bulk-board-target"]').setValue('7');
    await wrapper.find('[data-test="bulk-board-submit"]').trigger('click');
    await settle();

    expect(API.Pin.bulk).toHaveBeenCalledWith({ ...expected, pin_ids: [41, 42] });
    expect(wrapper.emitted('completed')[0][0]).toMatchObject({
      total: 2, completed: 2, succeeded: 2,
    });
  });

  it('loads only the owner boards and excludes the move source board', async () => {
    const wrapper = mountBoardDialog({ mode: 'move', sourceBoardId: 3 });
    await settle();

    expect(API.Board.fetchFullList).toHaveBeenCalledWith('owner');
    expect(wrapper.vm.boardOptions).toEqual([{ id: 7, name: 'Target' }]);
    expect(wrapper.find('[data-test="bulk-board-target"]').text()).not.toContain('Source');
  });

  it('ignores late board-list and operation callbacks after destruction', async () => {
    const boards = deferred();
    API.Board.fetchFullList.mockReturnValueOnce(boards.promise);
    const wrapper = mountBoardDialog();
    wrapper.destroy();
    boards.resolve({ data: [{ id: 7, name: 'Target' }] });
    await settle();
    expect(wrapper.vm.boardOptions).toEqual([]);

    API.Board.fetchFullList.mockResolvedValueOnce({ data: [{ id: 7, name: 'Target' }] });
    const operation = deferred();
    API.Pin.bulk.mockReturnValueOnce(operation.promise);
    const running = mountBoardDialog();
    await settle();
    running.vm.targetBoardId = 7;
    const submission = running.vm.submit();
    expect(running.vm.operationInFlight).toBe(true);
    running.destroy();
    operation.resolve({
      data: {
        operation: 'add_to_board',
        results: [
          { id: 41, status: 'updated' }, { id: 42, status: 'updated' },
        ],
      },
    });
    await submission;

    expect(running.vm.operationInFlight).toBe(true);
    expect(running.emitted('completed')).toBeUndefined();
  });

  it('builds only explicitly selected privacy and tag changes', () => {
    expect(buildChanges({
      privacyMode: 'private', tagMode: null, tagValues: ['ignored'],
    })).toEqual({ private: true });
    expect(buildChanges({
      privacyMode: 'public', tagMode: 'add', tagValues: [' alpha ', '', ' beta '],
    })).toEqual({
      private: false,
      tags: { mode: 'add', values: ['alpha', 'beta'] },
    });
    expect(buildChanges({
      privacyMode: null, tagMode: 'replace', tagValues: ['   '],
    })).toEqual({ tags: { mode: 'replace', values: [] } });
  });

  it('enables submit only for a chosen effective change', async () => {
    const wrapper = mountBulkEdit();
    expect(wrapper.vm.canSubmit).toBe(false);

    await wrapper.setData({ tagMode: 'add', tagValues: [' ', ''] });
    expect(wrapper.vm.canSubmit).toBe(false);
    await wrapper.setData({ tagMode: 'remove', tagValues: [' alpha '] });
    expect(wrapper.vm.canSubmit).toBe(true);
    await wrapper.setData({ tagMode: 'replace', tagValues: [] });
    expect(wrapper.vm.canSubmit).toBe(true);
    await wrapper.setData({ tagMode: null, privacyMode: 'public' });
    expect(wrapper.vm.canSubmit).toBe(true);
  });

  it.each([
    ['public', 'add'],
    ['public', 'remove'],
    ['private', 'add'],
    ['private', 'remove'],
  ])(
    'blocks %s with whitespace-only %s tags instead of sending an invalid payload',
    async (privacyMode, tagMode) => {
      const wrapper = mountBulkEdit();
      await wrapper.setData({ privacyMode, tagMode, tagValues: [' ', '\t', '\n'] });

      expect(wrapper.vm.canSubmit).toBe(false);
      expect(wrapper.find('[data-test="bulk-edit-submit"]').attributes('disabled'))
        .toBe('disabled');
      expect(wrapper.vm.submit()).toBeNull();
      expect(API.Pin.bulk).not.toHaveBeenCalled();
    },
  );

  it('submits exact update changes and blocks a second in-flight submit', async () => {
    const operation = deferred();
    API.Pin.bulk.mockReturnValue(operation.promise);
    const wrapper = mountBulkEdit();
    await wrapper.setData({
      privacyMode: 'private', tagMode: 'remove', tagValues: [' alpha ', ''],
    });
    wrapper.vm.submit();
    wrapper.vm.submit();

    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
    expect(API.Pin.bulk).toHaveBeenCalledWith({
      operation: 'update',
      pin_ids: [41, 42],
      changes: { private: true, tags: { mode: 'remove', values: ['alpha'] } },
    });
    operation.resolve({
      data: {
        operation: 'update',
        results: [
          { id: 41, status: 'updated' }, { id: 42, status: 'updated' },
        ],
      },
    });
    await settle();
    expect(wrapper.emitted('completed')).toHaveLength(1);
  });

  it('opens board and edit modal helpers with copied operation props', () => {
    const vm = { $buefy: { modal: { open: jest.fn() } } };
    const selectedIds = [41, 42];
    openPinBulkBoard(vm, {
      mode: 'move', sourceBoardId: 3, selectedIds, username: 'owner',
    });
    openPinBulkEdit(vm, { selectedIds });
    selectedIds.push(43);

    const boardConfig = vm.$buefy.modal.open.mock.calls[0][0];
    expect(boardConfig).toMatchObject({
      parent: vm,
      component: PinBulkBoardDialog,
      hasModalCard: true,
      props: {
        mode: 'move', sourceBoardId: 3, selectedIds: [41, 42], username: 'owner',
      },
    });
    expect(vm.$buefy.modal.open.mock.calls[1][0]).toMatchObject({
      parent: vm,
      component: PinBulkEdit,
      hasModalCard: true,
      props: { selectedIds: [41, 42] },
    });
  });
});

describe('Pins bulk operation orchestration', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.spyOn(Pins.methods, 'initializeMeta').mockImplementation(function initializeMeta() {
      this.editorMeta.user = { loggedIn: true, meta: { username: 'owner' } };
      this.metaReady.user = true;
      this.syncLoadedSelection();
    });
    API.fetchPins = jest.fn();
    API.fetchPin = jest.fn();
    API.Board.get = jest.fn();
    API.Pin.fetchSelectionIds = jest.fn();
    API.Pin.bulk = jest.fn(
      payload => bulkResponse(payload.pin_ids, {}, payload.operation),
    );
  });

  afterEach(() => {
    mountedWrappers.forEach(wrapper => wrapper.destroy());
    mountedWrappers = [];
    Pins.methods.initializeMeta.mockRestore();
  });

  it('opens add, move, and edit operations with immutable selection snapshots', async () => {
    const mine = mountPins();
    await settle();
    selectScope(mine, [41, 40]);
    await mine.vm.$nextTick();
    await mine.find('[data-test="pin-selection-add-to-board"]').trigger('click');
    await mine.find('[data-test="pin-selection-edit"]').trigger('click');

    expect(mine.modal.open.mock.calls[0][0].props).toEqual({
      mode: 'add', sourceBoardId: null, selectedIds: [41, 40], username: 'owner',
    });
    expect(mine.modal.open.mock.calls[1][0].props).toEqual({ selectedIds: [41, 40] });

    const board = mountPins({ pinFilters: { boardFilter: 3 }, pins: [pin(41, 'other')] });
    await settle();
    selectScope(board, [41], false);
    await board.vm.$nextTick();
    await board.find('[data-test="pin-selection-move"]').trigger('click');
    expect(board.modal.open.mock.calls[0][0].props).toEqual({
      mode: 'move', sourceBoardId: 3, selectedIds: [41], username: 'owner',
    });
  });

  it('guards add, edit, and delete in methods when selection includes a non-owned pin', async () => {
    const wrapper = mountPins({ pins: [pin(41), pin(40, 'other')] });
    await settle();
    selectScope(wrapper, [41, 40], false);

    wrapper.vm.openBulkBoard('add');
    wrapper.vm.openBulkEdit();
    wrapper.vm.confirmBulkDelete();

    expect(wrapper.modal.open).not.toHaveBeenCalled();
    expect(wrapper.dialog.confirm).not.toHaveBeenCalled();
    expect(API.Pin.bulk).not.toHaveBeenCalled();
  });

  it('includes the selected count in delete confirmation and consumes it once', async () => {
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41, 40]);
    await wrapper.vm.$nextTick();

    await wrapper.find('[data-test="pin-selection-delete"]').trigger('click');
    await wrapper.find('[data-test="pin-selection-delete"]').trigger('click');
    expect(wrapper.dialog.confirm).toHaveBeenCalledTimes(1);
    const config = wrapper.dialog.confirm.mock.calls[0][0];
    expect(config.message).toBe('bulkPinDeleteConfirm:2');
    config.onConfirm();
    config.onConfirm();
    await settle();
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
  });

  it('shows committed progress and blocks every second execution while running', async () => {
    const ids = Array.from({ length: 430 }, (_, index) => index + 1);
    const first = deferred();
    const second = deferred();
    const third = deferred();
    const fourth = deferred();
    API.Pin.bulk
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
      .mockReturnValueOnce(third.promise)
      .mockReturnValueOnce(fourth.promise);
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, ids);
    wrapper.vm.confirmBulkDelete();
    wrapper.dialog.confirm.mock.calls[0][0].onConfirm();
    first.resolve({
      data: {
        operation: 'delete',
        results: ids.slice(0, 50).map(id => ({ id, status: 'deleted' })),
      },
    });
    await settle();
    second.resolve({
      data: {
        operation: 'delete',
        results: ids.slice(50, 100).map(id => ({ id, status: 'deleted' })),
      },
    });
    await settle();
    third.resolve({
      data: {
        operation: 'delete',
        results: ids.slice(100, 150).map(id => ({ id, status: 'deleted' })),
      },
    });
    await settle();

    expect(wrapper.vm.selection.operationInFlight).toBe(true);
    expect(wrapper.vm.selection.progress).toMatchObject({ completed: 150, total: 430 });
    expect(wrapper.find('[data-test="pin-bulk-progress"]').text()).toBe('150/430');
    wrapper.vm.confirmBulkDelete();
    wrapper.vm.openBulkEdit();
    wrapper.vm.openBulkBoard('add');
    expect(wrapper.dialog.confirm).toHaveBeenCalledTimes(1);
    expect(wrapper.modal.open).not.toHaveBeenCalled();
    expect(API.Pin.bulk).toHaveBeenCalledTimes(4);
  });

  it('keeps the bulk lock when Escape tries to exit and re-enter selection', async () => {
    const operation = deferred();
    API.Pin.bulk
      .mockReturnValueOnce(operation.promise)
      .mockImplementation(
        payload => bulkResponse(payload.pin_ids, { 41: 'deleted' }, payload.operation),
      );
    const wrapper = mountPins({ pins: [pin(41)] });
    await settle();
    selectScope(wrapper, [41]);
    wrapper.vm.confirmBulkDelete();
    wrapper.dialog.confirm.mock.calls[0][0].onConfirm();

    dispatchKey(document, 'Escape');
    wrapper.vm.exitSelection();
    wrapper.vm.enterSelection();
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');
    wrapper.vm.confirmBulkDelete();
    if (wrapper.dialog.confirm.mock.calls[1]) {
      wrapper.dialog.confirm.mock.calls[1][0].onConfirm();
    }

    expect(wrapper.vm.selection.active).toBe(true);
    expect(wrapper.vm.selection.selectedIds).toEqual([41]);
    expect(wrapper.vm.selection.operationInFlight).toBe(true);
    expect(wrapper.dialog.confirm).toHaveBeenCalledTimes(1);
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);

    operation.resolve({
      data: { operation: 'delete', results: [{ id: 41, status: 'deleted' }] },
    });
    await settle();

    expect(wrapper.vm.selection.operationInFlight).toBe(false);
    expect(wrapper.vm.selection.progress).toBeNull();
    expect(wrapper.vm.selection.result).toMatchObject({ succeeded: 1, failed: 0 });
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
  });

  it('shows succeeded and failed counts and retries only refreshed failures', async () => {
    API.Pin.bulk
      .mockImplementationOnce(payload => bulkResponse(payload.pin_ids, {
        41: 'deleted', 40: 'deleted', 39: 'failed',
      }, payload.operation))
      .mockImplementationOnce(
        payload => bulkResponse(payload.pin_ids, { 39: 'deleted' }, payload.operation),
      );
    API.Pin.fetchSelectionIds.mockResolvedValue({
      data: { count: 2, results: [{ id: 40, owned: true }, { id: 39, owned: true }] },
    });
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41, 40, 39]);
    wrapper.vm.confirmBulkDelete();
    wrapper.dialog.confirm.mock.calls[0][0].onConfirm();
    await settle();

    expect(API.Pin.fetchSelectionIds).toHaveBeenCalledWith({ boardId: null });
    expect(wrapper.vm.selection.result).toMatchObject({
      succeeded: 2, preserved: 0, failed: 1, retryIds: [39],
    });
    expect(wrapper.find('[data-test="pin-bulk-result"]').text())
      .toContain('bulkPinResultSucceeded:2');
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
    await wrapper.find('[data-test="pin-bulk-retry"]').trigger('click');
    await settle();
    expect(API.Pin.bulk).toHaveBeenLastCalledWith({ operation: 'delete', pin_ids: [39] });
  });

  it('never auto-retries an ambiguous delete and intersects remaining board IDs', async () => {
    API.Pin.bulk
      .mockRejectedValueOnce(new Error('network'))
      .mockImplementationOnce(
        payload => bulkResponse(payload.pin_ids, { 40: 'deleted' }, payload.operation),
      );
    API.Pin.fetchSelectionIds.mockResolvedValue({
      data: { count: 2, results: [{ id: 40, owned: true }, { id: 9, owned: true }] },
    });
    const wrapper = mountPins({ pinFilters: { boardFilter: 3 } });
    await settle();
    selectScope(wrapper, [41, 40, 39]);
    wrapper.vm.confirmBulkDelete();
    wrapper.dialog.confirm.mock.calls[0][0].onConfirm();
    await settle();

    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
    expect(API.Pin.fetchSelectionIds).toHaveBeenCalledWith({ boardId: 3 });
    expect(wrapper.vm.selection.result).toMatchObject({
      error: 'request_failed', retryIds: [40],
    });
    await wrapper.find('[data-test="pin-bulk-retry"]').trigger('click');
    await settle();
    expect(API.Pin.bulk).toHaveBeenCalledTimes(2);
    expect(API.Pin.bulk).toHaveBeenLastCalledWith({ operation: 'delete', pin_ids: [40] });
  });

  it.each([
    ['missing count', { results: [{ id: 41, owned: true }] }],
    ['negative count', { count: -1, results: [{ id: 41, owned: true }] }],
    ['fractional count', { count: 1.5, results: [{ id: 41, owned: true }] }],
    ['missing results', { count: 1 }],
    ['non-array results', { count: 1, results: { id: 41, owned: true } }],
    ['count mismatch', { count: 2, results: [{ id: 41, owned: true }] }],
    ['duplicate ids', {
      count: 2,
      results: [{ id: 41, owned: true }, { id: 41, owned: true }],
    }],
    ['zero id', {
      count: 2,
      results: [{ id: 41, owned: true }, { id: 0, owned: true }],
    }],
    ['string id', {
      count: 2,
      results: [{ id: 41, owned: true }, { id: '40', owned: true }],
    }],
    ['missing ownership', { count: 1, results: [{ id: 41 }] }],
    ['non-boolean ownership', { count: 1, results: [{ id: 41, owned: 1 }] }],
  ])('rejects malformed retry scope: %s', async (name, data) => {
    API.Pin.bulk.mockRejectedValueOnce(new Error('network'));
    API.Pin.fetchSelectionIds.mockResolvedValue({ data });
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41, 40]);
    wrapper.vm.confirmBulkDelete();
    wrapper.dialog.confirm.mock.calls[0][0].onConfirm();
    await settle();

    expect(wrapper.vm.selection.result).toMatchObject({ retryIds: [] });
    expect(wrapper.find('[data-test="pin-bulk-retry"]').exists()).toBe(false);
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
  });

  it('intersects retry candidates with only explicitly owned refreshed rows', async () => {
    API.Pin.bulk.mockRejectedValueOnce(new Error('network'));
    API.Pin.fetchSelectionIds.mockResolvedValue({
      data: {
        count: 2,
        results: [{ id: 41, owned: false }, { id: 40, owned: true }],
      },
    });
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41, 40]);
    wrapper.vm.confirmBulkDelete();
    wrapper.dialog.confirm.mock.calls[0][0].onConfirm();
    await settle();

    expect(wrapper.vm.selection.result).toMatchObject({ retryIds: [40] });
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
  });

  it('offers no retry when refreshing the delete scope fails', async () => {
    API.Pin.bulk.mockRejectedValueOnce(new Error('network'));
    API.Pin.fetchSelectionIds.mockRejectedValueOnce(new Error('refresh failed'));
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41, 40]);
    wrapper.vm.confirmBulkDelete();
    wrapper.dialog.confirm.mock.calls[0][0].onConfirm();
    await settle();

    expect(wrapper.vm.selection.result).toMatchObject({ retryIds: [] });
    expect(wrapper.find('[data-test="pin-bulk-retry"]').exists()).toBe(false);
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
  });

  it('ignores a completed modal callback after a route reset', async () => {
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41]);
    wrapper.vm.openBulkEdit();
    const { completed } = wrapper.modal.open.mock.calls[0][0].events;

    await wrapper.setProps({ pinFilters: { userFilter: 'other' } });
    await settle();
    completed({
      total: 1, completed: 1, succeeded: 1, preserved: 0, failed: 0,
    });
    await settle();

    expect(wrapper.vm.selection.result).toBeNull();
    expect(wrapper.vm.selection.active).toBe(false);
  });

  it.each(['resolve', 'reject'])(
    'ignores late delete %s callbacks after destruction',
    async (outcome) => {
      const operation = deferred();
      API.Pin.bulk.mockReturnValue(operation.promise);
      const wrapper = mountPins();
      await settle();
      selectScope(wrapper, [41]);
      wrapper.vm.confirmBulkDelete();
      wrapper.dialog.confirm.mock.calls[0][0].onConfirm();
      expect(wrapper.vm.selection.operationInFlight).toBe(true);
      wrapper.destroy();

      if (outcome === 'resolve') {
        operation.resolve({
          data: { operation: 'delete', results: [{ id: 41, status: 'deleted' }] },
        });
      } else {
        operation.reject(new Error('network'));
      }
      await settle();

      expect(API.Pin.fetchSelectionIds).not.toHaveBeenCalled();
      expect(wrapper.vm.selection.operationInFlight).toBe(false);
      expect(wrapper.vm.selection.result).toBeNull();
    },
  );
});
