/* eslint-env jest */

import axios from 'axios';
import flushPromises from 'flush-promises';
import { createLocalVue, mount } from '@vue/test-utils';

import API from '@/components/api';
import Boards from '@/components/Boards.vue';
import bus from '@/components/utils/bus';
import ko from '@/components/utils/i18n/locales/ko.json';

jest.mock('axios');
jest.mock('@/components/utils/scroll', () => ({ bindScroll2Bottom: jest.fn() }));

function board(id, username = 'alice') {
  return {
    id,
    name: `Board ${id}`,
    private: false,
    cover: null,
    cover_pin_id: null,
    total_pins: id,
    submitter: { username },
  };
}

function response(results, next = null) {
  return Promise.resolve({ data: { results, next } });
}

function orderResponse(version, boardIds) {
  return Promise.resolve({ data: { version, board_ids: boardIds } });
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function translate(key, values = {}) {
  const message = ko[key] || key;
  return Object.entries(values).reduce(
    (result, [name, value]) => result.replace(`{${name}}`, value),
    message,
  );
}

const wrappers = [];

function mountBoards(filters = { boardUsername: 'alice' }) {
  const localVue = createLocalVue();
  localVue.directive('masonry', {});
  localVue.directive('masonry-tile', {});
  const wrapper = mount(Boards, {
    localVue,
    attachTo: document.body,
    propsData: { filters },
    mocks: {
      $redrawVueMasonry: jest.fn(),
      $t: translate,
    },
    stubs: {
      BoardEditorUI: {
        template: '<div data-test="board-editor-stub"></div>',
      },
      loadingSpinner: true,
      noMore: true,
      'router-link': {
        props: ['to'],
        template: '<a data-test="board-link"><slot /></a>',
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

async function enterOrdering(wrapper) {
  await wrapper.find('[data-test="board-order-enter"]').trigger('click');
  await settle();
}

describe('Board custom ordering API', () => {
  beforeEach(() => jest.clearAllMocks());

  it('uses the snapshot endpoint and exact versioned save payload', async () => {
    axios.get.mockResolvedValue({ data: { version: 3, board_ids: [1, 2] } });
    axios.put.mockResolvedValue({ data: { version: 4, board_ids: [2, 1] } });

    await API.Board.fetchOrder();
    await API.Board.saveOrder(3, [2, 1]);

    expect(axios.get).toHaveBeenCalledWith('/api/v2/boards/order/');
    expect(axios.put).toHaveBeenCalledWith('/api/v2/boards/order/', {
      version: 3,
      board_ids: [2, 1],
    });
  });
});

describe('accessible Board custom ordering', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    window.scrollTo = jest.fn();
    API.User.fetchUserInfo = jest.fn().mockResolvedValue({ username: 'alice' });
    API.fetchBoardForUser = jest.fn(() => response([board(1), board(2), board(3)]));
    API.Board.fetchListWhichContains = jest.fn(() => response([board(1), board(2)]));
    API.Board.fetchOrder = jest.fn(() => orderResponse(7, [1, 2, 3]));
    API.Board.saveOrder = jest.fn(() => orderResponse(8, [2, 1, 3]));
  });

  afterEach(() => {
    wrappers.splice(0).forEach((wrapper) => {
      bus.bus.$off(bus.events.refreshBoards, wrapper.vm.reset);
      wrapper.destroy();
    });
  });

  it('shows entry only after owner metadata on a non-search custom list with two Boards', async () => {
    const user = deferred();
    API.User.fetchUserInfo
      .mockImplementationOnce(() => user.promise)
      .mockResolvedValueOnce({ username: 'bob' })
      .mockResolvedValueOnce({ username: 'alice' });

    const ownerWrapper = mountBoards();
    await settle();
    expect(ownerWrapper.find('[data-test="board-order-enter"]').exists()).toBe(false);

    user.resolve({ username: 'alice' });
    await settle();
    expect(ownerWrapper.find('[data-test="board-order-enter"]').exists()).toBe(true);
    expect(ownerWrapper.find('[data-test="board-order-enter"]').text()).toBe('순서 편집');

    const foreignWrapper = mountBoards();
    const searchWrapper = mountBoards({
      boardUsername: 'alice',
      boardNameContains: 'travel',
    });
    await settle();

    expect(foreignWrapper.find('[data-test="board-order-enter"]').exists()).toBe(false);
    expect(searchWrapper.find('[data-test="board-order-enter"]').exists()).toBe(false);

    ownerWrapper.vm.applySortMode('latest');
    await settle();
    expect(ownerWrapper.find('[data-test="board-order-enter"]').exists()).toBe(false);
  });

  it('waits for every page after the first 50 and enters only for an exact ID set', async () => {
    const firstPage = Array.from({ length: 50 }, (_value, index) => board(index + 1));
    const lastPage = deferred();
    API.fetchBoardForUser
      .mockImplementationOnce(() => response(firstPage, '/api/v2/boards/?offset=50'))
      .mockImplementationOnce(() => lastPage.promise);
    API.Board.fetchOrder.mockImplementation(
      () => orderResponse(11, Array.from({ length: 51 }, (_value, index) => index + 1)),
    );
    const wrapper = mountBoards();
    await settle();

    await wrapper.find('[data-test="board-order-enter"]').trigger('click');
    await settle();

    expect(API.fetchBoardForUser.mock.calls.map(call => call[1])).toEqual([0, 50]);
    expect(wrapper.vm.ordering.editing).toBe(false);
    expect(wrapper.find('[data-test="board-order-save"]').exists()).toBe(false);

    lastPage.resolve({ data: { results: [board(51)], next: null } });
    await settle();

    expect(wrapper.vm.ordering.editing).toBe(true);
    expect(wrapper.vm.blocks).toHaveLength(51);
    expect(wrapper.find('[data-test="board-order-save"]').exists()).toBe(true);
  });

  it('refuses partial editing when the loaded and snapshot ID sets differ', async () => {
    API.Board.fetchOrder.mockImplementation(() => orderResponse(7, [1, 2, 4]));
    const wrapper = mountBoards();
    await settle();

    await enterOrdering(wrapper);

    expect(wrapper.vm.ordering.editing).toBe(false);
    expect(wrapper.find('[data-test="board-order-error"]').text())
      .toContain('새로고침');
    expect(API.Board.saveOrder).not.toHaveBeenCalled();
  });

  it('uses one movement path for buttons and drag, redraws Masonry, and keeps focus', async () => {
    const wrapper = mountBoards();
    await settle();
    await enterOrdering(wrapper);
    wrapper.vm.$redrawVueMasonry.mockClear();

    const previous = wrapper.find('[data-test="board-order-previous-2"]');
    previous.element.focus();
    await previous.trigger('click');
    await wrapper.vm.$nextTick();

    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([2, 1, 3]);
    expect(document.activeElement.getAttribute('data-test')).toBe('board-order-previous-2');
    expect(wrapper.vm.$redrawVueMasonry).toHaveBeenCalled();
    expect(wrapper.find('[data-test="board-order-announcement"]').text())
      .toContain('1번째로 이동');
    expect(wrapper.find('[data-test="board-order-announcement"]').attributes('aria-live'))
      .toBe('polite');

    const dataTransfer = {
      effectAllowed: '',
      setData: jest.fn(),
      getData: jest.fn(() => '2'),
    };
    await wrapper.find('[data-test="board-order-handle-2"]')
      .trigger('dragstart', { dataTransfer });
    expect(wrapper.find('[data-test="board-order-handle-2"]').attributes('draggable'))
      .toBe('true');
    await wrapper.find('[data-test="board-order-card-3"]')
      .trigger('drop', { dataTransfer });
    await wrapper.vm.$nextTick();

    expect(dataTransfer.effectAllowed).toBe('move');
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([1, 3, 2]);
    expect(wrapper.vm.$redrawVueMasonry).toHaveBeenCalledTimes(2);
  });

  it('supports pick, arrow move, release, and Escape on the native handle', async () => {
    const wrapper = mountBoards();
    await settle();
    await enterOrdering(wrapper);
    const handle = wrapper.find('[data-test="board-order-handle-2"]');
    handle.element.focus();

    await handle.trigger('keydown', { key: ' ' });
    expect(handle.attributes('aria-grabbed')).toBe('true');

    await handle.trigger('keydown', { key: 'ArrowLeft' });
    await wrapper.vm.$nextTick();
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([2, 1, 3]);
    expect(document.activeElement.getAttribute('data-test')).toBe('board-order-handle-2');

    await wrapper.find('[data-test="board-order-handle-2"]')
      .trigger('keydown', { key: 'Enter' });
    expect(wrapper.vm.ordering.pickedBoardId).toBeNull();

    const movedHandle = wrapper.find('[data-test="board-order-handle-2"]');
    await movedHandle.trigger('keydown', { key: ' ' });
    await movedHandle.trigger('keydown', { key: 'Escape' });
    expect(wrapper.vm.ordering.pickedBoardId).toBeNull();
  });

  it('blocks links, existing editors, sorting, and pagination while editing', async () => {
    API.fetchBoardForUser.mockImplementationOnce(
      () => response([board(1), board(2), board(3)], '/api/v2/boards/?offset=3'),
    );
    const wrapper = mountBoards();
    await settle();
    API.fetchBoardForUser.mockClear();
    await enterOrdering(wrapper);
    API.fetchBoardForUser.mockClear();

    wrapper.vm.currentEditBoard = 1;
    await wrapper.vm.$nextTick();
    wrapper.vm.fetchMore();
    await settle();

    expect(wrapper.find('[data-test="board-link"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="board-editor-stub"]').isVisible()).toBe(false);
    expect(wrapper.find('[data-test="board-sort-custom"]').attributes('disabled'))
      .toBe('disabled');
    expect(API.fetchBoardForUser).not.toHaveBeenCalled();
  });

  it('restores the original snapshot on cancel', async () => {
    const wrapper = mountBoards();
    await settle();
    await enterOrdering(wrapper);
    await wrapper.find('[data-test="board-order-next-1"]').trigger('click');
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([2, 1, 3]);

    await wrapper.find('[data-test="board-order-cancel"]').trigger('click');
    await wrapper.vm.$nextTick();

    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([1, 2, 3]);
    expect(wrapper.vm.ordering.editing).toBe(false);
    expect(API.Board.saveOrder).not.toHaveBeenCalled();
  });

  it('saves one full versioned permutation and locks controls while busy', async () => {
    const save = deferred();
    API.Board.saveOrder.mockImplementation(() => save.promise);
    const wrapper = mountBoards();
    await settle();
    await enterOrdering(wrapper);
    await wrapper.find('[data-test="board-order-next-1"]').trigger('click');

    await wrapper.find('[data-test="board-order-save"]').trigger('click');
    await wrapper.find('[data-test="board-order-save"]').trigger('click');

    expect(API.Board.saveOrder).toHaveBeenCalledTimes(1);
    expect(API.Board.saveOrder).toHaveBeenCalledWith(7, [2, 1, 3]);
    expect(wrapper.find('[data-test="board-order-save"]').attributes('disabled'))
      .toBe('disabled');
    expect(wrapper.find('[data-test="board-order-cancel"]').attributes('disabled'))
      .toBe('disabled');

    save.resolve({ data: { version: 8, board_ids: [2, 1, 3] } });
    await settle();

    expect(wrapper.vm.ordering.editing).toBe(false);
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([2, 1, 3]);
  });

  it('keeps the draft and version on 409 and asks for a refresh', async () => {
    API.Board.saveOrder.mockRejectedValue({
      response: { status: 409, data: { code: 'board_order_changed' } },
    });
    const wrapper = mountBoards();
    await settle();
    await enterOrdering(wrapper);
    await wrapper.find('[data-test="board-order-next-1"]').trigger('click');
    await wrapper.find('[data-test="board-order-save"]').trigger('click');
    await settle();

    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([2, 1, 3]);
    expect(wrapper.vm.ordering.version).toBe(7);
    expect(wrapper.vm.ordering.editing).toBe(true);
    expect(wrapper.find('[data-test="board-order-error"]').text())
      .toContain('새로고침');
  });

  it('keeps the editable draft and version after a network save failure', async () => {
    API.Board.saveOrder.mockRejectedValue(new Error('offline'));
    const wrapper = mountBoards();
    await settle();
    await enterOrdering(wrapper);
    await wrapper.find('[data-test="board-order-next-1"]').trigger('click');
    await wrapper.find('[data-test="board-order-save"]').trigger('click');
    await settle();

    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([2, 1, 3]);
    expect(wrapper.vm.ordering.version).toBe(7);
    expect(wrapper.vm.ordering.editing).toBe(true);
    expect(wrapper.find('[data-test="board-order-error"]').text())
      .toContain('변경한 순서를 유지');
  });

  it('invalidates late full-page entry responses after filters change', async () => {
    const latePage = deferred();
    API.fetchBoardForUser
      .mockImplementationOnce(
        () => response([board(1), board(2)], '/api/v2/boards/?offset=2'),
      )
      .mockImplementationOnce(() => latePage.promise)
      .mockImplementationOnce(() => response([board(90, 'bob'), board(91, 'bob')]));
    API.User.fetchUserInfo
      .mockResolvedValueOnce({ username: 'alice' })
      .mockResolvedValueOnce({ username: 'bob' });
    API.Board.fetchOrder.mockImplementation(() => orderResponse(7, [1, 2, 3]));
    const wrapper = mountBoards();
    await settle();

    await wrapper.find('[data-test="board-order-enter"]').trigger('click');
    await settle();
    await wrapper.setProps({ filters: { boardUsername: 'bob' } });
    await settle();
    latePage.resolve({ data: { results: [board(3)], next: null } });
    await settle();

    expect(wrapper.vm.ordering.editing).toBe(false);
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([90, 91]);
    expect(wrapper.vm.sortStorageKey).toContain('bob');
  });
});
