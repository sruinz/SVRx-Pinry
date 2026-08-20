/* eslint-env jest */
import axios from 'axios';
import flushPromises from 'flush-promises';
import VueI18n from 'vue-i18n';
import { createLocalVue, shallowMount } from '@vue/test-utils';

import PinEditorUI from '@/components/editors/PinEditorUI.vue';
import en from '@/components/utils/i18n/locales/en.json';

jest.mock('axios');

function deferred() {
  const request = {};
  request.promise = new Promise((resolve, reject) => {
    request.resolve = resolve;
    request.reject = reject;
  });
  request.promise.catch(() => {});
  return request;
}

async function resolveRequest(request) {
  request.resolve({ status: 204 });
  await request.promise;
  await flushPromises();
  await flushPromises();
}

async function rejectRequest(request) {
  request.reject(new Error('network'));
  await request.promise.catch(() => {});
  await flushPromises();
  await flushPromises();
}

describe('PinEditorUI delete behavior', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.spyOn(console, 'log').mockImplementation(() => {});
    axios.delete.mockResolvedValue({ status: 204 });
  });

  afterEach(() => {
    console.log.mockRestore();
  });

  function mountEditor() {
    const localVue = createLocalVue();
    localVue.use(VueI18n);
    const dialog = { confirm: jest.fn() };
    const toast = { open: jest.fn() };
    return {
      dialog,
      toast,
      wrapper: shallowMount(PinEditorUI, {
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
      }),
    };
  }

  it('asks for confirmation before permanently deleting a pin', async () => {
    const { dialog, toast, wrapper } = mountEditor();
    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    expect(dialog.confirm).toHaveBeenCalledTimes(1);
    expect(dialog.confirm.mock.calls[0][0].message).toBe('Delete this Pin?');
    expect(axios.delete).not.toHaveBeenCalled();

    dialog.confirm.mock.calls[0][0].onConfirm();
    dialog.confirm.mock.calls[0][0].onConfirm();
    await flushPromises();

    expect(axios.delete).toHaveBeenCalledTimes(1);
    expect(axios.delete).toHaveBeenCalledWith('/api/v2/pins/41/');
    expect(toast.open).toHaveBeenCalledWith('Pin deleted');
    expect(wrapper.emitted('pin-delete-succeed')[0]).toEqual([41]);
  });

  it('keeps the pin and reports an error when direct deletion fails', async () => {
    axios.delete.mockRejectedValueOnce(new Error('network'));
    const { dialog, toast, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    dialog.confirm.mock.calls[0][0].onConfirm();
    await flushPromises();

    expect(toast.open).toHaveBeenCalledWith({
      type: 'is-danger', message: 'Failed to delete Pin',
    });
    expect(wrapper.emitted('pin-delete-succeed')).toBeUndefined();
  });

  it('opens one confirmation while a delete confirmation is already open', async () => {
    const request = deferred();
    axios.delete.mockImplementation(() => request.promise);
    const { dialog, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    await wrapper.find('[data-test="delete-pin"]').trigger('click');

    expect(dialog.confirm).toHaveBeenCalledTimes(1);
    expect(axios.delete).not.toHaveBeenCalled();
  });

  it('uses a confirmation callback only once before and after deletion settles', async () => {
    const request = deferred();
    axios.delete.mockImplementation(() => request.promise);
    const { dialog, toast, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    const { onConfirm } = dialog.confirm.mock.calls[0][0];
    onConfirm();
    onConfirm();

    expect(axios.delete).toHaveBeenCalledTimes(1);
    await resolveRequest(request);
    onConfirm();
    await flushPromises();

    expect(axios.delete).toHaveBeenCalledTimes(1);
    expect(wrapper.vm.deleteInFlight).toBe(false);
    expect(toast.open).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted('pin-delete-succeed')).toHaveLength(1);
  });

  it('allows a failed deletion to retry only through a new confirmation', async () => {
    const firstRequest = deferred();
    const secondRequest = deferred();
    axios.delete
      .mockReturnValueOnce(firstRequest.promise)
      .mockReturnValueOnce(secondRequest.promise);
    const { dialog, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    const firstConfirm = dialog.confirm.mock.calls[0][0].onConfirm;
    firstConfirm();
    await rejectRequest(firstRequest);
    firstConfirm();

    expect(axios.delete).toHaveBeenCalledTimes(1);
    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    expect(dialog.confirm).toHaveBeenCalledTimes(2);
    dialog.confirm.mock.calls[1][0].onConfirm();

    expect(axios.delete).toHaveBeenCalledTimes(2);
    await rejectRequest(secondRequest);
  });

  it('does not update state or notify after destruction when deletion succeeds', async () => {
    const request = deferred();
    axios.delete.mockImplementation(() => request.promise);
    const { dialog, toast, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    dialog.confirm.mock.calls[0][0].onConfirm();
    expect(wrapper.vm.deleteInFlight).toBe(true);
    wrapper.destroy();
    await resolveRequest(request);

    expect(wrapper.vm.deleteInFlight).toBe(true);
    expect(toast.open).not.toHaveBeenCalled();
    expect(wrapper.emitted('pin-delete-succeed')).toBeUndefined();
  });

  it('does not update state or notify after destruction when deletion fails', async () => {
    const request = deferred();
    axios.delete.mockImplementation(() => request.promise);
    const { dialog, toast, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    dialog.confirm.mock.calls[0][0].onConfirm();
    expect(wrapper.vm.deleteInFlight).toBe(true);
    wrapper.destroy();
    await rejectRequest(request);

    expect(wrapper.vm.deleteInFlight).toBe(true);
    expect(toast.open).not.toHaveBeenCalled();
    expect(wrapper.emitted('pin-delete-succeed')).toBeUndefined();
  });
});
