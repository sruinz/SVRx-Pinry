/* eslint-env jest */

import axios from 'axios';
import flushPromises from 'flush-promises';
import { mount } from '@vue/test-utils';

import API from '@/components/api';
import BoardCoverToolbar from '@/components/board_cover/BoardCoverToolbar.vue';
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

jest.mock('axios');

let mountedPinWrappers = [];

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function pin(id, { author = 'owner', private_ = false } = {}) {
  return {
    id,
    private: private_,
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

function guardedRouterLink(navigate) {
  return {
    props: ['to'],
    methods: {
      follow(event) {
        if (!event.defaultPrevented) navigate(this.to);
      },
    },
    template: '<a href="#" @click="follow"><slot /></a>',
  };
}

function page(pins) {
  return Promise.resolve({ data: { results: pins, next: null } });
}

function mountPins({
  pinFilters = { boardFilter: 7 },
  authenticatedUsername = 'owner',
  board = {
    id: 7,
    private: false,
    cover_pin_id: 41,
    submitter: { username: 'owner' },
  },
  pins = [pin(41), pin(40), pin(39, { private_: true })],
  routerLink = {
    props: ['to'],
    template: '<a href="#"><slot /></a>',
  },
} = {}) {
  API.fetchPins.mockImplementation(() => page(pins));
  API.Board.get.mockResolvedValue({ data: board });


  const wrapper = mount(Pins, {
    global: {
      directives: { masonry: {}, 'masonry-tile': {} },
      mocks: {
        $t: key => key,
      },
      stubs: {
        EditorUI: true,
        loadingSpinner: true,
        noMore: true,
        'router-link': routerLink,
      },
    },

    attachTo: document.body,
    props: { pinFilters },
  });
  wrapper.vm.editorMeta.user = authenticatedUsername === null
    ? { loggedIn: false, meta: {} }
    : { loggedIn: true, meta: { username: authenticatedUsername } };
  wrapper.vm.metaReady.user = true;
  mountedPinWrappers.push(wrapper);
  return wrapper;
}

async function settle() {
  await flushPromises();
  await flushPromises();
}

function dispatchEscape() {
  document.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Escape',
    bubbles: true,
    cancelable: true,
  }));
}

describe('Board cover API', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('sends the exact board-cover request', async () => {
    axios.patch.mockResolvedValue({ data: { id: 7, cover_pin_id: 31 } });

    await API.Board.setCover(7, 31);

    expect(axios.patch).toHaveBeenCalledWith(
      '/api/v2/boards/7/cover/',
      { pin_id: 31 },
    );
  });
});

describe('BoardCoverToolbar', () => {
  it('emits presentation events and exposes enter-button focus', async () => {
    const wrapper = mount(BoardCoverToolbar, {
      global: { mocks: { $t: key => key } },
      props: {
        active: false,
        selectedId: null,
        currentCoverId: 41,
        busy: false,
        canReset: true,
      },

      attachTo: document.body,
    });

    wrapper.vm.focusEnter();
    expect(document.activeElement).toBe(
      wrapper.find('[data-test="board-cover-enter"]').element,
    );
    await wrapper.find('[data-test="board-cover-enter"]').trigger('click');
    expect(wrapper.emitted('enter')).toHaveLength(1);

    await wrapper.setProps({ active: true, selectedId: 40 });
    const events = ['apply', 'cancel', 'reset'];
    events.forEach((event) => {
      wrapper.find(`[data-test="board-cover-${event}"]`).trigger('click');
    });
    await wrapper.vm.$nextTick();
    events.forEach((event) => {
      expect(wrapper.emitted(event)).toHaveLength(1);
    });
    wrapper.unmount();
  });
});

describe('Pins board-cover selection mode', () => {
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
    jest.spyOn(Pins.methods, 'initializeMeta').mockImplementation(() => {});
    API.fetchPins = jest.fn();
    API.fetchPin = jest.fn();
    API.Board.get = jest.fn();
    API.Board.setCover = jest.fn();
    API.User.fetchUserInfo = jest.fn();
    API.Pin.fetchSelectionIds = jest.fn();
    API.Pin.bulk = jest.fn();
  });

  afterEach(() => {
    mountedPinWrappers.forEach(wrapper => wrapper.unmount());
    if (Pins.methods.initializeMeta.mockRestore) Pins.methods.initializeMeta.mockRestore();
    localStorage.clear();
  });

  it.each([
    ['non-owned board', { boardFilter: 7 }, 'viewer', {
      id: 7, private: false, cover_pin_id: null, submitter: { username: 'owner' },
    }],
    ['My Pins profile', { userFilter: 'owner' }, 'owner', {}],
    ['tag list', { tagFilter: 'photo' }, 'owner', {}],
    ['general list', {}, 'owner', {}],
  ])('hides the toolbar outside an owned Board: %s', async (
    name, pinFilters, authenticatedUsername, board,
  ) => {
    const wrapper = mountPins({ pinFilters, authenticatedUsername, board });
    await settle();

    expect(wrapper.find('[data-test="board-cover-enter"]').exists()).toBe(false);
  });

  it('never activates bulk and cover selection together', async () => {
    const wrapper = mountPins();
    await settle();

    await wrapper.find('[data-test="board-cover-enter"]').trigger('click');
    expect(wrapper.vm.interactionMode).toBe('cover-selection');
    expect(wrapper.find('[data-test="pin-selection-enter"]').attributes('disabled'))
      .toBeDefined();
    wrapper.vm.enterSelection();
    expect(wrapper.vm.interactionMode).toBe('cover-selection');
    expect(wrapper.vm.selection.active).toBe(false);

    wrapper.vm.cancelCoverSelection();
    await wrapper.vm.$nextTick();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    expect(wrapper.vm.interactionMode).toBe('bulk-selection');
    expect(wrapper.find('[data-test="board-cover-enter"]').attributes('disabled'))
      .toBeDefined();
    wrapper.vm.enterCoverSelection();
    expect(wrapper.vm.interactionMode).toBe('bulk-selection');
    expect(wrapper.vm.coverSelection.candidateId).toBeNull();
  });

  it('uses card click, Enter, and Space for one cover candidate without preview', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="board-cover-enter"]').trigger('click');
    const first = wrapper.find('[data-test="pin-card-41"]');
    const second = wrapper.find('[data-test="pin-card-40"]');

    expect(first.attributes()).toMatchObject({
      role: 'button', tabindex: '0', 'aria-pressed': 'true',
    });
    await second.trigger('click');
    expect(wrapper.vm.coverSelection.candidateId).toBe(40);
    expect(first.attributes('aria-pressed')).toBe('false');
    expect(second.attributes('aria-pressed')).toBe('true');
    await first.trigger('keydown', { key: 'Enter' });
    expect(wrapper.vm.coverSelection.candidateId).toBe(41);
    await second.trigger('keydown', { key: ' ' });
    expect(wrapper.vm.coverSelection.candidateId).toBe(40);
    await wrapper.find('[data-test="pin-image-41"]').trigger('click');
    expect(wrapper.vm.coverSelection.candidateId).toBe(41);
    expect(overlays.openModal).not.toHaveBeenCalled();
  });

  it('prevents inner router-link navigation before selecting the card', async () => {
    const navigate = jest.fn();
    const taggedPin = pin(40);
    taggedPin.tags = ['photo'];
    const wrapper = mountPins({
      board: {
        id: 7,
        private: false,
        cover_pin_id: null,
        submitter: { username: 'owner' },
      },
      pins: [taggedPin],
      routerLink: guardedRouterLink(navigate),
    });
    await settle();
    await wrapper.find('[data-test="board-cover-enter"]').trigger('click');
    const links = wrapper.findAll('.pin-footer a');

    await links.at(0).trigger('click');

    expect(navigate).not.toHaveBeenCalled();
    expect(wrapper.vm.coverSelection.candidateId).toBe(40);

    wrapper.vm.cancelCoverSelection();
    wrapper.vm.enterSelection();
    await links.at(1).trigger('click');

    expect(navigate).not.toHaveBeenCalled();
    expect(wrapper.vm.selection.selectedIds).toEqual([40]);
  });

  it('disables a private candidate on a public board', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="board-cover-enter"]').trigger('click');
    const privateCard = wrapper.find('[data-test="pin-card-39"]');

    expect(privateCard.attributes('aria-disabled')).toBe('true');
    expect(privateCard.attributes('aria-label')).toBe('boardCoverPrivateUnavailable');
    await privateCard.trigger('click');
    expect(wrapper.vm.coverSelection.candidateId).toBe(41);
  });

  it('cancels with Escape and restores focus to the enter button', async () => {
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="board-cover-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-40"]').trigger('click');

    dispatchEscape();
    await wrapper.vm.$nextTick();

    expect(wrapper.vm.interactionMode).toBe('browse');
    expect(wrapper.vm.coverSelection.candidateId).toBeNull();
    expect(document.activeElement).toBe(
      wrapper.find('[data-test="board-cover-enter"]').element,
    );
  });

  it('discards the candidate and request token on route reset while preserving sort context', async () => {
    const wrapper = mountPins();
    await settle();
    wrapper.vm.sortState = { version: 1, mode: 'random', randomSeed: 17 };
    wrapper.vm.sortStorageKey = 'svrx.pinSort.v1:board:7';
    localStorage.setItem(
      'svrx.pinSort.v1:tag:photo',
      JSON.stringify({ version: 1, mode: 'oldest', randomSeed: 23 }),
    );
    wrapper.vm.enterCoverSelection();
    wrapper.vm.selectCoverCandidate(wrapper.vm.blocks[1]);
    wrapper.vm.coverSelection.inFlight = true;
    const token = wrapper.vm.coverSelection.requestToken;

    await wrapper.setProps({ pinFilters: { tagFilter: 'photo' } });
    await settle();

    expect(wrapper.vm.interactionMode).toBe('browse');
    expect(wrapper.vm.coverSelection).toMatchObject({
      candidateId: null,
      inFlight: false,
      error: null,
      requestToken: token + 1,
    });
    expect(wrapper.vm.sortState).toEqual({ version: 1, mode: 'oldest', randomSeed: 23 });
  });

  it('loads Board metadata once while later pages preserve a saved cover', async () => {
    const initialBoard = {
      id: 7,
      private: false,
      cover_pin_id: 41,
      submitter: { username: 'owner' },
    };
    const savedBoard = { ...initialBoard, cover_pin_id: 40 };
    const staleBoardRequest = deferred();
    const nextPageRequest = deferred();
    const wrapper = mountPins({
      board: initialBoard,
      pins: [pin(41), pin(40)],
    });
    await settle();
    wrapper.vm.status.hasNext = true;
    API.Board.get.mockReturnValueOnce(staleBoardRequest.promise);
    API.fetchPins.mockReturnValueOnce(nextPageRequest.promise);
    API.Board.setCover.mockResolvedValue({ data: savedBoard });

    wrapper.vm.fetchMore();
    wrapper.vm.enterCoverSelection();
    wrapper.vm.selectCoverCandidate(wrapper.vm.blocks[1]);
    await wrapper.vm.applyCoverPin(40);
    await settle();
    expect(wrapper.vm.editorMeta.currentBoard.cover_pin_id).toBe(40);

    staleBoardRequest.resolve({ data: initialBoard });
    await settle();
    nextPageRequest.resolve({
      data: { results: [pin(39)], next: null },
    });
    await settle();

    expect(wrapper.vm.editorMeta.currentBoard.cover_pin_id).toBe(40);
    expect(API.Board.get).toHaveBeenCalledTimes(1);
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([41, 40, 39]);
    expect(wrapper.vm.status.offset).toBe(3);
    expect(wrapper.vm.status.hasNext).toBe(false);
  });

  it('loads Board metadata again once after reset starts a new generation', async () => {
    const wrapper = mountPins({ pins: [pin(41)] });
    await settle();
    expect(API.Board.get).toHaveBeenCalledTimes(1);

    wrapper.vm.reset();
    await settle();

    expect(API.Board.get).toHaveBeenCalledTimes(2);
    expect(wrapper.vm.metaReady.board).toBe(true);
    expect(wrapper.vm.blocks.map(item => item.id)).toEqual([41]);
  });

  it('applies once, refreshes board metadata, exits, and restores focus', async () => {
    const request = deferred();
    API.Board.setCover.mockReturnValue(request.promise);
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="board-cover-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-40"]').trigger('click');

    await wrapper.find('[data-test="board-cover-apply"]').trigger('click');
    await wrapper.find('[data-test="board-cover-apply"]').trigger('click');

    expect(API.Board.setCover).toHaveBeenCalledTimes(1);
    expect(API.Board.setCover).toHaveBeenCalledWith(7, 40);
    request.resolve({
      data: {
        id: 7,
        private: false,
        cover_pin_id: 40,
        submitter: { username: 'owner' },
      },
    });
    await settle();

    expect(wrapper.vm.editorMeta.currentBoard.cover_pin_id).toBe(40);
    expect(wrapper.vm.interactionMode).toBe('browse');
    expect(wrapper.vm.coverSelection.candidateId).toBeNull();
    expect(overlays.toast).toHaveBeenCalledWith({
      message: 'boardCoverSaved',
      type: 'is-success',
    });
    expect(document.activeElement).toBe(
      wrapper.find('[data-test="board-cover-enter"]').element,
    );
  });

  it('finishes a successful save when the toolbar disappears before the response', async () => {
    const request = deferred();
    API.Board.setCover.mockReturnValue(request.promise);
    const wrapper = mountPins();
    await settle();
    wrapper.vm.enterCoverSelection();
    wrapper.vm.selectCoverCandidate(wrapper.vm.blocks[1]);
    wrapper.vm.applyCoverPin(40);

    wrapper.vm.editorMeta.user.meta.username = 'viewer';
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="board-cover-enter"]').exists()).toBe(false);
    request.resolve({
      data: {
        id: 7,
        private: false,
        cover_pin_id: 40,
        submitter: { username: 'owner' },
      },
    });
    await settle();

    expect(wrapper.vm.interactionMode).toBe('browse');
    expect(wrapper.vm.coverSelection.inFlight).toBe(false);
  });

  it('ignores a late apply result after the route filter changes', async () => {
    const request = deferred();
    API.Board.setCover.mockReturnValue(request.promise);
    const wrapper = mountPins();
    await settle();
    wrapper.vm.enterCoverSelection();
    wrapper.vm.selectCoverCandidate(wrapper.vm.blocks[1]);
    wrapper.vm.applyCoverPin(40);

    await wrapper.setProps({ pinFilters: { tagFilter: 'photo' } });
    await settle();
    request.resolve({
      data: {
        id: 7,
        private: false,
        cover_pin_id: 40,
        submitter: { username: 'owner' },
      },
    });
    await settle();

    expect(wrapper.vm.editorMeta.currentBoard.id).not.toBe(7);
    expect(wrapper.vm.interactionMode).toBe('browse');
    expect(wrapper.vm.coverSelection.inFlight).toBe(false);
  });

  it('keeps the candidate after a network failure and allows a retry', async () => {
    API.Board.setCover
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce({
        data: {
          id: 7,
          private: false,
          cover_pin_id: 40,
          submitter: { username: 'owner' },
        },
      });
    const wrapper = mountPins();
    await settle();
    wrapper.vm.enterCoverSelection();
    wrapper.vm.selectCoverCandidate(wrapper.vm.blocks[1]);

    await wrapper.vm.applyCoverPin(40);
    await settle();

    expect(wrapper.vm.interactionMode).toBe('cover-selection');
    expect(wrapper.vm.coverSelection).toMatchObject({
      candidateId: 40,
      inFlight: false,
      error: 'boardCoverSaveFailed',
    });
    expect(wrapper.find('[data-test="board-cover-error"]').text())
      .toBe('boardCoverSaveFailed');

    await wrapper.vm.applyCoverPin(40);
    await settle();
    expect(API.Board.setCover).toHaveBeenCalledTimes(2);
    expect(wrapper.vm.interactionMode).toBe('browse');
  });

  it('keeps the candidate after an HTTP 5xx response and allows a retry', async () => {
    API.Board.setCover
      .mockRejectedValueOnce({
        response: { status: 503, data: { code: 'temporary_failure' } },
      })
      .mockResolvedValueOnce({
        data: {
          id: 7,
          private: false,
          cover_pin_id: 40,
          submitter: { username: 'owner' },
        },
      });
    const wrapper = mountPins();
    await settle();
    wrapper.vm.enterCoverSelection();
    wrapper.vm.selectCoverCandidate(wrapper.vm.blocks[1]);

    await wrapper.vm.applyCoverPin(40);
    await settle();

    expect(wrapper.vm.interactionMode).toBe('cover-selection');
    expect(wrapper.vm.coverSelection).toMatchObject({
      candidateId: 40,
      inFlight: false,
      error: 'boardCoverSaveFailed',
    });
    expect(wrapper.text()).not.toContain('temporary_failure');

    await wrapper.vm.applyCoverPin(40);
    await settle();
    expect(API.Board.setCover).toHaveBeenCalledTimes(2);
    expect(wrapper.vm.interactionMode).toBe('browse');
  });

  it.each(['resolve', 'reject'])(
    'ignores a late %s after the component is destroyed',
    async (outcome) => {
      const request = deferred();
      API.Board.setCover.mockReturnValue(request.promise);
      const wrapper = mountPins();
      await settle();
      wrapper.vm.enterCoverSelection();
      wrapper.vm.selectCoverCandidate(wrapper.vm.blocks[1]);
      const originalBoard = wrapper.vm.editorMeta.currentBoard;
      const applyPromise = wrapper.vm.applyCoverPin(40);

      wrapper.unmount();
      if (outcome === 'resolve') {
        request.resolve({
          data: {
            id: 7,
            private: false,
            cover_pin_id: 40,
            submitter: { username: 'owner' },
          },
        });
      } else {
        request.reject({ response: { status: 503, data: { code: 'late_failure' } } });
      }
      await applyPromise;
      await settle();

      expect(wrapper.vm.editorMeta.currentBoard).toBe(originalBoard);
      expect(wrapper.vm.interactionMode).toBe('browse');
      expect(wrapper.vm.coverSelection).toMatchObject({
        candidateId: null,
        inFlight: false,
        error: null,
      });
      expect(overlays.toast).not.toHaveBeenCalled();
    },
  );

  it.each([
    [400, 'board_cover_invalid'],
    [400, 'board_cover_private_pin'],
    [409, 'board_cover_changed'],
  ])('refreshes after confirmed candidate error %i %s without exposing its code', async (
    status, code,
  ) => {
    API.Board.setCover.mockRejectedValue({ response: { status, data: { code } } });
    const wrapper = mountPins();
    await settle();
    wrapper.vm.enterCoverSelection();
    wrapper.vm.selectCoverCandidate(wrapper.vm.blocks[1]);
    const boardFetches = API.Board.get.mock.calls.length;

    await wrapper.vm.applyCoverPin(40);
    await settle();

    expect(wrapper.vm.interactionMode).toBe('browse');
    expect(wrapper.vm.coverSelection.candidateId).toBeNull();
    expect(API.Board.get.mock.calls.length).toBeGreaterThan(boardFetches);
    expect(overlays.toast).toHaveBeenCalledWith({
      message: 'boardCoverRefreshRequired',
      type: 'is-warning',
    });
    expect(wrapper.text()).not.toContain(code);
    expect(JSON.stringify(overlays.toast.mock.calls)).not.toContain(code);
  });

  it.each([403, 404])('exits and refreshes after an unavailable board response %i', async (status) => {
    API.Board.setCover.mockRejectedValue({
      response: { status, data: { code: 'board_cover_internal_code' } },
    });
    const wrapper = mountPins();
    await settle();
    wrapper.vm.enterCoverSelection();
    wrapper.vm.selectCoverCandidate(wrapper.vm.blocks[1]);
    const boardFetches = API.Board.get.mock.calls.length;

    await wrapper.vm.applyCoverPin(40);
    await settle();

    expect(wrapper.vm.interactionMode).toBe('browse');
    expect(wrapper.vm.coverSelection.candidateId).toBeNull();
    expect(API.Board.get.mock.calls.length).toBeGreaterThan(boardFetches);
    expect(overlays.toast).toHaveBeenCalledWith({
      message: 'boardCoverSaveFailed',
      type: 'is-danger',
    });
    expect(wrapper.text()).not.toContain('board_cover_internal_code');
    expect(JSON.stringify(overlays.toast.mock.calls))
      .not.toContain('board_cover_internal_code');
  });

  it('resets to automatic selection only after confirmation and submits once', async () => {
    const request = deferred();
    API.Board.setCover.mockReturnValue(request.promise);
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="board-cover-enter"]').trigger('click');

    await wrapper.find('[data-test="board-cover-reset"]').trigger('click');

    expect(overlays.confirm).toHaveBeenCalledTimes(1);
    expect(API.Board.setCover).not.toHaveBeenCalled();
    const { onConfirm } = overlays.confirm.mock.calls[0][1];
    onConfirm();
    onConfirm();
    expect(API.Board.setCover).toHaveBeenCalledTimes(1);
    expect(API.Board.setCover).toHaveBeenCalledWith(7, null);

    request.resolve({
      data: {
        id: 7,
        private: false,
        cover_pin_id: null,
        submitter: { username: 'owner' },
      },
    });
    await settle();
    expect(wrapper.vm.currentBoardCoverId).toBeNull();
    expect(wrapper.vm.interactionMode).toBe('browse');
  });
});
