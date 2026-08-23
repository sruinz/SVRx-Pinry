/* eslint-env jest */

import axios from 'axios';
import flushPromises from 'flush-promises';
import { createLocalVue, mount } from '@vue/test-utils';

import API from '@/components/api';
import BoardCoverToolbar from '@/components/board_cover/BoardCoverToolbar.vue';
import Pins from '@/components/Pins.vue';

jest.mock('axios');

let mountedPinWrappers = [];

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
} = {}) {
  API.fetchPins.mockImplementation(() => page(pins));
  API.Board.get.mockResolvedValue({ data: board });

  const localVue = createLocalVue();
  localVue.directive('masonry', {});
  localVue.directive('masonry-tile', {});
  const wrapper = mount(Pins, {
    localVue,
    attachTo: document.body,
    propsData: { pinFilters },
    mocks: {
      $buefy: { modal: { open: jest.fn() } },
      $t: key => key,
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
      propsData: {
        active: false,
        selectedId: null,
        currentCoverId: 41,
        busy: false,
        canReset: true,
      },
      mocks: { $t: key => key },
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
    wrapper.destroy();
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
    API.User.fetchUserInfo = jest.fn();
    API.Pin.fetchSelectionIds = jest.fn();
    API.Pin.bulk = jest.fn();
  });

  afterEach(() => {
    mountedPinWrappers.forEach(wrapper => wrapper.destroy());
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
      .toBe('disabled');
    wrapper.vm.enterSelection();
    expect(wrapper.vm.interactionMode).toBe('cover-selection');
    expect(wrapper.vm.selection.active).toBe(false);

    wrapper.vm.cancelCoverSelection();
    await wrapper.vm.$nextTick();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    expect(wrapper.vm.interactionMode).toBe('bulk-selection');
    expect(wrapper.find('[data-test="board-cover-enter"]').attributes('disabled'))
      .toBe('disabled');
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
    expect(wrapper.vm.$buefy.modal.open).not.toHaveBeenCalled();
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
});
