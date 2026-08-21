/* eslint-env jest */

import axios from 'axios';

import API from '@/components/api';
import { executeBulk, intersectRemaining } from '@/components/bulk/bulkExecutor';

jest.mock('axios');

function resultFor(ids, status = 'updated') {
  return {
    operation: 'update',
    succeeded: ids.length,
    preserved: 0,
    failed: 0,
    results: ids.map(id => ({ id, status })),
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((resolveRequest) => {
    resolve = resolveRequest;
  });
  return { promise, resolve };
}

function statusForIndex(index) {
  if (index === 0) return 'preserved';
  if (index === 1) return 'failed';
  return 'updated';
}

describe('bulk API adapter', () => {
  afterEach(() => jest.clearAllMocks());

  it('omits exclusive_owned when a board ID is not selected', () => {
    API.Pin.fetchSelectionIds({ exclusiveOwned: true });

    expect(axios.get).toHaveBeenCalledWith('/api/v2/pins/selection-ids/', {
      params: {},
    });
  });

  it('sends only board_id for a board-only selection', () => {
    API.Pin.fetchSelectionIds({ boardId: 7 });

    expect(axios.get).toHaveBeenCalledWith('/api/v2/pins/selection-ids/', {
      params: { board_id: 7 },
    });
  });

  it('uses exact selection and bulk request URLs and params', () => {
    API.Pin.fetchSelectionIds();
    API.Pin.fetchSelectionIds({ boardId: 7, exclusiveOwned: true });
    API.Pin.bulk({ operation: 'delete', pin_ids: [4] });
    API.Board.deletePreview(9);

    expect(axios.get).toHaveBeenNthCalledWith(
      1,
      '/api/v2/pins/selection-ids/',
      { params: {} },
    );
    expect(axios.get).toHaveBeenNthCalledWith(
      2,
      '/api/v2/pins/selection-ids/',
      { params: { board_id: 7, exclusive_owned: 'true' } },
    );
    expect(axios.post).toHaveBeenCalledWith('/api/v2/pins/bulk/', {
      operation: 'delete', pin_ids: [4],
    });
    expect(axios.get).toHaveBeenLastCalledWith('/api/v2/boards/9/delete-preview/');
  });
});

describe('executeBulk', () => {
  it('posts 51 ids as ordered 50 and 1 chunks', async () => {
    const ids = Array.from({ length: 51 }, (_, index) => index + 1);
    const request = jest.fn()
      .mockResolvedValueOnce({ data: resultFor(ids.slice(0, 50)) })
      .mockResolvedValueOnce({ data: resultFor([51]) });

    const result = await executeBulk({
      ids,
      operation: 'add_to_board',
      fields: { board_id: 7 },
      request,
      onProgress: jest.fn(),
    });

    expect(request.mock.calls[0][0]).toEqual({
      operation: 'add_to_board', pin_ids: ids.slice(0, 50), board_id: 7,
    });
    expect(request.mock.calls[1][0]).toEqual({
      operation: 'add_to_board', pin_ids: [51], board_id: 7,
    });
    expect(result).toMatchObject({ completed: 51, succeeded: 51, remainingIds: [] });
  });

  it('waits for a committed chunk before posting the next chunk', async () => {
    const ids = Array.from({ length: 51 }, (_, index) => index + 1);
    const first = deferred();
    const request = jest.fn()
      .mockReturnValueOnce(first.promise)
      .mockResolvedValueOnce({ data: resultFor([51]) });

    const execution = executeBulk({ ids, operation: 'update', request });

    expect(request).toHaveBeenCalledTimes(1);
    first.resolve({ data: resultFor(ids.slice(0, 50)) });
    await execution;
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('reports committed progress and aggregates succeeded, preserved, and failed results', async () => {
    const ids = Array.from({ length: 430 }, (_, index) => index + 1);
    const progress = jest.fn();
    const request = jest.fn(payload => Promise.resolve({
      data: {
        results: payload.pin_ids.map((id, index) => ({
          id,
          status: statusForIndex(index),
        })),
      },
    }));

    const result = await executeBulk({
      ids, operation: 'delete', request, onProgress: progress,
    });

    expect(progress).toHaveBeenCalledWith({
      completed: 150, total: 430, succeeded: 144, preserved: 3, failed: 3,
    });
    expect(result).toMatchObject({
      completed: 430, succeeded: 412, preserved: 9, failed: 9, remainingIds: [],
    });
    expect(result.failedIds).toEqual([2, 52, 102, 152, 202, 252, 302, 352, 402]);
  });

  it('stops on a mismatched server ID set without committing the current chunk', async () => {
    const request = jest.fn().mockResolvedValue({
      data: resultFor([1, 2]),
    });
    const progress = jest.fn();

    const result = await executeBulk({
      ids: [1, 2, 3], operation: 'delete', request, onProgress: progress,
    });

    expect(result).toMatchObject({
      completed: 0,
      error: 'invalid_bulk_response',
      remainingIds: [1, 2, 3],
    });
    expect(progress).not.toHaveBeenCalled();
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('does not retry an ambiguous delete request and retains its current and unstarted ids', async () => {
    const ids = Array.from({ length: 51 }, (_, index) => index + 1);
    const request = jest.fn().mockRejectedValue(new Error('network'));

    const result = await executeBulk({ ids, operation: 'delete', request });

    expect(request).toHaveBeenCalledTimes(1);
    expect(result).toMatchObject({
      completed: 0, error: 'request_failed', remainingIds: ids,
    });
  });

  it.each(['add_to_board', 'move_between_boards', 'update'])(
    'leaves %s retries to a later explicit execution',
    async (operation) => {
      const request = jest.fn().mockRejectedValue(new Error('network'));

      const result = await executeBulk({ ids: [1, 2], operation, request });

      expect(request).toHaveBeenCalledTimes(1);
      expect(result).toMatchObject({ error: 'request_failed', remainingIds: [1, 2] });
    },
  );
});

describe('intersectRemaining', () => {
  it('keeps original order while retaining only refreshed IDs', () => {
    expect(intersectRemaining([8, 7, 6, 5], [
      { id: 5, owned: true },
      { id: 7, owned: true },
      { id: 9, owned: true },
    ])).toEqual([7, 5]);
  });
});
