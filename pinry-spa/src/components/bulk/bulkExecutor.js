const CHUNK_SIZE = 50;
const OPERATION_STATUSES = {
  delete: new Set(['deleted', 'failed']),
  delete_if_exclusive_to_board: new Set(['deleted', 'preserved', 'failed']),
  add_to_board: new Set(['updated', 'unchanged']),
  move_between_boards: new Set(['moved', 'unchanged']),
  update: new Set(['updated']),
};
const ENVELOPE_FIELDS = [
  'operation', 'succeeded', 'preserved', 'failed', 'results',
];
const SUCCESS_FIELDS = ['id', 'status'];
const PRESERVED_FIELDS = ['id', 'status', 'code'];
const FAILED_FIELDS = ['id', 'status', 'code', 'retryable'];
const PRESERVED_CODES = new Set([
  'shared_pin', 'non_owned_pin', 'source_membership_changed',
]);
const FAILED_RETRYABILITY = {
  database_busy: true,
  internal_error: false,
  bulk_deadline_exceeded: true,
};

function hasExactFields(value, fields) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  return keys.length === fields.length && fields.every(field => keys.includes(field));
}

function hasValidItemSchema(item) {
  if (item.status === 'preserved') {
    return hasExactFields(item, PRESERVED_FIELDS) && PRESERVED_CODES.has(item.code);
  }
  if (item.status === 'failed') {
    return hasExactFields(item, FAILED_FIELDS)
      && Object.prototype.hasOwnProperty.call(FAILED_RETRYABILITY, item.code)
      && typeof item.retryable === 'boolean'
      && item.retryable === FAILED_RETRYABILITY[item.code];
  }
  return hasExactFields(item, SUCCESS_FIELDS);
}

function initialResult(ids) {
  return {
    total: ids.length,
    completed: 0,
    succeeded: 0,
    preserved: 0,
    failed: 0,
    failedIds: [],
    remainingIds: [],
  };
}

function validResults(response, ids, operation) {
  const allowedStatuses = OPERATION_STATUSES[operation];
  if (
    !response
    || response.status !== 200
    || !hasExactFields(response.data, ENVELOPE_FIELDS)
    || response.data.operation !== operation
    || !allowedStatuses
    || !Array.isArray(response.data.results)
  ) {
    return null;
  }

  const { results } = response.data;
  if (results.length !== ids.length) return null;

  const expectedIds = new Set(ids);
  const receivedIds = new Set();
  const counts = { succeeded: 0, preserved: 0, failed: 0 };
  const isValid = results.every((item) => {
    if (
      !item
      || !Number.isInteger(item.id)
      || !expectedIds.has(item.id)
      || receivedIds.has(item.id)
      || !allowedStatuses.has(item.status)
      || !hasValidItemSchema(item)
    ) {
      return false;
    }
    receivedIds.add(item.id);
    if (item.status === 'preserved') {
      counts.preserved += 1;
    } else if (item.status === 'failed') {
      counts.failed += 1;
    } else {
      counts.succeeded += 1;
    }
    return true;
  });

  const validCounts = ['succeeded', 'preserved', 'failed'].every(
    key => Number.isInteger(response.data[key])
      && response.data[key] >= 0
      && response.data[key] === counts[key],
  );
  return isValid && validCounts && receivedIds.size === expectedIds.size
    ? results
    : null;
}

function commitResults(summary, results) {
  const committed = {
    ...summary,
    failedIds: [...summary.failedIds],
  };
  results.forEach((item) => {
    committed.completed += 1;
    if (item.status === 'preserved') {
      committed.preserved += 1;
    } else if (item.status === 'failed') {
      committed.failed += 1;
      committed.failedIds.push(item.id);
    } else {
      committed.succeeded += 1;
    }
  });
  return committed;
}

export function executeBulk({
  ids,
  operation,
  fields = {},
  request,
  onProgress = () => {},
}) {
  let summary = initialResult(ids);

  function executeChunk(start) {
    if (start >= ids.length) return Promise.resolve(summary);
    const chunk = ids.slice(start, start + CHUNK_SIZE);
    let pending;
    try {
      // 순차 실행은 앞선 묶음의 완료 뒤에만 다음 요청을 보낸다.
      pending = request({ ...fields, operation, pin_ids: chunk });
    } catch (_error) {
      summary.error = 'request_failed';
      summary.remainingIds = ids.slice(start);
      return Promise.resolve(summary);
    }

    return Promise.resolve(pending).then(
      (response) => {
        const results = validResults(response, chunk, operation);
        if (results === null) {
          summary.error = 'invalid_bulk_response';
          summary.remainingIds = ids.slice(start);
          return summary;
        }

        summary = commitResults(summary, results);
        onProgress({
          completed: summary.completed,
          total: summary.total,
          succeeded: summary.succeeded,
          preserved: summary.preserved,
          failed: summary.failed,
        });
        return executeChunk(start + CHUNK_SIZE);
      },
      () => {
        summary.error = 'request_failed';
        summary.remainingIds = ids.slice(start);
        return summary;
      },
    );
  }

  return executeChunk(0);
}

export function intersectRemaining(originalIds, refreshedRows) {
  const refreshedIds = new Set(
    refreshedRows
      .filter(row => row && Number.isInteger(row.id))
      .map(row => row.id),
  );
  return originalIds.filter(id => refreshedIds.has(id));
}
