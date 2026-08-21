/* eslint-env jest */

import flushPromises from 'flush-promises';
import VueI18n from 'vue-i18n';
import { createLocalVue, shallowMount } from '@vue/test-utils';

import API from '@/components/api';
import BoardDeleteDialog from '@/components/bulk/BoardDeleteDialog.vue';
import localeUtils from '@/components/utils/i18n';

let mountedWrappers = [];

function deferred() {
  const request = {};
  request.promise = new Promise((resolve, reject) => {
    request.resolve = resolve;
    request.reject = reject;
  });
  request.promise.catch(() => {});
  return request;
}

function selectionRows(count) {
  return Array.from({ length: count }, (_, index) => ({
    id: count - index,
    owned: true,
  }));
}

function selectionResponse(count, rows = selectionRows(count)) {
  return { data: { count, results: rows } };
}

function bulkResponse(ids, statuses = {}) {
  const results = ids.map(id => ({ id, status: statuses[id] || 'deleted' }));
  return {
    data: {
      operation: 'delete_if_exclusive_to_board',
      succeeded: results.filter(item => item.status === 'deleted').length,
      preserved: results.filter(item => item.status === 'preserved').length,
      failed: results.filter(item => item.status === 'failed').length,
      results,
    },
  };
}

async function settle() {
  await flushPromises();
  await flushPromises();
  await flushPromises();
}

function mountDialog() {
  const localVue = createLocalVue();
  localVue.use(VueI18n);
  const i18n = new VueI18n({
    locale: 'ko',
    fallbackLocale: 'ko',
    messages: localeUtils.messages,
  });
  const wrapper = shallowMount(BoardDeleteDialog, {
    localVue,
    i18n,
    propsData: { board: { id: 7, name: 'Reference' } },
  });
  mountedWrappers.push(wrapper);
  return wrapper;
}

describe('BoardDeleteDialog', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    API.Board.deletePreview = jest.fn().mockResolvedValue({
      data: {
        exclusive_owned_count: 2,
        shared_owned_count: 3,
        non_owned_count: 4,
      },
    });
    API.Pin.fetchSelectionIds = jest.fn().mockResolvedValue(selectionResponse(2));
    API.Pin.bulk = jest.fn(payload => Promise.resolve(bulkResponse(payload.pin_ids)));
    API.Board.delete = jest.fn().mockResolvedValue({ status: 204 });
    API.Board.get = jest.fn().mockResolvedValue({ data: { id: 7, name: 'Reference' } });
  });

  afterEach(() => {
    mountedWrappers.forEach(wrapper => wrapper.destroy());
    mountedWrappers = [];
  });

  it('renders the board deletion preview and ready actions in Korean', async () => {
    const preview = deferred();
    API.Board.deletePreview.mockReturnValueOnce(preview.promise);
    const wrapper = mountDialog();

    expect(wrapper.find('.modal-card-title').text()).toBe('보드 삭제');
    expect(wrapper.text()).toContain('삭제할 항목을 확인하는 중입니다…');

    preview.resolve({
      data: {
        exclusive_owned_count: 2,
        shared_owned_count: 3,
        non_owned_count: 4,
      },
    });
    await settle();

    expect(wrapper.find('[data-test="board-delete-preview-exclusive"]').text())
      .toBe('삭제 가능한 전용 Pin: 2개');
    expect(wrapper.find('[data-test="board-delete-preview-shared"]').text())
      .toBe('다른 보드와 공유되어 보존할 내 Pin: 3개');
    expect(wrapper.find('[data-test="board-delete-preview-non-owned"]').text())
      .toBe('다른 사용자의 Pin이어서 보존: 4개');
    expect(wrapper.find('[data-test="board-delete-cancel"]').text()).toBe('취소');
    expect(wrapper.find('[data-test="board-delete-only"]').text()).toBe('보드만 삭제');
    expect(wrapper.find('[data-test="board-delete-with-pins"]').text())
      .toBe('보드와 전용 Pin 2개 삭제');
  });

  it('shows a localized selection error without exposing a server error code', async () => {
    API.Pin.fetchSelectionIds.mockRejectedValueOnce({
      response: { data: { code: 'internal_selection_detail' } },
    });
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    await settle();

    const error = wrapper.find('[data-test="board-delete-selection-error"]').text();
    expect(error).toBe('전용 Pin 목록을 불러오지 못했습니다.');
    expect(error).not.toContain('internal_selection_detail');
  });

  it('does not mutate pins or the board before a valid preview and confirmation', async () => {
    const preview = deferred();
    API.Board.deletePreview.mockReturnValueOnce(preview.promise);

    const wrapper = mountDialog();

    expect(wrapper.vm.phase).toBe('loading-preview');
    expect(API.Pin.fetchSelectionIds).not.toHaveBeenCalled();
    expect(API.Pin.bulk).not.toHaveBeenCalled();
    expect(API.Board.delete).not.toHaveBeenCalled();

    preview.resolve({
      data: {
        exclusive_owned_count: 2,
        shared_owned_count: 3,
        non_owned_count: 4,
      },
    });
    await settle();

    expect(wrapper.vm.phase).toBe('ready');
    expect(API.Pin.bulk).not.toHaveBeenCalled();
    expect(API.Board.delete).not.toHaveBeenCalled();
  });

  it('cancels from the ready phase without mutating pins or the board', async () => {
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-cancel"]').trigger('click');

    expect(API.Pin.fetchSelectionIds).not.toHaveBeenCalled();
    expect(API.Pin.bulk).not.toHaveBeenCalled();
    expect(API.Board.delete).not.toHaveBeenCalled();
  });

  it('emits closed before asking the modal parent to close on cancel', async () => {
    const order = [];
    const wrapper = mountDialog();
    wrapper.vm.$on('closed', () => order.push('closed'));
    wrapper.vm.$parent.close = jest.fn(() => order.push('parent-close'));
    await settle();

    await wrapper.find('[data-test="board-delete-cancel"]').trigger('click');

    expect(order).toEqual(['closed', 'parent-close']);
  });

  it('deletes only the board without fetching or mutating pins', async () => {
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-only"]').trigger('click');
    await settle();

    expect(API.Pin.fetchSelectionIds).not.toHaveBeenCalled();
    expect(API.Pin.bulk).not.toHaveBeenCalled();
    expect(API.Board.delete).toHaveBeenCalledTimes(1);
    expect(API.Board.delete).toHaveBeenCalledWith(7);
    expect(wrapper.emitted('completed')).toEqual([[7]]);
  });

  it('runs exclusive pin chunks sequentially before deleting the board', async () => {
    const firstChunk = deferred();
    const rows = selectionRows(51);
    API.Pin.fetchSelectionIds.mockResolvedValueOnce(selectionResponse(51, rows));
    API.Pin.bulk
      .mockReturnValueOnce(firstChunk.promise)
      .mockResolvedValueOnce(bulkResponse([1]));
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    await settle();

    expect(API.Pin.fetchSelectionIds).toHaveBeenCalledWith({
      boardId: 7, exclusiveOwned: true,
    });
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
    expect(API.Pin.bulk.mock.calls[0][0]).toEqual({
      operation: 'delete_if_exclusive_to_board',
      source_board_id: 7,
      pin_ids: rows.slice(0, 50).map(row => row.id),
    });
    expect(API.Board.delete).not.toHaveBeenCalled();

    firstChunk.resolve(bulkResponse(rows.slice(0, 50).map(row => row.id)));
    await settle();

    expect(API.Pin.bulk).toHaveBeenCalledTimes(2);
    expect(API.Pin.bulk.mock.calls[1][0].pin_ids).toEqual([1]);
    expect(API.Board.delete).toHaveBeenCalledTimes(1);
    expect(wrapper.vm.phase).toBe('completed');
  });

  it.each([
    ['missing count', { results: [{ id: 2, owned: true }] }],
    ['negative count', { count: -1, results: [] }],
    ['fractional count', { count: 1.5, results: [{ id: 2, owned: true }] }],
    ['count mismatch', { count: 2, results: [{ id: 2, owned: true }] }],
    ['duplicate ids', {
      count: 2,
      results: [{ id: 2, owned: true }, { id: 2, owned: true }],
    }],
    ['zero id', { count: 1, results: [{ id: 0, owned: true }] }],
    ['string id', { count: 1, results: [{ id: '2', owned: true }] }],
    ['non-exclusive row', { count: 1, results: [{ id: 2, owned: false }] }],
    ['extra row field', { count: 1, results: [{ id: 2, owned: true, name: 'secret' }] }],
    ['extra envelope field', { count: 0, results: [], next: null }],
  ])('rejects a malformed exclusive selection: %s', async (name, data) => {
    API.Pin.fetchSelectionIds.mockResolvedValueOnce({ data });
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    await settle();

    expect(wrapper.vm.phase).toBe('ready');
    expect(wrapper.find('[data-test="board-delete-selection-error"]').exists()).toBe(true);
    expect(API.Pin.bulk).not.toHaveBeenCalled();
    expect(API.Board.delete).not.toHaveBeenCalled();
  });

  it('disables exclusive deletion above 50000 while preserving board-only deletion', async () => {
    API.Board.deletePreview.mockResolvedValueOnce({
      data: {
        exclusive_owned_count: 50001,
        shared_owned_count: 0,
        non_owned_count: 0,
      },
    });
    const wrapper = mountDialog();
    await settle();

    const error = wrapper.find('[data-test="board-delete-selection-error"]');
    expect(error.exists()).toBe(true);
    expect(error.text())
      .toBe('전체 범위가 너무 커 현재 화면의 Pin만 선택할 수 있습니다.');
    expect(wrapper.find('[data-test="board-delete-with-pins"]').attributes('disabled'))
      .toBe('disabled');
    expect(wrapper.find('[data-test="board-delete-only"]').attributes('disabled'))
      .toBeUndefined();
    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    expect(API.Pin.fetchSelectionIds).not.toHaveBeenCalled();
    expect(API.Pin.bulk).not.toHaveBeenCalled();
    expect(API.Board.delete).not.toHaveBeenCalled();

    await wrapper.find('[data-test="board-delete-only"]').trigger('click');
    await settle();
    expect(API.Board.delete).toHaveBeenCalledWith(7);
  });

  it('clears an oversized preview warning after a later normal preview', async () => {
    API.Board.deletePreview
      .mockResolvedValueOnce({
        data: {
          exclusive_owned_count: 50001,
          shared_owned_count: 0,
          non_owned_count: 0,
        },
      })
      .mockResolvedValueOnce({
        data: {
          exclusive_owned_count: 2,
          shared_owned_count: 3,
          non_owned_count: 4,
        },
      });
    const wrapper = mountDialog();
    await settle();

    expect(wrapper.find('[data-test="board-delete-selection-error"]').exists()).toBe(true);

    await wrapper.vm.loadPreview();
    await settle();

    expect(wrapper.find('[data-test="board-delete-selection-error"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="board-delete-with-pins"]').attributes('disabled'))
      .toBeUndefined();
  });

  it('keeps board-only deletion available when selection grows beyond the server limit', async () => {
    API.Pin.fetchSelectionIds.mockRejectedValueOnce({
      response: { data: { code: 'selection_too_large' } },
    });
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    await settle();

    expect(wrapper.vm.phase).toBe('ready');
    expect(wrapper.find('[data-test="board-delete-with-pins"]').attributes('disabled'))
      .toBe('disabled');
    expect(wrapper.find('[data-test="board-delete-only"]').attributes('disabled'))
      .toBeUndefined();
    expect(API.Pin.bulk).not.toHaveBeenCalled();
    expect(API.Board.delete).not.toHaveBeenCalled();
  });

  it('fail-closes an oversized successful selection response', async () => {
    API.Pin.fetchSelectionIds.mockResolvedValueOnce(selectionResponse(50001));
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    await settle();

    expect(wrapper.vm.phase).toBe('ready');
    expect(wrapper.find('[data-test="board-delete-with-pins"]').attributes('disabled'))
      .toBe('disabled');
    expect(wrapper.find('[data-test="board-delete-only"]').attributes('disabled'))
      .toBeUndefined();
    expect(API.Pin.bulk).not.toHaveBeenCalled();
    expect(API.Board.delete).not.toHaveBeenCalled();
  });

  it('treats preserved pins as a successful boundary and shows preview and actual counts', async () => {
    API.Board.deletePreview.mockResolvedValueOnce({
      data: {
        exclusive_owned_count: 3,
        shared_owned_count: 4,
        non_owned_count: 5,
      },
    });
    API.Pin.fetchSelectionIds.mockResolvedValueOnce(selectionResponse(2));
    API.Pin.bulk.mockResolvedValueOnce(bulkResponse([2, 1], { 1: 'preserved' }));
    const wrapper = mountDialog();
    await settle();

    expect(wrapper.find('[data-test="board-delete-preview-exclusive"]').text()).toContain('3');
    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    await settle();

    expect(API.Board.delete).toHaveBeenCalledTimes(1);
    expect(wrapper.vm.actualExclusiveCount).toBe(2);
    expect(wrapper.vm.deleted).toBe(1);
    expect(wrapper.vm.preserved).toBe(1);
    expect(wrapper.find('[data-test="board-delete-actual-count"]').text()).toContain('2');
    expect(wrapper.find('[data-test="board-delete-result"]').text()).toContain('1');
  });

  it.each([
    ['a mismatched operation', {
      data: {
        ...bulkResponse([1]).data,
        operation: 'delete',
      },
    }],
    ['a status outside the conditional-delete contract', {
      data: {
        operation: 'delete_if_exclusive_to_board',
        succeeded: 1,
        preserved: 0,
        failed: 0,
        results: [{ id: 1, status: 'updated' }],
      },
    }],
  ])('keeps the board when bulk returns %s', async (name, response) => {
    API.Pin.fetchSelectionIds.mockResolvedValue(selectionResponse(1));
    API.Pin.bulk.mockResolvedValueOnce(response);
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    await settle();

    expect(wrapper.vm.phase).toBe('retrying-pins');
    expect(wrapper.vm.failedIds).toEqual([1]);
    expect(API.Board.delete).not.toHaveBeenCalled();
    expect(wrapper.find('[data-test="board-delete-retry"]').exists()).toBe(true);
  });

  it('re-fetches and intersects failed pins before exposing a manual retry', async () => {
    const rows = selectionRows(51);
    API.Pin.fetchSelectionIds
      .mockResolvedValueOnce(selectionResponse(51, rows))
      .mockResolvedValueOnce(selectionResponse(2, [
        { id: 1, owned: true },
        { id: 999, owned: true },
      ]));
    API.Pin.bulk
      .mockResolvedValueOnce(bulkResponse(rows.slice(0, 50).map(row => row.id)))
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce(bulkResponse([1]));
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    await settle();

    expect(wrapper.vm.phase).toBe('retrying-pins');
    expect(wrapper.vm.failedIds).toEqual([1]);
    expect(wrapper.vm.retryIds).toEqual([1]);
    expect(API.Pin.fetchSelectionIds).toHaveBeenNthCalledWith(2, {
      boardId: 7, exclusiveOwned: true,
    });
    expect(API.Pin.bulk).toHaveBeenCalledTimes(2);
    expect(API.Board.delete).not.toHaveBeenCalled();
    expect(wrapper.find('[data-test="board-delete-retry"]').exists()).toBe(true);

    await wrapper.find('[data-test="board-delete-retry"]').trigger('click');
    await settle();

    expect(API.Pin.bulk).toHaveBeenCalledTimes(3);
    expect(API.Pin.bulk.mock.calls[2][0].pin_ids).toEqual([1]);
    expect(API.Board.delete).toHaveBeenCalledTimes(1);
  });

  it('recovers a failed retry refresh only after an explicit refresh and pin retry', async () => {
    API.Pin.fetchSelectionIds
      .mockResolvedValueOnce(selectionResponse(1))
      .mockRejectedValueOnce(new Error('refresh failed'))
      .mockResolvedValueOnce(selectionResponse(1));
    API.Pin.bulk
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce(bulkResponse([1]));
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    await settle();

    expect(wrapper.vm.phase).toBe('failed-retry-refresh');
    expect(wrapper.find('[data-test="board-delete-retry-refresh"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="board-delete-close"]').exists()).toBe(true);
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
    expect(API.Board.delete).not.toHaveBeenCalled();

    await wrapper.find('[data-test="board-delete-retry-refresh"]').trigger('click');
    await settle();

    expect(wrapper.vm.phase).toBe('retrying-pins');
    expect(wrapper.vm.retryIds).toEqual([1]);
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);

    await wrapper.find('[data-test="board-delete-retry"]').trigger('click');
    await settle();

    expect(API.Pin.bulk).toHaveBeenCalledTimes(2);
    expect(API.Board.delete).toHaveBeenCalledTimes(1);
  });

  it('allows safe close when a retry refresh response is malformed', async () => {
    API.Pin.fetchSelectionIds
      .mockResolvedValueOnce(selectionResponse(1))
      .mockResolvedValueOnce({ data: { count: 1, results: [] } });
    API.Pin.bulk.mockRejectedValueOnce(new Error('network'));
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    await settle();

    expect(wrapper.vm.phase).toBe('failed-retry-refresh');
    expect(wrapper.find('[data-test="board-delete-retry-refresh"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="board-delete-close"]').exists()).toBe(true);
    await wrapper.find('[data-test="board-delete-close"]').trigger('click');

    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
    expect(API.Board.delete).not.toHaveBeenCalled();
  });

  it('completes when board lookup confirms a lost delete response already deleted it', async () => {
    API.Board.delete.mockRejectedValueOnce(new Error('response lost'));
    API.Board.get.mockRejectedValueOnce({ response: { status: 404 } });
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-only"]').trigger('click');
    await settle();

    expect(API.Board.get).toHaveBeenCalledWith(7);
    expect(wrapper.vm.phase).toBe('completed');
    expect(wrapper.emitted('completed')).toEqual([[7]]);
  });

  it.each([
    ['the board still exists', 'exists'],
    ['board lookup is inconclusive', 'unknown'],
  ])('keeps retry and safe close when %s after delete failure', async (name, outcome) => {
    API.Board.delete.mockRejectedValueOnce(new Error('delete failed'));
    if (outcome === 'unknown') {
      API.Board.get.mockRejectedValueOnce(new Error('lookup failed'));
    }
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-only"]').trigger('click');
    await settle();

    expect(wrapper.vm.phase).toBe('failed-board-delete');
    expect(wrapper.find('[data-test="board-delete-retry"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="board-delete-close"]').exists()).toBe(true);
    await wrapper.find('[data-test="board-delete-close"]').trigger('click');
    expect(API.Board.delete).toHaveBeenCalledTimes(1);
  });

  it('emits closed before closing a persistent board-delete failure', async () => {
    const order = [];
    API.Board.delete.mockRejectedValueOnce(new Error('delete failed'));
    const wrapper = mountDialog();
    wrapper.vm.$on('closed', () => order.push('closed'));
    wrapper.vm.$parent.close = jest.fn(() => order.push('parent-close'));
    await settle();
    await wrapper.find('[data-test="board-delete-only"]').trigger('click');
    await settle();

    await wrapper.find('[data-test="board-delete-close"]').trigger('click');

    expect(order).toEqual(['closed', 'parent-close']);
  });

  it('retries only the board call after exclusive pins were deleted', async () => {
    API.Pin.fetchSelectionIds.mockResolvedValueOnce(selectionResponse(1));
    API.Pin.bulk.mockResolvedValueOnce(bulkResponse([1]));
    API.Board.delete
      .mockRejectedValueOnce(new Error('network'))
      .mockResolvedValueOnce({ status: 204 });
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="board-delete-with-pins"]').trigger('click');
    await settle();

    expect(wrapper.vm.phase).toBe('failed-board-delete');
    expect(API.Pin.fetchSelectionIds).toHaveBeenCalledTimes(1);
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
    expect(API.Board.delete).toHaveBeenCalledTimes(1);

    await wrapper.find('[data-test="board-delete-retry"]').trigger('click');
    await settle();

    expect(API.Pin.fetchSelectionIds).toHaveBeenCalledTimes(1);
    expect(API.Pin.bulk).toHaveBeenCalledTimes(1);
    expect(API.Board.delete).toHaveBeenCalledTimes(2);
    expect(wrapper.emitted('completed')).toEqual([[7]]);
  });

  it('ignores late preview and deletion callbacks after destruction', async () => {
    const preview = deferred();
    API.Board.deletePreview.mockReturnValueOnce(preview.promise);
    const loading = mountDialog();
    loading.destroy();
    preview.resolve({
      data: {
        exclusive_owned_count: 2,
        shared_owned_count: 3,
        non_owned_count: 4,
      },
    });
    await settle();
    expect(loading.vm.phase).toBe('loading-preview');

    const deletion = deferred();
    API.Board.deletePreview.mockResolvedValueOnce({
      data: {
        exclusive_owned_count: 0,
        shared_owned_count: 0,
        non_owned_count: 0,
      },
    });
    API.Board.delete.mockReturnValueOnce(deletion.promise);
    const deleting = mountDialog();
    await settle();
    deleting.vm.deleteBoardOnly();
    deleting.destroy();
    deletion.resolve({ status: 204 });
    await settle();

    expect(deleting.vm.phase).toBe('deleting-board');
    expect(deleting.emitted('completed')).toBeUndefined();
  });
});
