/* eslint-env jest */

import flushPromises from 'flush-promises';
import { createLocalVue, mount } from '@vue/test-utils';

import API from '@/components/api';
import Pins from '@/components/Pins.vue';
import PinSortControls from '@/components/sorting/PinSortControls.vue';

const HOME_KEY = 'svrx.pinSort.v1:home';
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

function page(pins = [pin(30), pin(29)], next = null) {
  return Promise.resolve({ data: { results: pins, next } });
}

function storeState(key, mode, randomSeed) {
  localStorage.setItem(key, JSON.stringify({ version: 1, mode, randomSeed }));
}

function invalidSortError() {
  const error = new Error('invalid sort contract');
  error.response = { status: 400, data: { code: 'pin_sort_invalid' } };
  return error;
}

function mountPins({
  pinFilters = {},
  fetchPinsImplementation = null,
  fetchPinImplementation = null,
  boardImplementation = null,
} = {}) {
  API.fetchPins.mockImplementation(
    fetchPinsImplementation || (() => page()),
  );
  API.fetchPin.mockImplementation(
    fetchPinImplementation || (() => page([pin(pinFilters.idFilter || 1)])),
  );
  API.Board.get.mockImplementation(
    boardImplementation || (boardId => Promise.resolve({
      data: { id: boardId, submitter: { username: 'owner' } },
    })),
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

describe('PinSortControls', () => {
  it('presents all modes and emits the selected mode', async () => {
    const wrapper = mount(PinSortControls, {
      propsData: { mode: 'oldest' },
      mocks: { $t: key => key },
    });

    expect(wrapper.find('[data-test="pin-sort-latest"]').attributes('aria-pressed'))
      .toBe('false');
    expect(wrapper.find('[data-test="pin-sort-oldest"]').attributes('aria-pressed'))
      .toBe('true');
    expect(wrapper.find('[data-test="pin-sort-random"]').attributes('aria-pressed'))
      .toBe('false');

    await wrapper.find('[data-test="pin-sort-random"]').trigger('click');

    expect(wrapper.emitted('select')).toEqual([['random']]);
  });

  it.each([
    ['selection or bulk state', { disabled: true, busy: false }],
    ['loading state', { disabled: false, busy: true }],
  ])('disables every mode during %s', async (_name, props) => {
    const wrapper = mount(PinSortControls, {
      propsData: { mode: 'latest', ...props },
      mocks: { $t: key => key },
    });

    ['latest', 'oldest', 'random'].forEach((mode) => {
      expect(wrapper.find(`[data-test="pin-sort-${mode}"]`).attributes('disabled'))
        .toBe('disabled');
    });
  });
});

describe('Pins sorting', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mountedPinWrappers = [];
    localStorage.clear();
    storeState(HOME_KEY, 'latest', 5);
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
    API.User.fetchUserInfo = jest.fn();
    API.Pin.fetchSelectionIds = jest.fn();
    API.Pin.bulk = jest.fn();
  });

  afterEach(() => {
    mountedPinWrappers.forEach(wrapper => wrapper.destroy());
    if (Pins.methods.initializeMeta.mockRestore) {
      Pins.methods.initializeMeta.mockRestore();
    }
    localStorage.clear();
  });

  it('keeps loading and changing sort modes when the localStorage getter is blocked', async () => {
    const storageDescriptor = Object.getOwnPropertyDescriptor(window, 'localStorage');
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new DOMException('storage blocked', 'SecurityError');
      },
    });

    try {
      const wrapper = mountPins();
      await settle();

      expect(API.fetchPins).toHaveBeenCalledTimes(1);
      expect(API.fetchPins.mock.calls[0][4]).toEqual({
        version: 1, mode: 'latest', randomSeed: 5,
      });

      wrapper.vm.applySortMode('oldest');
      await settle();
      expect(wrapper.vm.sortState.mode).toBe('oldest');
      expect(API.fetchPins).toHaveBeenCalledTimes(2);

      expect(wrapper.vm.fallbackToLegacySort()).toBe(true);
      await settle();
      expect(wrapper.vm.sortLegacyFallback).toBe(true);
      expect(API.fetchPins).toHaveBeenCalledTimes(3);
    } finally {
      Object.defineProperty(window, 'localStorage', storageDescriptor);
    }
  });

  it('loads one isolated sort state for each list context', async () => {
    storeState('svrx.pinSort.v1:user:owner', 'oldest', 13);

    const wrapper = mountPins({ pinFilters: { userFilter: 'owner' } });
    await settle();

    expect(API.fetchPins.mock.calls[0][4]).toEqual({
      version: 1, mode: 'oldest', randomSeed: 13,
    });
    expect(wrapper.find('[data-test="pin-sort-oldest"]').attributes('aria-pressed'))
      .toBe('true');
  });

  it('loads the stored home, user, board, and tag state after each context change', async () => {
    storeState(HOME_KEY, 'oldest', 11);
    storeState('svrx.pinSort.v1:user:owner', 'random', 12);
    storeState('svrx.pinSort.v1:board:7', 'latest', 13);
    storeState('svrx.pinSort.v1:tag:%EC%95%A0%EB%8B%88', 'oldest', 14);
    const wrapper = mountPins();
    await settle();

    expect(API.fetchPins.mock.calls[0][4]).toEqual({
      version: 1, mode: 'oldest', randomSeed: 11,
    });

    const expectContext = async (filters, expected) => {
      await wrapper.setProps({ pinFilters: filters });
      await settle();
      const lastCall = API.fetchPins.mock.calls[API.fetchPins.mock.calls.length - 1];
      expect(lastCall[4]).toEqual(expected);
      expect(wrapper.find(`[data-test="pin-sort-${expected.mode}"]`)
        .attributes('aria-pressed')).toBe('true');
    };
    await expectContext(
      { userFilter: 'owner' },
      { version: 1, mode: 'random', randomSeed: 12 },
    );
    await expectContext(
      { boardFilter: 7 },
      { version: 1, mode: 'latest', randomSeed: 13 },
    );
    await expectContext(
      { tagFilter: '애니' },
      { version: 1, mode: 'oldest', randomSeed: 14 },
    );
  });

  it('persists a selected mode and does not reload an already active non-random mode', async () => {
    const wrapper = mountPins();
    await settle();

    await wrapper.find('[data-test="pin-sort-oldest"]').trigger('click');
    await settle();

    expect(JSON.parse(localStorage.getItem(HOME_KEY))).toEqual({
      version: 1, mode: 'oldest', randomSeed: 5,
    });
    const lastCall = API.fetchPins.mock.calls[API.fetchPins.mock.calls.length - 1];
    expect(lastCall[0]).toBe(0);
    expect(lastCall[4]).toEqual({ version: 1, mode: 'oldest', randomSeed: 5 });

    const requestCount = API.fetchPins.mock.calls.length;
    await wrapper.find('[data-test="pin-sort-oldest"]').trigger('click');
    await settle();
    expect(API.fetchPins).toHaveBeenCalledTimes(requestCount);
  });

  it('random re-click replaces seed, resets offset and scrolls to top', async () => {
    const wrapper = mountPins();
    await settle();
    wrapper.vm.sortState = { version: 1, mode: 'random', randomSeed: 5 };
    wrapper.vm.seedFactory = () => 6;
    wrapper.vm.status.offset = 30;

    await wrapper.find('[data-test="pin-sort-random"]').trigger('click');
    await settle();

    expect(wrapper.vm.sortState.randomSeed).toBe(6);
    expect(API.fetchPins.mock.calls[API.fetchPins.mock.calls.length - 1][0]).toBe(0);
    expect(window.scrollTo).toHaveBeenCalledWith(0, 0);
    expect(wrapper.find('[data-test="pin-sort-announcement"]').text())
      .toBe('pinSortReshuffled');
  });

  it('mutates the live region for every consecutive random reshuffle', async () => {
    storeState(HOME_KEY, 'random', 5);
    const wrapper = mountPins();
    await settle();
    let nextSeed = 5;
    wrapper.vm.seedFactory = () => {
      nextSeed += 1;
      return nextSeed;
    };
    const liveRegion = wrapper.find('[data-test="pin-sort-announcement"]').element;
    let mutationCount = 0;
    const observer = new MutationObserver(() => {
      mutationCount += 1;
    });
    observer.observe(liveRegion, { childList: true, characterData: true, subtree: true });

    try {
      await wrapper.find('[data-test="pin-sort-random"]').trigger('click');
      await settle();
      expect(liveRegion.textContent.trim()).toBe('pinSortReshuffled');
      expect(mutationCount).toBeGreaterThan(0);
      const firstMutationCount = mutationCount;

      await wrapper.find('[data-test="pin-sort-random"]').trigger('click');
      await settle();
      expect(liveRegion.textContent.trim()).toBe('pinSortReshuffled');
      expect(mutationCount).toBeGreaterThan(firstMutationCount);
    } finally {
      observer.disconnect();
    }
  });

  it('disables all modes during selection or an operation', async () => {
    const wrapper = mountPins();
    await settle();

    wrapper.vm.selection.active = true;
    await wrapper.vm.$nextTick();
    ['latest', 'oldest', 'random'].forEach((mode) => {
      expect(wrapper.find(`[data-test="pin-sort-${mode}"]`).attributes('disabled'))
        .toBe('disabled');
    });

    wrapper.vm.selection.active = false;
    wrapper.vm.selection.operationInFlight = true;
    await wrapper.vm.$nextTick();
    ['latest', 'oldest', 'random'].forEach((mode) => {
      expect(wrapper.find(`[data-test="pin-sort-${mode}"]`).attributes('disabled'))
        .toBe('disabled');
    });
  });

  it('clears list state on context change and ignores the previous deferred page', async () => {
    storeState('svrx.pinSort.v1:user:other', 'oldest', 17);
    const oldPage = deferred();
    const currentPage = deferred();
    let requestCount = 0;
    const wrapper = mountPins({
      fetchPinsImplementation: () => {
        requestCount += 1;
        if (requestCount === 1) return page([pin(41)], '/api/v2/pins/?offset=1');
        return requestCount === 2 ? oldPage.promise : currentPage.promise;
      },
    });
    await settle();
    wrapper.vm.fetchMore();

    await wrapper.setProps({ pinFilters: { userFilter: 'other' } });
    expect(wrapper.vm.blocks).toEqual([]);
    expect(wrapper.vm.blocksMap).toEqual({});
    expect(wrapper.vm.status.offset).toBe(0);
    expect(window.scrollTo).toHaveBeenCalledWith(0, 0);

    currentPage.resolve({ data: { results: [pin(80, 'other')], next: null } });
    await settle();
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([80]);
    expect(API.fetchPins.mock.calls[2][4]).toEqual({
      version: 1, mode: 'oldest', randomSeed: 17,
    });

    oldPage.resolve({ data: { results: [pin(40)], next: null } });
    await settle();
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([80]);
    expect(wrapper.vm.status.offset).toBe(1);
  });

  it('uses the same random mode and seed for every offset request', async () => {
    storeState(HOME_KEY, 'random', 23);
    const responses = [
      () => page([pin(30), pin(29)], '/api/v2/pins/?offset=2'),
      () => page([pin(28)], null),
    ];
    const wrapper = mountPins({ fetchPinsImplementation: () => responses.shift()() });
    await settle();

    wrapper.vm.fetchMore();
    await settle();

    expect(API.fetchPins.mock.calls.map(call => [call[0], call[4]])).toEqual([
      [0, { version: 1, mode: 'random', randomSeed: 23 }],
      [2, { version: 1, mode: 'random', randomSeed: 23 }],
    ]);
  });

  it('deduplicates later rows while advancing offset by consumed rows', async () => {
    const responses = [
      () => page([pin(30), pin(29)], '/api/v2/pins/?offset=2'),
      () => page([pin(29), pin(28)], null),
    ];
    const wrapper = mountPins({ fetchPinsImplementation: () => responses.shift()() });
    await settle();

    wrapper.vm.fetchMore();
    await settle();

    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([30, 29, 28]);
    expect(Object.keys(wrapper.vm.blocksMap).map(Number).sort((a, b) => a - b))
      .toEqual([28, 29, 30]);
    expect(wrapper.vm.status.offset).toBe(4);
  });

  it('deduplicates repeated IDs within one page while consuming every row', async () => {
    const wrapper = mountPins({
      fetchPinsImplementation: () => page([pin(30), pin(30), pin(29)], null),
    });

    await settle();

    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([30, 29]);
    expect(Object.keys(wrapper.vm.blocksMap).map(Number).sort((a, b) => a - b))
      .toEqual([29, 30]);
    expect(wrapper.vm.status.offset).toBe(3);
  });

  it('clears a partial sorted list before one offset-zero legacy retry', async () => {
    storeState(HOME_KEY, 'oldest', 31);
    const responses = [
      () => page([pin(30), pin(29)], '/api/v2/pins/?offset=2'),
      () => Promise.reject(invalidSortError()),
      () => page([pin(90), pin(89)], null),
    ];
    const wrapper = mountPins({ fetchPinsImplementation: () => responses.shift()() });
    await settle();
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([30, 29]);

    wrapper.vm.fetchMore();
    await settle();

    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([90, 89]);
    expect(wrapper.vm.blocks.map(item => item.id)).not.toContain(30);
    const legacyCalls = API.fetchPins.mock.calls.filter(call => call[4] === null);
    expect(legacyCalls).toHaveLength(1);
    expect(legacyCalls[0][0]).toBe(0);
    expect(localStorage.getItem(HOME_KEY)).toBeNull();
    expect(wrapper.vm.sortLegacyFallback).toBe(true);
    expect(wrapper.vm.sortFallbackAttempted).toBe(true);
  });

  it('does not loop when the legacy retry also reports an invalid sort contract', async () => {
    const invalid = () => Promise.reject(invalidSortError());
    const wrapper = mountPins({
      fetchPinsImplementation: jest.fn()
        .mockImplementationOnce(invalid)
        .mockImplementationOnce(invalid),
    });

    await settle();

    expect(API.fetchPins).toHaveBeenCalledTimes(2);
    expect(API.fetchPins.mock.calls.filter(call => call[4] === null)).toHaveLength(1);
    expect(wrapper.vm.sortLegacyFallback).toBe(true);
    expect(wrapper.vm.sortFallbackAttempted).toBe(true);
    expect(wrapper.vm.status.loading).toBe(false);
  });

  it('leaves legacy fallback when the user selects any sort, including latest', async () => {
    const wrapper = mountPins();
    await settle();
    wrapper.vm.sortLegacyFallback = true;
    wrapper.vm.sortFallbackAttempted = true;
    wrapper.vm.sortState = { version: 1, mode: 'latest', randomSeed: 5 };

    wrapper.vm.applySortMode('latest');
    await settle();

    expect(wrapper.vm.sortLegacyFallback).toBe(false);
    expect(wrapper.vm.sortFallbackAttempted).toBe(false);
    const lastCall = API.fetchPins.mock.calls[API.fetchPins.mock.calls.length - 1];
    expect(lastCall[0]).toBe(0);
    expect(lastCall[4]).toEqual({ version: 1, mode: 'latest', randomSeed: 5 });
  });

  it.each([
    ['network error', new Error('offline')],
    ['server error', { response: { status: 503, data: { code: 'unavailable' } } }],
  ])('keeps sort state without fallback after a %s', async (_name, error) => {
    storeState(HOME_KEY, 'random', 37);
    const wrapper = mountPins({
      fetchPinsImplementation: () => Promise.reject(error),
    });

    await settle();

    expect(wrapper.vm.sortState).toEqual({ version: 1, mode: 'random', randomSeed: 37 });
    expect(wrapper.vm.sortLegacyFallback).toBe(false);
    expect(wrapper.vm.sortFallbackAttempted).toBe(false);
    expect(JSON.parse(localStorage.getItem(HOME_KEY))).toEqual({
      version: 1, mode: 'random', randomSeed: 37,
    });
    expect(API.fetchPins).toHaveBeenCalledTimes(1);
    expect(API.fetchPins.mock.calls[0][4]).toEqual({
      version: 1, mode: 'random', randomSeed: 37,
    });
    expect(wrapper.vm.status.loading).toBe(false);
  });

  it('does not fallback when a 5xx body contains pin_sort_invalid', async () => {
    storeState(HOME_KEY, 'random', 41);
    const error = new Error('server error');
    error.response = {
      status: 503,
      data: { code: 'pin_sort_invalid' },
    };
    const wrapper = mountPins({
      fetchPinsImplementation: () => Promise.reject(error),
    });

    await settle();

    expect(wrapper.vm.sortState).toEqual({ version: 1, mode: 'random', randomSeed: 41 });
    expect(wrapper.vm.sortLegacyFallback).toBe(false);
    expect(wrapper.vm.sortFallbackAttempted).toBe(false);
    expect(JSON.parse(localStorage.getItem(HOME_KEY))).toEqual({
      version: 1, mode: 'random', randomSeed: 41,
    });
    expect(API.fetchPins).toHaveBeenCalledTimes(1);
    expect(wrapper.vm.status.loading).toBe(false);
  });

  it('검증된 애니메이션 GIF·WebP만 배지를 표시한다', async () => {
    const gif = pin(3);
    gif.image.animation_format = 'GIF';
    const webp = pin(4);
    webp.image.animation_format = 'WEBP';
    const stillGif = pin(5);
    stillGif.image.image = 'https://example.test/original.GIF?token=1';
    stillGif.image.animation_format = null;
    const stillWebp = pin(6);
    stillWebp.image.image = 'https://example.test/original.webp';
    stillWebp.image.animation_format = null;
    const jpg = pin(2);
    jpg.url = 'https://example.test/source.gif';
    jpg.image.thumbnail.image = 'https://example.test/thumb.gif';
    jpg.image.animation_format = 'unknown';
    const wrapper = mountPins({
      fetchPinsImplementation: () => page([gif, webp, stillGif, stillWebp, jpg]),
    });
    await settle();
    expect(wrapper.find('[data-test="pin-card-3"] .pin-animation-badge').text()).toBe('GIF ▶');
    expect(wrapper.find('[data-test="pin-card-4"] .pin-animation-badge').text()).toBe('WEBP ▶');
    [2, 5, 6].forEach((id) => {
      expect(wrapper.find(`[data-test="pin-card-${id}"] .pin-animation-badge`).exists()).toBe(false);
    });
  });

  it('상세보기는 같은 목록과 페이지 요청을 사용하고 목록 변경 때 닫힌다', async () => {
    const pending = deferred();
    const wrapper = mountPins({
      pinFilters: { tagFilter: 'cats' },
      fetchPinsImplementation: jest.fn()
        .mockImplementation(() => page())
        .mockImplementationOnce(() => page([pin(30)], '/next'))
        .mockImplementationOnce(() => pending.promise),
    });
    await settle();
    const close = jest.fn();
    wrapper.vm.$buefy.modal.open.mockReturnValue({ close, $once: jest.fn() });
    wrapper.vm.openPreview(wrapper.vm.blocks[0]);
    const config = wrapper.vm.$buefy.modal.open.mock.calls[0][0];
    expect(config.scroll).toBe('keep');
    expect(config.props.navigation().items.map(item => item.id)).toEqual([30]);
    const existingRequest = wrapper.vm.fetchMore();
    const previewRequest = config.props.loadNext();
    expect(API.fetchPins).toHaveBeenCalledTimes(2);
    expect(API.fetchPins.mock.calls[1][1]).toBe('cats');
    pending.resolve({ data: { results: [pin(29)], next: null } });
    await Promise.all([existingRequest, previewRequest]);
    expect(config.props.navigation().items.map(item => item.id)).toEqual([30, 29]);
    expect(config.props.navigation().hasNext).toBe(false);
    wrapper.vm.reset();
    expect(close).toHaveBeenCalledTimes(1);
  });

  it('hides controls for a single Pin lookup', async () => {
    const wrapper = mountPins({ pinFilters: { idFilter: 7 } });
    await settle();

    expect(wrapper.find('[data-test="pin-sort-latest"]').exists()).toBe(false);
    expect(API.fetchPins).not.toHaveBeenCalled();
    expect(API.fetchPin).toHaveBeenCalledWith(7);
    wrapper.vm.openPreview(wrapper.vm.blocks[0]);
    expect(wrapper.vm.$buefy.modal.open.mock.calls[0][0].props.navigation).toBeNull();
  });

  it('사용자가 닫은 모달을 다음 열기에서 다시 닫지 않는다', async () => {
    const wrapper = mountPins();
    await settle();
    const close = jest.fn();
    let onClose;
    wrapper.vm.$buefy.modal.open.mockReturnValue({
      close,
      $once(event, callback) { if (event === 'close') onClose = callback; },
    });
    wrapper.vm.openPreview(wrapper.vm.blocks[0]);
    onClose();
    wrapper.vm.openPreview(wrapper.vm.blocks[1]);
    expect(close).not.toHaveBeenCalled();
  });
});
