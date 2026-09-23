/* eslint-env jest */
import fs from 'fs';
import path from 'path';
import flushPromises from 'flush-promises';
import { shallowMount } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';

import PHeader from '@/components/PHeader.vue';
import api from '@/components/api';
import ko from '@/components/utils/i18n/locales/ko.json';


function mountHeader(user) {
  return shallowMount(PHeader, {
    global: {
      stubs: ['router-link'],
      plugins: [createI18n({ legacy: false, locale: 'ko', messages: { ko } })],
    },
    data() {
      return { user };
    },
  });
}

describe('헤더 사용자 아바타', () => {
  let fetchUserInfo;

  beforeEach(() => {
    fetchUserInfo = jest.spyOn(api.User, 'fetchUserInfo')
      .mockImplementation(() => new Promise(() => {}));
    jest.spyOn(api.SSO, 'policy').mockResolvedValue({
      password_login_enabled: true,
      allow_new_registrations: false,
    });
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('실제 Gravatar가 있으면 원형 사용자 아바타를 표시한다', () => {
    const wrapper = mountHeader({
      loggedIn: true,
      meta: { username: 'sruin', gravatar: 'email-hash' },
    });

    const avatar = wrapper.find('[data-test="header-avatar"]');
    expect(avatar.exists()).toBe(true);
    expect(avatar.attributes('src')).toBe(
      'https://www.gravatar.com/avatar/email-hash?d=404&s=64',
    );
    expect(avatar.classes()).toContain('user-avatar');
    expect(wrapper.find('[data-test="header-account-icon"]').exists()).toBe(false);
  });

  it('Gravatar가 없으면 기본 계정 아이콘으로 돌아간다', async () => {
    const wrapper = mountHeader({
      loggedIn: true,
      meta: { username: 'sruin', gravatar: 'email-hash' },
    });

    await wrapper.find('[data-test="header-avatar"]').trigger('error');

    expect(wrapper.find('[data-test="header-avatar"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="header-account-icon"]').exists()).toBe(true);
  });

  it('Gravatar 해시가 없으면 아바타 요청을 만들지 않는다', () => {
    const wrapper = mountHeader({
      loggedIn: true,
      meta: { username: 'sruin', gravatar: '' },
    });

    expect(wrapper.vm.headerAvatarUrl).toBe('');
    expect(wrapper.find('[data-test="header-account-icon"]').exists()).toBe(true);
  });

  it('사용자 정보를 다시 불러오면 아바타 실패 상태를 초기화한다', async () => {
    const wrapper = mountHeader({
      loggedIn: true,
      meta: { username: 'sruin', gravatar: 'email-hash' },
    });
    await wrapper.find('[data-test="header-avatar"]').trigger('error');
    expect(wrapper.vm.headerAvatarUrl).toBe('');

    fetchUserInfo.mockResolvedValue({ username: 'sruin', gravatar: 'new-email-hash' });
    wrapper.vm.initializeUser(true);
    await flushPromises();

    expect(wrapper.vm.userAvatarFailed).toBe(false);
    expect(wrapper.vm.headerAvatarUrl).toBe(
      'https://www.gravatar.com/avatar/new-email-hash?d=404&s=64',
    );
  });

  it('헤더 아바타는 항상 원형 크기로 렌더링된다', () => {
    const filename = path.resolve(__dirname, '../../src/components/PHeader.vue');
    const source = fs.readFileSync(filename, 'utf8');
    const avatarStyle = source.match(
      /\.navbar-end \.user-menu \.user-avatar \{[^}]+\}/,
    );

    expect(avatarStyle).not.toBeNull();
    expect(avatarStyle[0]).toContain('width: 34px');
    expect(avatarStyle[0]).toContain('height: 34px');
    expect(avatarStyle[0]).toContain('max-height: 34px');
    expect(avatarStyle[0]).toContain('border-radius: 50%');
    expect(avatarStyle[0]).toContain('object-fit: cover');
  });
});
