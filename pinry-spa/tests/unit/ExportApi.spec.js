/* eslint-env jest */
import axios from 'axios';
import API from '@/components/api';

jest.mock('axios');

const JOB_ID = '123e4567-e89b-42d3-a456-426614174000';

function makeJob() {
  return {
    schema_version: 1,
    id: JOB_ID,
    state: 'queued',
    phase_label: '대기 중',
    scope: 'pins',
    phase_percent: null,
    overall_percent: 0,
    counters: {
      requested_total: 1,
      target_total: 1,
      snapshot_done: 0,
      archive_total: 0,
      archive_done: 0,
      included_total: 0,
      excluded_total: 0,
      bytes_total: 0,
      bytes_done: 0,
    },
    created_at: '2026-08-30T12:34:56Z',
    snapshot_at: null,
    heartbeat_at: null,
    completed_at: null,
    expires_at: null,
    resume_count: 0,
    error: null,
    download_url: null,
  };
}

function makeLatest() {
  return { schema_version: 1, latest_attempt: null, downloadable_job: null };
}

describe('Export API boundary', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('uses the exact export URLs, methods, payloads, and has no download method', async () => {
    const previewPayload = { scope: 'board', board_id: 17 };
    const createPayload = { scope: 'pins', pin_ids: [1, 2] };
    axios.post.mockResolvedValue({ status: 202 });
    axios.get.mockResolvedValueOnce({ data: makeLatest() }).mockResolvedValueOnce({ data: makeJob() });

    await API.Export.preview(previewPayload);
    await API.Export.create(createPayload);
    await API.Export.fetchLatest();
    await API.Export.fetchJob(JOB_ID);

    expect(axios.post.mock.calls).toEqual([
      ['/api/v2/exports/preview/', previewPayload],
      ['/api/v2/exports/', createPayload],
    ]);
    expect(axios.get.mock.calls).toEqual([
      ['/api/v2/exports/latest/'],
      [`/api/v2/exports/${JOB_ID}/`],
    ]);
    expect(API.Export.download).toBeUndefined();
  });

  it('preserves raw responses only for preview and create but returns validated data for reads', async () => {
    const previewResponse = { status: 200, data: { schema_version: 1 } };
    const createResponse = { status: 202, data: { id: JOB_ID } };
    const latest = makeLatest();
    const job = makeJob();
    axios.post.mockResolvedValueOnce(previewResponse).mockResolvedValueOnce(createResponse);
    axios.get.mockResolvedValueOnce({ data: latest }).mockResolvedValueOnce({ data: job });

    expect(await API.Export.preview({ scope: 'pins' })).toBe(previewResponse);
    expect(await API.Export.create({ scope: 'pins' })).toBe(createResponse);
    expect(await API.Export.fetchLatest()).toBe(latest);
    expect(await API.Export.fetchJob(JOB_ID)).toBe(job);
  });

  it('fails closed at the API boundary without leaking malformed response data', async () => {
    const latest = makeLatest();
    latest.debug_path = '/data/exports/secret';
    const job = makeJob();
    delete job.phase_label;
    axios.get.mockResolvedValueOnce({ data: latest }).mockResolvedValueOnce({ data: job });

    await expect(API.Export.fetchLatest()).rejects.toThrow('invalid_export_contract');
    await expect(API.Export.fetchJob(JOB_ID)).rejects.toThrow('invalid_export_contract');
  });
});
