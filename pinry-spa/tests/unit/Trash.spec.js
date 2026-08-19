/* eslint-env jest */
import axios from 'axios';
import flushPromises from 'flush-promises';
import VueI18n from 'vue-i18n';
import { createLocalVue, shallowMount } from '@vue/test-utils';

import API from '@/components/api';
import PHeader from '@/components/PHeader.vue';
import TrashPins from '@/components/TrashPins.vue';
import router from '@/router';
import en from '@/components/utils/i18n/locales/en.json';
import fr from '@/components/utils/i18n/locales/fr.json';
import zh from '@/components/utils/i18n/locales/zh.json';

jest.mock('axios');

const trashedPin = {
  resource_link: 'http://localhost/api/v2/pins/41/',
  private: false,
  id: 41,
  submitter: {
    id: 7,
    username: 'owner',
    gravatar: 'avatar-hash',
  },
  url: 'http://localhost/media/originals/original.jpg',
  description: 'A trashed pin',
  referer: 'https://example.com/source',
  image: {
    id: 11,
    image: 'http://localhost/media/originals/original.jpg',
    width: 1200,
    height: 800,
    standard: {
      image: 'http://localhost/media/derivatives/standard.jpg',
      width: 600,
      height: 400,
    },
    thumbnail: {
      image: 'http://localhost/media/derivatives/thumbnail.jpg',
      width: 240,
      height: 160,
    },
    square: {
      image: 'http://localhost/media/derivatives/square.jpg',
      width: 125,
      height: 125,
    },
  },
  tags: ['example'],
};

const activePin = { ...trashedPin };
const messages = { en, fr, zh };
const mountedWrappers = [];

function deferred() {
  const request = {};
  request.promise = new Promise((resolve, reject) => {
    request.resolve = resolve;
    request.reject = reject;
  });
  return request;
}

function pinWithId(id) {
  return { ...trashedPin, id };
}

function trashState(vm) {
  return {
    pinIds: vm.pins.map(pin => pin.id),
    busyPinIds: Object.keys(vm.busyPins).sort(),
    mutationInFlight: vm.mutationInFlight,
    fetchQueued: vm.fetchQueued,
    status: { ...vm.status },
  };
}

function mountTrash(options = {}) {
  const localVue = createLocalVue();
  localVue.use(VueI18n);
  const i18n = new VueI18n({
    locale: options.locale || 'en',
    fallbackLocale: 'en',
    messages,
  });
  const buefy = {
    dialog: {
      confirm: jest.fn(),
    },
    toast: {
      open: jest.fn(),
    },
  };
  const wrapper = shallowMount(TrashPins, {
    localVue,
    i18n,
    mocks: { $buefy: buefy },
  });
  mountedWrappers.push(wrapper);
  return { wrapper, buefy };
}

afterEach(() => {
  mountedWrappers.splice(0).forEach((wrapper) => {
    wrapper.destroy();
  });
});

describe('trash API contract', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('uses the exact trash, restore, and permanent delete endpoints', async () => {
    axios.get.mockResolvedValue({ data: { results: [], next: null } });
    axios.post.mockResolvedValue({ data: activePin });
    axios.delete.mockResolvedValue({ status: 204 });

    await API.Pin.fetchTrash(60);
    await API.Pin.restore(trashedPin.id);
    await API.Pin.deletePermanently(trashedPin.id);

    expect(axios.get).toHaveBeenCalledWith('/api/v2/pins/trash/', {
      params: { ordering: '-id', limit: 30, offset: 60 },
    });
    expect(axios.post).toHaveBeenCalledWith('/api/v2/pins/41/restore/');
    expect(axios.delete).toHaveBeenCalledWith('/api/v2/pins/41/permanent/');
  });
});

describe('TrashPins', () => {
  beforeEach(() => {
    jest.restoreAllMocks();
  });

  it('removes the old scroll listener before a remount handles bottom scroll', async () => {
    const scrollHandlers = [];
    const addEventListener = window.addEventListener.bind(window);
    jest.spyOn(window, 'addEventListener').mockImplementation((type, handler, options) => {
      if (type === 'scroll') {
        scrollHandlers.push(handler);
      }
      addEventListener(type, handler, options);
    });
    jest.spyOn(API.Pin, 'fetchTrash').mockImplementation(offset => Promise.resolve({
      data: {
        count: 3,
        results: [pinWithId(offset === 0 ? 41 : 40)],
        next: '/api/v2/pins/trash/?limit=30&offset=2',
        previous: null,
      },
    }));

    const oldWrapper = mountTrash().wrapper;
    await flushPromises();
    const oldFetch = jest.spyOn(oldWrapper.vm, 'fetchMore');
    oldWrapper.destroy();

    const currentWrapper = mountTrash().wrapper;
    await flushPromises();
    const currentFetch = jest.spyOn(currentWrapper.vm, 'fetchMore');
    API.Pin.fetchTrash.mockClear();

    window.dispatchEvent(new Event('scroll'));
    await flushPromises();

    expect(oldFetch).not.toHaveBeenCalled();
    expect(currentFetch).toHaveBeenCalledTimes(1);
    expect(API.Pin.fetchTrash).toHaveBeenCalledTimes(1);

    scrollHandlers.forEach((handler) => {
      window.removeEventListener('scroll', handler);
    });
  });

  it('does not start a page fetch after destruction', async () => {
    jest.spyOn(API.Pin, 'fetchTrash').mockResolvedValue({
      data: {
        count: 2,
        results: [trashedPin],
        next: '/api/v2/pins/trash/?limit=30&offset=1',
        previous: null,
      },
    });

    const { wrapper } = mountTrash();
    await flushPromises();
    wrapper.destroy();
    const stateAfterDestroy = trashState(wrapper.vm);
    API.Pin.fetchTrash.mockClear();

    wrapper.vm.fetchMore();

    expect(API.Pin.fetchTrash).not.toHaveBeenCalled();
    expect(trashState(wrapper.vm)).toEqual(stateAfterDestroy);
    expect(wrapper.vm.disposed).toBe(true);
  });

  it.each(['resolve', 'reject'])(
    'ignores an in-flight page fetch %s after destruction',
    async (settle) => {
      const pageRequest = deferred();
      jest.spyOn(API.Pin, 'fetchTrash').mockReturnValue(pageRequest.promise);

      const { wrapper, buefy } = mountTrash();
      expect(API.Pin.fetchTrash).toHaveBeenCalledTimes(1);
      wrapper.destroy();
      const stateAfterDestroy = trashState(wrapper.vm);

      if (settle === 'resolve') {
        pageRequest.resolve({
          data: {
            count: 1,
            results: [trashedPin],
            next: null,
            previous: null,
          },
        });
      } else {
        pageRequest.reject(new Error('fetch failed'));
      }
      await flushPromises();

      expect(API.Pin.fetchTrash).toHaveBeenCalledTimes(1);
      expect(buefy.toast.open).not.toHaveBeenCalled();
      expect(buefy.dialog.confirm).not.toHaveBeenCalled();
      expect(trashState(wrapper.vm)).toEqual(stateAfterDestroy);
      expect(wrapper.vm.disposed).toBe(true);
      expect(wrapper.vm.fetchQueued).toBe(false);
    },
  );

  it.each([
    ['restore', 'resolve'],
    ['restore', 'reject'],
    ['permanent delete', 'resolve'],
    ['permanent delete', 'reject'],
  ])(
    'ignores queued fetch and UI updates when %s %s settles after destruction',
    async (action, settle) => {
      const mutationRequest = deferred();
      jest.spyOn(API.Pin, 'fetchTrash').mockResolvedValue({
        data: {
          count: 2,
          results: [trashedPin, pinWithId(40)],
          next: '/api/v2/pins/trash/?limit=30&offset=2',
          previous: null,
        },
      });
      jest.spyOn(API.Pin, 'restore').mockReturnValue(mutationRequest.promise);
      jest.spyOn(API.Pin, 'deletePermanently').mockReturnValue(mutationRequest.promise);

      const { wrapper, buefy } = mountTrash();
      await flushPromises();
      if (action === 'restore') {
        wrapper.vm.restore(trashedPin);
      } else {
        wrapper.vm.confirmPermanentDelete(trashedPin);
        buefy.dialog.confirm.mock.calls[0][0].onConfirm();
      }
      wrapper.vm.fetchMore();
      expect(wrapper.vm.fetchQueued).toBe(true);

      wrapper.destroy();
      const stateAfterDestroy = trashState(wrapper.vm);
      const dialogCallsAfterDestroy = buefy.dialog.confirm.mock.calls.length;
      API.Pin.fetchTrash.mockClear();

      if (settle === 'resolve') {
        mutationRequest.resolve(action === 'restore' ? { data: activePin } : { status: 204 });
      } else {
        mutationRequest.reject(new Error(`${action} failed`));
      }
      await flushPromises();

      expect(API.Pin.fetchTrash).not.toHaveBeenCalled();
      expect(buefy.toast.open).not.toHaveBeenCalled();
      expect(buefy.dialog.confirm).toHaveBeenCalledTimes(dialogCallsAfterDestroy);
      expect(trashState(wrapper.vm)).toEqual(stateAfterDestroy);
      expect(wrapper.vm.disposed).toBe(true);
      expect(wrapper.vm.fetchQueued).toBe(false);
    },
  );

  it('blocks restore and permanent delete while a page fetch is in flight', async () => {
    const pageRequest = deferred();
    jest.spyOn(API.Pin, 'fetchTrash')
      .mockImplementation(offset => (offset === 0 ? Promise.resolve({
        data: {
          count: 3,
          results: [trashedPin, pinWithId(40)],
          next: '/api/v2/pins/trash/?limit=30&offset=2',
          previous: null,
        },
      }) : pageRequest.promise));
    jest.spyOn(API.Pin, 'restore').mockReturnValue(deferred().promise);
    jest.spyOn(API.Pin, 'deletePermanently').mockReturnValue(deferred().promise);

    const { wrapper } = mountTrash();
    await flushPromises();
    wrapper.vm.fetchMore();
    wrapper.vm.restore(trashedPin);
    wrapper.vm.deletePermanently(pinWithId(40));
    await wrapper.vm.$nextTick();

    expect(API.Pin.restore).not.toHaveBeenCalled();
    expect(API.Pin.deletePermanently).not.toHaveBeenCalled();
    wrapper.findAll('[data-test="restore"]').wrappers.forEach((button) => {
      expect(button.attributes('disabled')).toBe('disabled');
    });

    pageRequest.resolve({
      data: {
        count: 3,
        results: [pinWithId(39)],
        next: null,
        previous: '/api/v2/pins/trash/?limit=30&offset=0',
      },
    });
    await flushPromises();
    expect(wrapper.find('[data-test="restore"]').attributes('disabled')).toBeUndefined();
  });

  it('queues one page fetch until restore succeeds and uses the reduced offset', async () => {
    const restoreRequest = deferred();
    const pageRequest = deferred();
    jest.spyOn(API.Pin, 'fetchTrash')
      .mockImplementation(offset => (offset === 0 ? Promise.resolve({
        data: {
          count: 3,
          results: [trashedPin, pinWithId(40)],
          next: '/api/v2/pins/trash/?limit=30&offset=2',
          previous: null,
        },
      }) : pageRequest.promise));
    jest.spyOn(API.Pin, 'restore').mockReturnValue(restoreRequest.promise);

    const { wrapper } = mountTrash();
    await flushPromises();
    expect(API.Pin.fetchTrash.mock.calls).toEqual([[0]]);
    expect(wrapper.vm.status).toMatchObject({
      loading: false,
      hasNext: true,
      offset: 2,
    });
    wrapper.vm.restore(trashedPin);
    expect(wrapper.vm.status.loading).toBe(false);
    wrapper.vm.fetchMore();
    wrapper.vm.fetchMore();

    expect(API.Pin.restore).toHaveBeenCalledTimes(1);
    expect(API.Pin.fetchTrash.mock.calls).toEqual([[0]]);

    restoreRequest.resolve({ data: activePin });
    await flushPromises();
    expect(API.Pin.fetchTrash.mock.calls).toEqual([[0], [1]]);

    pageRequest.resolve({
      data: {
        count: 2,
        results: [pinWithId(39)],
        next: null,
        previous: '/api/v2/pins/trash/?limit=30&offset=0',
      },
    });
    await flushPromises();

    const ids = wrapper.findAll('[data-pin-id]').wrappers
      .map(card => Number(card.attributes('data-pin-id')));
    expect(ids).toEqual([40, 39]);
    expect(new Set(ids).size).toBe(ids.length);
    expect(wrapper.vm.status.offset).toBe(2);
    expect(wrapper.find('[data-test="restore"]').attributes('disabled')).toBeUndefined();
  });

  it('queues one page fetch until permanent delete fails without changing offset', async () => {
    const deleteRequest = deferred();
    const pageRequest = deferred();
    jest.spyOn(API.Pin, 'fetchTrash')
      .mockImplementation(offset => (offset === 0 ? Promise.resolve({
        data: {
          count: 3,
          results: [trashedPin, pinWithId(40)],
          next: '/api/v2/pins/trash/?limit=30&offset=2',
          previous: null,
        },
      }) : pageRequest.promise));
    jest.spyOn(API.Pin, 'deletePermanently').mockReturnValue(deleteRequest.promise);

    const { wrapper } = mountTrash();
    await flushPromises();
    wrapper.vm.deletePermanently(trashedPin);
    wrapper.vm.fetchMore();
    wrapper.vm.fetchMore();

    expect(API.Pin.deletePermanently).toHaveBeenCalledTimes(1);
    expect(API.Pin.fetchTrash.mock.calls).toEqual([[0]]);

    deleteRequest.reject(new Error('delete failed'));
    await flushPromises();
    expect(API.Pin.fetchTrash.mock.calls).toEqual([[0], [2]]);

    pageRequest.resolve({
      data: {
        count: 3,
        results: [pinWithId(39)],
        next: null,
        previous: '/api/v2/pins/trash/?limit=30&offset=0',
      },
    });
    await flushPromises();

    const ids = wrapper.findAll('[data-pin-id]').wrappers
      .map(card => Number(card.attributes('data-pin-id')));
    expect(ids).toEqual([41, 40, 39]);
    expect(new Set(ids).size).toBe(ids.length);
    expect(wrapper.vm.status.offset).toBe(3);
    expect(wrapper.find('[data-test="restore"]').attributes('disabled')).toBeUndefined();
  });

  it('restores one pin and removes it from the trash list', async () => {
    jest.spyOn(API.Pin, 'fetchTrash').mockResolvedValue({
      data: {
        count: 1, results: [trashedPin], next: null, previous: null,
      },
    });
    jest.spyOn(API.Pin, 'restore').mockResolvedValue({ data: activePin });

    const { wrapper } = mountTrash();
    await flushPromises();
    await wrapper.find('[data-test="restore"]').trigger('click');
    await flushPromises();

    expect(API.Pin.restore).toHaveBeenCalledWith(trashedPin.id);
    expect(wrapper.find(`[data-pin-id="${trashedPin.id}"]`).exists()).toBe(false);
  });

  it('does not permanently delete before explicit confirmation', async () => {
    jest.spyOn(API.Pin, 'fetchTrash').mockResolvedValue({
      data: {
        count: 1, results: [trashedPin], next: null, previous: null,
      },
    });
    jest.spyOn(API.Pin, 'deletePermanently').mockResolvedValue({ status: 204 });

    const { wrapper, buefy } = mountTrash();
    await flushPromises();
    await wrapper.find('[data-test="delete-permanently"]').trigger('click');

    expect(buefy.dialog.confirm).toHaveBeenCalledTimes(1);
    expect(buefy.dialog.confirm.mock.calls[0][0].message).toContain('Pinry');
    expect(buefy.dialog.confirm.mock.calls[0][0].message).toContain('Immich');
    expect(API.Pin.deletePermanently).not.toHaveBeenCalled();
    expect(wrapper.find(`[data-pin-id="${trashedPin.id}"]`).exists()).toBe(true);
  });

  it('permanently deletes after approval and removes the card', async () => {
    jest.spyOn(API.Pin, 'fetchTrash').mockResolvedValue({
      data: {
        count: 1, results: [trashedPin], next: null, previous: null,
      },
    });
    jest.spyOn(API.Pin, 'deletePermanently').mockResolvedValue({ status: 204 });

    const { wrapper, buefy } = mountTrash();
    await flushPromises();
    await wrapper.find('[data-test="delete-permanently"]').trigger('click');
    buefy.dialog.confirm.mock.calls[0][0].onConfirm();
    await flushPromises();

    expect(API.Pin.deletePermanently).toHaveBeenCalledWith(trashedPin.id);
    expect(wrapper.find(`[data-pin-id="${trashedPin.id}"]`).exists()).toBe(false);
  });

  it('disables actions and prevents duplicate restore requests', async () => {
    let resolveRestore;
    jest.spyOn(API.Pin, 'fetchTrash').mockResolvedValue({
      data: {
        count: 1, results: [trashedPin], next: null, previous: null,
      },
    });
    jest.spyOn(API.Pin, 'restore').mockReturnValue(new Promise((resolve) => {
      resolveRestore = resolve;
    }));

    const { wrapper } = mountTrash();
    await flushPromises();
    await wrapper.find('[data-test="restore"]').trigger('click');
    await wrapper.vm.restore(trashedPin);

    expect(wrapper.find('[data-test="restore"]').attributes('disabled')).toBe('disabled');
    expect(API.Pin.restore).toHaveBeenCalledTimes(1);

    resolveRestore({ data: activePin });
    await flushPromises();
  });

  it('advances the offset while next is present and stops at the last page', async () => {
    const secondPin = { ...trashedPin, id: 40 };
    jest.spyOn(API.Pin, 'fetchTrash')
      .mockResolvedValueOnce({
        data: {
          count: 2,
          results: [trashedPin],
          next: '/api/v2/pins/trash/?limit=30&offset=1',
          previous: null,
        },
      })
      .mockResolvedValueOnce({
        data: {
          count: 2,
          results: [secondPin],
          next: null,
          previous: '/api/v2/pins/trash/?limit=30&offset=0',
        },
      });

    const { wrapper } = mountTrash();
    await flushPromises();
    await wrapper.vm.fetchMore();
    await flushPromises();
    await wrapper.vm.fetchMore();

    expect(API.Pin.fetchTrash.mock.calls).toEqual([[0], [1]]);
    expect(wrapper.findAll('[data-pin-id]').length).toBe(2);
  });

  it('shows loading, empty, and load error states', async () => {
    let resolveRequest;
    jest.spyOn(API.Pin, 'fetchTrash').mockReturnValue(new Promise((resolve) => {
      resolveRequest = resolve;
    }));
    const pending = mountTrash().wrapper;
    expect(pending.find('[data-test="trash-loading"]').exists()).toBe(true);
    resolveRequest({
      data: {
        count: 0, results: [], next: null, previous: null,
      },
    });
    await flushPromises();
    expect(pending.find('[data-test="trash-empty"]').exists()).toBe(true);

    jest.restoreAllMocks();
    jest.spyOn(API.Pin, 'fetchTrash').mockRejectedValue(new Error('network'));
    const failed = mountTrash().wrapper;
    await flushPromises();
    expect(failed.find('[data-test="trash-error"]').exists()).toBe(true);
  });
});

describe('trash navigation and translations', () => {
  beforeEach(() => {
    jest.restoreAllMocks();
  });

  it('registers /trash and shows its link only after login', async () => {
    expect(router.match('/trash').name).toBe('trash');
    jest.spyOn(API.User, 'fetchUserInfo').mockResolvedValue({
      id: 7,
      username: 'owner',
    });
    const localVue = createLocalVue();
    localVue.use(VueI18n);
    const wrapper = shallowMount(PHeader, {
      localVue,
      i18n: new VueI18n({ locale: 'en', messages }),
      stubs: ['router-link', 'b-icon'],
    });
    expect(wrapper.find('[data-test="trash-link"]').exists()).toBe(false);
    await flushPromises();

    expect(wrapper.find('[data-test="trash-link"]').exists()).toBe(true);
  });

  it.each(['en', 'fr', 'zh'])(
    'defines and renders trash translations for %s without raw fallbacks',
    async (locale) => {
      const requiredKeys = [
        'trashLink',
        'trashTitle',
        'trashLoading',
        'trashEmpty',
        'trashLoadError',
        'trashRestoreButton',
        'trashPermanentDeleteButton',
        'trashRestoreSuccess',
        'trashRestoreError',
        'trashPermanentDeleteConfirm',
        'trashPermanentDeleteSuccess',
        'trashPermanentDeleteError',
        'pinMoveToTrashConfirm',
        'pinMovedToTrash',
        'pinMoveToTrashError',
      ];

      requiredKeys.forEach((key) => {
        expect(messages[locale][key]).toEqual(expect.any(String));
        expect(messages[locale][key].length).toBeGreaterThan(0);
        expect(messages[locale][key]).not.toBe(key);
      });
      expect(messages[locale].trashPermanentDeleteConfirm).toContain('Pinry');
      expect(messages[locale].trashPermanentDeleteConfirm).toContain('Immich');

      jest.spyOn(API.Pin, 'fetchTrash').mockResolvedValue({
        data: {
          count: 0, results: [], next: null, previous: null,
        },
      });
      const { wrapper } = mountTrash({ locale });
      await flushPromises();
      expect(wrapper.text()).toContain(messages[locale].trashEmpty);
      expect(wrapper.text()).not.toContain('trashEmpty');
    },
  );
});
