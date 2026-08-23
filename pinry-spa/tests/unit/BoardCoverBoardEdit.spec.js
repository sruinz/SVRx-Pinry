/* eslint-env jest */

import Buefy from 'buefy';
import flushPromises from 'flush-promises';
import { createLocalVue, mount } from '@vue/test-utils';

import API from '@/components/api';
import BoardEdit from '@/components/BoardEdit.vue';
import Boards from '@/components/Boards.vue';
import BoardEditUI from '@/components/editors/BoardEditUI.vue';
import ko from '@/components/utils/i18n/locales/ko.json';
import bus from '@/components/utils/bus';
import scroll from '@/components/utils/scroll';

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
  beforeEach(() => {
    jest.clearAllMocks();
  });

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

  it('preserves cover metadata through Boards and opens an editor that can warn', async () => {
    jest.spyOn(scroll, 'bindScroll2Bottom').mockImplementation(() => {});
    const board = {
      id: 7,
      name: 'Private board',
      private: true,
      total_pins: 1,
      cover_pin_id: 31,
      cover: {
        id: 31,
        private: true,
        image: {
          thumbnail: {
            image: 'https://example.test/cover.jpg',
            width: 240,
            height: 180,
          },
        },
      },
      submitter: { username: 'owner' },
    };
    API.User.fetchUserInfo = jest.fn().mockResolvedValue({ username: 'owner' });
    API.fetchBoardForUser = jest.fn().mockResolvedValue({
      data: { results: [board], next: null },
    });
    const modal = { open: jest.fn() };
    const localVue = createLocalVue();
    localVue.directive('masonry', {});
    localVue.directive('masonry-tile', {});
    const boardsWrapper = mount(Boards, {
      localVue,
      propsData: { filters: { boardUsername: 'owner' } },
      mocks: {
        $buefy: { modal },
        $t: key => ko[key] || key,
      },
      stubs: {
        'b-icon': true,
        loadingSpinner: true,
        noMore: true,
        'router-link': {
          props: ['to'],
          template: '<a href="#"><slot /></a>',
        },
      },
    });
    await flushPromises();

    const editor = boardsWrapper.findComponent(BoardEditUI);
    editor.vm.editBoard();

    const modalConfig = modal.open.mock.calls[0][0];
    expect(modalConfig.component).toBe(BoardEdit);
    expect(modalConfig.props.board).toMatchObject({
      id: 7,
      private: true,
      cover_pin_id: 31,
      cover: { id: 31, private: true },
    });
    const boardEditWrapper = mountBoardEdit(modalConfig.props.board);
    await boardEditWrapper.find('.b-checkbox input').setChecked(false);
    expect(boardEditWrapper.find('[data-test="board-cover-publish-warning"]').exists())
      .toBe(true);

    bus.bus.$off(bus.events.refreshBoards, boardsWrapper.vm.reset);
    boardEditWrapper.destroy();
    boardsWrapper.destroy();
    scroll.bindScroll2Bottom.mockRestore();
  });
});
