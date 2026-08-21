/* eslint-env jest */

import axios from 'axios';

import API from '@/components/api';
import { executeBulk, intersectRemaining } from '@/components/bulk/bulkExecutor';

jest.mock('axios');

function itemFor(id, status) {
  if (status === 'preserved') {
    return { id, status, code: 'shared_pin' };
  }
  if (status === 'failed') {
    return {
      id, status, code: 'internal_error', retryable: false,
    };
  }
  return { id, status };
}

function resultFor(ids, status = 'updated', operation = 'update') {
  const results = ids.map(id => itemFor(id, status));
  return {
    operation,
    succeeded: results.filter(item => !['preserved', 'failed'].includes(item.status)).length,
    preserved: results.filter(item => item.status === 'preserved').length,
    failed: results.filter(item => item.status === 'failed').length,
    results,
  };
}

function responseFor(ids, status = 'updated', operation = 'update') {
  return { status: 200, data: resultFor(ids, status, operation) };
}

function withoutKey(value, key) {
  const copy = { ...value };
  delete copy[key];
  return copy;
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
  return 'deleted';
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
      .mockResolvedValueOnce(responseFor(
        ids.slice(0, 50), 'updated', 'add_to_board',
      ))
      .mockResolvedValueOnce(responseFor([51], 'updated', 'add_to_board'));

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
      .mockResolvedValueOnce(responseFor([51]));

    const execution = executeBulk({ ids, operation: 'update', request });

    expect(request).toHaveBeenCalledTimes(1);
    first.resolve(responseFor(ids.slice(0, 50)));
    await execution;
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('reports committed progress and aggregates succeeded, preserved, and failed results', async () => {
    const ids = Array.from({ length: 430 }, (_, index) => index + 1);
    const progress = jest.fn();
    const request = jest.fn((payload) => {
      const results = payload.pin_ids.map(
        (id, index) => itemFor(id, statusForIndex(index)),
      );
      return Promise.resolve({
        status: 200,
        data: {
          operation: 'delete_if_exclusive_to_board',
          succeeded: results.filter(item => item.status === 'deleted').length,
          preserved: results.filter(item => item.status === 'preserved').length,
          failed: results.filter(item => item.status === 'failed').length,
          results,
        },
      });
    });

    const result = await executeBulk({
      ids, operation: 'delete_if_exclusive_to_board', request, onProgress: progress,
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
    const request = jest.fn().mockResolvedValue(responseFor([1, 2], 'deleted', 'delete'));
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

  it.each([
    [
      'delete',
      ['deleted', 'failed'],
      {
        completed: 2, succeeded: 1, preserved: 0, failed: 1,
      },
    ],
    [
      'delete_if_exclusive_to_board',
      ['deleted', 'preserved', 'failed'],
      {
        completed: 3, succeeded: 1, preserved: 1, failed: 1,
      },
    ],
    [
      'add_to_board',
      ['updated', 'unchanged'],
      {
        completed: 2, succeeded: 2, preserved: 0, failed: 0,
      },
    ],
    [
      'move_between_boards',
      ['moved', 'unchanged'],
      {
        completed: 2, succeeded: 2, preserved: 0, failed: 0,
      },
    ],
    [
      'update',
      ['updated'],
      {
        completed: 1, succeeded: 1, preserved: 0, failed: 0,
      },
    ],
  ])('accepts only the documented result set for %s', async (operation, statuses, expected) => {
    const ids = statuses.map((status, index) => index + 1);
    const results = ids.map((id, index) => itemFor(id, statuses[index]));
    const request = jest.fn().mockResolvedValue({
      status: 200,
      data: {
        operation,
        succeeded: results.filter(
          item => !['preserved', 'failed'].includes(item.status),
        ).length,
        preserved: results.filter(item => item.status === 'preserved').length,
        failed: results.filter(item => item.status === 'failed').length,
        results,
      },
    });

    const result = await executeBulk({ ids, operation, request });

    expect(result).toMatchObject({ ...expected, remainingIds: [] });
    expect(result.error).toBeUndefined();
  });

  it.each([
    ['delete', 'preserved'],
    ['delete_if_exclusive_to_board', 'updated'],
    ['add_to_board', 'moved'],
    ['move_between_boards', 'deleted'],
    ['update', 'unchanged'],
  ])('rejects status %s does not allow: %s', async (operation, status) => {
    const progress = jest.fn();
    const request = jest.fn().mockResolvedValue({
      status: 200,
      data: {
        operation,
        succeeded: 1,
        preserved: 0,
        failed: 0,
        results: [{ id: 1, status }],
      },
    });

    const result = await executeBulk({
      ids: [1], operation, request, onProgress: progress,
    });

    expect(result).toMatchObject({
      completed: 0,
      error: 'invalid_bulk_response',
      remainingIds: [1],
    });
    expect(progress).not.toHaveBeenCalled();
  });

  it.each([
    ['missing', undefined],
    ['mismatched', 'update'],
  ])('rejects a %s response operation before committing progress', async (name, responseOperation) => {
    const progress = jest.fn();
    const request = jest.fn().mockResolvedValue({
      status: 200,
      data: {
        operation: responseOperation,
        succeeded: 1,
        preserved: 0,
        failed: 0,
        results: [{ id: 1, status: 'deleted' }],
      },
    });

    const result = await executeBulk({
      ids: [1], operation: 'delete', request, onProgress: progress,
    });

    expect(result).toMatchObject({
      completed: 0,
      error: 'invalid_bulk_response',
      remainingIds: [1],
    });
    expect(progress).not.toHaveBeenCalled();
  });

  const ids = Array.from({ length: 51 }, (_, index) => index + 1);
  const chunk = ids.slice(0, 50);
  const validDelete = () => responseFor(chunk, 'deleted', 'delete');
  const validPreserved = () => responseFor(
    chunk, 'preserved', 'delete_if_exclusive_to_board',
  );
  const validFailed = () => responseFor(chunk, 'failed', 'delete');
  const invalidResponses = [
    ['missing response', undefined],
    ['missing HTTP status', { data: validDelete().data }],
    ['non-200 HTTP status', { ...validDelete(), status: 201 }],
    ...['operation', 'succeeded', 'preserved', 'failed', 'results'].map(key => [
      `missing envelope key ${key}`,
      { status: 200, data: withoutKey(validDelete().data, key) },
    ]),
    ['extra envelope key', {
      status: 200,
      data: { ...validDelete().data, detail: 'unsafe' },
    }],
    ...['succeeded', 'preserved', 'failed'].flatMap(key => [
      [`negative ${key} count`, {
        status: 200, data: { ...validDelete().data, [key]: -1 },
      }],
      [`fractional ${key} count`, {
        status: 200, data: { ...validDelete().data, [key]: 0.5 },
      }],
      [`mismatched ${key} count`, {
        status: 200,
        data: {
          ...validDelete().data,
          [key]: validDelete().data[key] === 0 ? 1 : 0,
        },
      }],
    ]),
    ...['id', 'status'].map(key => [
      `successful result missing ${key}`,
      {
        status: 200,
        data: {
          ...validDelete().data,
          results: [
            withoutKey(validDelete().data.results[0], key),
            ...validDelete().data.results.slice(1),
          ],
        },
      },
    ]),
    ['successful result with extra key', {
      status: 200,
      data: {
        ...validDelete().data,
        results: [
          { ...validDelete().data.results[0], code: 'internal_error' },
          ...validDelete().data.results.slice(1),
        ],
      },
    }],
    ...['id', 'status', 'code'].map(key => [
      `preserved result missing ${key}`,
      {
        status: 200,
        data: {
          ...validPreserved().data,
          results: [
            withoutKey(validPreserved().data.results[0], key),
            ...validPreserved().data.results.slice(1),
          ],
        },
      },
      'delete_if_exclusive_to_board',
    ]),
    ['preserved result with extra key', {
      status: 200,
      data: {
        ...validPreserved().data,
        results: [
          { ...validPreserved().data.results[0], retryable: false },
          ...validPreserved().data.results.slice(1),
        ],
      },
    }, 'delete_if_exclusive_to_board'],
    ['preserved result with unknown code', {
      status: 200,
      data: {
        ...validPreserved().data,
        results: [
          { ...validPreserved().data.results[0], code: 'unknown_reason' },
          ...validPreserved().data.results.slice(1),
        ],
      },
    }, 'delete_if_exclusive_to_board'],
    ['preserved result with failed-item code', {
      status: 200,
      data: {
        ...validPreserved().data,
        results: [
          { ...validPreserved().data.results[0], code: 'database_busy' },
          ...validPreserved().data.results.slice(1),
        ],
      },
    }, 'delete_if_exclusive_to_board'],
    ...['id', 'status', 'code', 'retryable'].map(key => [
      `failed result missing ${key}`,
      {
        status: 200,
        data: {
          ...validFailed().data,
          results: [
            withoutKey(validFailed().data.results[0], key),
            ...validFailed().data.results.slice(1),
          ],
        },
      },
    ]),
    ['failed result with extra key', {
      status: 200,
      data: {
        ...validFailed().data,
        results: [
          { ...validFailed().data.results[0], detail: 'unsafe' },
          ...validFailed().data.results.slice(1),
        ],
      },
    }],
    ['failed result with non-boolean retryable', {
      status: 200,
      data: {
        ...validFailed().data,
        results: [
          { ...validFailed().data.results[0], retryable: 0 },
          ...validFailed().data.results.slice(1),
        ],
      },
    }],
    ['failed database_busy result marked non-retryable', {
      status: 200,
      data: {
        ...validFailed().data,
        results: [
          {
            ...validFailed().data.results[0],
            code: 'database_busy',
            retryable: false,
          },
          ...validFailed().data.results.slice(1),
        ],
      },
    }],
    ['failed internal_error result marked retryable', {
      status: 200,
      data: {
        ...validFailed().data,
        results: [
          { ...validFailed().data.results[0], retryable: true },
          ...validFailed().data.results.slice(1),
        ],
      },
    }],
    ['failed result with unknown code', {
      status: 200,
      data: {
        ...validFailed().data,
        results: [
          { ...validFailed().data.results[0], code: 'unknown_failure' },
          ...validFailed().data.results.slice(1),
        ],
      },
    }],
    ['failed result with preserved-item code', {
      status: 200,
      data: {
        ...validFailed().data,
        results: [
          { ...validFailed().data.results[0], code: 'shared_pin' },
          ...validFailed().data.results.slice(1),
        ],
      },
    }],
  ];

  it.each(invalidResponses)(
    'rejects %s without committing its current or unstarted chunks',
    async (name, response, operation = 'delete') => {
      const progress = jest.fn();
      const request = jest.fn().mockResolvedValue(response);

      const result = await executeBulk({
        ids, operation, request, onProgress: progress,
      });

      expect(result).toMatchObject({
        completed: 0,
        succeeded: 0,
        preserved: 0,
        failed: 0,
        error: 'invalid_bulk_response',
        remainingIds: ids,
      });
      expect(progress).not.toHaveBeenCalled();
      expect(request).toHaveBeenCalledTimes(1);
    },
  );

  it.each(['shared_pin', 'non_owned_pin', 'source_membership_changed'])(
    'accepts the documented preserved code %s',
    async (code) => {
      const response = responseFor([1], 'preserved', 'delete_if_exclusive_to_board');
      response.data.results[0].code = code;

      const result = await executeBulk({
        ids: [1],
        operation: 'delete_if_exclusive_to_board',
        request: jest.fn().mockResolvedValue(response),
      });

      expect(result).toMatchObject({ completed: 1, preserved: 1, remainingIds: [] });
      expect(result.error).toBeUndefined();
    },
  );

  it.each([
    ['database_busy', true],
    ['internal_error', false],
    ['bulk_deadline_exceeded', true],
  ])('accepts the documented failed code %s', async (code, retryable) => {
    const response = responseFor([1], 'failed', 'delete');
    response.data.results[0] = {
      id: 1, status: 'failed', code, retryable,
    };

    const result = await executeBulk({
      ids: [1], operation: 'delete', request: jest.fn().mockResolvedValue(response),
    });

    expect(result).toMatchObject({
      completed: 1, failed: 1, failedIds: [1], remainingIds: [],
    });
    expect(result.error).toBeUndefined();
  });
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
