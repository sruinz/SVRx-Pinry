/* eslint-env jest */
import axios from 'axios';
import flushPromises from 'flush-promises';
import { shallowMount } from '@vue/test-utils';
import LoginForm from '@/components/LoginForm.vue';
import Profile from '@/components/user/profile.vue';
import API from '@/components/api';

jest.mock('axios');
const options = { mocks: { $t: key => key }, stubs: ['b-field', 'b-input'] };

describe('SSO policy screens', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    axios.get.mockResolvedValue({ data: { providers: [], password_login_enabled: false, api_tokens_enabled: false } });
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
