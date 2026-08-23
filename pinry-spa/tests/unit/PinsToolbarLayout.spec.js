/* eslint-env jest */

import flushPromises from 'flush-promises';
import { createLocalVue, mount } from '@vue/test-utils';

import API from '@/components/api';
import Pins from '@/components/Pins.vue';

let authenticatedUsername = 'owner';
let wrappers = [];

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

function mountPins({
  pinFilters = {},
  username = 'owner',
  boardOwner = 'owner',
} = {}) {
  authenticatedUsername = username;
  API.fetchPins.mockResolvedValue({
    data: { results: [pin(41), pin(40)], next: null },
  });
  API.fetchPin.mockResolvedValue({
    data: { results: [pin(pinFilters.idFilter || 41)], next: null },
  });
  API.Board.get.mockResolvedValue({
    data: {
      id: pinFilters.boardFilter,
      private: false,
      cover_pin_id: 41,
      submitter: { username: boardOwner },
    },
  });

  const localVue = createLocalVue();
  localVue.directive('masonry', {});
  localVue.directive('masonry-tile', {});
  const wrapper = mount(Pins, {
    localVue,
    attachTo: document.body,
    propsData: { pinFilters },
    mocks: {
      $buefy: {
        dialog: { confirm: jest.fn() },
        modal: { open: jest.fn() },
        toast: { open: jest.fn() },
      },
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
  wrappers.push(wrapper);
  return wrapper;
}

async function settle() {
  await flushPromises();
  await flushPromises();
}

describe('Pins responsive tool area', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    wrappers = [];
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
      this.editorMeta.user = authenticatedUsername === null
        ? { loggedIn: false, meta: {} }
        : { loggedIn: true, meta: { username: authenticatedUsername } };
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
    wrappers.forEach(wrapper => wrapper.destroy());
    if (Pins.methods.initializeMeta.mockRestore) Pins.methods.initializeMeta.mockRestore();
    localStorage.clear();
  });

  it.each([
    ['main list', {}, 'owner', 'owner', false, false],
    ['another user list', { userFilter: 'other' }, 'owner', 'owner', false, false],
    ['My Pins list', { userFilter: 'owner' }, 'owner', 'owner', true, false],
    ['owned Board Pin list', { boardFilter: 7 }, 'owner', 'owner', true, true],
    ['foreign Board Pin list', { boardFilter: 7 }, 'owner', 'other', false, false],
  ])('groups available controls on the %s', async (
    _name, pinFilters, username, boardOwner, showBulk, showCover,
  ) => {
    const wrapper = mountPins({ pinFilters, username, boardOwner });
    await settle();

    const tools = wrapper.find('[data-test="pin-tools"]');
    expect(tools.exists()).toBe(true);
    expect(tools.classes()).toContain('container');
    expect(tools.find('[data-test="pin-tools-primary"]').exists()).toBe(true);
    expect(tools.find('[data-test="pin-tools-active"]').exists()).toBe(false);
    expect(tools.find('.pin-sort__buttons').classes()).toEqual(
      expect.arrayContaining(['buttons', 'has-addons']),
    );
    expect(tools.find('[data-test="pin-selection-enter"]').exists()).toBe(showBulk);
    expect(tools.find('[data-test="board-cover-enter"]').exists()).toBe(showCover);
  });

  it('keeps bulk entry controls in the first row and actions in the second row', async () => {
    const wrapper = mountPins({ pinFilters: { boardFilter: 7 } });
    await settle();
    const primary = wrapper.find('[data-test="pin-tools-primary"]');

    await primary.find('[data-test="pin-selection-enter"]').trigger('click');

    expect(wrapper.vm.selection.active).toBe(true);
    expect(primary.find('[data-test="pin-selection-enter"]').exists()).toBe(true);
    expect(primary.find('[data-test="pin-selection-enter"]').attributes('disabled'))
      .toBe('disabled');
    expect(primary.find('[data-test="board-cover-enter"]').attributes('disabled'))
      .toBe('disabled');
    const active = wrapper.find('[data-test="pin-tools-active"]');
    const bulkToolbar = active.find('.pin-bulk-toolbar');
    expect(bulkToolbar.classes()).toContain('is-active');
    expect(bulkToolbar.find('[data-test="pin-selection-summary"]').exists()).toBe(true);
    expect(active.find('[data-test="pin-selection-select-loaded"]').exists()).toBe(true);

    await active.find('[data-test="pin-selection-exit"]').trigger('click');

    expect(wrapper.vm.selection.active).toBe(false);
    expect(wrapper.find('[data-test="pin-tools-active"]').exists()).toBe(false);
  });

  it('keeps cover entry controls in the first row and status and actions in the second row', async () => {
    const wrapper = mountPins({ pinFilters: { boardFilter: 7 } });
    await settle();
    const primary = wrapper.find('[data-test="pin-tools-primary"]');

    await primary.find('[data-test="board-cover-enter"]').trigger('click');

    expect(wrapper.vm.interactionMode).toBe('cover-selection');
    expect(primary.find('[data-test="pin-selection-enter"]').attributes('disabled'))
      .toBe('disabled');
    expect(primary.find('[data-test="board-cover-enter"]').attributes('disabled'))
      .toBe('disabled');
    const active = wrapper.find('[data-test="pin-tools-active"]');
    expect(active.find('[data-test="board-cover-status"]').text())
      .toContain('bulkPinSelectedCount:1');
    expect(active.find('[data-test="board-cover-apply"]').exists()).toBe(true);

    await active.find('[data-test="board-cover-cancel"]').trigger('click');

    expect(wrapper.vm.interactionMode).toBe('browse');
    expect(wrapper.find('[data-test="pin-tools-active"]').exists()).toBe(false);
    expect(document.activeElement).toBe(
      wrapper.find('[data-test="board-cover-enter"]').element,
    );
  });

  it('hides every active bulk control when Board management permission is lost', async () => {
    const wrapper = mountPins({ pinFilters: { boardFilter: 7 } });
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');

    wrapper.vm.editorMeta.currentBoard = {
      ...wrapper.vm.editorMeta.currentBoard,
      submitter: { username: 'other' },
    };
    await wrapper.vm.$nextTick();

    expect(wrapper.vm.canManagePins).toBe(false);
    expect(wrapper.find('[data-test="pin-tools-active"]').exists()).toBe(false);
    [
      'summary', 'select-loaded', 'clear', 'select-all', 'move', 'edit', 'delete', 'exit',
    ].forEach((name) => {
      expect(wrapper.find(`[data-test="pin-selection-${name}"]`).exists()).toBe(false);
    });
  });

  it('hides every active cover control when Board ownership is lost', async () => {
    const wrapper = mountPins({ pinFilters: { boardFilter: 7 } });
    await settle();
    await wrapper.find('[data-test="board-cover-enter"]').trigger('click');

    wrapper.vm.editorMeta.currentBoard = {
      ...wrapper.vm.editorMeta.currentBoard,
      submitter: { username: 'other' },
    };
    await wrapper.vm.$nextTick();

    expect(wrapper.vm.isOwnedBoardRoute).toBe(false);
    expect(wrapper.find('[data-test="pin-tools-active"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="board-cover-status"]').exists()).toBe(false);
    ['apply', 'cancel', 'reset'].forEach((name) => {
      expect(wrapper.find(`[data-test="board-cover-${name}"]`).exists()).toBe(false);
    });
  });

  it('does not add the list tool area to a single Pin route', async () => {
    const wrapper = mountPins({ pinFilters: { idFilter: 41 } });
    await settle();

    expect(wrapper.find('[data-test="pin-tools"]').exists()).toBe(false);
    expect(API.fetchPins).not.toHaveBeenCalled();
    expect(API.fetchPin).toHaveBeenCalledWith(41);
  });
});
