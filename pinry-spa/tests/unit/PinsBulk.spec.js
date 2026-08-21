/* eslint-env jest */

import flushPromises from 'flush-promises';
import { createLocalVue, mount } from '@vue/test-utils';

import API from '@/components/api';
import PinBulkToolbar from '@/components/bulk/PinBulkToolbar.vue';
import Pins from '@/components/Pins.vue';

let mockAuthenticatedUsername = 'owner';
let mountedPinWrappers = [];

function deferred() {
  const request = {};
  request.promise = new Promise((resolve, reject) => {
    request.resolve = resolve;
    request.reject = reject;
  });
  return request;
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

function page(pins = [pin(41), pin(40), pin(39)]) {
  return Promise.resolve({ data: { results: pins, next: null } });
}

function mountPins({
  pinFilters = { userFilter: 'owner' },
  authenticatedUsername = 'owner',
  boardRequest = null,
  pins = [pin(41), pin(40), pin(39)],
  fetchPinsImplementation = null,
  boardImplementation = null,
} = {}) {
  mockAuthenticatedUsername = authenticatedUsername;
  API.fetchPins.mockImplementation(
    fetchPinsImplementation || (() => page(pins)),
  );
  API.Board.get.mockImplementation(
    boardImplementation || (() => (
      boardRequest || Promise.resolve({
        data: { id: pinFilters.boardFilter, submitter: { username: authenticatedUsername } },
      })
    )),
  );

  const localVue = createLocalVue();
  localVue.directive('masonry', {});
  localVue.directive('masonry-tile', {});
  const wrapper = mount(Pins, {
    localVue,
    propsData: { pinFilters },
    mocks: {
      $buefy: { modal: { open: jest.fn() } },
      $t: (key, values) => (values ? `${key}:${values.count}` : key),
    },
    stubs: {
      EditorUI: true,
      loadingSpinner: true,
      noMore: true,
      'router-link': {
        props: ['to'],
        template: '<a href="#"><slot /></a>',
      },
    },
  });
  mountedPinWrappers.push(wrapper);
  return wrapper;
}

async function settle() {
  await flushPromises();
  await flushPromises();
}

function dispatchKey(target, key, options = {}) {
  const event = new KeyboardEvent('keydown', {
    key,
    bubbles: true,
    cancelable: true,
    ...options,
  });
  if (options.isComposing) {
    Object.defineProperty(event, 'isComposing', { value: true });
  }
  target.dispatchEvent(event);
  return event;
}

describe('PinBulkToolbar', () => {
  it('emits only presentation-level management events', async () => {
    const wrapper = mount(PinBulkToolbar, {
      propsData: {
        active: true,
        selectedCount: 2,
        loadedCount: 3,
        scope: 'loaded',
        allCount: 0,
        showAddToBoard: true,
        canAddToBoard: true,
        showMove: true,
        canMove: true,
        showEdit: true,
        canEdit: true,
        showDelete: true,
        canDelete: true,
        operationInFlight: false,
        announcement: '2 selected',
      },
      mocks: { $t: key => key },
    });

    const events = [
      'exit', 'select-loaded', 'clear', 'select-all', 'add-to-board',
      'move', 'edit', 'delete',
    ];
    events.forEach((name) => {
      wrapper.find(`[data-test="pin-selection-${name}"]`).trigger('click');
    });
    await wrapper.vm.$nextTick();
    events.forEach((name) => {
      expect(wrapper.emitted(name)).toHaveLength(1);
    });

    await wrapper.setProps({ active: false });
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    expect(wrapper.emitted('enter')).toHaveLength(1);
  });
});

describe('Pins selection mode', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mountedPinWrappers = [];
    jest.spyOn(Pins.methods, 'initializeMeta').mockImplementation(function initializeMeta() {
      this.editorMeta.user = mockAuthenticatedUsername === null
        ? { loggedIn: false, meta: {} }
        : { loggedIn: true, meta: { username: mockAuthenticatedUsername } };
      this.metaReady.user = true;
      this.syncLoadedSelection();
    });
    API.fetchPins = jest.fn();
    API.fetchPin = jest.fn();
    API.Board.get = jest.fn();
    API.User.fetchUserInfo = jest.fn();
    API.Pin.fetchSelectionIds = jest.fn();
    API.Pin.bulk = jest.fn();
  });

  afterEach(() => {
    mountedPinWrappers.forEach((wrapper) => {
      wrapper.destroy();
    });
    if (Pins.methods.initializeMeta.mockRestore) {
      Pins.methods.initializeMeta.mockRestore();
    }
  });

  it('shows management only for the authenticated owner route', async () => {
    const wrapper = mountPins();
    await settle();
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(true);

    await wrapper.setProps({ pinFilters: { userFilter: 'other' } });
    await settle();
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(false);
  });

  it('waits for owned board metadata before showing management', async () => {
    const boardRequest = deferred();
    const wrapper = mountPins({
      pinFilters: { boardFilter: 7 },
      boardRequest: boardRequest.promise,
    });
    await flushPromises();
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(false);

    boardRequest.resolve({
      data: { id: 7, submitter: { username: 'owner' } },
    });
    await settle();
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(true);
  });

  it('hides management for a non-owned board after metadata loads', async () => {
    const wrapper = mountPins({
      pinFilters: { boardFilter: 7 },
      boardRequest: Promise.resolve({
        data: { id: 7, submitter: { username: 'other' } },
      }),
    });
    await settle();

    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(false);
  });

  it('ignores an old owned-board response after a newer foreign board loads', async () => {
    const oldBoard = deferred();
    const currentBoard = deferred();
    const wrapper = mountPins({
      pinFilters: { boardFilter: 7 },
      boardImplementation: boardId => (boardId === 7
        ? oldBoard.promise
        : currentBoard.promise),
      fetchPinsImplementation: (offset, tagFilter, userFilter, boardFilter) => (
        page([pin(boardFilter * 10, boardFilter === 7 ? 'owner' : 'other')])
      ),
    });
    await wrapper.setProps({ pinFilters: { boardFilter: 8 } });
    currentBoard.resolve({
      data: { id: 8, submitter: { username: 'other' } },
    });
    await settle();
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([80]);
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(false);

    oldBoard.resolve({
      data: { id: 7, submitter: { username: 'owner' } },
    });
    await settle();

    expect(API.fetchPins).toHaveBeenCalledTimes(1);
    expect(API.fetchPins.mock.calls[0][3]).toBe(8);
    expect(wrapper.vm.editorMeta.currentBoard.id).toBe(8);
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([80]);
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(false);
  });

  it('ignores an old page response after a newer route page loads', async () => {
    const oldPage = deferred();
    const currentPage = deferred();
    let requestCount = 0;
    const wrapper = mountPins({
      fetchPinsImplementation: () => {
        requestCount += 1;
        return requestCount === 1 ? oldPage.promise : currentPage.promise;
      },
    });
    await wrapper.setProps({ pinFilters: { userFilter: 'other' } });
    currentPage.resolve({ data: { results: [pin(80, 'other')], next: null } });
    await settle();
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([80]);

    oldPage.resolve({ data: { results: [pin(41)], next: null } });
    await settle();

    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([80]);
    expect(wrapper.vm.status.offset).toBe(1);
  });

  it('ignores old user metadata after a newer route metadata request resolves', async () => {
    Pins.methods.initializeMeta.mockRestore();
    const oldUser = deferred();
    const currentUser = deferred();
    API.User.fetchUserInfo
      .mockReturnValueOnce(oldUser.promise)
      .mockReturnValueOnce(currentUser.promise);
    const wrapper = mountPins({ pinFilters: { userFilter: 'owner' } });
    await wrapper.setProps({ pinFilters: { userFilter: 'other' } });
    currentUser.resolve({ username: 'other' });
    await settle();
    expect(wrapper.vm.editorMeta.user.meta.username).toBe('other');
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(true);

    oldUser.resolve({ username: 'owner' });
    await settle();

    expect(wrapper.vm.editorMeta.user.meta.username).toBe('other');
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(true);
  });

  it('keeps normal-mode image preview behavior', async () => {
    const wrapper = mountPins();
    await settle();

    await wrapper.find('[data-test="pin-image-41"]').trigger('click');

    expect(wrapper.vm.$buefy.modal.open).toHaveBeenCalledTimes(1);
    expect(wrapper.vm.$buefy.modal.open.mock.calls[0][0].props.pinItem.id).toBe(41);
  });

  it('selects instead of opening preview while selection mode is active', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');

    expect(wrapper.vm.selection.selectedIds).toEqual([41]);
    expect(wrapper.vm.$buefy.modal.open).not.toHaveBeenCalled();
  });

  it('supports Shift ranges and Ctrl or Command toggles in loaded order', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');
    await wrapper.find('[data-test="pin-card-39"]').trigger('click', { shiftKey: true });
    expect(wrapper.vm.selection.selectedIds).toEqual([41, 40, 39]);

    await wrapper.find('[data-test="pin-card-40"]').trigger('click', { ctrlKey: true });
    await wrapper.find('[data-test="pin-card-39"]').trigger('click', { metaKey: true });
    expect(wrapper.vm.selection.selectedIds).toEqual([41]);
  });

  it('toggles focused cards with Enter and Space', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    const card = wrapper.find('[data-test="pin-card-41"]');

    await card.trigger('keydown', { key: 'Enter' });
    expect(wrapper.vm.selection.selectedIds).toEqual([41]);
    await card.trigger('keydown', { key: ' ' });
    expect(wrapper.vm.selection.selectedIds).toEqual([]);
  });

  it('selects all loaded cards with Ctrl+A or Command+A', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');

    dispatchKey(document, 'a', { ctrlKey: true });
    expect(wrapper.vm.selection.selectedIds).toEqual([41, 40, 39]);
    await wrapper.find('[data-test="pin-selection-clear"]').trigger('click');
    dispatchKey(document, 'A', { metaKey: true });
    expect(wrapper.vm.selection.selectedIds).toEqual([41, 40, 39]);
  });

  it.each([
    ['input', '<input>'],
    ['textarea', '<textarea></textarea>'],
    ['select', '<select></select>'],
    ['contenteditable', '<div contenteditable="true"></div>'],
  ])('does not capture Ctrl+A from a real %s target', async (name, markup) => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    const host = document.createElement('div');
    host.innerHTML = markup;
    const target = host.firstElementChild;
    document.body.appendChild(target);

    dispatchKey(target, 'a', { ctrlKey: true });

    expect(wrapper.vm.selection.selectedIds).toEqual([]);
    target.remove();
  });

  it('does not capture Ctrl+A during IME composition', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');

    dispatchKey(document, 'a', { ctrlKey: true, isComposing: true });

    expect(wrapper.vm.selection.selectedIds).toEqual([]);
  });

  it('exits and clears selection with Escape', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');

    dispatchKey(document, 'Escape');

    expect(wrapper.vm.selection.active).toBe(false);
    expect(wrapper.vm.selection.selectedIds).toEqual([]);
  });

  it('selects and clears the currently loaded cards from the toolbar', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');

    await wrapper.find('[data-test="pin-selection-select-loaded"]').trigger('click');
    expect(wrapper.vm.selection.selectedIds).toEqual([41, 40, 39]);
    await wrapper.find('[data-test="pin-selection-clear"]').trigger('click');
    expect(wrapper.vm.selection.selectedIds).toEqual([]);
  });

  it('applies the server-wide selection and announces its count', async () => {
    API.Pin.fetchSelectionIds = jest.fn().mockResolvedValue({
      data: {
        count: 4,
        results: [
          { id: 41, owned: true },
          { id: 40, owned: true },
          { id: 39, owned: true },
          { id: 9, owned: true },
        ],
      },
    });
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');

    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');
    await settle();

    expect(API.Pin.fetchSelectionIds).toHaveBeenCalledWith({ boardId: null });
    expect(wrapper.vm.selection).toMatchObject({
      selectedIds: [41, 40, 39, 9],
      scope: 'all',
      allCount: 4,
    });
    expect(wrapper.find('[data-test="pin-selection-summary"]').text())
      .toContain('bulkPinAllSelected:4');
  });

  it.each([
    ['My Pins', { userFilter: 'owner' }, null],
    ['owned Board', { boardFilter: 7 }, 7],
  ])('shows a visible limit alert on %s while preserving loaded selection', async (
    name,
    pinFilters,
    boardId,
  ) => {
    API.Pin.fetchSelectionIds = jest.fn().mockRejectedValue({
      response: { status: 409, data: { code: 'selection_too_large' } },
    });
    const wrapper = mountPins({ pinFilters });
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');

    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');
    await settle();

    expect(wrapper.vm.selection).toMatchObject({
      selectedIds: [41], scope: 'loaded', allCount: 0,
    });
    expect(API.Pin.fetchSelectionIds).toHaveBeenCalledWith({ boardId });
    expect(wrapper.find('[data-test="pin-selection-too-large"]').text())
      .toBe('bulkPinSelectionTooLarge');
    expect(wrapper.find('[data-test="pin-selection-live"]').text())
      .toContain('bulkPinSelectionTooLarge');
  });

  it('rejects a resolved 50,001-row scope before mutating the loaded selection', async () => {
    const results = Array.from({ length: 50001 }, (_, index) => ({
      id: index + 1,
      owned: true,
    }));
    API.Pin.fetchSelectionIds.mockResolvedValue({
      data: { count: 50001, results },
    });
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');

    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');
    await settle();

    expect(wrapper.vm.selection).toMatchObject({
      selectedIds: [41],
      scope: 'loaded',
      allCount: 0,
      result: { code: 'selection_too_large' },
    });
    expect(wrapper.find('[data-test="pin-selection-too-large"]').text())
      .toBe('bulkPinSelectionTooLarge');

    wrapper.vm.openBulkEdit();
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="pin-selection-too-large"]').exists()).toBe(false);
    expect(API.Pin.bulk).not.toHaveBeenCalled();
  });

  it('clears a visible limit alert after a later valid select-all response', async () => {
    API.Pin.fetchSelectionIds
      .mockRejectedValueOnce({
        response: { status: 409, data: { code: 'selection_too_large' } },
      })
      .mockResolvedValueOnce({
        data: { count: 2, results: [{ id: 41, owned: true }, { id: 9, owned: true }] },
      });
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');

    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');
    await settle();
    expect(wrapper.find('[data-test="pin-selection-too-large"]').exists()).toBe(true);

    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');
    await settle();
    expect(wrapper.find('[data-test="pin-selection-too-large"]').exists()).toBe(false);
    expect(wrapper.vm.selection).toMatchObject({
      selectedIds: [41, 9], scope: 'all', allCount: 2, result: null,
    });
  });

  it.each([
    ['duplicate id', {
      count: 2,
      results: [{ id: 41, owned: true }, { id: 41, owned: true }],
    }],
    ['non-positive id', {
      count: 2,
      results: [{ id: 41, owned: true }, { id: 0, owned: true }],
    }],
    ['non-boolean ownership', {
      count: 2,
      results: [{ id: 41, owned: true }, { id: 9, owned: 1 }],
    }],
    ['count mismatch', {
      count: 3,
      results: [{ id: 41, owned: true }, { id: 9, owned: true }],
    }],
    ['extra response field', {
      count: 2,
      results: [{ id: 41, owned: true }, { id: 9, owned: true }],
      next: null,
    }],
    ['extra row field', {
      count: 2,
      results: [
        { id: 41, owned: true },
        { id: 9, owned: true, private: false },
      ],
    }],
  ])('keeps an existing all scope unchanged for a malformed response: %s', async (name, data) => {
    API.Pin.fetchSelectionIds
      .mockResolvedValueOnce({
        data: {
          count: 2,
          results: [{ id: 41, owned: true }, { id: 8, owned: true }],
        },
      })
      .mockResolvedValueOnce({ data });
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');
    await settle();
    const before = {
      selectedIds: [...wrapper.vm.selection.selectedIds],
      scope: wrapper.vm.selection.scope,
      allCount: wrapper.vm.selection.allCount,
    };

    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');
    await settle();

    expect(wrapper.vm.selection).toMatchObject({
      ...before,
      result: { code: 'selection_failed' },
    });
    expect(API.Pin.bulk).not.toHaveBeenCalled();
  });

  it('blocks card and keyboard selection changes while select-all is in flight', async () => {
    const selectionRequest = deferred();
    API.Pin.fetchSelectionIds.mockReturnValue(selectionRequest.promise);
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');
    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');
    expect(wrapper.vm.selection.operationInFlight).toBe(true);

    const checkbox = wrapper.find('[data-test="pin-selection-check-41"]');
    await checkbox.trigger('click');
    const card = wrapper.find('[data-test="pin-card-40"]');
    await card.trigger('click');
    await card.trigger('keydown', { key: 'Enter' });
    dispatchKey(document, 'a', { ctrlKey: true });

    expect(wrapper.vm.selection.selectedIds).toEqual([41]);
    expect(checkbox.element.checked).toBe(true);
  });

  it('ignores a late select-all success after Escape exits selection mode', async () => {
    const selectionRequest = deferred();
    API.Pin.fetchSelectionIds.mockReturnValue(selectionRequest.promise);
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');
    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');

    dispatchKey(document, 'Escape');
    expect(wrapper.vm.selection).toMatchObject({
      active: false,
      selectedIds: [],
      operationInFlight: false,
      result: null,
    });
    selectionRequest.resolve({
      data: { count: 1, results: [{ id: 41, owned: true }] },
    });
    await settle();

    expect(wrapper.vm.selection).toMatchObject({
      active: false,
      selectedIds: [],
      operationInFlight: false,
      result: null,
    });
  });

  it('ignores a late select-all failure after a route reset', async () => {
    const selectionRequest = deferred();
    API.Pin.fetchSelectionIds.mockReturnValue(selectionRequest.promise);
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');
    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');

    await wrapper.setProps({ pinFilters: { userFilter: 'other' } });
    await settle();
    expect(wrapper.vm.selection).toMatchObject({
      active: false,
      selectedIds: [],
      operationInFlight: false,
      result: null,
    });
    selectionRequest.reject({
      response: { status: 409, data: { code: 'selection_too_large' } },
    });
    await settle();

    expect(wrapper.vm.selection).toMatchObject({
      active: false,
      selectedIds: [],
      operationInFlight: false,
      result: null,
    });
  });

  it('ignores a late select-all success after destruction', async () => {
    const selectionRequest = deferred();
    API.Pin.fetchSelectionIds.mockReturnValue(selectionRequest.promise);
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');

    wrapper.destroy();
    expect(wrapper.vm.selection).toMatchObject({
      active: false,
      selectedIds: [],
      operationInFlight: false,
      result: null,
    });
    selectionRequest.resolve({
      data: { count: 1, results: [{ id: 41, owned: true }] },
    });
    await settle();

    expect(wrapper.vm.selection).toMatchObject({
      active: false,
      selectedIds: [],
      operationInFlight: false,
      result: null,
    });
  });

  it('shows add but hides move on My Pins and enables owned-pin actions', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');

    expect(wrapper.find('[data-test="pin-selection-add-to-board"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="pin-selection-add-to-board"]').attributes('disabled'))
      .toBeUndefined();
    expect(wrapper.find('[data-test="pin-selection-move"]').exists()).toBe(false);
    ['edit', 'delete'].forEach((action) => {
      expect(wrapper.find(`[data-test="pin-selection-${action}"]`).attributes('disabled'))
        .toBeUndefined();
    });
  });

  it('fails closed for add, edit, and delete on My Pins when a pin is not owned', async () => {
    const wrapper = mountPins({ pins: [pin(41), pin(40, 'other')] });
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');
    await wrapper.find('[data-test="pin-card-40"]').trigger('click');

    expect(wrapper.find('[data-test="pin-selection-move"]').exists()).toBe(false);
    ['add-to-board', 'edit', 'delete'].forEach((action) => {
      expect(wrapper.find(`[data-test="pin-selection-${action}"]`).attributes('disabled'))
        .toBe('disabled');
    });
  });

  it('shows move but hides add on an owned board and permits visible foreign pins', async () => {
    const wrapper = mountPins({
      pinFilters: { boardFilter: 7 },
      pins: [pin(41), pin(40, 'other')],
    });
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');
    await wrapper.find('[data-test="pin-card-40"]').trigger('click');

    expect(wrapper.find('[data-test="pin-selection-add-to-board"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="pin-selection-move"]').attributes('disabled'))
      .toBeUndefined();
    ['edit', 'delete'].forEach((action) => {
      expect(wrapper.find(`[data-test="pin-selection-${action}"]`).attributes('disabled'))
        .toBe('disabled');
    });
  });

  it('fails closed when selected-pin ownership is unknown', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');
    wrapper.vm.$delete(wrapper.vm.selection.ownershipById, 41);
    await wrapper.vm.$nextTick();

    ['add-to-board', 'edit', 'delete'].forEach((action) => {
      expect(wrapper.find(`[data-test="pin-selection-${action}"]`).attributes('disabled'))
        .toBe('disabled');
    });
  });

  it('renders selected cards with button semantics, checks, borders, and a live count', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    const card = wrapper.find('[data-test="pin-card-41"]');
    await card.trigger('click');

    expect(card.attributes()).toMatchObject({
      role: 'button', tabindex: '0', 'aria-selected': 'true',
    });
    expect(card.classes()).toContain('is-selected');
    expect(wrapper.find('[data-test="pin-selection-check-41"]').element.checked).toBe(true);
    const live = wrapper.find('[data-test="pin-selection-live"]');
    expect(live.attributes('aria-live')).toBe('polite');
    expect(live.text()).toContain('bulkPinSelectedCount:1');
  });

  it('binds and unbinds the same document keydown handler exactly once', () => {
    const addSpy = jest.spyOn(document, 'addEventListener');
    const removeSpy = jest.spyOn(document, 'removeEventListener');
    const wrapper = mountPins();
    const addCalls = addSpy.mock.calls.filter(call => call[0] === 'keydown');

    expect(addCalls).toHaveLength(1);
    wrapper.destroy();
    const removeCalls = removeSpy.mock.calls.filter(call => call[0] === 'keydown');
    expect(removeCalls).toHaveLength(1);
    expect(removeCalls[0][1]).toBe(addCalls[0][1]);
    addSpy.mockRestore();
    removeSpy.mockRestore();
  });
});
