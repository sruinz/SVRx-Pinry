/* eslint-env jest */
import { shallowMount } from '@vue/test-utils';
import { createRouter, createMemoryHistory } from 'vue-router';
import flushPromises from 'flush-promises';

import Search from '@/views/Search.vue';
import Pins from '@/components/Pins.vue';


function mountSearch() {
  return shallowMount(Search, {
    global: { stubs: ['PHeader', 'SearchPanel', 'Pins', 'Boards'] },
  });
}


describe('multiple-tag search state', () => {
  it.each([
    '?mode=pins&animation=animated&animation=static',
    '?mode=pins&aspect=',
    '?mode=pins&min_height=2147483648',
    '?mode=pins&date_from=2026-02-30',
    '?mode=pins&date_to=9999-12-31',
    '?mode=pins&date_from=2026-09-14&date_to=2026-09-13',
  ])('does not request results for invalid URL %s', async (query) => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/search', component: Search }] });
    await router.push(`/search${query}`);
    const wrapper = shallowMount(Search, {
      global: { plugins: [router], mocks: { $t: key => key }, stubs: ['PHeader', 'SearchPanel', 'Pins', 'Boards'] },
    });
    expect(wrapper.find('pins-stub').exists()).toBe(false);
    expect(wrapper.find('[role="alert"]').exists()).toBe(true);
    wrapper.unmount();
  });

  it('ignores unknown URL fields while preserving multiple tags and valid leap dates', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/search', component: Search }] });
    await router.push('/search?mode=pins&tag=alpha&tag=beta&date_from=2024-02-29&owner=other');
    const wrapper = shallowMount(Search, {
      global: { plugins: [router], mocks: { $t: key => key }, stubs: ['PHeader', 'SearchPanel', 'Pins', 'Boards'] },
    });
    expect(wrapper.vm.pinFilters).toEqual({ tagFilter: ['alpha', 'beta'], searchFilters: { date_from: '2024-02-29' } });
    wrapper.unmount();
  });
  it('searches by image filters without requiring a tag', async () => {
    const wrapper = mountSearch();
    wrapper.vm.doSearch({ filterType: 'Tag', selected: [], filters: { animation: 'animated' } });
    expect(wrapper.vm.pinFilters).toEqual({ tagFilter: [], searchFilters: { animation: 'animated' } });
    await wrapper.vm.$nextTick();
    expect(wrapper.findComponent(Pins).props('searchMode')).toBe(true);
  });

  it('restores filters and board mode from navigation and rejects invalid URLs', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/search', component: Search }] });
    await router.push('/search?mode=pins&tag=alpha&animation=animated&min_width=800');
    const wrapper = shallowMount(Search, {
      global: { plugins: [router], mocks: { $t: key => key }, stubs: ['PHeader', 'SearchPanel', 'Pins', 'Boards'] },
    });
    expect(wrapper.vm.pinFilters).toEqual({ tagFilter: ['alpha'], searchFilters: { animation: 'animated', min_width: '800' } });
    wrapper.vm.doSearch({ filterType: 'Board', selected: 'travel' });
    await flushPromises();
    expect(router.currentRoute.value.query).toEqual({ mode: 'boards', q: 'travel' });
    router.back();
    await flushPromises();
    expect(wrapper.vm.pinFilters.searchFilters.animation).toBe('animated');
    await router.push('/search?mode=pins&min_width=-1');
    await flushPromises();
    expect(wrapper.vm.pinFilters).toEqual({ tagFilter: ['alpha'], searchFilters: { animation: 'animated', min_width: '800' } });
    expect(wrapper.find('[role="alert"]').exists()).toBe(true);
    wrapper.unmount();
  });
  it('copies tags and searches all pins when the selection is cleared', () => {
    const wrapper = mountSearch();
    const selected = ['alpha', 'beta'];

    wrapper.vm.doSearch({ filterType: 'Tag', selected });
    expect(wrapper.vm.pinFilters).toEqual({
      tagFilter: ['alpha', 'beta'],
    });

    selected.push('later');
    expect(wrapper.vm.pinFilters).toEqual({
      tagFilter: ['alpha', 'beta'],
    });

    wrapper.vm.doSearch({ filterType: 'Tag', selected: [] });
    expect(wrapper.vm.pinFilters).toEqual({ tagFilter: [] });
  });

  it('restores an unfiltered search from the URL without requiring tags or dimensions', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/search', component: Search }] });
    await router.push('/search');
    const wrapper = shallowMount(Search, {
      global: { plugins: [router], mocks: { $t: key => key }, stubs: ['PHeader', 'SearchPanel', 'Pins', 'Boards'] },
    });
    expect(wrapper.vm.pinFilters).toBeNull();
    wrapper.vm.doSearch({ filterType: 'Tag', selected: [], filters: {} });
    await flushPromises();
    expect(router.currentRoute.value.query).toEqual({ mode: 'pins' });
    expect(wrapper.vm.pinFilters).toEqual({ tagFilter: [] });
    await router.push('/search?mode=pins&animation=static');
    router.back();
    await flushPromises();
    expect(wrapper.vm.pinFilters).toEqual({ tagFilter: [] });
    wrapper.unmount();
  });

  it('keeps the existing board search behavior', () => {
    const wrapper = mountSearch();

    wrapper.vm.doSearch({ filterType: 'Board', selected: 'travel' });

    expect(wrapper.vm.pinFilters).toBeNull();
    expect(wrapper.vm.boardFilters).toEqual({
      boardNameContains: 'travel',
    });
  });
});
