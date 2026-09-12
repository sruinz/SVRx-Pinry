/* eslint-env jest */

import flushPromises from 'flush-promises';
import { mount } from '@vue/test-utils';

import API from '@/components/api';
import PinBulkToolbar from '@/components/bulk/PinBulkToolbar.vue';
import Pins from '@/components/Pins.vue';
import overlays from '@/components/utils/overlays';

jest.mock('@/components/utils/overlays', () => ({
  __esModule: true,
  default: {
    openModal: jest.fn(), confirm: jest.fn(), toast: jest.fn(), openLoading: jest.fn(),
  },
}));
beforeEach(() => {
  overlays.openModal.mockReset();
  overlays.confirm.mockReset();
  overlays.toast.mockReset();
  overlays.openLoading.mockReset().mockReturnValue({ close: jest.fn() });
});

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


  const wrapper = mount(Pins, {
    global: {
      directives: { masonry: {}, 'masonry-tile': {} },
      mocks: {
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
    },

    props: { pinFilters },
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
      global: { mocks: { $t: key => key } },
      props: {
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
        showExport: true,
        canExport: true,
        showDelete: true,
        canDelete: true,
        canSelectAll: true,
        operationInFlight: false,
        announcement: '2 selected',
      },
    });

    const events = [
      'exit', 'select-loaded', 'clear', 'select-all', 'add-to-board',
      'move', 'edit', 'export', 'delete',
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
    localStorage.clear();
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
      wrapper.unmount();
    });
    if (Pins.methods.initializeMeta.mockRestore) {
      Pins.methods.initializeMeta.mockRestore();
    }
    localStorage.clear();
  });

  it.each([
    ['home', {}],
    ['search or tag', { tagFilter: 'landscape' }],
    ['another user', { userFilter: 'other' }],
  ])('lets an authenticated user explicitly select and export public Pins on %s', async (
    _name, pinFilters,
  ) => {
    const wrapper = mountPins({ pinFilters, pins: [pin(41, 'other')] });
    await settle();

    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');

    expect(wrapper.vm.canSelectPins).toBe(true);
    expect(wrapper.vm.canManagePins).toBe(false);
    expect(wrapper.find('[data-test="pin-selection-export"]').attributes('disabled'))
      .toBeUndefined();
    expect(wrapper.find('[data-test="pin-selection-edit"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="pin-selection-delete"]').exists()).toBe(false);
  });

  it('does not expose selection export to an anonymous user', async () => {
    const wrapper = mountPins({ pinFilters: {}, authenticatedUsername: null });
    await settle();

    expect(wrapper.vm.canSelectPins).toBe(false);
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(false);
    wrapper.vm.enterSelection();
    await wrapper.vm.$nextTick();
    expect(wrapper.vm.selection.active).toBe(false);
    expect(wrapper.find('[data-test="pin-selection-export"]').exists()).toBe(false);
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

  it('allows selection export but hides owner actions on a public non-owned board', async () => {
    const wrapper = mountPins({
      pinFilters: { boardFilter: 7 },
      boardRequest: Promise.resolve({
        data: { id: 7, submitter: { username: 'other' } },
      }),
    });
    await settle();

    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(true);
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');
    expect(wrapper.find('[data-test="pin-selection-export"]').attributes('disabled'))
      .toBeUndefined();
    expect(wrapper.find('[data-test="pin-selection-edit"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="pin-selection-delete"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="board-export"]').exists()).toBe(false);
  });

  it('does not exit selection merely because board ownership changes', async () => {
    const wrapper = mountPins({ pinFilters: { boardFilter: 7 } });
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');

    wrapper.vm.editorMeta.currentBoard = {
      ...wrapper.vm.editorMeta.currentBoard,
      submitter: { username: 'other' },
    };
    await wrapper.vm.$nextTick();

    expect(wrapper.vm.canSelectPins).toBe(true);
    expect(wrapper.vm.canManagePins).toBe(false);
    expect(wrapper.vm.selection).toMatchObject({ active: true, selectedIds: [41] });
    expect(wrapper.find('[data-test="pin-selection-export"]').exists()).toBe(true);
  });

  it('requires strict non-empty usernames before showing owner board export', async () => {
    const wrapper = mountPins({
      pinFilters: { boardFilter: 7 },
      authenticatedUsername: '',
      boardRequest: Promise.resolve({ data: { id: 7, submitter: { username: '' } } }),
    });
    await settle();

    expect(wrapper.vm.isOwnedBoardRoute).toBe(false);
    expect(wrapper.find('[data-test="board-export"]').exists()).toBe(false);
  });

  it('keeps selection closed when a non-owned private board lookup returns 404', async () => {
    const notFound = new Error('not found');
    notFound.response = { status: 404 };
    const wrapper = mountPins({
      pinFilters: { boardFilter: 7 },
      boardRequest: Promise.reject(notFound),
    });
    await settle();

    expect(wrapper.vm.metaReady.board).toBe(false);
    expect(wrapper.vm.canSelectPins).toBe(false);
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(false);
    wrapper.vm.enterSelection();
    await wrapper.vm.$nextTick();
    expect(wrapper.vm.selection.active).toBe(false);
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
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(true);

    oldBoard.resolve({
      data: { id: 7, submitter: { username: 'owner' } },
    });
    await settle();

    expect(API.fetchPins).toHaveBeenCalledTimes(1);
    expect(API.fetchPins.mock.calls[0][3]).toBe(8);
    expect(wrapper.vm.editorMeta.currentBoard.id).toBe(8);
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([80]);
    expect(wrapper.find('[data-test="pin-selection-enter"]').exists()).toBe(true);
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

    expect(overlays.openModal).toHaveBeenCalledTimes(1);
    expect(overlays.openModal.mock.calls[0][1].props.pinItem.id).toBe(41);
  });

  it('selects instead of opening preview while selection mode is active', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');

    expect(wrapper.vm.selection.selectedIds).toEqual([41]);
    expect(overlays.openModal).not.toHaveBeenCalled();
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

  it('disables server-wide select-all without blocking loaded-ID selection', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');

    const selectAll = wrapper.find('[data-test="pin-selection-select-all"]');
    expect(selectAll.attributes('disabled')).toBeDefined();
    await selectAll.trigger('click');
    await settle();
    expect(API.Pin.fetchSelectionIds).not.toHaveBeenCalled();

    await wrapper.find('[data-test="pin-selection-select-loaded"]').trigger('click');
    expect(wrapper.vm.selection).toMatchObject({
      selectedIds: [41, 40, 39], scope: 'loaded', allCount: 0,
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
    expect(wrapper.find('[data-test="pin-selection-export"]').attributes('disabled'))
      .toBeUndefined();
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
        .toBeDefined();
    });
    expect(wrapper.find('[data-test="pin-selection-export"]').attributes('disabled'))
      .toBeUndefined();
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
        .toBeDefined();
    });
    expect(wrapper.find('[data-test="pin-selection-export"]').attributes('disabled'))
      .toBeUndefined();
  });

  it('fails closed when selected-pin ownership is unknown', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');
    delete wrapper.vm.selection.ownershipById[41];
    await wrapper.vm.$nextTick();

    ['add-to-board', 'edit', 'delete'].forEach((action) => {
      expect(wrapper.find(`[data-test="pin-selection-${action}"]`).attributes('disabled'))
        .toBeDefined();
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
    wrapper.unmount();
    const removeCalls = removeSpy.mock.calls.filter(call => call[0] === 'keydown');
    expect(removeCalls).toHaveLength(1);
    expect(removeCalls[0][1]).toBe(addCalls[0][1]);
    addSpy.mockRestore();
    removeSpy.mockRestore();
  });
});
