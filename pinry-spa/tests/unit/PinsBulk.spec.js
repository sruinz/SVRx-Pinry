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
} = {}) {
  mockAuthenticatedUsername = authenticatedUsername;
  API.fetchPins.mockImplementation(() => page(pins));
  API.Board.get.mockReturnValue(
    boardRequest || Promise.resolve({
      data: { id: pinFilters.boardFilter, submitter: { username: authenticatedUsername } },
    }),
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
        canModify: true,
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
    API.Pin.fetchSelectionIds = jest.fn();
  });

  afterEach(() => {
    mountedPinWrappers.forEach((wrapper) => {
      wrapper.destroy();
    });
    Pins.methods.initializeMeta.mockRestore();
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

  it('preserves the loaded selection when the server scope exceeds 50,000', async () => {
    API.Pin.fetchSelectionIds = jest.fn().mockRejectedValue({
      response: { status: 409, data: { code: 'selection_too_large' } },
    });
    const wrapper = mountPins();
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');

    await wrapper.find('[data-test="pin-selection-select-all"]').trigger('click');
    await settle();

    expect(wrapper.vm.selection).toMatchObject({
      selectedIds: [41], scope: 'loaded', allCount: 0,
    });
    expect(wrapper.find('[data-test="pin-selection-live"]').text())
      .toContain('bulkPinSelectionTooLarge');
  });

  it('fails closed for delete and update when a selected pin is not owned', async () => {
    const wrapper = mountPins({ pins: [pin(41), pin(40, 'other')] });
    await settle();
    await wrapper.find('[data-test="pin-selection-enter"]').trigger('click');
    await wrapper.find('[data-test="pin-card-41"]').trigger('click');
    await wrapper.find('[data-test="pin-card-40"]').trigger('click');

    expect(wrapper.find('[data-test="pin-selection-add-to-board"]').attributes('disabled'))
      .toBeUndefined();
    ['move', 'edit', 'delete'].forEach((action) => {
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

    ['move', 'edit', 'delete'].forEach((action) => {
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
