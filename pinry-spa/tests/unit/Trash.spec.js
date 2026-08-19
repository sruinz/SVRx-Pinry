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
  return { wrapper, buefy };
}

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
