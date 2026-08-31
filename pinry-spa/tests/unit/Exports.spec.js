/* eslint-env jest */

import flushPromises from 'flush-promises';
import { mount } from '@vue/test-utils';

import API from '@/components/api';
import modals from '@/components/modals';
import Exports from '@/views/Exports.vue';

const JOB_ID = '123e4567-e89b-42d3-a456-426614174000';
const UTC = '2026-08-30T12:34:56Z';
let wrappers = [];

function deferred() {
  const request = {};
  request.promise = new Promise((resolve, reject) => {
    request.resolve = resolve;
    request.reject = reject;
  });
  request.promise.catch(() => {});
  return request;
}

function job(overrides = {}) {
  const state = overrides.state || 'archiving';
  return {
    schema_version: 1,
    id: JOB_ID,
    state,
    phase_label: '압축 중',
    scope: 'pins',
    phase_percent: 40,
    overall_percent: 40,
    counters: {
      requested_total: 2,
      target_total: 2,
      snapshot_done: 2,
      archive_total: 2,
      archive_done: 1,
      included_total: 2,
      excluded_total: 0,
      bytes_total: 100,
      bytes_done: 40,
    },
    created_at: UTC,
    snapshot_at: UTC,
    heartbeat_at: UTC,
    completed_at: state === 'complete' ? UTC : null,
    expires_at: null,
    resume_count: 0,
    error: null,
    download_url: state === 'complete' ? `/api/v2/exports/${JOB_ID}/download/` : null,
    ...overrides,
  };
}

function latest(latestAttempt = job(), downloadableJob = null) {
  return {
    schema_version: 1,
    latest_attempt: latestAttempt,
    downloadable_job: downloadableJob,
  };
}

function mountView() {
  const wrapper = mount(Exports, {
    mocks: {
      $buefy: { modal: { open: jest.fn() } },
      $t: (key, values) => (values ? `${key}:${JSON.stringify(values)}` : key),
    },
  });
  wrappers.push(wrapper);
  return wrapper;
}

async function settle() {
  await flushPromises();
  await flushPromises();
}

describe('Exports view', () => {
  beforeEach(() => {
    jest.useFakeTimers();
    wrappers = [];
    API.Export.fetchLatest = jest.fn();
  });

  afterEach(() => {
    wrappers.forEach(wrapper => wrapper.destroy());
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('requests the exact latest envelope immediately and polls an active job', async () => {
    API.Export.fetchLatest
      .mockResolvedValueOnce(latest())
      .mockResolvedValueOnce(latest(job({ overall_percent: 41 })));
    const wrapper = mountView();

    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(1);
    await settle();
    expect(wrapper.find('[data-test="export-latest-card"]').exists()).toBe(true);
    jest.advanceTimersByTime(1999);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(1);
    jest.advanceTimersByTime(1);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(2);
  });

  it('stops terminal periodic polling but allows a manual refresh', async () => {
    const complete = job({ state: 'complete', overall_percent: 100 });
    API.Export.fetchLatest.mockResolvedValue(latest(complete, complete));
    const wrapper = mountView();
    await settle();

    jest.advanceTimersByTime(30000);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(1);
    await wrapper.find('[data-test="export-refresh"]').trigger('click');
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(2);
  });

  it('keeps the last good cards during a transient request failure', async () => {
    API.Export.fetchLatest
      .mockResolvedValueOnce(latest())
      .mockRejectedValueOnce(new Error('/private/transport-detail'));
    const wrapper = mountView();
    await settle();

    jest.advanceTimersByTime(2000);
    await settle();

    expect(wrapper.find('[data-test="export-latest-card"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="export-status-error"]').text())
      .toBe('exportStatusTemporaryError');
    expect(wrapper.html()).not.toContain('/private/transport-detail');
  });

  it('treats an initial exact 401 as login-required and restarts after login', async () => {
    API.Export.fetchLatest
      .mockRejectedValueOnce({
        response: { status: 401, data: { detail: '/private/session' } },
      })
      .mockResolvedValueOnce(latest(null, null));
    const openLogin = jest.spyOn(modals, 'openLogin').mockImplementation(() => {});
    const wrapper = mountView();
    await settle();

    expect(wrapper.find('[data-test="export-login-required"]').text())
      .toContain('exportLoginRequired');
    expect(wrapper.find('[data-test="exports-empty"]').exists()).toBe(false);
    expect(wrapper.html()).not.toContain('/private/session');
    jest.advanceTimersByTime(30000);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(1);

    await wrapper.find('[data-test="export-login"]').trigger('click');
    expect(openLogin).toHaveBeenCalledTimes(1);
    const onSucceed = openLogin.mock.calls[0][1];
    onSucceed();
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(2);
    await settle();
    expect(wrapper.find('[data-test="exports-empty"]').exists()).toBe(true);
  });

  it('stops on an exact contract error and manual recheck starts a fresh request', async () => {
    API.Export.fetchLatest
      .mockRejectedValueOnce(new Error('invalid_export_contract'))
      .mockResolvedValueOnce(latest());
    const wrapper = mountView();
    await settle();

    expect(wrapper.find('[data-test="export-contract-error"]').text())
      .toBe('exportContractError');
    jest.advanceTimersByTime(30000);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(1);

    await wrapper.find('[data-test="export-refresh"]').trigger('click');
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(2);
    await settle();
    expect(wrapper.find('[data-test="export-latest-card"]').exists()).toBe(true);
  });

  it('ignores a late response after route instance destruction', async () => {
    const request = deferred();
    API.Export.fetchLatest.mockReturnValue(request.promise);
    const wrapper = mountView();
    wrapper.destroy();
    request.resolve(latest());
    await settle();

    expect(wrapper.vm.latestEnvelope).toBeNull();
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(1);
  });

  it('stops active polling after route instance destruction', async () => {
    API.Export.fetchLatest.mockResolvedValue(latest());
    const wrapper = mountView();
    await settle();
    wrapper.destroy();

    jest.advanceTimersByTime(30000);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(1);
  });

  it('clears an armed expiry timer after route instance destruction', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(Date.parse(UTC));
    const complete = job({
      state: 'complete', overall_percent: 100, expires_at: '2026-08-30T12:35:01Z',
    });
    API.Export.fetchLatest.mockResolvedValue(latest(complete, complete));
    const wrapper = mountView();
    await settle();
    wrapper.destroy();

    jest.advanceTimersByTime(30000);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(1);
    now.mockRestore();
  });

  it('refreshes once at expiry and does not rearm a fired identical key', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(Date.parse(UTC));
    const complete = job({
      state: 'complete',
      overall_percent: 100,
      expires_at: '2026-08-30T12:35:01Z',
    });
    const envelope = latest(complete, complete);
    API.Export.fetchLatest.mockResolvedValue(envelope);
    mountView();
    await settle();

    jest.advanceTimersByTime(4999);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(1);
    jest.advanceTimersByTime(1);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(2);
    await settle();
    jest.advanceTimersByTime(30000);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(2);
    now.mockRestore();
  });

  it('clears and rearms expiry only when the downloadable key changes', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(Date.parse(UTC));
    const first = job({
      state: 'complete', overall_percent: 100, expires_at: '2026-08-30T12:35:01Z',
    });
    const replacement = job({
      id: '223e4567-e89b-42d3-a456-426614174000',
      state: 'complete',
      overall_percent: 100,
      expires_at: '2026-08-30T12:35:06Z',
      download_url: '/api/v2/exports/223e4567-e89b-42d3-a456-426614174000/download/',
    });
    API.Export.fetchLatest
      .mockResolvedValueOnce(latest(first, first))
      .mockResolvedValueOnce(latest(replacement, replacement))
      .mockResolvedValueOnce(latest(replacement, replacement));
    const wrapper = mountView();
    await settle();

    await wrapper.find('[data-test="export-refresh"]').trigger('click');
    await settle();
    jest.advanceTimersByTime(5000);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(2);
    jest.advanceTimersByTime(5000);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(3);
    now.mockRestore();
  });

  it('clears expiry when the downloadable job disappears', async () => {
    const now = jest.spyOn(Date, 'now').mockReturnValue(Date.parse(UTC));
    const complete = job({
      state: 'complete', overall_percent: 100, expires_at: '2026-08-30T12:35:01Z',
    });
    API.Export.fetchLatest
      .mockResolvedValueOnce(latest(complete, complete))
      .mockResolvedValueOnce(latest(null, null));
    const wrapper = mountView();
    await settle();

    await wrapper.find('[data-test="export-refresh"]').trigger('click');
    await settle();
    jest.advanceTimersByTime(30000);
    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(2);
    now.mockRestore();
  });

  it('refreshes an active view when it becomes visible', async () => {
    const hidden = Object.getOwnPropertyDescriptor(document, 'hidden');
    Object.defineProperty(document, 'hidden', { configurable: true, value: false });
    try {
      API.Export.fetchLatest.mockResolvedValue(latest());
      mountView();
      await settle();

      document.dispatchEvent(new Event('visibilitychange'));
      expect(API.Export.fetchLatest).toHaveBeenCalledTimes(2);
    } finally {
      if (hidden) Object.defineProperty(document, 'hidden', hidden);
      else delete document.hidden;
    }
  });

  it('announces semantic progress changes without repeating heartbeat-only updates', async () => {
    API.Export.fetchLatest
      .mockResolvedValueOnce(latest(job({ overall_percent: 40.8 })))
      .mockResolvedValueOnce(latest(job({
        overall_percent: 40.8,
        heartbeat_at: '2026-08-30T12:35:00Z',
      })))
      .mockResolvedValueOnce(latest(job({
        overall_percent: 40.8,
        phase_label: '권한 변경 반영 후 다시 압축 중',
      })))
      .mockResolvedValueOnce(latest(job({ overall_percent: 41.1 })));
    const wrapper = mountView();
    await settle();
    const live = wrapper.find('[data-test="export-announcement"]');
    expect(live.attributes()).toMatchObject({
      'aria-live': 'polite',
      'aria-atomic': 'true',
    });
    let mutationCount = 0;
    const observer = new MutationObserver(() => {
      mutationCount += 1;
    });
    observer.observe(live.element, { childList: true, characterData: true, subtree: true });

    await wrapper.find('[data-test="export-refresh"]').trigger('click');
    await settle();
    expect(mutationCount).toBe(0);

    await wrapper.find('[data-test="export-refresh"]').trigger('click');
    await settle();
    expect(mutationCount).toBeGreaterThan(0);
    const afterPhaseChange = mutationCount;

    await wrapper.find('[data-test="export-refresh"]').trigger('click');
    await settle();
    expect(mutationCount).toBeGreaterThan(afterPhaseChange);
    observer.disconnect();
  });

  it.each([
    ['complete', 'exportStateComplete'],
    ['failed', 'exportStateFailed'],
  ])('announces a transition to terminal %s', async (state, stateKey) => {
    API.Export.fetchLatest
      .mockResolvedValueOnce(latest())
      .mockResolvedValueOnce(latest(job({
        state,
        phase_label: state,
        completed_at: UTC,
        overall_percent: state === 'complete' ? 100 : 40,
        download_url: state === 'complete'
          ? `/api/v2/exports/${JOB_ID}/download/` : null,
      })));
    const wrapper = mountView();
    await settle();

    await wrapper.find('[data-test="export-refresh"]').trigger('click');
    await settle();
    expect(wrapper.find('[data-test="export-announcement"]').text()).toContain(stateKey);
  });
});
