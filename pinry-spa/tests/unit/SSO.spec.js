/* eslint-env jest */
import axios from 'axios';
import Buefy from 'buefy';
import flushPromises from 'flush-promises';
import VueI18n from 'vue-i18n';
import { createLocalVue, mount, shallowMount } from '@vue/test-utils';
import LoginForm from '@/components/LoginForm.vue';
import Profile from '@/components/user/profile.vue';
import API from '@/components/api';
import modals from '@/components/modals';
import ko from '@/components/utils/i18n/locales/ko.json';

jest.mock('axios');
const options = {
  mocks: {
    $t: (key, params) => (params && params.provider ? `${key} ${params.provider}` : key),
  },
  stubs: ['b-field', 'b-input'],
};

describe('SSO policy screens', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    axios.get.mockResolvedValue({ data: { providers: [], password_login_enabled: false, api_tokens_enabled: false } });
  });

  it('keeps Escape and outside cancellation without rendering Buefy close X', () => {
    const open = jest.fn();
    const vm = { $buefy: { modal: { open } } };

    modals.openLogin(vm, jest.fn());

    expect(open.mock.calls[0][0].canCancel).toEqual(['escape', 'outside']);
  });

  it('renders the square brand icon beside the SVRx Pinry name', () => {
    const wrapper = shallowMount(LoginForm, options);
    const brand = wrapper.find('[data-test="login-brand"]');

    expect(brand.text()).toBe('SVRx Pinry');
    expect(brand.find('img').attributes('alt')).toBe('');
  });

  it('associates visible password login labels with the real inputs', async () => {
    axios.get.mockResolvedValue({
      data: { providers: [], password_login_enabled: true, api_tokens_enabled: false },
    });
    const localVue = createLocalVue();
    localVue.use(Buefy);
    localVue.use(VueI18n);
    const wrapper = mount(LoginForm, {
      localVue,
      i18n: new VueI18n({ locale: 'ko', messages: { ko } }),
    });
    await flushPromises();

    expect(wrapper.find('label[for="login-username"]').exists()).toBe(true);
    expect(wrapper.find('#login-username').exists()).toBe(true);
    expect(wrapper.find('label[for="login-password"]').exists()).toBe(true);
    expect(wrapper.find('#login-password').exists()).toBe(true);
    wrapper.destroy();
  });

  it('keeps manual account linking in collapsed advanced controls', async () => {
    const wrapper = shallowMount(Profile, options);
    await flushPromises();
    expect(wrapper.text()).toContain('ssoAutoLinkHelp');
    expect(wrapper.find('details').exists()).toBe(true);
    expect(wrapper.find('details').attributes('open')).toBeUndefined();
  });

  it('shows enabled provider login buttons and hides the password form', async () => {
    axios.get.mockResolvedValue({
      data: {
        providers: [{ id: 'one', name: '회사 계정', login_url: '/api/v2/sso/one/login/' },
          {
            id: 'two', name: '숨김', enabled: false, login_url: '/api/v2/sso/two/login/',
          }],
        password_login_enabled: false,
        api_tokens_enabled: false,
      },
    });
    const wrapper = shallowMount(LoginForm, options);
    await flushPromises();
    expect(wrapper.find('a[href="/api/v2/sso/one/login/"]').text()).toContain('회사 계정');
    expect(wrapper.text()).not.toContain('숨김');
    expect(wrapper.find('[data-test="password-form"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="password-disclosure"]').exists()).toBe(false);
    expect(wrapper.text()).not.toContain('ssoPasswordDisabled');
  });

  it('uses provider kinds for icons and falls back to the generic OIDC icon', async () => {
    axios.get.mockResolvedValue({
      data: {
        providers: [
          {
            id: 'a', kind: 'authentik', name: 'Custom A', login_url: '/a',
          },
          {
            id: 'g', kind: 'google', name: 'Custom G', login_url: '/g',
          },
          {
            id: 'm', kind: 'microsoft', name: 'Custom M', login_url: '/m',
          },
          {
            id: 'gh', kind: 'github', name: 'Custom GH', login_url: '/gh',
          },
          {
            id: 's', kind: 'synology', name: 'Custom S', login_url: '/s',
          },
          {
            id: 'o', kind: 'oidc', name: 'Custom O', login_url: '/o',
          },
          {
            id: 'u', kind: 'something-custom', name: 'Custom U', login_url: '/u',
          },
        ],
        password_login_enabled: false,
        api_tokens_enabled: false,
      },
    });

    const wrapper = shallowMount(LoginForm, options);
    await flushPromises();

    expect(wrapper.findAll('[data-test="provider-icon"]').wrappers.map(icon => icon.attributes('src')))
      .toEqual([
        '/static/auth/providers/authentik.svg',
        '/static/auth/providers/google.svg',
        '/static/auth/providers/microsoft.svg',
        '/static/auth/providers/github.svg',
        '/static/auth/providers/synology.svg',
        '/static/auth/providers/oidc.svg',
        '/static/auth/providers/oidc.svg',
      ]);
  });

  it('collapses password login beside providers and expands it on request', async () => {
    axios.get.mockResolvedValue({
      data: {
        providers: [{
          id: 'one', kind: 'oidc', name: 'SSO', login_url: '/sso',
        }],
        password_login_enabled: true,
        api_tokens_enabled: false,
      },
    });
    const wrapper = shallowMount(LoginForm, options);
    await flushPromises();

    expect(wrapper.find('[data-test="password-disclosure"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="password-form"]').exists()).toBe(false);

    await wrapper.find('[data-test="password-disclosure"]').trigger('click');

    expect(wrapper.find('[data-test="password-form"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="password-disclosure"]').attributes('aria-expanded')).toBe('true');
  });

  it('does not submit password login when Enter is pressed while it is collapsed', async () => {
    axios.post.mockReturnValue(new Promise(() => {}));
    axios.get.mockResolvedValue({
      data: {
        providers: [{
          id: 'one', kind: 'oidc', name: 'SSO', login_url: '/sso',
        }],
        password_login_enabled: true,
        api_tokens_enabled: false,
      },
    });
    const wrapper = shallowMount(LoginForm, options);
    await flushPromises();

    await wrapper.find('.login-modal > div').trigger('keydown', { key: 'Enter', keyCode: 13 });

    expect(axios.post).not.toHaveBeenCalled();
  });

  it('never falls back to password login when settings fail', async () => {
    axios.get.mockRejectedValue(new Error('offline'));
    const wrapper = shallowMount(LoginForm, options);
    await flushPromises();
    expect(wrapper.text()).toContain('ssoSettingsFailed');
    expect(wrapper.find('[data-test="password-form"]').exists()).toBe(false);
  });

  it('shows password inputs only after affirmative policy', async () => {
    axios.get.mockResolvedValue({ data: { providers: [], password_login_enabled: true, api_tokens_enabled: true } });
    const wrapper = shallowMount(LoginForm, options);
    expect(wrapper.find('[data-test="password-form"]').exists()).toBe(false);
    await flushPromises();
    expect(wrapper.find('[data-test="password-form"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="password-disclosure"]').exists()).toBe(false);
  });

  it('submits the direct password form through the existing login API', async () => {
    axios.get.mockResolvedValue({ data: { providers: [], password_login_enabled: true, api_tokens_enabled: true } });
    axios.post.mockRejectedValue({ response: { data: { username: ['invalid'] } } });
    const wrapper = shallowMount(LoginForm, options);
    await flushPromises();
    await wrapper.setData({
      form: {
        ...wrapper.vm.form,
        username: { ...wrapper.vm.form.username, value: 'alice' },
        password: { ...wrapper.vm.form.password, value: 'secret' },
      },
    });

    await wrapper.find('[data-test="password-form"]').trigger('submit');
    await flushPromises();

    expect(axios.post).toHaveBeenCalledWith('/api/v2/profile/login/', {
      username: 'alice',
      password: 'secret',
    });
    expect(wrapper.vm.form.username.type).toBe('is-danger');
  });

  it('hides cached tokens and reports failed identity loading', async () => {
    axios.get.mockImplementation(url => (url.endsWith('/identities/')
      ? Promise.reject(new Error('offline'))
      : Promise.resolve({ data: { providers: [], password_login_enabled: false, api_tokens_enabled: false } })));
    const wrapper = shallowMount(Profile, { ...options, propsData: { token: 'stale-secret' } });
    await flushPromises();
    expect(wrapper.text()).not.toContain('stale-secret');
    expect(wrapper.text()).toContain('ssoIdentitiesFailed');
    expect(wrapper.text()).toContain('ssoTokensDisabled');
  });

  it('renders an accessible Pinry password reauthentication row', async () => {
    axios.get.mockImplementation(url => Promise.resolve({
      data: url.endsWith('/identities/')
        ? []
        : { providers: [], password_login_enabled: true, api_tokens_enabled: false },
    }));
    const wrapper = shallowMount(Profile, options);
    await flushPromises();

    const form = wrapper.find('[data-test="password-reauth-form"]');
    expect(form.find('label[for="sso-password"]').exists()).toBe(true);
    expect(form.find('#sso-password').attributes('autocomplete')).toBe('current-password');
    expect(form.find('button[type="submit"]').text()).toBe('ssoReauth');
  });

  it('shows provider presentation and keeps destructive unlink visually distinct', async () => {
    axios.get.mockImplementation(url => Promise.resolve({
      data: url.endsWith('/identities/')
        ? [
          {
            id: 4, provider_id: 'google-work', provider_name: 'Custom Workspace', enabled: true,
          },
          {
            id: 5, provider_id: 'retired', provider_name: 'Retired SSO', enabled: false,
          },
        ]
        : {
          providers: [{
            id: 'google-work', kind: 'google', name: 'Custom Workspace', login_url: '/login',
          }],
          password_login_enabled: true,
          api_tokens_enabled: false,
        },
    }));
    const wrapper = shallowMount(Profile, options);
    await flushPromises();

    const rows = wrapper.findAll('[data-test="identity-row"]');
    expect(rows.at(0).find('[data-test="provider-icon"]').attributes('src'))
      .toBe('/static/auth/providers/google.svg');
    expect(rows.at(0).text()).toContain('Custom Workspace');
    expect(rows.at(0).find('form[action="/api/v2/sso/google-work/reauth/"]').exists()).toBe(true);
    expect(rows.at(0).find('[data-test="unlink-button"]').classes()).toEqual(
      expect.arrayContaining(['is-danger', 'is-outlined']),
    );
    expect(rows.at(1).find('[data-test="provider-icon"]').attributes('src'))
      .toBe('/static/auth/providers/oidc.svg');
    expect(rows.at(1).text()).toContain('ssoUnavailable');
  });

  it('explains SSO reauthentication when Pinry password login is unavailable', async () => {
    axios.get.mockImplementation(url => Promise.resolve({
      data: url.endsWith('/identities/')
        ? [{
          id: 4, provider_id: 'one', provider_name: 'SSO', enabled: true,
        }]
        : {
          providers: [{
            id: 'one', kind: 'oidc', name: 'SSO', login_url: '/login',
          }],
          password_login_enabled: false,
          api_tokens_enabled: false,
        },
    }));
    const wrapper = shallowMount(Profile, options);
    await flushPromises();

    expect(wrapper.find('[data-test="password-reauth-form"]').exists()).toBe(false);
    expect(wrapper.text()).toContain('ssoManualSsoOnlyHelp');
  });

  it('renders a native CSRF form for provider reauthentication', async () => {
    document.cookie = 'csrftoken=csrf-value';
    axios.get.mockImplementation(url => Promise.resolve({
      data: url.endsWith('/identities/')
        ? [{
          id: 4, provider_id: 'one', provider_name: '회사 계정', enabled: true,
        }]
        : { providers: [], password_login_enabled: false, api_tokens_enabled: false },
    }));
    const wrapper = shallowMount(Profile, options);
    await flushPromises();
    const form = wrapper.find('form[action="/api/v2/sso/one/reauth/"]');
    expect(form.attributes('method')).toBe('post');
    expect(form.find('[name="csrfmiddlewaretoken"]').element.value).toBe('csrf-value');
  });

  it('refreshes the user from the server instead of trusting a previous account cache', async () => {
    localStorage.setItem('pinry.user', JSON.stringify({ value: { username: 'old', token: 'old-token' }, expires_at: Date.now() + 60000 }));
    axios.get.mockResolvedValue({ data: [{ username: 'current', token: null }] });
    const user = await API.User.fetchUserInfo();
    expect(user).toEqual({ username: 'current', token: null });
  });

  it('logs out with POST and clears the cached user only after success', async () => {
    localStorage.setItem('pinry.user', JSON.stringify({ value: { username: 'current' }, expires_at: Date.now() + 60000 }));
    axios.post.mockResolvedValue({ status: 200 });

    await API.User.logOut();

    expect(axios.post).toHaveBeenCalledWith('/api-auth/logout/');
    expect(JSON.parse(localStorage.getItem('pinry.user')).value).toBeNull();
  });

  it('rejects a failed logout without clearing the cached user', async () => {
    const cached = { value: { username: 'current' }, expires_at: Date.now() + 60000 };
    localStorage.setItem('pinry.user', JSON.stringify(cached));
    const failure = new Error('logout failed');
    axios.post.mockRejectedValue(failure);

    await expect(API.User.logOut()).rejects.toBe(failure);

    expect(JSON.parse(localStorage.getItem('pinry.user'))).toEqual(cached);
  });
});
