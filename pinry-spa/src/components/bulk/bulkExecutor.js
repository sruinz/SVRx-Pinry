const CHUNK_SIZE = 50;
const SUCCESS_STATUSES = new Set(['deleted', 'updated', 'moved', 'unchanged']);
const VALID_STATUSES = new Set([...SUCCESS_STATUSES, 'preserved', 'failed']);

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

function validResults(response, ids) {
  if (!response || !response.data || !Array.isArray(response.data.results)) {
    return null;
  }

  const { results } = response.data;
  if (results.length !== ids.length) return null;

  const expectedIds = new Set(ids);
  const receivedIds = new Set();
  const isValid = results.every((item) => {
    if (
      !item
      || !Number.isInteger(item.id)
      || !expectedIds.has(item.id)
      || receivedIds.has(item.id)
      || !VALID_STATUSES.has(item.status)
    ) {
      return false;
    }
    receivedIds.add(item.id);
    return true;
  });

  return isValid && receivedIds.size === expectedIds.size ? results : null;
}

function commitResults(summary, results) {
  const committed = {
    ...summary,
    failedIds: [...summary.failedIds],
  };
  results.forEach((item) => {
    committed.completed += 1;
    if (SUCCESS_STATUSES.has(item.status)) {
      committed.succeeded += 1;
    } else if (item.status === 'preserved') {
      committed.preserved += 1;
    } else {
      committed.failed += 1;
      committed.failedIds.push(item.id);
    }
  });
  return committed;
}

export async function executeBulk({
  ids,
  operation,
  fields = {},
  request,
  onProgress = () => {},
}) {
  let summary = initialResult(ids);

  for (let start = 0; start < ids.length; start += CHUNK_SIZE) {
    const chunk = ids.slice(start, start + CHUNK_SIZE);
    let response;
    try {
      // 순차 실행은 앞선 묶음의 완료 뒤에만 다음 요청을 보낸다.
      // eslint-disable-next-line no-await-in-loop
      response = await request({ ...fields, operation, pin_ids: chunk });
    } catch (error) {
      summary.error = 'request_failed';
      summary.remainingIds = ids.slice(start);
      return summary;
    }

    const results = validResults(response, chunk);
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
  }

  return summary;
}

export function intersectRemaining(originalIds, refreshedRows) {
  const refreshedIds = new Set(
    refreshedRows
      .filter(row => row && Number.isInteger(row.id))
      .map(row => row.id),
  );
  return originalIds.filter(id => refreshedIds.has(id));
}
