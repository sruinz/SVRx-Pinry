/* eslint-env jest */

import flushPromises from 'flush-promises';
import { mount } from '@vue/test-utils';

import API from '@/components/api';
import BoardEdit from '@/components/BoardEdit.vue';
import Boards from '@/components/Boards.vue';
import BoardEditUI from '@/components/editors/BoardEditUI.vue';
import ko from '@/components/utils/i18n/locales/ko.json';
import bus from '@/components/utils/bus';
import scroll from '@/components/utils/scroll';
import overlays from '@/components/utils/overlays';

jest.mock('@/components/utils/overlays', () => ({
  __esModule: true,
  default: {
    openModal: jest.fn(), confirm: jest.fn(), toast: jest.fn(), openLoading: jest.fn(),
  },
}));
beforeEach(() => {
  overlays.openModal.mockReset();
  overlays.confirm.mockReset();
  overlays.toast.mockReset();
  overlays.openLoading.mockReset().mockReturnValue({ close: jest.fn() });
});

function mountBoardEdit(board) {
  return mount(BoardEdit, {
    global: { directives: { masonry: {}, 'masonry-tile': {} }, mocks: { $t: key => ko[key] || key } },

    props: { isEdit: true, board },
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

    await wrapper.find('input[type="checkbox"]').setChecked(false);

    expect(wrapper.find('[data-test="board-cover-publish-warning"]').text())
      .toContain('자동 대표 이미지');
    wrapper.unmount();
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

    await wrapper.find('input[type="checkbox"]').setChecked(false);

    expect(wrapper.find('[data-test="board-cover-publish-warning"]').exists()).toBe(false);
    wrapper.unmount();
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
    const modal = { open: overlays.openModal };


    const boardsWrapper = mount(Boards, {
      global: {
        directives: { masonry: {}, 'masonry-tile': {} },
        mocks: {
          $t: key => ko[key] || key,
        },
        stubs: {
          loadingSpinner: true,
          noMore: true,
          'router-link': {
            props: ['to'],
            template: '<a href="#"><slot /></a>',
          },
        },
      },

      props: { filters: { boardUsername: 'owner' } },
    });
    await flushPromises();

    const editor = boardsWrapper.findComponent(BoardEditUI);
    editor.vm.editBoard();

    const modalConfig = modal.open.mock.calls[0][1];
    expect(modalConfig.component).toBe(BoardEdit);
    expect(modalConfig.props.board).toMatchObject({
      id: 7,
      private: true,
      cover_pin_id: 31,
      cover: { id: 31, private: true },
    });
    const boardEditWrapper = mountBoardEdit(modalConfig.props.board);
    await boardEditWrapper.find('input[type="checkbox"]').setChecked(false);
    expect(boardEditWrapper.find('[data-test="board-cover-publish-warning"]').exists())
      .toBe(true);

    bus.bus.off(bus.events.refreshBoards, boardsWrapper.vm.reset);
    boardEditWrapper.unmount();
    boardsWrapper.unmount();
    scroll.bindScroll2Bottom.mockRestore();
  });
});
