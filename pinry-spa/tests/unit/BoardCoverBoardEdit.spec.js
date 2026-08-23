/* eslint-env jest */

import Buefy from 'buefy';
import { createLocalVue, mount } from '@vue/test-utils';

import BoardEdit from '@/components/BoardEdit.vue';
import ko from '@/components/utils/i18n/locales/ko.json';

function mountBoardEdit(board) {
  const localVue = createLocalVue();
  localVue.use(Buefy);
  return mount(BoardEdit, {
    localVue,
    propsData: { isEdit: true, board },
    mocks: { $t: key => ko[key] || key },
  });
}

describe('Board cover publication warning', () => {
  it('warns when publishing a private board with a private manual cover', async () => {
    const wrapper = mountBoardEdit({
      id: 7,
      name: 'Private board',
      private: true,
      cover_pin_id: 31,
      cover: { id: 31, private: true },
    });
    expect(wrapper.find('[data-test="board-cover-publish-warning"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="board-private-checkbox"]').exists()).toBe(true);

    await wrapper.find('.b-checkbox input').setChecked(false);

    expect(wrapper.find('[data-test="board-cover-publish-warning"]').text())
      .toContain('자동 대표 이미지');
    wrapper.destroy();
  });

  it.each([
    ['an automatic cover', null, { id: 31, private: true }],
    ['a public manual cover', 31, { id: 31, private: false }],
  ])('does not warn when publishing with %s', async (_name, coverPinId, cover) => {
    const wrapper = mountBoardEdit({
      id: 7,
      name: 'Private board',
      private: true,
      cover_pin_id: coverPinId,
      cover,
    });

    await wrapper.find('.b-checkbox input').setChecked(false);

    expect(wrapper.find('[data-test="board-cover-publish-warning"]').exists()).toBe(false);
    wrapper.destroy();
  });
});
