const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const UTC_PATTERN = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{6})?Z$/;
const SCOPES = ['board', 'pins'];
const STATES = ['queued', 'snapshotting', 'archiving', 'verifying', 'complete', 'failed', 'expired'];
const ERROR_CLASSES = ['retryable', 'operator_action_required', 'fatal'];

function invalid() {
  throw new Error('invalid_export_contract');
}

function hasExactKeys(value, keys) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every(key => actual.includes(key));
}

function isIntegerAtLeastZero(value) {
  return typeof value === 'number' && Number.isFinite(value)
    && Number.isInteger(value) && value >= 0;
}

function isPercent(value) {
  return value === null || (typeof value === 'number' && Number.isFinite(value)
    && value >= 0 && value <= 100);
}

function isUtcDate(value) {
  if (typeof value !== 'string') return false;
  const match = UTC_PATTERN.exec(value);
  if (!match) return false;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return false;
  return date.getUTCFullYear() === Number(match[1])
    && date.getUTCMonth() + 1 === Number(match[2])
    && date.getUTCDate() === Number(match[3])
    && date.getUTCHours() === Number(match[4])
    && date.getUTCMinutes() === Number(match[5])
    && date.getUTCSeconds() === Number(match[6]);
}

function isNullableUtcDate(value) {
  return value === null || isUtcDate(value);
}

function isUuid(value) {
  return typeof value === 'string' && UUID_PATTERN.test(value);
}

function isOneOf(value, values) {
  return values.includes(value);
}

function validateCounts(value, keys) {
  return keys.every(key => isIntegerAtLeastZero(value[key]));
}

function validateError(value) {
  if (value === null) return true;
  return hasExactKeys(value, ['code', 'class', 'retryable', 'message'])
    && typeof value.code === 'string'
    && isOneOf(value.class, ERROR_CLASSES)
    && typeof value.retryable === 'boolean'
    && typeof value.message === 'string';
}

export function validateExportPreview(value) {
  const countKeys = [
    'requested_total', 'eligible_total', 'excluded_total', 'owned_private_total',
    'estimated_original_files', 'estimated_original_bytes', 'estimated_zip_bytes',
  ];
  if (!hasExactKeys(value, ['schema_version', 'scope', 'as_of', ...countKeys])
    || value.schema_version !== 1 || !isOneOf(value.scope, SCOPES)
    || !isUtcDate(value.as_of) || !validateCounts(value, countKeys)) invalid();
  return value;
}

export function validateExportCreate(value) {
  const countKeys = ['requested_total', 'target_total', 'excluded_total'];
  if (!hasExactKeys(value, ['schema_version', 'id', 'state', 'scope', ...countKeys, 'status_url'])
    || value.schema_version !== 1 || !isUuid(value.id) || value.state !== 'queued'
    || !isOneOf(value.scope, SCOPES) || !validateCounts(value, countKeys)
    || value.status_url !== `/api/v2/exports/${value.id}/`) invalid();
  return value;
}

export function validateExportJob(value) {
  const counterKeys = [
    'requested_total', 'target_total', 'snapshot_done', 'archive_total', 'archive_done',
    'included_total', 'excluded_total', 'bytes_total', 'bytes_done',
  ];
  const dateKeys = ['created_at', 'snapshot_at', 'heartbeat_at', 'completed_at', 'expires_at'];
  const keys = [
    'schema_version', 'id', 'state', 'phase_label', 'scope', 'phase_percent', 'overall_percent',
    'counters', ...dateKeys, 'resume_count', 'error', 'download_url',
  ];
  const expectedDownloadUrl = `/api/v2/exports/${value && value.id}/download/`;
  const downloadIsValid = value && (value.state === 'complete'
    ? value.download_url === expectedDownloadUrl
    : value.download_url === null);
  if (!hasExactKeys(value, keys) || value.schema_version !== 1 || !isUuid(value.id)
    || !isOneOf(value.state, STATES) || typeof value.phase_label !== 'string'
    || !isOneOf(value.scope, SCOPES) || !isPercent(value.phase_percent)
    || !isPercent(value.overall_percent) || !hasExactKeys(value.counters, counterKeys)
    || !validateCounts(value.counters, counterKeys) || !dateKeys.every(key => isNullableUtcDate(value[key]))
    || !isIntegerAtLeastZero(value.resume_count) || !validateError(value.error) || !downloadIsValid) invalid();
  return value;
}

export function validateLatestExports(value) {
  if (!hasExactKeys(value, ['schema_version', 'latest_attempt', 'downloadable_job'])
    || value.schema_version !== 1) invalid();
  if (value.latest_attempt !== null) validateExportJob(value.latest_attempt);
  if (value.downloadable_job !== null) {
    validateExportJob(value.downloadable_job);
    if (value.downloadable_job.state !== 'complete') invalid();
  }
  return value;
}
