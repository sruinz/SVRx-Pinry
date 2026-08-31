/* eslint-env jest */

import { createLocalVue, shallowMount } from '@vue/test-utils';

import PHeader from '@/components/PHeader.vue';
import router from '@/router';

function mountHeader() {
  const initializeUser = jest.spyOn(PHeader.methods, 'initializeUser')
    .mockImplementation(() => {});
  const localVue = createLocalVue();
  const wrapper = shallowMount(PHeader, {
    localVue,
    mocks: { $t: key => key },
    stubs: {
      'b-icon': true,
      'router-link': {
        name: 'RouterLinkStub',
        props: ['to'],
        template: '<a><slot /></a>',
      },
    },
  });
  initializeUser.mockRestore();
  return wrapper;
}

describe('export route and header entry point', () => {
  it('registers one exact named route before the wildcard', () => {
    const { routes } = router.options;
    expect(routes.filter(route => route.path === '/exports')).toHaveLength(1);
    expect(routes.filter(route => route.name === 'exports')).toHaveLength(1);
    expect(routes.find(route => route.name === 'exports').path).toBe('/exports');
    expect(routes.findIndex(route => route.name === 'exports'))
      .toBeLessThan(routes.findIndex(route => route.path === '*'));
    expect(router.resolve({ name: 'exports' }).route.path).toBe('/exports');
  });

  it('orders the authenticated My menu as Pins, boards, exports, then profile', async () => {
    const wrapper = mountHeader();
    await wrapper.setData({
      user: { loggedIn: true, meta: { username: 'owner' } },
    });
    const menu = wrapper.find('[data-test="my-menu"]');

    expect([...menu.element.children].map(item => item.dataset.test)).toEqual([
      'my-pins-link',
      'my-boards-link',
      'my-exports-link',
      'my-profile-link',
    ]);
    const link = wrapper.find('[data-test="my-exports-link"]');
    expect(link.props('to')).toEqual({ name: 'exports' });
    expect(link.element.tagName).toBe('A');
    expect(link.attributes('tabindex')).toBeUndefined();
  });

  it('does not expose the export menu entry to anonymous users', () => {
    const wrapper = mountHeader();

    expect(wrapper.find('[data-test="my-menu"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="my-exports-link"]').exists()).toBe(false);
  });
});
