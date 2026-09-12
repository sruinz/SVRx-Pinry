/* eslint-env jest */

import flushPromises from 'flush-promises';
import { shallowMount } from '@vue/test-utils';

import API from '@/components/api';
import UserProfileCard from '@/components/UserProfileCard.vue';


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
    gravatar: `${username}-gravatar`,
    resource_link: `/api/v2/profile/public-users/${username}/`,
  };
}


function mountCard(username = 'owner') {
  const router = { push: jest.fn() };
  const wrapper = shallowMount(UserProfileCard, {
    global: {
      mocks: {
        $router: router,
        $t: key => key,
      },
      stubs: {
        'b-skeleton': true,
      },
    },
    props: { username },
  });
  return { router, wrapper };
}


describe('UserProfileCard request lifecycle', () => {
  beforeEach(() => {
    jest.spyOn(API.User, 'fetchUserInfoByName')
      .mockResolvedValue(publicUser('owner'));
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('ignores a missing-user response after the card is destroyed', async () => {
    const request = deferred();
    API.User.fetchUserInfoByName.mockReturnValue(request.promise);
    const { router, wrapper } = mountCard('missing');

    wrapper.unmount();
    request.resolve(null);
    await flushPromises();

    expect(router.push).not.toHaveBeenCalled();
    expect(wrapper.vm.user).toEqual({ avatar: '', username: '' });
  });

  it.each([
    ['an old user', publicUser('owner')],
    ['a missing old user', null],
  ])('keeps the new user when %s resolves late', async (_name, staleResult) => {
    const ownerRequest = deferred();
    API.User.fetchUserInfoByName.mockImplementation(
      username => (
        username === 'owner'
          ? ownerRequest.promise
          : Promise.resolve(publicUser('other'))
      ),
    );
    const { router, wrapper } = mountCard('owner');

    await wrapper.setProps({ username: 'other' });
    await flushPromises();

    expect(API.User.fetchUserInfoByName).toHaveBeenCalledWith('other');
    expect(wrapper.vm.user.username).toBe('other');

    ownerRequest.resolve(staleResult);
    await flushPromises();

    expect(wrapper.vm.user.username).toBe('other');
    expect(router.push).not.toHaveBeenCalled();
  });

  it('clears the old card while loading a changed username', async () => {
    const otherRequest = deferred();
    API.User.fetchUserInfoByName.mockImplementation(
      username => (
        username === 'owner'
          ? Promise.resolve(publicUser('owner'))
          : otherRequest.promise
      ),
    );
    const { wrapper } = mountCard('owner');
    await flushPromises();
    wrapper.vm.onAvatarLoaded();
    await wrapper.vm.$nextTick();
    expect(wrapper.vm.user.username).toBe('owner');
    expect(wrapper.vm.avatarLoading).toBe(false);

    await wrapper.setProps({ username: 'other' });

    expect(wrapper.vm.user).toEqual({ avatar: '', username: '' });
    expect(wrapper.vm.avatarLoading).toBe(true);

    otherRequest.resolve(publicUser('other'));
    await flushPromises();
    expect(wrapper.vm.user.username).toBe('other');
  });

  it('still routes an existing card to not-found for a missing user', async () => {
    API.User.fetchUserInfoByName.mockResolvedValue(null);
    const { router } = mountCard('missing');
    await flushPromises();

    expect(router.push).toHaveBeenCalledWith({ name: 'PageNotFound' });
  });
});
