/* eslint-env jest */
import axios from 'axios';
import flushPromises from 'flush-promises';
import VueI18n from 'vue-i18n';
import { createLocalVue, shallowMount } from '@vue/test-utils';

import PinEditorUI from '@/components/editors/PinEditorUI.vue';
import en from '@/components/utils/i18n/locales/en.json';

jest.mock('axios');

describe('PinEditorUI delete behavior', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.spyOn(console, 'log').mockImplementation(() => {});
    axios.delete.mockResolvedValue({ status: 204 });
  });

  afterEach(() => {
    console.log.mockRestore();
  });

  it('keeps the detail DELETE endpoint and reports moving the pin to trash', async () => {
    const localVue = createLocalVue();
    localVue.use(VueI18n);
    const dialog = { confirm: jest.fn() };
    const toast = { open: jest.fn() };
    const wrapper = shallowMount(PinEditorUI, {
      localVue,
      i18n: new VueI18n({ locale: 'en', messages: { en } }),
      propsData: {
        currentUsername: 'owner',
        pin: { id: 41, author: 'owner' },
      },
      mocks: {
        $buefy: { dialog, toast },
      },
      stubs: ['b-icon'],
    });

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    dialog.confirm.mock.calls[0][0].onConfirm();
    await flushPromises();

    expect(axios.delete).toHaveBeenCalledWith('/api/v2/pins/41/');
    expect(toast.open).toHaveBeenCalledWith('Pin moved to trash');
    expect(wrapper.emitted('pin-delete-succeed')[0]).toEqual([41]);
  });
});
