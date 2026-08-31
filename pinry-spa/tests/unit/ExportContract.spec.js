/* eslint-env jest */
import {
  validateExportPreview,
  validateExportCreate,
  validateExportJob,
  validateLatestExports,
} from '@/components/export/exportContract';

const JOB_ID = '123e4567-e89b-42d3-a456-426614174000';
const OTHER_JOB_ID = '223e4567-e89b-42d3-a456-426614174000';
const UTC = '2026-08-30T12:34:56.123456Z';

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function makePreview() {
  return {
    schema_version: 1,
    scope: 'board',
    as_of: UTC,
    requested_total: 8,
    eligible_total: 7,
    excluded_total: 1,
    owned_private_total: 2,
    estimated_original_files: 7,
    estimated_original_bytes: 1234,
    estimated_zip_bytes: 987,
  };
}

function makeCreate() {
  return {
    schema_version: 1,
    id: JOB_ID,
    state: 'queued',
    scope: 'pins',
    requested_total: 8,
    target_total: 7,
    excluded_total: 1,
    status_url: `/api/v2/exports/${JOB_ID}/`,
  };
}

function makeJob(overrides = {}) {
  const id = overrides.id || JOB_ID;
  const state = overrides.state || 'archiving';
  const downloadUrl = state === 'complete'
    ? `/api/v2/exports/${id}/download/`
    : null;
  return {
    schema_version: 1,
    id,
    state,
    phase_label: '압축 중',
    scope: 'pins',
    phase_percent: 37.4,
    overall_percent: 40,
    counters: {
      requested_total: 8,
      target_total: 7,
      snapshot_done: 6,
      archive_total: 7,
      archive_done: 3,
      included_total: 5,
      excluded_total: 1,
      bytes_total: 1234,
      bytes_done: 456,
    },
    created_at: UTC,
    snapshot_at: null,
    heartbeat_at: '2026-08-30T12:34:56Z',
    completed_at: null,
    expires_at: null,
    resume_count: 0,
    error: null,
    download_url: downloadUrl,
    ...overrides,
  };
}

function makeLatest() {
  return {
    schema_version: 1,
    latest_attempt: makeJob({
      state: 'failed',
      error: {
        code: 'export_storage_unsafe',
        class: 'fatal',
        retryable: false,
        message: '안전한 내보내기 저장소를 확인할 수 없습니다.',
      },
    }),
    downloadable_job: makeJob({ state: 'complete' }),
  };
}

function expectInvalid(call) {
  expect(call).toThrow('invalid_export_contract');
  try {
    call();
  } catch (error) {
    expect(error.message).toBe('invalid_export_contract');
    expect(String(error)).toBe('Error: invalid_export_contract');
  }
}

describe('export exact response contract', () => {
  it('accepts the four complete literal envelopes without copying them', () => {
    const preview = makePreview();
    const create = makeCreate();
    const job = makeJob();
    const latest = makeLatest();

    expect(validateExportPreview(preview)).toBe(preview);
    expect(validateExportCreate(create)).toBe(create);
    expect(validateExportJob(job)).toBe(job);
    expect(validateLatestExports(latest)).toBe(latest);
  });

  it('rejects every missing or extra key at each exact object level', () => {
    const cases = [
      [validateExportPreview, makePreview()],
      [validateExportCreate, makeCreate()],
      [validateExportJob, makeJob()],
      [validateLatestExports, makeLatest()],
    ];

    cases.forEach(([validate, input]) => {
      Object.keys(input).forEach((key) => {
        const missing = clone(input);
        delete missing[key];
        expectInvalid(() => validate(missing));
      });
      const extra = clone(input);
      extra.debug_path = '/data/exports/secret';
      expectInvalid(() => validate(extra));
    });

    const missingCounter = makeJob();
    delete missingCounter.counters.bytes_done;
    expectInvalid(() => validateExportJob(missingCounter));
    const extraCounter = makeJob();
    extraCounter.counters.debug_path = '/data/exports/secret';
    expectInvalid(() => validateExportJob(extraCounter));

    const missingError = makeJob({
      error: {
        code: 'retry_later', class: 'retryable', retryable: true,
      },
    });
    expectInvalid(() => validateExportJob(missingError));
    const extraError = makeJob({
      error: {
        code: 'retry_later', class: 'retryable', retryable: true, message: '다시 시도', debug_path: '/data/exports/secret',
      },
    });
    expectInvalid(() => validateExportJob(extraError));

    const inherited = makePreview();
    const prototype = { scope: inherited.scope };
    delete inherited.scope;
    Object.setPrototypeOf(inherited, prototype);
    expectInvalid(() => validateExportPreview(inherited));
  });

  it('enforces number boundaries while allowing documented percent values', () => {
    ['requested_total', 'estimated_original_bytes'].forEach((key) => {
      [true, '1', -1, 1.5, NaN, Infinity].forEach((value) => {
        const preview = makePreview();
        preview[key] = value;
        expectInvalid(() => validateExportPreview(preview));
      });
    });
    [true, '0', -1, 1.5, NaN, Infinity].forEach((value) => {
      const job = makeJob();
      job.resume_count = value;
      expectInvalid(() => validateExportJob(job));
    });
    [true, NaN, Infinity, -0.1, 100.1].forEach((value) => {
      const job = makeJob();
      job.phase_percent = value;
      expectInvalid(() => validateExportJob(job));
    });
    [0, 37.4, 100, null].forEach((value) => {
      const job = makeJob();
      job.phase_percent = value;
      expect(validateExportJob(job)).toBe(job);
    });
  });

  it('enforces enums, types, and the exact UTC date representation', () => {
    [['state', 'running'], ['scope', 'all']].forEach(([key, value]) => {
      const job = makeJob();
      job[key] = value;
      expectInvalid(() => validateExportJob(job));
    });
    const badClass = makeJob({
      error: {
        code: 'retry_later', class: 'unknown', retryable: true, message: '다시 시도',
      },
    });
    expectInvalid(() => validateExportJob(badClass));
    [['retryable', 'true'], ['code', 1], ['message', false]].forEach(([key, value]) => {
      const job = makeJob({
        error: {
          code: 'retry_later', class: 'retryable', retryable: true, message: '다시 시도',
        },
      });
      job.error[key] = value;
      expectInvalid(() => validateExportJob(job));
    });
    const label = makeJob();
    label.phase_label = 10;
    expectInvalid(() => validateExportJob(label));
    ['2026-08-30T12:34:56+09:00', '2026-08-30', '2026-02-30T12:34:56Z', '2026-08-30T12:34:56.123Z'].forEach((date) => {
      const job = makeJob();
      job.created_at = date;
      expectInvalid(() => validateExportJob(job));
    });
    const nullableDates = makeJob();
    ['created_at', 'snapshot_at', 'heartbeat_at', 'completed_at', 'expires_at'].forEach((key) => {
      nullableDates[key] = null;
    });
    expect(validateExportJob(nullableDates)).toBe(nullableDates);
    const preview = makePreview();
    preview.as_of = null;
    expectInvalid(() => validateExportPreview(preview));
  });

  it('binds UUID paths and downloadability to the same job and state', () => {
    const noncanonical = makeCreate();
    noncanonical.id = JOB_ID.toUpperCase();
    noncanonical.status_url = `/api/v2/exports/${noncanonical.id}/`;
    expectInvalid(() => validateExportCreate(noncanonical));
    const otherStatus = makeCreate();
    otherStatus.status_url = `/api/v2/exports/${OTHER_JOB_ID}/`;
    expectInvalid(() => validateExportCreate(otherStatus));
    [`/api/v2/exports/${JOB_ID}/?debug=1`, `https://example.test/api/v2/exports/${JOB_ID}/`, `/api/v2/exports/${JOB_ID}/#part`].forEach((url) => {
      const create = makeCreate();
      create.status_url = url;
      expectInvalid(() => validateExportCreate(create));
    });
    const activeDownload = makeJob();
    activeDownload.download_url = `/api/v2/exports/${JOB_ID}/download/`;
    expectInvalid(() => validateExportJob(activeDownload));
    const completeWithoutDownload = makeJob({ state: 'complete' });
    completeWithoutDownload.download_url = null;
    expectInvalid(() => validateExportJob(completeWithoutDownload));
    const otherDownload = makeJob({ state: 'complete' });
    otherDownload.download_url = `/api/v2/exports/${OTHER_JOB_ID}/download/`;
    expectInvalid(() => validateExportJob(otherDownload));
    const latest = makeLatest();
    latest.downloadable_job = makeJob();
    expectInvalid(() => validateLatestExports(latest));
  });

  it('never leaks invalid server values through its error', () => {
    const job = makeJob();
    job.download_url = '/data/exports/secret';
    expectInvalid(() => validateExportJob(job));
  });
});
