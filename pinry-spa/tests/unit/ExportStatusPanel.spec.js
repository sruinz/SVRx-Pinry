/* eslint-env jest */

import { mount } from '@vue/test-utils';

import ExportStatusPanel from '@/components/export/ExportStatusPanel.vue';

const JOB_ID = '123e4567-e89b-42d3-a456-426614174000';
const PREVIOUS_JOB_ID = '223e4567-e89b-42d3-a456-426614174000';
const UTC = '2026-08-30T12:34:56Z';

function job(overrides = {}) {
  const id = overrides.id || JOB_ID;
  const state = overrides.state || 'archiving';
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
      snapshot_done: 7,
      archive_total: 6,
      archive_done: 3,
      included_total: 6,
      excluded_total: 2,
      bytes_total: 1234,
      bytes_done: 456,
    },
    created_at: UTC,
    snapshot_at: UTC,
    heartbeat_at: UTC,
    completed_at: state === 'complete' ? UTC : null,
    expires_at: state === 'complete' ? '2026-08-31T12:34:56Z' : null,
    resume_count: 2,
    error: null,
    download_url: state === 'complete' ? `/api/v2/exports/${id}/download/` : null,
    ...overrides,
  };
}

function translate(key, values) {
  if (!values) return key;
  return `${key}:${Object.entries(values).map(([name, value]) => `${name}=${value}`).join(',')}`;
}

function mountPanel(latestAttempt, downloadableJob = null) {
  return mount(ExportStatusPanel, {
    global: { mocks: { $t: translate } },
    props: { latestAttempt, downloadableJob },
  });
}

describe('ExportStatusPanel', () => {
  it.each([
    ['queued', 'exportStateQueued'],
    ['snapshotting', 'exportStateSnapshotting'],
    ['archiving', 'exportStateArchiving'],
    ['verifying', 'exportStateVerifying'],
    ['complete', 'exportStateComplete'],
    ['failed', 'exportStateFailed'],
    ['expired', 'exportStateExpired'],
  ])('renders the validated %s state and server phase as text', (state, stateKey) => {
    const wrapper = mountPanel(job({
      state,
      phase_label: `${state} phase`,
      completed_at: ['complete', 'failed', 'expired'].includes(state) ? UTC : null,
      expires_at: state === 'complete' ? '2026-08-31T12:34:56Z' : null,
      download_url: state === 'complete' ? `/api/v2/exports/${JOB_ID}/download/` : null,
    }));

    expect(wrapper.find('[data-test="export-state"]').text()).toContain(stateKey);
    expect(wrapper.find('[data-test="export-phase"]').text()).toContain(`${state} phase`);
  });

  it('explains that a queued export is waiting for the worker', () => {
    const wrapper = mountPanel(job({
      state: 'queued',
      phase_label: '대기 중',
      phase_percent: null,
      overall_percent: null,
    }));

    expect(wrapper.find('[data-test="export-worker-waiting"]').text())
      .toBe('exportWorkerWaiting');
  });

  it('reflects a server progress rollback from 80 to 40', async () => {
    const wrapper = mountPanel(job({ overall_percent: 80 }));

    expect(wrapper.find('[data-test="export-progress"]').attributes('aria-valuenow'))
      .toBe('80');
    await wrapper.setProps({ latestAttempt: job({ overall_percent: 40 }) });

    const progress = wrapper.find('[data-test="export-progress"]');
    expect(progress.attributes()).toMatchObject({
      role: 'progressbar',
      'aria-valuemin': '0',
      'aria-valuemax': '100',
      'aria-valuenow': '40',
      'aria-valuetext': 'exportProgress:percent=40',
    });
    expect(progress.text()).toContain('exportProgress:percent=40');
  });

  it('does not misrepresent unknown progress as zero', () => {
    const wrapper = mountPanel(job({ phase_percent: null, overall_percent: null }));
    const progress = wrapper.find('[data-test="export-progress"]');

    expect(progress.attributes('aria-valuenow')).toBeUndefined();
    expect(progress.attributes('aria-valuetext')).toBe('exportProgressUnknown');
    expect(progress.text()).toContain('exportProgressUnknown');
    expect(progress.text()).not.toContain('0%');
  });

  it('renders heartbeat age, resume count, included, excluded, and byte counters', () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(Date.parse(UTC) + 65000);
    const wrapper = mountPanel(job());

    expect(wrapper.find('[data-test="export-heartbeat"]').text())
      .toContain('exportHeartbeat:seconds=65');
    expect(wrapper.find('[data-test="export-resume-count"]').text())
      .toContain('exportResumeCount:count=2');
    expect(wrapper.find('[data-test="export-included-total"]').text())
      .toContain('exportIncludedTotal:count=6');
    expect(wrapper.find('[data-test="export-excluded-total"]').text())
      .toContain('exportExcludedTotal:count=2');
    expect(wrapper.find('[data-test="export-bytes"]').text())
      .toContain('exportBytesProgress:done=456,total=1234');
    now.mockRestore();
  });

  it('shows a safe retryable failure and keeps server strings text-only', () => {
    const wrapper = mountPanel(job({
      state: 'failed',
      phase_label: '<img src=x onerror=alert(1)>',
      completed_at: UTC,
      overall_percent: 40,
      error: {
        code: 'retry_later_with_a_very_long_safe_code',
        class: 'retryable',
        retryable: true,
        message: '<script>not executable</script>',
      },
    }));

    expect(wrapper.find('[data-test="export-error-code"]').text())
      .toContain('retry_later_with_a_very_long_safe_code');
    expect(wrapper.find('[data-test="export-error-message"]').text())
      .toContain('<script>not executable</script>');
    expect(wrapper.find('[data-test="export-retryable"]').text()).toBe('exportRetryableHint');
    expect(wrapper.find('script').exists()).toBe(false);
    expect(wrapper.find('img').exists()).toBe(false);
  });

  it('uses the validated canonical download URL on an ordinary anchor', () => {
    const complete = job({ state: 'complete', overall_percent: 100 });
    const wrapper = mountPanel(complete, complete);
    const download = wrapper.find('[data-test="export-download"]');

    expect(download.element.tagName).toBe('A');
    expect(download.attributes('href')).toBe(`/api/v2/exports/${JOB_ID}/download/`);
    expect(download.attributes('tabindex')).toBeUndefined();
    expect(wrapper.find('[data-test="export-previous-card"]').exists()).toBe(false);
  });

  it('keeps a different previous downloadable job as a separate expiring card', () => {
    const previous = job({
      id: PREVIOUS_JOB_ID,
      state: 'complete',
      phase_label: '완료',
      overall_percent: 100,
      expires_at: '2026-09-01T01:02:03Z',
    });
    const wrapper = mountPanel(job({
      state: 'failed',
      completed_at: UTC,
      error: {
        code: 'archive_failed',
        class: 'retryable',
        retryable: true,
        message: '다시 시도할 수 있습니다.',
      },
    }), previous);

    expect(wrapper.findAll('[data-test$="-card"]')).toHaveLength(2);
    expect(wrapper.find('[data-test="export-previous-card"]').text())
      .toContain('exportPreviousTitle');
    expect(wrapper.find('[data-test="export-previous-expires"]').text())
      .toContain('2026-09-01T01:02:03Z');
    const download = wrapper.find('[data-test="export-previous-download"]');
    expect(download.element.tagName).toBe('A');
    expect(download.attributes('href'))
      .toBe(`/api/v2/exports/${PREVIOUS_JOB_ID}/download/`);
  });

  it('keeps narrow-screen content in wrap-friendly semantic groups', () => {
    const wrapper = mountPanel(job({ state: 'complete', overall_percent: 100 }));

    expect(wrapper.classes()).toContain('export-status-panel');
    expect(wrapper.find('[data-test="export-latest-card"]').classes())
      .toContain('export-status-card');
    expect(wrapper.find('[data-test="export-counters"]').classes())
      .toContain('export-counters');
    expect(wrapper.find('[data-test="export-actions"]').classes())
      .toContain('export-actions');
  });
});
