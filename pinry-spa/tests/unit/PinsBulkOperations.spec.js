/* eslint-env jest */

import flushPromises from 'flush-promises';
import { createLocalVue, mount, shallowMount } from '@vue/test-utils';

import API from '@/components/api';
import bus from '@/components/utils/bus';
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

function bulkAxiosResponse(ids, statuses = {}, operation = 'update') {
  const results = ids.map((id) => {
    const status = statuses[id] || defaultStatus(operation);
    if (status === 'preserved') return { id, status, code: 'shared_pin' };
    if (status === 'failed') {
      return {
        id, status, code: 'internal_error', retryable: false,
      };
    }
    return { id, status };
  });
  return {
    status: 200,
    data: {
      operation,
      succeeded: results.filter(
        item => !['preserved', 'failed'].includes(item.status),
      ).length,
      preserved: results.filter(item => item.status === 'preserved').length,
      failed: results.filter(item => item.status === 'failed').length,
      results,
    },
  };
}

function bulkResponse(ids, statuses = {}, operation = 'update') {
  return Promise.resolve(bulkAxiosResponse(ids, statuses, operation));
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

function mountBulkEdit(propsData = {}) {
  const wrapper = shallowMount(PinBulkEdit, {
    propsData: { selectedIds: [41, 42], ...propsData },
    mocks: { $t: (key, values) => (values ? `${key}:${values.count}` : key) },
    stubs: ['b-taginput'],
  });
  mountedWrappers.push(wrapper);
  return wrapper;
}

function mountConfiguredBulkDialog(config) {
  const wrapper = shallowMount(config.component, {
    propsData: config.props,
    mocks: { $t: (key, values) => (values ? `${key}:${values.count}` : key) },
    stubs: ['b-taginput'],
  });
  Object.entries(config.events || {}).forEach(([event, handler]) => {
    wrapper.vm.$on(event, handler);
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

async function readyRetryDialog(mode) {
  if (mode === 'edit') {
    const wrapper = mountBulkEdit();
    await wrapper.setData({ privacyMode: 'private' });
    return {
      wrapper,
      operation: 'update',
      resultSelector: '[data-test="bulk-edit-result"]',
      retrySelector: '[data-test="bulk-edit-retry"]',
    };
  }

  const wrapper = mountBoardDialog({
    mode,
    sourceBoardId: mode === 'move' ? 3 : null,
  });
  await settle();
  await wrapper.find('[data-test="bulk-board-target"]').setValue('7');
  return {
    wrapper,
    operation: mode === 'move' ? 'move_between_boards' : 'add_to_board',
    resultSelector: '[data-test="bulk-board-result"]',
    retrySelector: '[data-test="bulk-board-retry"]',
  };
}

function selectScope(wrapper, ids, owned = true) {
  const rows = ids.map(id => ({ id, owned }));
  wrapper.vm.updateSelection(wrapper.vm.selectionModel.applyScope(rows), {
    active: true,
    allCount: ids.length,
    result: null,
  });
}

function dispatchKey(target, key, options = {}) {
  const event = new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
    ...options,
  });
  target.dispatchEvent(event);
  return event;
}

describe('bulk operation dialogs', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    API.Board.create = jest.fn();
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

  it('shows board creation only for add mode with labelled accessible controls', async () => {
    const add = mountBoardDialog({ mode: 'add' });
    const move = mountBoardDialog({ mode: 'move', sourceBoardId: 3 });
    await settle();

    expect(add.find('[data-test="bulk-board-create-form"]').exists()).toBe(true);
    expect(move.find('[data-test="bulk-board-create-form"]').exists()).toBe(false);
    expect(add.find('label[for="pin-bulk-new-board-name"]').exists()).toBe(true);
    expect(add.find('label[for="pin-bulk-new-board-private"]').exists()).toBe(true);
    expect(add.find('[data-test="bulk-board-create-status"]')
      .attributes('aria-live')).toBe('polite');
  });

  it('creates a private board once, selects it, then applies the existing bulk add', async () => {
    const creation = deferred();
    API.Board.create.mockReturnValueOnce(creation.promise);
    const refreshBoards = jest.spyOn(bus.bus, '$emit');
    const wrapper = mountBoardDialog({ mode: 'add' });
    await settle();
    const target = wrapper.find('[data-test="bulk-board-target"]');
    const focus = jest.spyOn(target.element, 'focus');

    await wrapper.find('[data-test="bulk-board-new-name"]').setValue('  New Board  ');
    await wrapper.find('[data-test="bulk-board-new-private"]').setChecked(true);
    await wrapper.find('[data-test="bulk-board-create-form"]').trigger('submit');
    await wrapper.find('[data-test="bulk-board-create-form"]').trigger('submit');

    expect(API.Board.create).toHaveBeenCalledTimes(1);
    expect(API.Board.create).toHaveBeenCalledWith('New Board', true);
    expect(wrapper.find('[data-test="bulk-board-target"]').attributes('disabled'))
      .toBe('disabled');
    expect(wrapper.find('[data-test="bulk-board-submit"]').attributes('disabled'))
      .toBe('disabled');
    expect(wrapper.find('[data-test="bulk-board-close"]').attributes('disabled'))
      .toBe('disabled');

    creation.resolve({ id: 11, name: 'New Board', private: true });
    await settle();

    expect(wrapper.vm.boardOptions).toEqual([
      { id: 11, name: 'New Board' },
      { id: 3, name: 'Source' },
      { id: 7, name: 'Target' },
    ]);
    expect(wrapper.vm.targetBoardId).toBe(11);
    expect(refreshBoards).toHaveBeenCalledWith(bus.events.refreshBoards);
    expect(focus).toHaveBeenCalled();
    expect(API.Pin.bulk).not.toHaveBeenCalled();

    await wrapper.find('[data-test="bulk-board-submit"]').trigger('click');
    await settle();
    expect(API.Pin.bulk).toHaveBeenCalledWith({
      operation: 'add_to_board', board_id: 11, pin_ids: [41, 42],
    });
    refreshBoards.mockRestore();
  });

  it('blocks a blank board name locally and preserves the current target', async () => {
    const wrapper = mountBoardDialog({ mode: 'add' });
    await settle();
    await wrapper.find('[data-test="bulk-board-target"]').setValue('7');
    await wrapper.find('[data-test="bulk-board-new-name"]').setValue('   ');
    await wrapper.find('[data-test="bulk-board-create-form"]').trigger('submit');

    expect(API.Board.create).not.toHaveBeenCalled();
    expect(wrapper.vm.targetBoardId).toBe(7);
    expect(wrapper.find('[data-test="bulk-board-create-error"]').text())
      .toBe('bulkPinBoardNameRequired');
  });

  it('keeps create fields and target after failure and allows an explicit retry', async () => {
    API.Board.create
      .mockRejectedValueOnce(undefined)
      .mockResolvedValueOnce({ id: 12, name: 'Retry Board', private: true });
    const wrapper = mountBoardDialog({ mode: 'add' });
    await settle();
    await wrapper.find('[data-test="bulk-board-target"]').setValue('7');
    await wrapper.find('[data-test="bulk-board-new-name"]').setValue('Retry Board');
    await wrapper.find('[data-test="bulk-board-new-private"]').setChecked(true);

    await wrapper.find('[data-test="bulk-board-create-form"]').trigger('submit');
    await settle();
    expect(wrapper.find('[data-test="bulk-board-create-error"]').text())
      .toBe('bulkPinBoardCreateError');
    expect(wrapper.find('[data-test="bulk-board-new-name"]').element.value)
      .toBe('Retry Board');
    expect(wrapper.find('[data-test="bulk-board-new-private"]').element.checked).toBe(true);
    expect(wrapper.vm.targetBoardId).toBe(7);

    await wrapper.find('[data-test="bulk-board-create-form"]').trigger('submit');
    await settle();
    expect(API.Board.create).toHaveBeenCalledTimes(2);
    expect(wrapper.vm.targetBoardId).toBe(12);
  });

  it('ignores a board creation response after destruction', async () => {
    const creation = deferred();
    API.Board.create.mockReturnValueOnce(creation.promise);
    const wrapper = mountBoardDialog({ mode: 'add' });
    await settle();
    await wrapper.find('[data-test="bulk-board-new-name"]').setValue('Late Board');
    const request = wrapper.vm.createBoard();
    wrapper.destroy();
    creation.resolve({ id: 13, name: 'Late Board', private: false });
    await request;

    expect(wrapper.vm.targetBoardId).toBeNull();
    expect(wrapper.vm.boardOptions.some(board => board.id === 13)).toBe(false);
    expect(wrapper.emitted('started')).toBeUndefined();
    expect(wrapper.emitted('completed')).toBeUndefined();
  });

  it('does not create a board while list loading or bulk retry state owns the modal', async () => {
    const boards = deferred();
    API.Board.fetchFullList.mockReturnValueOnce(boards.promise);
    const loading = mountBoardDialog({ mode: 'add' });
    expect(loading.find('[data-test="bulk-board-create"]').attributes('disabled'))
      .toBe('disabled');
    await expect(loading.vm.createBoard()).resolves.toBeNull();

    boards.resolve({ data: [{ id: 7, name: 'Target' }] });
    await settle();
    API.Pin.bulk.mockRejectedValueOnce(new Error('network'));
    await loading.find('[data-test="bulk-board-target"]').setValue('7');
    await loading.find('[data-test="bulk-board-submit"]').trigger('click');
    await settle();

    expect(loading.vm.result.retryable).toBe(true);
    expect(loading.find('[data-test="bulk-board-create"]').attributes('disabled'))
      .toBe('disabled');
    await expect(loading.vm.createBoard()).resolves.toBeNull();
    expect(API.Board.create).not.toHaveBeenCalled();
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
    operation.resolve(bulkAxiosResponse([41, 42], {}, 'add_to_board'));
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
    operation.resolve(bulkAxiosResponse([41, 42], {}, 'update'));
    await settle();
    expect(wrapper.emitted('completed')).toHaveLength(1);
  });

  it('emits started once and prevents resubmitting a completed board operation', async () => {
    const wrapper = mountBoardDialog();
    await settle();
    await wrapper.find('[data-test="bulk-board-target"]').setValue('7');

    await wrapper.find('[data-test="bulk-board-submit"]').trigger('click');
    await settle();
    wrapper.vm.submit();

    expect(wrapper.emitted('started')).toHaveLength(1);
    expect(wrapper.emitted('completed')).toHaveLength(1);
    expect(wrapper.vm.canSubmit).toBe(false);
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
  });

  it('emits started once and prevents resubmitting a completed edit operation', async () => {
    const wrapper = mountBulkEdit();
    await wrapper.setData({ privacyMode: 'private' });

    await wrapper.find('[data-test="bulk-edit-submit"]').trigger('click');
    await settle();
    wrapper.vm.submit();

    expect(wrapper.emitted('started')).toHaveLength(1);
    expect(wrapper.emitted('completed')).toHaveLength(1);
    expect(wrapper.vm.canSubmit).toBe(false);
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['board', () => mountBoardDialog()],
    ['edit', () => mountBulkEdit()],
  ])('emits closed once before explicitly closing the %s modal', async (name, mountDialog) => {
    const wrapper = mountDialog();
    await settle();
    const order = [];
    wrapper.vm.$on('closed', () => order.push('closed'));
    wrapper.vm.$parent.close = jest.fn(() => order.push('parent-close'));

    wrapper.vm.close();
    wrapper.vm.close();

    expect(order).toEqual(['closed', 'parent-close']);
  });

  it('opens board and edit modal helpers with copied props and lifecycle events', () => {
    const vm = { $buefy: { modal: { open: jest.fn() } } };
    const selectedIds = [41, 42];
    const boardCompleted = jest.fn();
    const boardStarted = jest.fn();
    const boardClosed = jest.fn();
    const editCompleted = jest.fn();
    const editStarted = jest.fn();
    const editClosed = jest.fn();
    openPinBulkBoard(vm, {
      mode: 'move', sourceBoardId: 3, selectedIds, username: 'owner',
    }, boardCompleted, { started: boardStarted, closed: boardClosed });
    openPinBulkEdit(
      vm,
      { selectedIds },
      editCompleted,
      { started: editStarted, closed: editClosed },
    );
    selectedIds.push(43);

    const boardConfig = vm.$buefy.modal.open.mock.calls[0][0];
    expect(boardConfig).toMatchObject({
      parent: vm,
      component: PinBulkBoardDialog,
      hasModalCard: true,
      props: {
        mode: 'move', sourceBoardId: 3, selectedIds: [41, 42], username: 'owner',
      },
      events: {
        started: boardStarted,
        completed: boardCompleted,
        closed: boardClosed,
      },
    });
    expect(vm.$buefy.modal.open.mock.calls[1][0]).toMatchObject({
      parent: vm,
      component: PinBulkEdit,
      hasModalCard: true,
      props: { selectedIds: [41, 42] },
      events: {
        started: editStarted,
        completed: editCompleted,
        closed: editClosed,
      },
    });
  });

  it.each([
    ['add', 'transport'],
    ['add', 'invalid response'],
    ['move', 'transport'],
    ['move', 'invalid response'],
    ['edit', 'transport'],
    ['edit', 'invalid response'],
  ])('keeps %s %s failure retryable until eventual success', async (mode, failureMode) => {
    API.Pin.bulk.mockReset();
    if (failureMode === 'transport') {
      API.Pin.bulk.mockRejectedValueOnce(new Error('network'));
    } else {
      API.Pin.bulk.mockResolvedValueOnce({ status: 200, data: { results: [] } });
    }
    API.Pin.bulk.mockImplementationOnce(
      payload => bulkResponse(payload.pin_ids, {}, payload.operation),
    );
    const {
      wrapper, operation, resultSelector, retrySelector,
    } = await readyRetryDialog(mode);

    await wrapper.vm.submit();
    await settle();

    expect(wrapper.vm.operationCompleted).toBe(false);
    expect(wrapper.emitted('completed')).toBeUndefined();
    expect(wrapper.emitted('settled')).toHaveLength(1);
    expect(wrapper.find(resultSelector).text())
      .toContain('bulkPinResultFailed:2');
    expect(wrapper.find(retrySelector).text()).toBe('bulkPinRetry');

    await wrapper.find(retrySelector).trigger('click');
    await settle();

    expect(API.Pin.bulk).toHaveBeenCalledTimes(2);
    expect(API.Pin.bulk.mock.calls[1][0]).toEqual(API.Pin.bulk.mock.calls[0][0]);
    expect(API.Pin.bulk.mock.calls[1][0]).toMatchObject({
      operation,
      pin_ids: [41, 42],
    });
    expect(wrapper.emitted('started')).toHaveLength(2);
    expect(wrapper.emitted('settled')).toHaveLength(1);
    expect(wrapper.emitted('completed')).toHaveLength(1);
    expect(wrapper.vm.operationCompleted).toBe(true);
    expect(wrapper.find(retrySelector).exists()).toBe(false);
  });

  it.each([
    ['add', 'transport', null, { operation: 'add_to_board', board_id: 7 }],
    [
      'move',
      'invalid response',
      3,
      { operation: 'move_between_boards', source_board_id: 3, target_board_id: 7 },
    ],
  ])('retries %s with the initial target after a later chunk %s failure', async (
    mode,
    failureMode,
    sourceBoardId,
    expected,
  ) => {
    const selectedIds = Array.from({ length: 51 }, (_value, index) => index + 1);
    API.Pin.bulk.mockReset();
    API.Pin.bulk.mockImplementationOnce(
      payload => bulkResponse(payload.pin_ids, {}, payload.operation),
    );
    if (failureMode === 'transport') {
      API.Pin.bulk.mockRejectedValueOnce(new Error('network'));
    } else {
      API.Pin.bulk.mockResolvedValueOnce({ status: 200, data: { results: [] } });
    }
    API.Pin.bulk.mockImplementation(
      payload => bulkResponse(payload.pin_ids, {}, payload.operation),
    );
    const wrapper = mountBoardDialog({ mode, sourceBoardId, selectedIds });
    await settle();
    await wrapper.find('[data-test="bulk-board-target"]').setValue('7');

    await wrapper.vm.submit();
    await wrapper.setData({ targetBoardId: 8 });
    await wrapper.find('[data-test="bulk-board-retry"]').trigger('click');
    await settle();

    expect(API.Pin.bulk.mock.calls.slice(2).map(call => call[0])).toEqual([
      { ...expected, pin_ids: selectedIds.slice(0, 50) },
      { ...expected, pin_ids: [51] },
    ]);
    expect(wrapper.emitted('settled')).toHaveLength(1);
    expect(wrapper.emitted('completed')).toHaveLength(1);
  });

  it.each([
    [
      'add',
      'move',
      null,
      { operation: 'add_to_board', board_id: 7 },
    ],
    [
      'move',
      'add',
      3,
      { operation: 'move_between_boards', source_board_id: 3, target_board_id: 7 },
    ],
  ])('retries the initial %s operation after mode changes to %s', async (
    mode,
    retryMode,
    sourceBoardId,
    expected,
  ) => {
    const selectedIds = Array.from({ length: 51 }, (_value, index) => index + 1);
    API.Pin.bulk.mockReset();
    API.Pin.bulk
      .mockImplementationOnce(
        payload => bulkResponse(payload.pin_ids, {}, payload.operation),
      )
      .mockRejectedValueOnce(new Error('network'))
      .mockImplementation(
        payload => bulkResponse(payload.pin_ids, {}, payload.operation),
      );
    const wrapper = mountBoardDialog({ mode, sourceBoardId, selectedIds });
    await settle();
    await wrapper.find('[data-test="bulk-board-target"]').setValue('7');

    await wrapper.vm.submit();
    await wrapper.setProps({ mode: retryMode });
    await wrapper.find('[data-test="bulk-board-retry"]').trigger('click');
    await settle();

    expect(API.Pin.bulk.mock.calls.slice(2).map(call => call[0])).toEqual([
      { ...expected, pin_ids: selectedIds.slice(0, 50) },
      { ...expected, pin_ids: [51] },
    ]);
    expect(wrapper.emitted('settled')).toHaveLength(1);
    expect(wrapper.emitted('completed')).toHaveLength(1);
    expect(wrapper.vm.operationCompleted).toBe(true);
  });

  it('retries edit with the exact initial changes after a later chunk failure', async () => {
    const selectedIds = Array.from({ length: 51 }, (_value, index) => index + 1);
    const initialChanges = {
      private: true,
      tags: { mode: 'remove', values: ['alpha', 'beta'] },
    };
    API.Pin.bulk.mockReset();
    API.Pin.bulk
      .mockImplementationOnce(
        payload => bulkResponse(payload.pin_ids, {}, payload.operation),
      )
      .mockRejectedValueOnce(new Error('network'))
      .mockImplementation(
        payload => bulkResponse(payload.pin_ids, {}, payload.operation),
      );
    const wrapper = mountBulkEdit({ selectedIds });
    await wrapper.setData({
      privacyMode: 'private', tagMode: 'remove', tagValues: [' alpha ', 'beta'],
    });

    await wrapper.vm.submit();
    await wrapper.setData({
      privacyMode: 'public', tagMode: 'replace', tagValues: ['changed'],
    });
    await wrapper.find('[data-test="bulk-edit-retry"]').trigger('click');
    await settle();

    expect(API.Pin.bulk.mock.calls.slice(2).map(call => call[0])).toEqual([
      {
        operation: 'update', pin_ids: selectedIds.slice(0, 50), changes: initialChanges,
      },
      { operation: 'update', pin_ids: [51], changes: initialChanges },
    ]);
    expect(wrapper.emitted('settled')).toHaveLength(1);
    expect(wrapper.emitted('completed')).toHaveLength(1);
  });

  it('disables the target control while a board retry is available', async () => {
    API.Pin.bulk.mockRejectedValueOnce(new Error('network'));
    const wrapper = mountBoardDialog();
    await settle();
    await wrapper.find('[data-test="bulk-board-target"]').setValue('7');

    await wrapper.vm.submit();
    await wrapper.vm.$nextTick();

    expect(wrapper.find('[data-test="bulk-board-target"]').attributes('disabled'))
      .toBe('disabled');
  });

  it('disables every edit control while an edit retry is available', async () => {
    API.Pin.bulk.mockRejectedValueOnce(new Error('network'));
    const wrapper = mountBulkEdit();
    await wrapper.setData({
      privacyMode: 'private', tagMode: 'remove', tagValues: ['alpha'],
    });

    await wrapper.vm.submit();
    await wrapper.vm.$nextTick();

    expect(wrapper.find('[data-test="bulk-edit-privacy"]').attributes('disabled'))
      .toBe('disabled');
    expect(wrapper.find('[data-test="bulk-edit-tag-mode"]').attributes('disabled'))
      .toBe('disabled');
    expect(wrapper.find('[data-test="bulk-edit-tags"]').attributes('disabled'))
      .toBe('true');
  });
});

describe('Pins bulk operation orchestration', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    Object.defineProperty(window, 'crypto', {
      configurable: true,
      value: {
        getRandomValues(values) {
          values.set([5]);
          return values;
        },
      },
    });
    window.scrollTo = jest.fn();
    jest.spyOn(Pins.methods, 'initializeMeta').mockImplementation(function initializeMeta() {
      this.editorMeta.user = { loggedIn: true, meta: { username: 'owner' } };
      this.metaReady.user = true;
      this.syncLoadedSelection();
    });
    API.fetchPins = jest.fn();
    API.fetchPin = jest.fn();
    API.Board.get = jest.fn();
    API.Board.fetchFullList = jest.fn().mockResolvedValue({
      data: [{ id: 7, name: 'Target' }],
    });
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
    const addConfig = mine.modal.open.mock.calls[0][0];
    addConfig.events.closed();
    await mine.vm.$nextTick();
    await mine.find('[data-test="pin-selection-edit"]').trigger('click');

    expect(mine.modal.open.mock.calls[0][0].props).toMatchObject({
      mode: 'add', sourceBoardId: null, selectedIds: [41, 40], username: 'owner',
    });
    expect(mine.modal.open.mock.calls[0][0].props.canStartOperation).toEqual(
      expect.any(Function),
    );
    expect(mine.modal.open.mock.calls[1][0].props).toMatchObject({ selectedIds: [41, 40] });
    expect(mine.modal.open.mock.calls[1][0].props.canStartOperation).toEqual(
      expect.any(Function),
    );

    const board = mountPins({ pinFilters: { boardFilter: 3 }, pins: [pin(41, 'other')] });
    await settle();
    selectScope(board, [41], false);
    await board.vm.$nextTick();
    await board.find('[data-test="pin-selection-move"]').trigger('click');
    expect(board.modal.open.mock.calls[0][0].props).toMatchObject({
      mode: 'move', sourceBoardId: 3, selectedIds: [41], username: 'owner',
    });
  });

  it('opens only one bulk modal for two rapid actions and locks parent interactions', async () => {
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41]);
    await wrapper.vm.$nextTick();

    wrapper.vm.openBulkEdit();
    wrapper.vm.openBulkBoard('add');
    wrapper.modal.open.mock.calls[0][0].events.started();
    dispatchKey(document, 'Escape');
    dispatchKey(document, 'a', { ctrlKey: true });
    await wrapper.find('[data-test="pin-card-40"]').trigger('click');
    wrapper.vm.confirmBulkDelete();

    expect(wrapper.modal.open).toHaveBeenCalledTimes(1);
    expect(wrapper.dialog.confirm).not.toHaveBeenCalled();
    expect(wrapper.vm.selection).toMatchObject({
      active: true,
      selectedIds: [41],
      operationInFlight: true,
      bulkOperationInFlight: true,
    });
  });

  it('releases the parent latch when modal opening throws', async () => {
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41]);
    wrapper.modal.open.mockImplementationOnce(() => { throw new Error('open failed'); });

    wrapper.vm.openBulkEdit();
    expect(wrapper.vm.selection.operationInFlight).toBe(false);

    wrapper.vm.openBulkBoard('add');
    expect(wrapper.modal.open).toHaveBeenCalledTimes(2);
    expect(wrapper.vm.selection.operationInFlight).toBe(true);
  });

  it('consumes close once and ignores stale callbacks while a newer modal owns the latch', async () => {
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41]);

    wrapper.vm.openBulkEdit();
    const staleEvents = wrapper.modal.open.mock.calls[0][0].events;
    staleEvents.closed();
    staleEvents.closed();
    expect(wrapper.vm.selection.operationInFlight).toBe(false);

    wrapper.vm.openBulkBoard('add');
    const currentEvents = wrapper.modal.open.mock.calls[1][0].events;
    staleEvents.started();
    staleEvents.completed({
      total: 1, completed: 1, succeeded: 1, preserved: 0, failed: 0,
    });

    expect(wrapper.vm.selection.operationInFlight).toBe(true);
    expect(wrapper.vm.selection.result).toBeNull();
    currentEvents.closed();
    expect(wrapper.vm.selection.operationInFlight).toBe(false);
  });

  it('consumes a completed modal generation exactly once', async () => {
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41]);
    wrapper.vm.openBulkEdit();
    const [{ events }] = wrapper.modal.open.mock.calls[0];
    const result = {
      total: 1, completed: 1, succeeded: 1, preserved: 0, failed: 0,
    };
    const generation = wrapper.vm.requestGeneration;

    events.started();
    events.started();
    events.completed(result);
    events.completed(result);
    await settle();

    expect(wrapper.vm.requestGeneration).toBe(generation + 1);
    expect(wrapper.vm.selection.result).toMatchObject({
      ...result,
      operation: 'update',
    });
    expect(wrapper.vm.selection.operationInFlight).toBe(false);
  });

  it('keeps the parent lock when a closed callback arrives after the operation started', async () => {
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41]);
    wrapper.vm.openBulkEdit();
    const [{ events }] = wrapper.modal.open.mock.calls[0];
    const result = {
      total: 1, completed: 1, succeeded: 1, preserved: 0, failed: 0,
    };

    events.started();
    events.closed();

    expect(wrapper.vm.selection.operationInFlight).toBe(true);
    expect(wrapper.vm.selection.result).toBeNull();

    events.completed(result);
    await settle();
    expect(wrapper.vm.selection.operationInFlight).toBe(false);
    expect(wrapper.vm.selection.result).toMatchObject({ ...result, operation: 'update' });
  });

  it('ignores every modal lifecycle callback after destruction', async () => {
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41]);
    wrapper.vm.openBulkEdit();
    const [{ events }] = wrapper.modal.open.mock.calls[0];

    wrapper.destroy();
    events.started();
    events.completed({
      total: 1, completed: 1, succeeded: 1, preserved: 0, failed: 0,
    });
    events.closed();
    await settle();

    expect(wrapper.vm.selection.operationInFlight).toBe(false);
    expect(wrapper.vm.selection.result).toBeNull();
  });

  it('blocks a stale board-dialog submit after a route reset and closes its modal handle', async () => {
    const wrapper = mountPins();
    const modalHandle = { close: jest.fn() };
    wrapper.modal.open.mockReturnValue(modalHandle);
    await settle();
    selectScope(wrapper, [41]);
    wrapper.vm.openBulkBoard('add');
    const [config] = wrapper.modal.open.mock.calls[0];
    const dialog = mountConfiguredBulkDialog(config);
    await settle();
    dialog.vm.targetBoardId = 7;

    await wrapper.setProps({ pinFilters: { userFilter: 'other' } });
    await settle();
    const submission = dialog.vm.submit();

    expect(submission).toBeNull();
    expect(API.Pin.bulk).not.toHaveBeenCalled();
    expect(modalHandle.close).toHaveBeenCalledTimes(1);
  });

  it('blocks a stale edit-dialog submit after a component reset and closes its modal handle', async () => {
    const wrapper = mountPins();
    const modalHandle = { close: jest.fn() };
    wrapper.modal.open.mockReturnValue(modalHandle);
    await settle();
    selectScope(wrapper, [41]);
    wrapper.vm.openBulkEdit();
    const [config] = wrapper.modal.open.mock.calls[0];
    const dialog = mountConfiguredBulkDialog(config);
    await dialog.setData({ privacyMode: 'private' });

    wrapper.vm.reset();
    await settle();
    const submission = dialog.vm.submit();

    expect(submission).toBeNull();
    expect(API.Pin.bulk).not.toHaveBeenCalled();
    expect(modalHandle.close).toHaveBeenCalledTimes(1);
  });

  it('keeps the parent modal lock after retryable settle and releases it on Close', async () => {
    API.Pin.bulk.mockRejectedValueOnce(new Error('network'));
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41]);
    wrapper.vm.openBulkEdit();
    const [config] = wrapper.modal.open.mock.calls[0];
    const dialog = mountConfiguredBulkDialog(config);
    await dialog.setData({ privacyMode: 'private' });

    await dialog.vm.submit();
    await settle();

    expect(wrapper.vm.bulkModalOpen).toBe(true);
    expect(wrapper.vm.bulkModalStarted).toBe(false);
    expect(wrapper.vm.selection.operationInFlight).toBe(true);
    dialog.vm.close();
    expect(wrapper.vm.selection.operationInFlight).toBe(false);
  });

  it('keeps the parent lock through retry and consumes only eventual success', async () => {
    API.Pin.bulk
      .mockRejectedValueOnce(new Error('network'))
      .mockImplementationOnce(
        payload => bulkResponse(payload.pin_ids, {}, payload.operation),
      );
    const wrapper = mountPins();
    await settle();
    selectScope(wrapper, [41]);
    wrapper.vm.openBulkEdit();
    const [config] = wrapper.modal.open.mock.calls[0];
    const dialog = mountConfiguredBulkDialog(config);
    await dialog.setData({ privacyMode: 'private' });

    await dialog.vm.submit();
    await settle();
    expect(wrapper.vm.selection.operationInFlight).toBe(true);

    await dialog.find('[data-test="bulk-edit-retry"]').trigger('click');
    await settle();

    expect(wrapper.vm.selection.operationInFlight).toBe(false);
    expect(wrapper.vm.selection.result).toMatchObject({
      operation: 'update', succeeded: 1, preserved: 0, failed: 0,
    });
    expect(dialog.emitted('completed')).toHaveLength(1);
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
    first.resolve(bulkAxiosResponse(ids.slice(0, 50), {}, 'delete'));
    await settle();
    second.resolve(bulkAxiosResponse(ids.slice(50, 100), {}, 'delete'));
    await settle();
    third.resolve(bulkAxiosResponse(ids.slice(100, 150), {}, 'delete'));
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

    operation.resolve(bulkAxiosResponse([41], {}, 'delete'));
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
        operation.resolve(bulkAxiosResponse([41], {}, 'delete'));
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
