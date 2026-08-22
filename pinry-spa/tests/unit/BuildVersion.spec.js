/* eslint-env jest */
import axios from 'axios';
import flushPromises from 'flush-promises';
import VueI18n from 'vue-i18n';
import { createLocalVue, shallowMount } from '@vue/test-utils';

import API from '@/components/api';
import Profile from '@/components/user/profile.vue';
import ko from '@/components/utils/i18n/locales/ko.json';


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


function mountProfile() {
  const localVue = createLocalVue();
  localVue.use(VueI18n);
  return shallowMount(Profile, {
    localVue,
    i18n: new VueI18n({ locale: 'ko', messages: { ko } }),
    propsData: { token: 'secret-token' },
  });
}


describe('Profile build version', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    axios.get.mockReturnValue(new Promise(() => {}));
  });

  it('renders the running display version as selectable code text', async () => {
    axios.get.mockResolvedValue({
      data: {
        source_commit: '9b54cf1b5a5a209b9aa8f000b6db53238b626001',
        display_version: '9b54cf1b5a5a',
      },
    });

    const wrapper = mountProfile();
    await flushPromises();

    const version = wrapper.find('[data-test="build-version"]');
    const brand = wrapper.find('[data-test="build-brand"]');
    expect(axios.get).toHaveBeenCalledWith('/api/v2/version/');
    expect(wrapper.text()).toContain('빌드 정보');
    expect(wrapper.text()).toContain('제품');
    expect(wrapper.text()).toContain('실행 버전');
    expect(brand.text()).toBe('SVRx Pinry');
    expect(version.element.tagName).toBe('CODE');
    expect(version.text()).toBe('9b54cf1b5a5a');
  });

  it('does not render a fabricated version when the request fails', async () => {
    axios.get.mockRejectedValue(new Error('/private/build/path'));

    const wrapper = mountProfile();
    await flushPromises();

    expect(wrapper.find('[data-test="build-version"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="build-version-unavailable"]').text())
      .toBe('—');
    expect(wrapper.text()).not.toContain('/private/build/path');
    expect(wrapper.text()).not.toContain('undefined');
  });

  it('ignores a malformed response without exposing other fields', async () => {
    axios.get.mockResolvedValue({
      data: {
        source_commit: '9b54cf1b5a5a209b9aa8f000b6db53238b626001',
        build_path: '/private/build/path',
      },
    });

    const wrapper = mountProfile();
    await flushPromises();

    expect(wrapper.find('[data-test="build-version"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="build-version-unavailable"]').text())
      .toBe('—');
    expect(wrapper.text()).not.toContain('/private/build/path');
    expect(wrapper.text()).not.toContain('undefined');
  });

  it('keeps the newest response when requests settle out of order', async () => {
    const oldRequest = deferred();
    const newRequest = deferred();
    axios.get
      .mockReturnValueOnce(oldRequest.promise)
      .mockReturnValueOnce(newRequest.promise);
    const wrapper = mountProfile();

    wrapper.vm.fetchBuildVersion();
    newRequest.resolve({ data: { display_version: 'newer1234567' } });
    await flushPromises();
    expect(wrapper.find('[data-test="build-version"]').text())
      .toBe('newer1234567');

    oldRequest.resolve({ data: { display_version: 'older1234567' } });
    await flushPromises();
    expect(wrapper.find('[data-test="build-version"]').text())
      .toBe('newer1234567');
  });

  it('ignores a response after the profile is destroyed', async () => {
    const request = deferred();
    axios.get.mockReturnValue(request.promise);
    const wrapper = mountProfile();

    wrapper.destroy();
    request.resolve({ data: { display_version: 'stale1234567' } });
    await flushPromises();

    expect(wrapper.vm.displayVersion).toBeNull();
  });

  it('handles a rejected response after the profile is destroyed', async () => {
    const request = deferred();
    axios.get.mockReturnValue(request.promise);
    const wrapper = mountProfile();

    wrapper.destroy();
    request.reject(new Error('/private/build/path'));
    await request.promise.catch(() => {});
    await flushPromises();

    expect(wrapper.vm.displayVersion).toBeNull();
  });

  it('starts a fresh version request for every profile mount', () => {
    const first = mountProfile();
    const second = mountProfile();

    expect(axios.get).toHaveBeenCalledTimes(2);
    first.destroy();
    second.destroy();
  });

  it('exposes the read-only version request through the API module', async () => {
    const response = { data: { display_version: 'development' } };
    axios.get.mockResolvedValue(response);

    await expect(API.Version.fetch()).resolves.toBe(response);
    expect(axios.get).toHaveBeenCalledWith('/api/v2/version/');
  });
});
