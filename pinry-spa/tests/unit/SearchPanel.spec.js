/* eslint-env jest */
import axios from 'axios';
import flushPromises from 'flush-promises';
import { createI18n } from 'vue-i18n';
import { shallowMount } from '@vue/test-utils';

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
  return shallowMount(SearchPanel, {
    global: {
      directives: { masonry: {}, 'masonry-tile': {} },
      stubs: ['FormField', 'TagInput'],
      plugins: [createI18n({ legacy: true, locale: 'en', messages: { en } })],
    },
  });
}


describe('multiple-tag search panel', () => {
  it('submits all pins when controls are empty without sending placeholder dimensions', async () => {
    const wrapper = mountPanel();
    await wrapper.setProps({ query: { mode: 'pins' } });
    await flushPromises();
    await wrapper.find('[data-search-apply]').trigger('click');
    expect(wrapper.emitted('selected').slice(-1)[0][0]).toEqual({ filterType: 'Tag', selected: [] });
    expect(wrapper.find('input[name="min_width"]').element.value).toBe('');
    expect(wrapper.find('input[name="min_height"]').element.value).toBe('');
    wrapper.unmount();
  });
  it('restores actual fields, tags and errors from URL query changes without submitting', async () => {
    const wrapper = mountPanel();
    await flushPromises();
    await wrapper.setProps({ query: { min_width: '-1' } });
    expect(wrapper.find('input[name="min_width"]').attributes('aria-invalid')).toBe('true');
    expect(wrapper.find('details').element.open).toBe(true);
    const query = {
      mode: 'pins', tag: ['alpha', 'beta'], animation: 'animated', date_from: '2024-02-29',
    };
    await wrapper.setProps({ query });
    expect(wrapper.vm.selectedTags).toEqual(['alpha', 'beta']);
    expect(wrapper.find('select[name="animation"]').element.value).toBe('animated');
    expect(wrapper.find('input[name="date_from"]').element.value).toBe('2024-02-29');
    expect(wrapper.find('input[name="min_width"]').element.value).toBe('');
    expect(wrapper.find('[role="alert"]').exists()).toBe(false);
    await wrapper.setProps({ query: { mode: 'boards', q: 'travel' } });
    expect(wrapper.vm.boardText).toBe('travel');
    expect(wrapper.find('details').exists()).toBe(false);
    await wrapper.setProps({ query });
    expect(wrapper.vm.selectedTags).toEqual(['alpha', 'beta']);
    expect(wrapper.find('select[name="animation"]').element.value).toBe('animated');
    expect(wrapper.emitted('selected')).toBeUndefined();
    wrapper.unmount();
  });
  it('applies image-only filters and prevents invalid draft submission', async () => {
    const wrapper = mountPanel();
    await flushPromises();
    await wrapper.setData({ filterType: 'Tag' });
    const animation = wrapper.find('select[name="animation"]');
    expect(animation.exists()).toBe(true);
    await animation.setValue('animated');
    await wrapper.find('input[name="min_width"]').setValue('800');
    await wrapper.find('[data-search-apply]').trigger('click');
    expect(wrapper.emitted('selected').slice(-1)[0][0]).toEqual({ filterType: 'Tag', selected: [], filters: { animation: 'animated', min_width: '800' } });
    const count = wrapper.emitted('selected').length;
    await wrapper.find('input[name="min_width"]').setValue('-1');
    await wrapper.find('[data-search-apply]').trigger('click');
    expect(wrapper.emitted('selected')).toHaveLength(count);
    expect(wrapper.find('#search-error-min_width').exists()).toBe(true);
    await wrapper.find('[data-search-reset]').trigger('click');
    expect(wrapper.find('input[name="min_width"]').element.value).toBe('');
    expect(wrapper.find('select[name="animation"]').element.value).toBe('');
    wrapper.unmount();
  });
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
