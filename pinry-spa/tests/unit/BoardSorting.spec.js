/* eslint-env jest */
import axios from 'axios';
import { createLocalVue, mount } from '@vue/test-utils';
import flushPromises from 'flush-promises';

import API from '@/components/api';
import Boards from '@/components/Boards.vue';
import BoardSortControls from '@/components/board_ordering/BoardSortControls.vue';
import {
  boardSortStorageKey,
  generateRandomSeed,
  readBoardSortState,
  transitionBoardSortState,
  writeBoardSortState,
} from '@/components/board_ordering/boardSortState';

jest.mock('axios');

function board(id, username = 'alice') {
  return {
    id,
    name: `Board ${id}`,
    private: false,
    cover: null,
    cover_pin_id: null,
    total_pins: 0,
    submitter: { username },
  };
}

function page(results, next = null) {
  return Promise.resolve({ data: { results, next } });
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function mountBoards(filters = {}) {
  const localVue = createLocalVue();
  localVue.directive('masonry', {});
  localVue.directive('masonry-tile', {});
  return mount(Boards, {
    localVue,
    propsData: { filters },
    mocks: { $t: key => key },
    stubs: {
      BoardEditorUI: true,
      loadingSpinner: true,
      noMore: true,
      'router-link': true,
    },
  });
}

async function settle() {
  await flushPromises();
  await flushPromises();
}

describe('browser-local Board sorting state', () => {
  beforeEach(() => localStorage.clear());

  it('uses the custom default and isolates encoded user keys', () => {
    const aliceKey = boardSortStorageKey('alice');
    const bobKey = boardSortStorageKey('a b');

    expect(aliceKey).toBe('svrx.boardSort.v1:user:alice');
    expect(bobKey).toBe('svrx.boardSort.v1:user:a%20b');
    expect(readBoardSortState(localStorage, aliceKey, () => 17)).toEqual({
      version: 1, mode: 'custom', randomSeed: 17,
    });
    expect(localStorage.getItem(bobKey)).toBeNull();
  });

  it('recovers malformed state and reseeds only when random is selected again', () => {
    const key = boardSortStorageKey('alice');
    localStorage.setItem(key, '{broken');
    const state = readBoardSortState(localStorage, key, () => 17);

    expect(state).toEqual({ version: 1, mode: 'custom', randomSeed: 17 });
    expect(JSON.parse(localStorage.getItem(key))).toEqual(state);
    const random = transitionBoardSortState(state, 'random', () => 18);
    expect(random.state.randomSeed).toBe(17);
    expect(transitionBoardSortState(random.state, 'random', () => 18).state.randomSeed).toBe(18);
  });

  it('uses an in-memory custom default when storage access fails', () => {
    const storage = {
      getItem: () => { throw new Error('read failed'); },
      removeItem: () => { throw new Error('cleanup failed'); },
      setItem: () => { throw new Error('write failed'); },
    };

    expect(readBoardSortState(storage, boardSortStorageKey('alice'), () => 17)).toEqual({
      version: 1, mode: 'custom', randomSeed: 17,
    });
    expect(writeBoardSortState(storage, boardSortStorageKey('alice'), {
      version: 1, mode: 'custom', randomSeed: 17, extra: true,
    })).toBe(false);
  });

  it('creates a bounded fallback seed when Web Crypto is unavailable', () => {
    const random = jest.spyOn(Math, 'random').mockReturnValue(0.5);
    Object.defineProperty(window, 'crypto', { configurable: true, value: undefined });

    expect(generateRandomSeed()).toBe(1073741823);
    random.mockRestore();
  });
});

describe('Board sorting query and controls', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    axios.get.mockResolvedValue({ data: { results: [], next: null } });
    Object.defineProperty(window, 'crypto', {
      configurable: true,
      value: {
        getRandomValues(values) {
          values.set([17]);
          return values;
        },
      },
    });
    window.scrollTo = jest.fn();
  });

  it('serializes the user Board random sort query', async () => {
    await API.fetchBoardForUser('alice', 50, 50, { mode: 'random', randomSeed: 17 });

    expect(axios.get).toHaveBeenCalledWith('/api/v2/boards/', {
      params: {
        submitter__username: 'alice',
        offset: 50,
        limit: 50,
        sort: 'random',
        random_seed: 17,
      },
    });
  });

  it('keeps legacy ordering when no Board sort state is supplied', async () => {
    await API.fetchBoardForUser('alice', 0, 50);

    expect(axios.get).toHaveBeenCalledWith('/api/v2/boards/', {
      params: {
        submitter__username: 'alice', offset: 0, limit: 50, ordering: '-id',
      },
    });
  });

  it('renders four modes with the active aria-pressed state', async () => {
    const wrapper = mount(BoardSortControls, {
      propsData: { mode: 'oldest' },
      mocks: { $t: key => key },
    });

    expect(wrapper.find('[data-test="board-sort-custom"]').attributes('aria-pressed')).toBe('false');
    expect(wrapper.find('[data-test="board-sort-latest"]').attributes('aria-pressed')).toBe('false');
    expect(wrapper.find('[data-test="board-sort-oldest"]').attributes('aria-pressed')).toBe('true');
    expect(wrapper.find('[data-test="board-sort-random"]').attributes('aria-pressed')).toBe('false');
    expect(wrapper.findAll('button').wrappers.map(button => button.text())).toEqual([
      'boardSortCustom', 'boardSortLatest', 'boardSortOldest', 'boardSortRandom',
    ]);
    expect(wrapper.find('[data-test="board-sort-oldest"]').classes()).toContain('is-primary');
    expect(wrapper.find('[data-test="board-sort-custom"]').classes()).not.toContain('is-primary');
  });

  it('does not show controls or send a sort query for Board search', async () => {
    API.User.fetchUserInfo = jest.fn(() => Promise.resolve(null));
    API.Board.fetchListWhichContains = jest.fn(() => page([board(1)]));
    const wrapper = mountBoards({ boardNameContains: 'travel' });
    await settle();

    expect(wrapper.find('[data-test="board-sort-custom"]').exists()).toBe(false);
    expect(API.Board.fetchListWhichContains).toHaveBeenCalledWith('travel', 0);
    wrapper.destroy();
  });

  it('keeps the user Board screen working when the localStorage getter throws', async () => {
    API.User.fetchUserInfo = jest.fn(() => Promise.resolve(null));
    API.fetchBoardForUser = jest.fn(() => page([]));
    const descriptor = Object.getOwnPropertyDescriptor(window, 'localStorage');
    let wrapper;
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() { throw new DOMException('blocked', 'SecurityError'); },
    });

    try {
      wrapper = mountBoards({ boardUsername: 'alice' });
      await settle();
      expect(wrapper.vm.sortState).toEqual({
        version: 1, mode: 'custom', randomSeed: 17,
      });

      wrapper.vm.applySortMode('latest');
      await settle();
      expect(wrapper.vm.sortState).toEqual({
        version: 1, mode: 'latest', randomSeed: 17,
      });
      expect(API.fetchBoardForUser.mock.calls[1][3]).toEqual(wrapper.vm.sortState);
    } finally {
      if (wrapper) wrapper.destroy();
      Object.defineProperty(window, 'localStorage', descriptor);
    }
  });

  it('keeps in-memory sorting when the same user context is reapplied after write failure', async () => {
    API.User.fetchUserInfo = jest.fn(() => Promise.resolve(null));
    API.fetchBoardForUser = jest.fn(() => page([]));
    const wrapper = mountBoards({ boardUsername: 'alice' });
    await settle();
    const setItem = jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('write failed');
    });

    try {
      wrapper.vm.applySortMode('latest');
      await settle();
      expect(wrapper.vm.sortState).toEqual({
        version: 1, mode: 'latest', randomSeed: 17,
      });

      await wrapper.setProps({ filters: { boardUsername: 'alice' } });
      await settle();
      expect(wrapper.vm.sortState).toEqual({
        version: 1, mode: 'latest', randomSeed: 17,
      });
      expect(API.fetchBoardForUser.mock.calls[2][3]).toEqual(wrapper.vm.sortState);
    } finally {
      setItem.mockRestore();
      wrapper.destroy();
    }
  });

  it('uses the same random mode and seed for every offset request', async () => {
    localStorage.setItem(boardSortStorageKey('alice'), JSON.stringify({
      version: 1, mode: 'random', randomSeed: 23,
    }));
    API.User.fetchUserInfo = jest.fn(() => Promise.resolve(null));
    API.fetchBoardForUser = jest.fn()
      .mockImplementationOnce(() => page([board(1)], '/api/v2/boards/?offset=1'))
      .mockImplementationOnce(() => page([board(2)], null));
    const wrapper = mountBoards({ boardUsername: 'alice' });
    await settle();

    wrapper.vm.fetchMore();
    await settle();

    expect(API.fetchBoardForUser.mock.calls.map(call => [call[1], call[3]])).toEqual([
      [0, { version: 1, mode: 'random', randomSeed: 23 }],
      [1, { version: 1, mode: 'random', randomSeed: 23 }],
    ]);
    wrapper.destroy();
  });

  it('deduplicates Board pages while advancing offset by consumed rows', async () => {
    API.User.fetchUserInfo = jest.fn(() => Promise.resolve(null));
    const responses = [
      () => page([board(1), board(2), board(2)], '/api/v2/boards/?offset=3'),
      () => page([board(2), board(3), board(3)], '/api/v2/boards/?offset=6'),
      () => page([], null),
    ];
    API.fetchBoardForUser = jest.fn(() => responses.shift()());
    const wrapper = mountBoards({ boardUsername: 'alice' });
    await settle();

    wrapper.vm.fetchMore();
    await settle();
    wrapper.vm.fetchMore();
    await settle();

    expect(API.fetchBoardForUser.mock.calls.map(call => call[1])).toEqual([0, 3, 6]);
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([1, 2, 3]);
    expect(Object.keys(wrapper.vm.blocksMap).map(Number).sort()).toEqual([1, 2, 3]);
    expect(wrapper.vm.status.offset).toBe(6);
    wrapper.destroy();
  });

  it('keeps sort state for pagination and discards late responses after a mode change', async () => {
    API.User.fetchUserInfo = jest.fn(() => Promise.resolve(null));
    const oldPage = deferred();
    API.fetchBoardForUser = jest.fn()
      .mockImplementationOnce(() => page([board(1)], '/api/v2/boards/?offset=1'))
      .mockImplementationOnce(() => oldPage.promise)
      .mockImplementationOnce(() => page([board(3)], null));
    const wrapper = mountBoards({ boardUsername: 'alice' });
    await settle();

    wrapper.vm.fetchMore();
    wrapper.vm.status.loading = false;
    wrapper.vm.applySortMode('latest');
    await settle();
    oldPage.resolve({ data: { results: [board(2)], next: null } });
    await settle();

    expect(API.fetchBoardForUser.mock.calls.map(call => [call[1], call[3]])).toEqual([
      [0, { version: 1, mode: 'custom', randomSeed: expect.any(Number) }],
      [1, { version: 1, mode: 'custom', randomSeed: expect.any(Number) }],
      [0, { version: 1, mode: 'latest', randomSeed: expect.any(Number) }],
    ]);
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([3]);
    wrapper.destroy();
  });
});
