/* eslint-env jest */

import flushPromises from 'flush-promises';
import { shallowMount } from '@vue/test-utils';

import API from '@/components/api';
import Profile from '@/components/user/profile.vue';
import ko from '@/components/utils/i18n/locales/ko.json';
import Profile4User from '@/views/Profile4User.vue';


function deferred() {
  const request = {};
  request.promise = new Promise((resolve, reject) => {
    request.resolve = resolve;
    request.reject = reject;
  });
  request.promise.catch(() => {});
  return request;
}


function publicUser(username) {
  return {
    username,
    gravatar: `https://example.test/${username}.png`,
    resource_link: `/api/v2/profile/public-users/${username}/`,
  };
}


function currentUser(username, canAccessAdmin = false) {
  return {
    username,
    email: `${username}@example.test`,
    gravatar: `https://example.test/${username}.png`,
    token: `${username}-token`,
    can_access_admin: canAccessAdmin,
    resource_link: `/api/v2/profile/users/${username}/`,
  };
}


function mountProfile(propsData = {}) {
  jest.spyOn(API.SSO, 'policy').mockReturnValue(new Promise(() => {}));
  jest.spyOn(API.SSO, 'identities').mockReturnValue(new Promise(() => {}));
  return shallowMount(Profile, {
    propsData: { token: 'owner-token', ...propsData },
    mocks: { $t: key => ko[key] || key },
  });
}


function mountProfilePage(username = 'owner') {
  const router = { push: jest.fn() };
  const wrapper = shallowMount(Profile4User, {
    mocks: {
      $route: { params: { username } },
      $router: router,
    },
    stubs: {
      PHeader: true,
      UserProfileCard: true,
    },
  });
  return { router, wrapper };
}


describe('Profile administrator link', () => {
  beforeEach(() => {
    jest.spyOn(API.Version, 'fetch').mockReturnValue(new Promise(() => {}));
    jest.spyOn(API.User, 'fetchUserInfoByName')
      .mockResolvedValue(publicUser('owner'));
    jest.spyOn(API.User, 'fetchUserInfo')
      .mockResolvedValue(currentUser('owner'));
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('links an administrator to the local Django admin page', () => {
    const wrapper = mountProfile({ canAccessAdmin: true });
    const link = wrapper.find('[data-test="admin-settings-link"]');

    expect(link.exists()).toBe(true);
    expect(link.attributes('href')).toBe('/admin/');
    expect(link.text()).toBe('관리자 설정');
  });

  it.each([
    ['the default value', {}],
    ['an explicit false value', { canAccessAdmin: false }],
  ])('hides the administrator link for %s', (_name, propsData) => {
    const wrapper = mountProfile(propsData);

    expect(wrapper.find('[data-test="admin-settings-link"]').exists()).toBe(false);
  });
});


describe('Profile page ownership boundary', () => {
  beforeEach(() => {
    jest.spyOn(API.Version, 'fetch').mockReturnValue(new Promise(() => {}));
    jest.spyOn(API.User, 'fetchUserInfoByName')
      .mockResolvedValue(publicUser('owner'));
    jest.spyOn(API.User, 'fetchUserInfo')
      .mockResolvedValue(currentUser('owner'));
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('checks the public user before forcing a current-user request', async () => {
    const publicRequest = deferred();
    API.User.fetchUserInfoByName.mockReturnValue(publicRequest.promise);

    mountProfilePage();

    expect(API.User.fetchUserInfo).not.toHaveBeenCalled();
    publicRequest.resolve(publicUser('owner'));
    await flushPromises();

    expect(API.User.fetchUserInfo).toHaveBeenCalledTimes(1);
    expect(API.User.fetchUserInfo).toHaveBeenCalledWith(true);
  });

  it('does not render private profile data from public-user existence alone', async () => {
    API.User.fetchUserInfo.mockReturnValue(new Promise(() => {}));
    const { wrapper } = mountProfilePage();
    await flushPromises();

    expect(wrapper.findComponent(Profile).exists()).toBe(false);
  });

  it('renders the current user private profile with a Boolean admin capability', async () => {
    API.User.fetchUserInfo.mockResolvedValue(currentUser('owner', true));
    const { wrapper } = mountProfilePage();
    await flushPromises();

    const profile = wrapper.findComponent(Profile);
    expect(profile.exists()).toBe(true);
    expect(profile.props('token')).toBe('owner-token');
    expect(profile.props('canAccessAdmin')).toBe(true);
    expect(typeof profile.props('canAccessAdmin')).toBe('boolean');
  });

  it('passes false as a Boolean admin capability for a regular current user', async () => {
    const { wrapper } = mountProfilePage();
    await flushPromises();

    const profile = wrapper.findComponent(Profile);
    expect(profile.exists()).toBe(true);
    expect(profile.props('canAccessAdmin')).toBe(false);
    expect(typeof profile.props('canAccessAdmin')).toBe('boolean');
  });

  it('does not render a private profile while viewing another user', async () => {
    API.User.fetchUserInfoByName.mockResolvedValue(publicUser('other'));
    const { wrapper } = mountProfilePage('other');
    await flushPromises();

    expect(wrapper.findComponent(Profile).exists()).toBe(false);
  });

  it('does not render a private profile for an anonymous visitor', async () => {
    API.User.fetchUserInfo.mockResolvedValue(null);
    const { wrapper } = mountProfilePage();
    await flushPromises();

    expect(wrapper.findComponent(Profile).exists()).toBe(false);
  });

  it('routes a missing public user to the not-found page without a private request', async () => {
    API.User.fetchUserInfoByName.mockResolvedValue(null);
    const { router, wrapper } = mountProfilePage('missing');
    await flushPromises();

    expect(router.push).toHaveBeenCalledWith({ name: 'PageNotFound' });
    expect(API.User.fetchUserInfo).not.toHaveBeenCalled();
    expect(wrapper.findComponent(Profile).exists()).toBe(false);
  });

  it('ignores a missing public-user response after the page is destroyed', async () => {
    const publicRequest = deferred();
    API.User.fetchUserInfoByName.mockReturnValue(publicRequest.promise);
    const { router, wrapper } = mountProfilePage('missing');

    wrapper.destroy();
    publicRequest.resolve(null);
    await flushPromises();

    expect(router.push).not.toHaveBeenCalled();
    expect(API.User.fetchUserInfo).not.toHaveBeenCalled();
  });

  it('ignores a current-user response after the page is destroyed', async () => {
    const currentUserRequest = deferred();
    API.User.fetchUserInfo.mockReturnValue(currentUserRequest.promise);
    const { wrapper } = mountProfilePage();
    await flushPromises();
    expect(API.User.fetchUserInfo).toHaveBeenCalledWith(true);

    wrapper.destroy();
    currentUserRequest.resolve(currentUser('owner', true));
    await flushPromises();

    expect(wrapper.vm.profile).toBeNull();
  });

  it('ignores stale private data after the route changes to another user', async () => {
    const oldCurrentUserRequest = deferred();
    API.User.fetchUserInfoByName.mockImplementation(
      username => Promise.resolve(publicUser(username)),
    );
    API.User.fetchUserInfo
      .mockReturnValueOnce(oldCurrentUserRequest.promise)
      .mockResolvedValueOnce(currentUser('owner'));
    const { wrapper } = mountProfilePage('owner');
    await flushPromises();

    const next = jest.fn();
    Profile4User.beforeRouteUpdate.call(
      wrapper.vm,
      { params: { username: 'other' } },
      { params: { username: 'owner' } },
      next,
    );
    await wrapper.vm.$nextTick();
    await flushPromises();

    expect(next).toHaveBeenCalledTimes(1);
    expect(API.User.fetchUserInfoByName).toHaveBeenCalledWith('other');
    expect(wrapper.findComponent(Profile).exists()).toBe(false);

    oldCurrentUserRequest.resolve(currentUser('owner', true));
    await flushPromises();

    expect(wrapper.findComponent(Profile).exists()).toBe(false);
  });
});
