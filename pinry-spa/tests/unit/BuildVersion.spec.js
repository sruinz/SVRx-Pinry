/* eslint-env jest */
import axios from 'axios';
import flushPromises from 'flush-promises';
import { createI18n } from 'vue-i18n';
import { shallowMount } from '@vue/test-utils';

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
  return shallowMount(Profile, {
    global: {
      directives: { masonry: {}, 'masonry-tile': {} },
      plugins: [createI18n({ legacy: false, locale: 'ko', messages: { ko } })],
    },

    props: { token: 'secret-token' },
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

  it('renders local open-source attribution without repository links', () => {
    const wrapper = mountProfile();
    const notice = wrapper.find('[data-test="open-source-license"]');

    expect(notice.exists()).toBe(true);
    expect(notice.text()).toContain('오픈소스 라이선스');
    expect(notice.text()).toContain(
      '이 제품에는 BSD 2-Clause 라이선스로 제공되는 Pinry 구성 요소가 포함되어 있습니다.',
    );
    expect(notice.text()).toContain(
      "Copyright (c) 2019, Pinry's Contributors",
    );
    expect(notice.find('a').exists()).toBe(false);
  });

  it('renders runtime dependencies between build and license cards with one request', async () => {
    axios.get.mockResolvedValue({
      data: {
        display_version: '9b54cf1b5a5a',
        dependencies: {
          python: '3.14.7', django: '5.2.17', drf: '3.16.1', pillow: '12.3.0',
        },
      },
    });
    const wrapper = mountProfile();
    await flushPromises();

    const card = wrapper.find('[data-test="dependency-versions"]');
    expect(card.text()).toContain('의존성 버전');
    ['Python', 'Django', 'Django REST Framework', 'Pillow', '3.14.7', '5.2.17', '3.16.1', '12.3.0']
      .forEach(value => expect(card.text()).toContain(value));
    expect(card.element.previousElementSibling.classList.contains('build-info-card')).toBe(true);
    expect(card.element.nextElementSibling.classList.contains('open-source-card')).toBe(true);
    expect(axios.get.mock.calls.filter(([url]) => url === '/api/v2/version/')).toHaveLength(1);
  });

  it.each([undefined, {
    python: null, django: {}, drf: '', pillow: 123, secret: 'hidden',
  }])(
    'renders placeholders for unavailable or malformed dependency versions', async (dependencies) => {
      axios.get.mockResolvedValue({ data: { display_version: 'development', dependencies } });
      const wrapper = mountProfile();
      await flushPromises();
      const card = wrapper.find('[data-test="dependency-versions"]');
      expect(card.findAll('code')).toHaveLength(0);
      expect(card.text().match(/—/g)).toHaveLength(4);
      expect(card.text()).not.toContain('hidden');
      expect(card.text()).not.toContain('[object Object]');
    },
  );

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
    const versionRequests = [oldRequest.promise, newRequest.promise];
    axios.get.mockImplementation(url => (url === '/api/v2/version/'
      ? versionRequests.shift() : new Promise(() => {})));
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

    wrapper.unmount();
    request.resolve({ data: { display_version: 'stale1234567' } });
    await flushPromises();

    expect(wrapper.vm.displayVersion).toBeNull();
  });

  it('handles a rejected response after the profile is destroyed', async () => {
    const request = deferred();
    axios.get.mockReturnValue(request.promise);
    const wrapper = mountProfile();

    wrapper.unmount();
    request.reject(new Error('/private/build/path'));
    await request.promise.catch(() => {});
    await flushPromises();

    expect(wrapper.vm.displayVersion).toBeNull();
  });

  it('starts a fresh version request for every profile mount', () => {
    const first = mountProfile();
    const second = mountProfile();

    expect(axios.get.mock.calls.filter(([url]) => url === '/api/v2/version/')).toHaveLength(2);
    first.unmount();
    second.unmount();
  });

  it('exposes the read-only version request through the API module', async () => {
    const response = { data: { display_version: 'development' } };
    axios.get.mockResolvedValue(response);

    await expect(API.Version.fetch()).resolves.toBe(response);
    expect(axios.get).toHaveBeenCalledWith('/api/v2/version/');
  });
});
