/* eslint-env jest */
import axios from 'axios';
import flushPromises from 'flush-promises';
import VueI18n from 'vue-i18n';
import { createLocalVue, mount, shallowMount } from '@vue/test-utils';
import { ConfigProgrammatic } from 'buefy';

import API from '@/components/api';
import FileUpload from '@/components/pin_edit/FileUpload.vue';
import modals from '@/components/modals';
import PinCreateModal from '@/components/pin_edit/PinCreateModal.vue';
import bus from '@/components/utils/bus';
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


function mountFileUpload() {
  const localVue = createLocalVue();
  localVue.use(VueI18n);
  return shallowMount(FileUpload, {
    localVue,
    i18n: new VueI18n({ locale: 'en', messages: { en } }),
    stubs: ['b-field', 'b-upload', 'b-icon'],
  });
}


function mountCreateModal() {
  const localVue = createLocalVue();
  localVue.use(VueI18n);
  const loading = { close: jest.fn() };
  const wrapper = shallowMount(PinCreateModal, {
    localVue,
    i18n: new VueI18n({ locale: 'en', messages: { en } }),
    propsData: { username: 'owner' },
    mocks: {
      $buefy: { loading: { open: jest.fn(() => loading) } },
    },
    stubs: ['b-field', 'b-input', 'b-checkbox', 'b-taginput'],
  });
  const close = jest.fn();
  wrapper.vm.$parent.close = close;
  return {
    close,
    loading,
    wrapper,
  };
}


function mountCreateModalWithFileUpload() {
  const localVue = createLocalVue();
  localVue.use(VueI18n);
  const wrapper = mount(PinCreateModal, {
    localVue,
    i18n: new VueI18n({ locale: 'en', messages: { en } }),
    propsData: { username: 'owner' },
    mocks: {
      $buefy: {
        loading: { open: jest.fn(() => ({ close: jest.fn() })) },
      },
    },
    stubs: {
      FilterSelect: true,
      'b-field': true,
      'b-input': true,
      'b-checkbox': true,
      'b-taginput': true,
      'b-upload': true,
      'b-icon': true,
    },
  });
  const close = jest.fn(() => wrapper.destroy());
  wrapper.vm.$parent.close = close;
  return { close, wrapper };
}


describe('local pin file selection', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    global.URL.createObjectURL = jest.fn(() => 'blob:local-preview');
    global.URL.revokeObjectURL = jest.fn();
    axios.post.mockReturnValue(new Promise(() => {}));
  });

  it('previews the selected file locally without an HTTP request', async () => {
    const file = new File(['image-bytes'], 'photo.png', { type: 'image/png' });
    const wrapper = mountFileUpload();

    await wrapper.setData({ dropFile: file });

    expect(global.URL.createObjectURL).toHaveBeenCalledWith(file);
    expect(wrapper.find('img').attributes('src')).toBe('blob:local-preview');
    expect(wrapper.emitted('imageSelected')).toEqual([[file]]);
    expect(axios.post).not.toHaveBeenCalled();
    expect(axios.get).not.toHaveBeenCalled();
  });

  it('revokes the previous preview when the selected file is replaced', async () => {
    global.URL.createObjectURL
      .mockReturnValueOnce('blob:first-preview')
      .mockReturnValueOnce('blob:second-preview');
    const first = new File(['first'], 'first.png', { type: 'image/png' });
    const second = new File(['second'], 'second.png', { type: 'image/png' });
    const wrapper = mountFileUpload();

    await wrapper.setData({ dropFile: first });
    await wrapper.setData({ dropFile: second });

    expect(global.URL.revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(global.URL.revokeObjectURL).toHaveBeenCalledWith('blob:first-preview');
    expect(wrapper.find('img').attributes('src')).toBe('blob:second-preview');
    expect(wrapper.emitted('imageSelected')).toEqual([[first], [second]]);
    expect(axios.post).not.toHaveBeenCalled();
  });

  it('revokes its current preview when the component is destroyed', async () => {
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    const wrapper = mountFileUpload();
    await wrapper.setData({ dropFile: file });

    wrapper.destroy();

    expect(global.URL.revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(global.URL.revokeObjectURL).toHaveBeenCalledWith('blob:local-preview');
  });

  it('clears the preview and emits null when the file selection is cleared', async () => {
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    const wrapper = mountFileUpload();
    await wrapper.setData({ dropFile: file });

    await wrapper.setData({ dropFile: null });

    expect(global.URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(global.URL.revokeObjectURL).toHaveBeenCalledWith('blob:local-preview');
    expect(wrapper.find('img').isVisible()).toBe(false);
    expect(wrapper.emitted('imageSelected')).toEqual([[file], [null]]);
  });
});


describe('local pin upload API', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    axios.post.mockResolvedValue({ data: { id: 41 }, status: 201 });
  });

  it('posts one FormData payload directly to the pin collection', async () => {
    const formData = new FormData();
    formData.append('image_file', new File(['image'], 'photo.png'));

    await API.Pin.createFromUpload(formData);

    expect(axios.post).toHaveBeenCalledTimes(1);
    expect(axios.post).toHaveBeenCalledWith('/api/v2/pins/', formData);
  });

  it('does not expose the removed two-step image upload helpers', () => {
    expect(API.Pin.uploadImage).toBeUndefined();
    expect(API.Pin.createFromUploaded).toBeUndefined();
  });
});


describe('local pin creation', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.spyOn(console, 'log').mockImplementation(() => {});
    axios.get.mockResolvedValue({ data: [] });
    axios.patch.mockResolvedValue({ status: 200 });
    axios.all.mockImplementation(requests => Promise.all(requests));
  });

  afterEach(() => {
    console.log.mockRestore();
  });

  it('submits one multipart pin request with exact scalar and repeated fields', async () => {
    const response = { data: { id: 41 }, status: 201 };
    axios.post.mockResolvedValue(response);
    const refresh = jest.spyOn(bus.bus, '$emit');
    const { close, loading, wrapper } = mountCreateModal();
    await flushPromises();
    jest.clearAllMocks();
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    wrapper.findComponent(FileUpload).vm.$emit('imageSelected', file);
    wrapper.vm.pinModel.form.referer.value = null;
    wrapper.vm.pinModel.form.description.value = null;
    wrapper.vm.pinModel.form.private.value = true;
    wrapper.vm.pinModel.form.tags.value = ['alpha', 'beta'];
    wrapper.vm.onSelectBoard([9, 2]);

    wrapper.vm.createPin();
    await flushPromises();

    expect(axios.post).toHaveBeenCalledTimes(1);
    const [url, formData] = axios.post.mock.calls[0];
    expect(url).toBe('/api/v2/pins/');
    expect(formData).toBeInstanceOf(FormData);
    expect(formData.getAll('image_file')).toEqual([file]);
    expect(formData.getAll('referer')).toEqual(['']);
    expect(formData.getAll('description')).toEqual(['']);
    expect(formData.getAll('private')).toEqual(['true']);
    expect(formData.getAll('tags')).toEqual(['alpha', 'beta']);
    expect(formData.getAll('board_ids')).toEqual(['9', '2']);
    expect(formData.getAll('url')).toEqual([]);
    expect(formData.getAll('image_by_id')).toEqual([]);
    expect(axios.patch).not.toHaveBeenCalled();
    expect(refresh).toHaveBeenCalledWith(bus.events.refreshPin);
    expect(wrapper.emitted('pinCreated')).toEqual([[response]]);
    expect(close).toHaveBeenCalledTimes(1);
    expect(loading.close).toHaveBeenCalledTimes(1);
    refresh.mockRestore();
  });

  it('normalizes missing text fields and serializes default privacy as false', async () => {
    axios.post.mockResolvedValue({ data: { id: 42 }, status: 201 });
    const { wrapper } = mountCreateModal();
    await flushPromises();
    jest.clearAllMocks();
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    wrapper.findComponent(FileUpload).vm.$emit('imageSelected', file);
    wrapper.vm.pinModel.form.referer.value = undefined;
    wrapper.vm.pinModel.form.description.value = undefined;

    wrapper.vm.createPin();
    await flushPromises();

    const formData = axios.post.mock.calls[0][1];
    expect(formData.getAll('referer')).toEqual(['']);
    expect(formData.getAll('description')).toEqual(['']);
    expect(formData.getAll('private')).toEqual(['false']);
    expect(formData.getAll('tags')).toEqual([]);
    expect(formData.getAll('board_ids')).toEqual([]);
  });

  it('revokes the local preview when Close destroys the pin modal', async () => {
    global.URL.createObjectURL = jest.fn(() => 'blob:modal-preview');
    global.URL.revokeObjectURL = jest.fn();
    const { close, wrapper } = mountCreateModalWithFileUpload();
    await flushPromises();
    const fileUpload = wrapper.findComponent(FileUpload);
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    await fileUpload.setData({ dropFile: file });

    await wrapper.find('.modal-card-foot > button').trigger('click');

    expect(close).toHaveBeenCalledTimes(1);
    expect(global.URL.revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(global.URL.revokeObjectURL).toHaveBeenCalledWith('blob:modal-preview');
    expect(axios.post).not.toHaveBeenCalled();
  });

  it('preserves URL JSON creation and its existing board updates', async () => {
    const response = { data: { id: 73 }, status: 201 };
    axios.post.mockResolvedValue(response);
    const { close, loading, wrapper } = mountCreateModal();
    await flushPromises();
    jest.clearAllMocks();
    wrapper.vm.pinModel.form.url.value = 'https://images.example/photo.png';
    wrapper.vm.pinModel.form.referer.value = 'https://page.example/';
    wrapper.vm.pinModel.form.description.value = 'URL pin';
    wrapper.vm.pinModel.form.private.value = false;
    wrapper.vm.pinModel.form.tags.value = ['remote'];
    wrapper.vm.onSelectBoard([9, 2]);

    wrapper.vm.createPin();
    await flushPromises();

    expect(axios.post).toHaveBeenCalledTimes(1);
    expect(axios.post).toHaveBeenCalledWith('/api/v2/pins/', {
      url: 'https://images.example/photo.png',
      referer: 'https://page.example/',
      description: 'URL pin',
      private: false,
      tags: ['remote'],
    });
    expect(axios.patch.mock.calls).toEqual([
      ['/api/v2/boards/9/', { pins_to_add: [73] }],
      ['/api/v2/boards/2/', { pins_to_add: [73] }],
    ]);
    expect(wrapper.emitted('pinCreated')).toEqual([[response]]);
    expect(close).toHaveBeenCalledTimes(1);
    expect(loading.close).toHaveBeenCalledTimes(1);
  });

  it('ignores repeated Create attempts while the upload is pending', async () => {
    const request = deferred();
    axios.post.mockImplementation(() => request.promise);
    const { wrapper } = mountCreateModal();
    await flushPromises();
    jest.clearAllMocks();
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    wrapper.findComponent(FileUpload).vm.$emit('imageSelected', file);

    wrapper.vm.createPin();
    wrapper.vm.createPin();

    expect(axios.post).toHaveBeenCalledTimes(1);
  });

  it('disables the Create and Close buttons while the upload is pending', async () => {
    const request = deferred();
    axios.post.mockImplementation(() => request.promise);
    const { close, wrapper } = mountCreateModal();
    await flushPromises();
    jest.clearAllMocks();
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    wrapper.findComponent(FileUpload).vm.$emit('imageSelected', file);
    wrapper.vm.createPin();
    await wrapper.vm.$nextTick();
    const buttons = wrapper.findAll('.modal-card-foot > button');

    expect(buttons.at(0).attributes('disabled')).toBe('disabled');
    expect(buttons.at(1).attributes('disabled')).toBe('disabled');
    await buttons.at(0).trigger('click');

    expect(close).not.toHaveBeenCalled();
    expect(axios.post).toHaveBeenCalledTimes(1);
  });

  it('opens the pin modal with implicit cancellation disabled', () => {
    const config = ConfigProgrammatic.getOptions();
    const defaultCancelOptions = config.defaultModalCanCancel.slice();
    const open = jest.fn();
    const vm = { $buefy: { modal: { open } } };

    modals.openPinEdit(vm);

    expect(open).toHaveBeenCalledTimes(1);
    expect(open.mock.calls[0][0].canCancel).toBe(false);
    expect(config.defaultModalCanCancel).toEqual(defaultCancelOptions);
  });

  it('keeps the modal open and exposes the server error after upload failure', async () => {
    const request = deferred();
    axios.post.mockImplementation(() => request.promise);
    const { close, loading, wrapper } = mountCreateModal();
    await flushPromises();
    jest.clearAllMocks();
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    wrapper.findComponent(FileUpload).vm.$emit('imageSelected', file);
    wrapper.vm.createPin();

    request.reject({
      response: { data: { image_file: ['invalid_image_content'] } },
    });
    await request.promise.catch(() => {});
    await flushPromises();
    await wrapper.vm.$nextTick();

    expect(close).not.toHaveBeenCalled();
    expect(wrapper.vm.createInFlight).toBe(false);
    expect(loading.close).toHaveBeenCalledTimes(1);
    expect(wrapper.find('[data-test="create-error"]').text())
      .toBe('invalid_image_content');
    const buttons = wrapper.findAll('.modal-card-foot > button');
    expect(buttons.at(0).attributes('disabled')).toBeUndefined();
    expect(buttons.at(1).attributes('disabled')).toBeUndefined();
  });

  it('uses the localized generic message when no safe server detail exists', async () => {
    const request = deferred();
    axios.post.mockImplementation(() => request.promise);
    const { wrapper } = mountCreateModal();
    await flushPromises();
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    wrapper.findComponent(FileUpload).vm.$emit('imageSelected', file);
    wrapper.vm.createPin();

    request.reject(new Error('/private/network/detail'));
    await request.promise.catch(() => {});
    await flushPromises();

    expect(wrapper.find('[data-test="create-error"]').text())
      .toBe('Failed to create Pin');
  });

  it('has no success UI side effects after destruction', async () => {
    const request = deferred();
    axios.post.mockImplementation(() => request.promise);
    const refresh = jest.spyOn(bus.bus, '$emit');
    const { close, loading, wrapper } = mountCreateModal();
    await flushPromises();
    jest.clearAllMocks();
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    wrapper.findComponent(FileUpload).vm.$emit('imageSelected', file);
    wrapper.vm.createPin();

    wrapper.destroy();
    expect(loading.close).toHaveBeenCalledTimes(1);
    request.resolve({ data: { id: 41 }, status: 201 });
    await request.promise;
    await flushPromises();

    expect(refresh).not.toHaveBeenCalled();
    expect(wrapper.emitted('pinCreated')).toBeUndefined();
    expect(close).not.toHaveBeenCalled();
    expect(loading.close).toHaveBeenCalledTimes(1);
    refresh.mockRestore();
  });

  it('has no failure UI side effects after destruction', async () => {
    const request = deferred();
    axios.post.mockImplementation(() => request.promise);
    const { close, loading, wrapper } = mountCreateModal();
    await flushPromises();
    jest.clearAllMocks();
    const file = new File(['image'], 'photo.png', { type: 'image/png' });
    wrapper.findComponent(FileUpload).vm.$emit('imageSelected', file);
    wrapper.vm.createPin();

    wrapper.destroy();
    expect(loading.close).toHaveBeenCalledTimes(1);
    request.reject(new Error('network'));
    await request.promise.catch(() => {});
    await flushPromises();

    expect(console.log).not.toHaveBeenCalled();
    expect(wrapper.vm.createError).toBeNull();
    expect(close).not.toHaveBeenCalled();
    expect(loading.close).toHaveBeenCalledTimes(1);
  });
});
