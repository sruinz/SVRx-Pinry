/* eslint-env jest */
import { shallowMount } from '@vue/test-utils';

import Search from '@/views/Search.vue';


function mountSearch() {
  return shallowMount(Search, {
    stubs: ['PHeader', 'SearchPanel', 'Pins', 'Boards'],
  });
}


describe('multiple-tag search state', () => {
  it('copies a non-empty tag selection and clears an empty selection', () => {
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
    expect(wrapper.vm.pinFilters).toBeNull();
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
