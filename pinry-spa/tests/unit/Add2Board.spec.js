/* eslint-env jest */
import axios from 'axios';
import flushPromises from 'flush-promises';
import { createI18n } from 'vue-i18n';
import { mount } from '@vue/test-utils';

import API from '@/components/api';
import Add2Board from '@/components/pin_edit/Add2Board.vue';
import FilterSelect from '@/components/pin_edit/FilterSelect.vue';
import ko from '@/components/utils/i18n/locales/ko.json';
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

jest.mock('axios');

const messages = {
  ko: {
    ...ko,
    pinBoardAlreadyIncluded: '이미 포함됨',
    pinBoardMembershipLoadError: '보드 포함 상태를 불러오지 못했습니다.',
  },
};

function deferred() {
  const request = {};
  request.promise = new Promise((resolve, reject) => {
    request.resolve = resolve;
    request.reject = reject;
  });
  request.promise.catch(() => {});
  return request;
}

async function settle() {
  await flushPromises();
  await flushPromises();
}

function membershipResponse() {
  return {
    data: {
      boards: [
        { id: 3, name: '이미 있는 보드', contains_pin: true },
        { id: 7, name: '추가 보드', contains_pin: false },
      ],
    },
  };
}

function installMembershipDouble(response = membershipResponse()) {
  API.Pin.fetchBoardMemberships = jest.fn().mockResolvedValue(response);
}

function mountDialog() {
  const wrapper = mount(Add2Board, {
    global: {
      directives: { masonry: {}, 'masonry-tile': {} },
      plugins: [createI18n({ legacy: false, locale: 'ko', messages })],
    },

    props: {
      pin: { id: 41, url: '/media/pin-41.jpg' },
      username: 'owner',
    },
  });
  const close = jest.fn();
  wrapper.setProps({ onClose: close });
  return { close, wrapper };
}

function mountFilterSelect() {
  return mount(FilterSelect, {
    global: {
      directives: { masonry: {}, 'masonry-tile': {} },
      plugins: [createI18n({ legacy: false, locale: 'ko', messages })],
    },

    props: { allOptions: [] },
  });
}

describe('FilterSelect 옵션 메타데이터', () => {
  it('일반 보드 옵션은 기존 이름을 유지하고 활성화한다', async () => {
    const wrapper = mountFilterSelect();
    await wrapper.setProps({
      allOptions: [{ name: '원본 이름', value: 7 }],
    });
    await wrapper.vm.$nextTick();

    const option = wrapper.find('option[value="7"]');
    expect(option.exists()).toBe(true);
    expect(option.attributes('disabled')).toBeUndefined();
    expect(option.text()).toBe('원본 이름');
    wrapper.unmount();
  });
});

describe('Pin 보드 포함 API', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    axios.get.mockResolvedValue({ data: { boards: [] } });
  });

  it('Pin 상세 경로에서 보드 포함 상태를 조회한다', async () => {
    if (typeof API.Pin.fetchBoardMemberships === 'function') {
      await API.Pin.fetchBoardMemberships(41);
    }

    expect(axios.get).toHaveBeenCalledWith('/api/v2/pins/41/board-memberships/');
  });
});

describe('Add2Board 포함 상태', () => {
  const originalFetchBoardMemberships = API.Pin.fetchBoardMemberships;
  const originalFetchFullList = API.Board.fetchFullList;
  const originalAddToBoard = API.Board.addToBoard;
  const originalCreateBoard = API.Board.create;

  beforeEach(() => {
    jest.clearAllMocks();
    installMembershipDouble();
    API.Board.fetchFullList = jest.fn().mockResolvedValue({ data: [] });
    API.Board.addToBoard = jest.fn().mockResolvedValue({ status: 200 });
    API.Board.create = jest.fn().mockResolvedValue({ id: 11, name: '새 보드' });
  });

  afterAll(() => {
    if (originalFetchBoardMemberships === undefined) {
      delete API.Pin.fetchBoardMemberships;
    } else {
      API.Pin.fetchBoardMemberships = originalFetchBoardMemberships;
    }
    API.Board.fetchFullList = originalFetchFullList;
    API.Board.addToBoard = originalAddToBoard;
    API.Board.create = originalCreateBoard;
  });

  it('현재 Pin ID로 내 보드들의 포함 상태를 불러온다', async () => {
    const { wrapper } = mountDialog();
    await settle();

    expect(API.Pin.fetchBoardMemberships).toHaveBeenCalledWith(41);
    expect(API.Board.fetchFullList).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it('이미 Pin이 포함된 보드라면 선태가 비활성하고 포함 문구라로 표시한다', async () => {
    const { wrapper } = mountDialog();
    await settle();

    const options = wrapper.findComponent(FilterSelect).findAll('option');
    const included = options.find(option => option.attributes('value') === '3');

    expect(included).toBeDefined();
    expect(included.attributes('disabled')).toBeDefined();
    expect(included.text()).toContain('이미 포함됨');
    wrapper.unmount();
  });

  it('포함된 ID가 포함되지 않은 ID와 섞여되어도 PATCH에서 제외한다', async () => {
    const { wrapper } = mountDialog();
    await settle();
    wrapper.findComponent(FilterSelect).vm.$emit('selected', [3, 7]);
    await wrapper.vm.$nextTick();

    await wrapper.find('.modal-card-foot .button.is-primary').trigger('click');
    await settle();

    expect(API.Board.addToBoard).toHaveBeenCalledTimes(1);
    expect(API.Board.addToBoard).toHaveBeenCalledWith(7, [41]);
    wrapper.unmount();
  });

  it('새로 만든 보드는 현재 Pin이 포함되지 않으므로 선태하고 PATCH한다', async () => {
    const { wrapper } = mountDialog();
    await settle();
    const filterSelect = wrapper.findComponent(FilterSelect);
    filterSelect.vm.form.name.value = '새 보드';

    filterSelect.vm.createNewBoard();
    await settle();

    const option = filterSelect.find('option[value="11"]');
    expect(option.exists()).toBe(true);
    expect(option.attributes('disabled')).toBeUndefined();
    expect(wrapper.vm.boardIds).toEqual([11]);

    await wrapper.find('.modal-card-foot .button.is-primary').trigger('click');
    await settle();

    expect(API.Board.addToBoard).toHaveBeenCalledTimes(1);
    expect(API.Board.addToBoard).toHaveBeenCalledWith(11, [41]);
    wrapper.unmount();
  });

  it('제출을 연다라 눌러도 하나의 PATCH만 보낸다', async () => {
    const request = deferred();
    API.Board.addToBoard.mockReturnValueOnce(request.promise);
    const { wrapper } = mountDialog();
    await settle();
    wrapper.findComponent(FilterSelect).vm.$emit('selected', [7]);
    await wrapper.vm.$nextTick();
    const submit = wrapper.find('.modal-card-foot .button.is-primary');

    await submit.trigger('click');
    await submit.trigger('click');

    expect(API.Board.addToBoard).toHaveBeenCalledTimes(1);
    request.resolve({ status: 200 });
    await settle();
    wrapper.unmount();
  });

  it('제출 중 모달이 파괴되면 늦은 응답으로 다시 닫거나 알림을 표시하지 않는다', async () => {
    const request = deferred();
    API.Board.addToBoard.mockReturnValueOnce(request.promise);
    const { close, wrapper } = mountDialog();
    await settle();
    wrapper.findComponent(FilterSelect).vm.$emit('selected', [7]);
    await wrapper.vm.$nextTick();
    const toast = jest.spyOn(overlays, 'toast');

    await wrapper.find('.modal-card-foot .button.is-primary').trigger('click');
    wrapper.unmount();
    request.resolve({ status: 200 });
    await settle();

    expect(close).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
  });

  it('일부 PATCH가 먼저 실패해도 모두 종료될 때까지 잠금하고 실패한 보드만 재시도한다', async () => {
    installMembershipDouble({
      data: {
        boards: [
          { id: 7, name: '즉시 실패 보드', contains_pin: false },
          { id: 9, name: '지연 성공 보드', contains_pin: false },
        ],
      },
    });
    const pendingRequest = deferred();
    const attempts = {};
    API.Board.addToBoard.mockImplementation((boardId) => {
      attempts[boardId] = (attempts[boardId] || 0) + 1;
      if (boardId === 7 && attempts[boardId] === 1) {
        return Promise.reject(new Error('network'));
      }
      if (boardId === 9 && attempts[boardId] === 1) {
        return pendingRequest.promise;
      }
      return Promise.resolve({ status: 200 });
    });
    const { wrapper } = mountDialog();
    await settle();
    wrapper.findComponent(FilterSelect).vm.$emit('selected', [7, 9]);
    await wrapper.vm.$nextTick();
    const submit = wrapper.find('.modal-card-foot .button.is-primary');

    await submit.trigger('click');
    await settle();
    const disabledWhilePending = submit.attributes('disabled');
    await submit.trigger('click');
    const callCountWhilePending = API.Board.addToBoard.mock.calls.length;

    pendingRequest.resolve({ status: 200 });
    await settle();
    await submit.trigger('click');
    await settle();

    expect({
      disabledWhilePending,
      callCountWhilePending,
      calledBoardIds: API.Board.addToBoard.mock.calls.map(call => call[0]),
    }).toEqual({
      disabledWhilePending: '',
      callCountWhilePending: 2,
      calledBoardIds: [7, 9, 7],
    });
    wrapper.unmount();
  });

  it('새 보드 추가만 성공하면 모달에서 즉시 이미 포함됨으로 비활성화한다', async () => {
    const { wrapper } = mountDialog();
    await settle();
    const filterSelect = wrapper.findComponent(FilterSelect);
    filterSelect.vm.form.name.value = '새 보드';
    filterSelect.vm.createNewBoard();
    await settle();
    filterSelect.vm.$emit('selected', [11, 7]);
    await wrapper.vm.$nextTick();
    API.Board.addToBoard.mockImplementation(boardId => (
      boardId === 11
        ? Promise.resolve({ status: 200 })
        : Promise.reject(new Error('network'))
    ));

    await wrapper.find('.modal-card-foot .button.is-primary').trigger('click');
    await settle();

    const createdOption = filterSelect.find('option[value="11"]');
    expect(createdOption.exists()).toBe(true);
    expect(createdOption.attributes('disabled')).toBeDefined();
    expect(createdOption.text()).toContain('이미 포함됨');
    expect(wrapper.vm.boardIds).toEqual([7]);
    wrapper.unmount();
  });

  it('느린 포함 상태 응답이 나중에 도착해도 새 보드 옵션과 선택을 유지한다', async () => {
    const membershipRequest = deferred();
    API.Pin.fetchBoardMemberships.mockReturnValueOnce(membershipRequest.promise);
    const { wrapper } = mountDialog();
    const filterSelect = wrapper.findComponent(FilterSelect);
    filterSelect.vm.form.name.value = '새 보드';

    filterSelect.vm.createNewBoard();
    await settle();

    membershipRequest.resolve(membershipResponse());
    await settle();

    const createdOption = filterSelect.find('option[value="11"]');
    expect(createdOption.exists()).toBe(true);
    expect(createdOption.attributes('disabled')).toBeUndefined();
    expect(filterSelect.vm.selectedOptions).toEqual([11]);
    expect(wrapper.vm.boardIds).toEqual([11]);
    wrapper.unmount();
  });

  it('포함 상태 조회가 실패하면 빈 목록과 구분되는 실패로 표시한다', async () => {
    API.Pin.fetchBoardMemberships.mockRejectedValueOnce(new Error('network'));
    const { wrapper } = mountDialog();
    await settle();

    const error = wrapper.find('[data-test="add2board-membership-error"]');
    expect(error.exists()).toBe(true);
    expect(error.attributes('role')).toBe('alert');
    expect(error.text()).toBe('보드 포함 상태를 불러오지 못했습니다.');
    expect(wrapper.find('.modal-card-foot .button.is-primary').attributes('disabled'))
      .toBeDefined();
    wrapper.unmount();
  });
});
