/* eslint-env jest */
import axios from 'axios';
import flushPromises from 'flush-promises';
import VueI18n from 'vue-i18n';
import { createLocalVue, shallowMount } from '@vue/test-utils';

import SearchPanel from '@/components/search/SearchPanel.vue';
import en from '@/components/utils/i18n/locales/en.json';

jest.mock('axios');


function deferred() {
  const request = {};
  request.promise = new Promise((resolve) => {
    request.resolve = resolve;
  });
  return request;
}


function mountPanel() {
  const localVue = createLocalVue();
  localVue.use(VueI18n);
  return shallowMount(SearchPanel, {
    localVue,
    i18n: new VueI18n({ locale: 'en', messages: { en } }),
    stubs: ['b-field', 'b-select', 'b-taginput', 'b-input', 'b-button'],
  });
}


describe('multiple-tag search panel', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    axios.get.mockResolvedValue({
      data: [{ name: 'alpha' }, { name: 'beta' }],
    });
  });

  it('emits a copied array containing every selected existing tag', async () => {
    const wrapper = mountPanel();
    await flushPromises();

    await wrapper.setData({ filterType: 'Tag' });
    await wrapper.setData({ selectedTags: ['alpha', 'beta'] });

    const payload = wrapper.emitted('selected').slice(-1)[0][0];
    expect(payload).toEqual({
      filterType: 'Tag',
      selected: ['alpha', 'beta'],
    });
    wrapper.vm.selectedTags.push('later');
    expect(payload.selected).toEqual(['alpha', 'beta']);
  });

  it('offers only matching tags that have not already been selected', async () => {
    const wrapper = mountPanel();
    await flushPromises();

    await wrapper.setData({ filterType: 'Tag' });
    await wrapper.setData({ selectedTags: ['alpha'], name: 'be' });

    expect(wrapper.vm.options.Tag).toEqual(['alpha', 'beta']);
    expect(wrapper.vm.filteredDataArray).toEqual(['beta']);
  });

  it('shows tags when the list finishes loading after tag mode is selected', async () => {
    const request = deferred();
    axios.get.mockReturnValueOnce(request.promise);
    const wrapper = mountPanel();

    await wrapper.setData({ filterType: 'Tag' });
    request.resolve({ data: [{ name: 'late-tag' }] });
    await flushPromises();

    expect(wrapper.vm.filteredDataArray).toEqual(['late-tag']);
  });
});
