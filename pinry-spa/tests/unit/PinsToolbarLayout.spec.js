/* eslint-env jest */

import flushPromises from 'flush-promises';
import { mount } from '@vue/test-utils';

import API from '@/components/api';
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

    attachTo: document.body,
    props: { pinFilters },
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
    wrappers.forEach(wrapper => wrapper.unmount());
    if (Pins.methods.initializeMeta.mockRestore) Pins.methods.initializeMeta.mockRestore();
    localStorage.clear();
  });

  it('밀도 변경은 핀·정렬·선택을 보존하고 다시 요청하지 않는다', async () => {
    const wrapper = mountPins({ pinFilters: { userFilter: 'owner' } });
    await settle();
    expect(wrapper.attributes('data-density')).toBe('normal');
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-selection-check-41"]').trigger('click');
    expect(wrapper.vm.selection.selectedIds).toEqual([41]);
    const requests = API.fetchPins.mock.calls.length;
    const order = wrapper.vm.blocks.map(item => item.id);
    const sort = { ...wrapper.vm.sortState };
    await wrapper.find('[data-test="pin-density-small"]').trigger('click');
    expect(wrapper.attributes('data-density')).toBe('small');
    expect(wrapper.find('[data-test="pin-density-small"]').attributes('aria-pressed')).toBe('true');
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual(order);
    expect(wrapper.vm.sortState).toEqual(sort);
    expect(wrapper.vm.selection.selectedIds).toEqual([41]);
    expect(API.fetchPins.mock.calls.length).toBe(requests);
    await wrapper.find('[data-test="pin-density-large"]').trigger('click');
    expect(wrapper.attributes('data-density')).toBe('large');
  });

  it('다른 목록을 열면 마지막 밀도를 복원한다', async () => {
    const first = mountPins();
    await settle();
    await first.find('[data-test="pin-density-large"]').trigger('click');
    const second = mountPins({ pinFilters: { boardFilter: 7 } });
    await settle();
    expect(second.attributes('data-density')).toBe('large');
    expect(second.find('[data-test="pin-density-large"]').attributes('aria-pressed')).toBe('true');
    second.vm.reset();
    await settle();
    expect(second.attributes('data-density')).toBe('large');
  });

  it('잘못된 저장 값은 보통으로 되돌리고 저장소가 차단되어도 크기를 바꾼다', async () => {
    localStorage.setItem('pinry-pin-density', 'invalid');
    const wrapper = mountPins();
    await settle();
    expect(wrapper.attributes('data-density')).toBe('normal');
    const write = jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    try {
      await wrapper.find('[data-test="pin-density-small"]').trigger('click');
      expect(wrapper.attributes('data-density')).toBe('small');
      wrapper.vm.reset();
      await settle();
      expect(wrapper.attributes('data-density')).toBe('small');
    } finally {
      write.mockRestore();
    }
  });

  it('저장소 읽기가 차단된 경우에도 보통 크기로 목록을 연다', async () => {
    const read = jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    try {
      const wrapper = mountPins();
      await settle();
      expect(wrapper.attributes('data-density')).toBe('normal');
      expect(wrapper.find('[data-test="pin-image-41"]').exists()).toBe(true);
    } finally {
      read.mockRestore();
    }
  });

  it('좁은 썸네일도 카드 폭을 채우며 로딩 전후 비율과 목록 순서를 유지한다', async () => {
    const wrapper = mountPins();
    const portrait = pin(41);
    portrait.image.thumbnail.width = 120;
    portrait.image.thumbnail.height = 240;
    API.fetchPins.mockResolvedValue({ data: { results: [portrait, pin(40)], next: null } });
    await settle();
    wrapper.vm.reset();
    await settle();

    const image = wrapper.find('[data-test="pin-image-41"]');
    expect(image.element.style.width).toBe('100%');
    expect(image.element.style.height).toBe('auto');
    expect(image.element.style.aspectRatio).toBe('120 / 240');
    await image.trigger('load');
    expect(image.element.style.width).toBe('100%');
    expect(image.element.style.height).toBe('auto');
    expect(wrapper.findAll('.pin-preview-image').map(item => item.attributes('data-test')))
      .toEqual(['pin-image-41', 'pin-image-40']);
  });

  it.each([
    ['main list', {}, 'owner', 'owner', true, false, false],
    ['another user list', { userFilter: 'other' }, 'owner', 'owner', true, false, false],
    ['My Pins list', { userFilter: 'owner' }, 'owner', 'owner', true, false, false],
    ['owned Board Pin list', { boardFilter: 7 }, 'owner', 'owner', true, true, true],
    ['foreign Board Pin list', { boardFilter: 7 }, 'owner', 'other', true, false, false],
  ])('groups available controls on the %s', async (
    _name, pinFilters, username, boardOwner, showBulk, showCover, showBoardExport,
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
    expect(tools.find('[data-test="board-export"]').exists()).toBe(showBoardExport);
  });

  it('keeps bulk entry controls in the first row and actions in the second row', async () => {
    const wrapper = mountPins({ pinFilters: { boardFilter: 7 } });
    await settle();
    const primary = wrapper.find('[data-test="pin-tools-primary"]');

    await primary.find('[data-test="pin-selection-enter"]').trigger('click');

    expect(wrapper.vm.selection.active).toBe(true);
    expect(primary.find('[data-test="pin-selection-enter"]').exists()).toBe(true);
    expect(primary.find('[data-test="pin-selection-enter"]').attributes('disabled'))
      .toBeDefined();
    expect(primary.find('[data-test="board-cover-enter"]').attributes('disabled'))
      .toBeDefined();
    const active = wrapper.find('[data-test="pin-tools-active"]');
    const bulkToolbar = active.find('.pin-bulk-toolbar');
    expect(bulkToolbar.classes()).toContain('is-active');
    expect(bulkToolbar.find('[data-test="pin-selection-summary"]').exists()).toBe(true);
    expect(active.find('[data-test="pin-selection-select-loaded"]').exists()).toBe(true);
    const actionNames = active.findAll('.pin-bulk-toolbar__buttons > button')
      .map(button => button.attributes('data-test'));
    expect(actionNames.indexOf('pin-selection-edit'))
      .toBeLessThan(actionNames.indexOf('pin-selection-export'));
    expect(actionNames.indexOf('pin-selection-export'))
      .toBeLessThan(actionNames.indexOf('pin-selection-delete'));
    expect(active.find('[data-test="pin-selection-select-all"]').attributes('disabled'))
      .toBeDefined();

    await active.find('[data-test="pin-selection-exit"]').trigger('click');

    expect(wrapper.vm.selection.active).toBe(false);
    expect(wrapper.find('[data-test="pin-tools-active"]').exists()).toBe(false);
  });

  it('places owned-board export only in the fixed management area', async () => {
    const wrapper = mountPins({ pinFilters: { boardFilter: '7' } });
    await settle();

    const management = wrapper.find('.pin-tools__management');
    const button = management.find('[data-test="board-export"]');
    expect(button.exists()).toBe(true);
    expect(wrapper.find('.pin-card [data-test="board-export"]').exists()).toBe(false);

    await button.trigger('click');

    const config = overlays.openModal.mock.calls[0][1];
    expect(config.props).toEqual({ boardId: 7 });
    expect(config.canCancel).toBe(true);
  });

  it('keeps cover entry controls in the first row and status and actions in the second row', async () => {
    const wrapper = mountPins({ pinFilters: { boardFilter: 7 } });
    await settle();
    const primary = wrapper.find('[data-test="pin-tools-primary"]');

    await primary.find('[data-test="board-cover-enter"]').trigger('click');

    expect(wrapper.vm.interactionMode).toBe('cover-selection');
    expect(primary.find('[data-test="pin-selection-enter"]').attributes('disabled'))
      .toBeDefined();
    expect(primary.find('[data-test="board-cover-enter"]').attributes('disabled'))
      .toBeDefined();
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

  it('keeps export selection while hiding owner actions when Board ownership is lost', async () => {
    const wrapper = mountPins({ pinFilters: { boardFilter: 7 } });
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');

    wrapper.vm.editorMeta.currentBoard = {
      ...wrapper.vm.editorMeta.currentBoard,
      submitter: { username: 'other' },
    };
    await wrapper.vm.$nextTick();

    expect(wrapper.vm.canManagePins).toBe(false);
    expect(wrapper.vm.canSelectPins).toBe(true);
    expect(wrapper.vm.selection.active).toBe(true);
    expect(wrapper.vm.interactionMode).toBe('bulk-selection');
    expect(wrapper.find('[data-test="pin-tools-active"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="pin-selection-check-41"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="pin-selection-export"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="pin-selection-move"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="pin-selection-edit"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="pin-selection-delete"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="board-export"]').exists()).toBe(false);
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
    expect(wrapper.vm.interactionMode).toBe('browse');
    expect(wrapper.vm.coverSelection.candidateId).toBeNull();
    expect(wrapper.find('[data-test="pin-tools-active"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="board-cover-status"]').exists()).toBe(false);
    ['apply', 'cancel', 'reset'].forEach((name) => {
      expect(wrapper.find(`[data-test="board-cover-${name}"]`).exists()).toBe(false);
    });
  });

  it('exits bulk selection when the My Pins session is lost', async () => {
    const wrapper = mountPins({ pinFilters: { userFilter: 'owner' } });
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');

    wrapper.vm.editorMeta.user = { loggedIn: false, meta: {} };
    await wrapper.vm.$nextTick();

    expect(wrapper.vm.canManagePins).toBe(false);
    expect(wrapper.vm.selection.active).toBe(false);
    expect(wrapper.vm.interactionMode).toBe('browse');
    expect(wrapper.find('[data-test="pin-selection-check-41"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="pin-sort-latest"]').attributes('disabled')).toBeUndefined();
  });

  it('does not add the list tool area to a single Pin route', async () => {
    const wrapper = mountPins({ pinFilters: { idFilter: 41 } });
    await settle();

    expect(wrapper.find('[data-test="pin-tools"]').exists()).toBe(false);
    expect(API.fetchPins).not.toHaveBeenCalled();
    expect(API.fetchPin).toHaveBeenCalledWith(41);
  });
});
