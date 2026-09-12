/* eslint-env jest */
/* eslint-disable no-underscore-dangle */
import flushPromises from 'flush-promises';
import { shallowMount } from '@vue/test-utils';
import Profile from '@/components/user/profile.vue';
import API from '@/components/api';
import ko from '@/components/utils/i18n/locales/ko.json';

describe('프로필 정보 표시', () => {
  let wrapper;
  beforeEach(() => {
    global.__PINRY_BUILD_DEPENDENCIES__ = {
      frontend: [{ key: 'vue', label: 'Vue', version: '3.5.42' }],
      buildTools: [{ key: 'node', label: 'Node.js', version: '24.21.0' }],
    };
    jest.spyOn(API.Version, 'fetch').mockResolvedValue({
      data: {
        display_version: 'trial', dependencies: { python: '3.14.7', django: '5.2.17' },
      },
    });
    jest.spyOn(API.SSO, 'policy').mockResolvedValue({
      api_tokens_enabled: true,
      password_login_enabled: true,
      providers: [{ id: 'a', name: 'Authentik', kind: 'authentik' },
        { id: 'g', name: 'Google', kind: 'google' }],
    });
    jest.spyOn(API.SSO, 'identities').mockResolvedValue([
      {
        id: 1, provider_id: 'a', provider_name: 'Authentik', enabled: true,
      },
    ]);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true, value: { writeText: jest.fn().mockResolvedValue() },
    });
  });
  afterEach(() => {
    if (wrapper) wrapper.unmount();
    delete global.__PINRY_BUILD_DEPENDENCIES__;
    delete navigator.clipboard;
    delete document.execCommand;
    jest.restoreAllMocks();
  });
  async function mountProfile() {
    wrapper = shallowMount(Profile, {
      props: { token: 'synthetic-token-for-ui' },
      global: { mocks: { $t: key => ko[key] || key } },
    });
    await flushPromises();
  }

  it('실행 버전과 빌드에서 기록한 버전을 세 그룹에 표시한다', async () => {
    await mountProfile();
    expect(wrapper.find('[data-test="dependencies-backend"]').text()).toContain('3.14.7');
    expect(wrapper.find('[data-test="dependencies-frontend"]').text()).toContain('Vue 3.5.42');
    expect(wrapper.find('[data-test="dependencies-buildTools"]').text()).toContain('Node.js 24.21.0');
  });
  it('토큰은 처음에는 DOM에 노출하지 않고 표시 요청 때만 보여준다', async () => {
    await mountProfile();
    expect(wrapper.html()).not.toContain('synthetic-token-for-ui');
    await wrapper.find('[data-test="token-toggle"]').trigger('click');
    expect(wrapper.find('[data-test="token-value"]').text()).toBe('synthetic-token-for-ui');
    await wrapper.find('[data-test="token-toggle"]').trigger('click');
    expect(wrapper.html()).not.toContain('synthetic-token-for-ui');
  });
  it('숨긴 토큰을 명시적 복사 요청으로 복사하고 성공을 알린다', async () => {
    await mountProfile();
    await wrapper.find('[data-test="token-copy"]').trigger('click');
    await flushPromises();
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('synthetic-token-for-ui');
    expect(wrapper.find('[data-test="token-status"]').text()).toBe(ko.profileTokenCopied);
    expect(wrapper.html()).not.toContain('synthetic-token-for-ui');
  });
  it('클립보드가 없거나 거부되면 성공으로 표시하지 않는다', async () => {
    await mountProfile();
    delete navigator.clipboard;
    await wrapper.find('[data-test="token-copy"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="token-status"]').text()).toBe(ko.profileTokenCopyFailed);
  });
  it('HTTP 접속에서는 임시 선택 영역으로 복사한 뒤 토큰을 DOM에서 제거한다', async () => {
    await mountProfile();
    delete navigator.clipboard;
    document.execCommand = jest.fn(() => {
      expect(document.activeElement.value).toBe('synthetic-token-for-ui');
      return true;
    });
    await wrapper.find('[data-test="token-copy"]').trigger('click');
    await flushPromises();
    expect(document.execCommand).toHaveBeenCalledWith('copy');
    expect(document.querySelector('textarea')).toBeNull();
    expect(wrapper.find('[data-test="token-status"]').text()).toBe(ko.profileTokenCopied);
  });
  it('토큰 정책이 꺼지면 표시·복사 경로를 모두 숨긴다', async () => {
    API.SSO.policy.mockResolvedValue({ api_tokens_enabled: false, providers: [] });
    await mountProfile();
    expect(wrapper.find('[data-test="token-toggle"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="token-copy"]').exists()).toBe(false);
    expect(wrapper.html()).not.toContain('synthetic-token-for-ui');
  });
  it('이미 연결된 제공자는 새 연결 목록에 중복 표시하지 않는다', async () => {
    await mountProfile();
    const links = wrapper.findAll('[data-test="provider-link-row"]');
    expect(links).toHaveLength(1);
    expect(links[0].text()).toContain('Google');
    expect(wrapper.find('[data-test="identity-row"]').text()).toContain('Authentik');
  });
});
