/* eslint-env jest */

import flushPromises from 'flush-promises';
import { createLocalVue, mount } from '@vue/test-utils';
import VueRouter from 'vue-router';

import API from '@/components/api';
import ExportDialog from '@/components/export/ExportDialog.vue';
import router from '@/router';

const routerPush = jest.spyOn(router, 'push').mockImplementation(() => Promise.resolve());

const JOB_ID = '123e4567-e89b-42d3-a456-426614174000';
const UTC = '2026-08-30T12:34:56.123456Z';

function deferred() {
  const request = {};
  request.promise = new Promise((resolve, reject) => {
    request.resolve = resolve;
    request.reject = reject;
  });
  request.promise.catch(() => {});
  return request;
}

function preview(overrides = {}) {
  return {
    schema_version: 1,
    scope: 'pins',
    as_of: UTC,
    requested_total: 3,
    eligible_total: 2,
    excluded_total: 1,
    owned_private_total: 1,
    estimated_original_files: 2,
    estimated_original_bytes: 1234,
    estimated_zip_bytes: 987,
    ...overrides,
  };
}

function created(overrides = {}) {
  return {
    schema_version: 1,
    id: JOB_ID,
    state: 'queued',
    scope: 'pins',
    requested_total: 3,
    target_total: 2,
    excluded_total: 1,
    status_url: `/api/v2/exports/${JOB_ID}/`,
    ...overrides,
  };
}

function job(overrides = {}) {
  return {
    schema_version: 1,
    id: JOB_ID,
    state: 'queued',
    phase_label: '대기 중',
    scope: 'pins',
    phase_percent: null,
    overall_percent: 0,
    counters: {
      requested_total: 3,
      target_total: 2,
      snapshot_done: 0,
      archive_total: 0,
      archive_done: 0,
      included_total: 0,
      excluded_total: 1,
      bytes_total: 0,
      bytes_done: 0,
    },
    created_at: UTC,
    snapshot_at: null,
    heartbeat_at: null,
    completed_at: null,
    expires_at: null,
    resume_count: 0,
    error: null,
    download_url: null,
    ...overrides,
  };
}

function latest() {
  return {
    schema_version: 1,
    latest_attempt: job(),
    downloadable_job: null,
  };
}

function mountDialog(propsData = { pinIds: [9, 4] }) {
  const close = jest.fn();
  const toast = { open: jest.fn() };
  const localVue = createLocalVue();
  localVue.use(VueRouter);
  const wrapper = mount(ExportDialog, {
    localVue,
    router,
    propsData,
    mocks: {
      $buefy: { toast },
      $t: (key, values) => (values ? `${key}:${JSON.stringify(values)}` : key),
    },
  });
  wrapper.vm.$parent.close = close;
  wrapper.push = routerPush;
  wrapper.close = close;
  wrapper.toast = toast;
  return wrapper;
}

async function settle() {
  await flushPromises();
  await flushPromises();
}

describe('ExportDialog', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    API.Export.preview = jest.fn();
    API.Export.create = jest.fn();
    API.Export.fetchLatest = jest.fn();
  });

  it('snapshots the Pin payload and renders every validated preview field', async () => {
    const pinIds = [9, 4];
    API.Export.preview.mockResolvedValue({ data: preview() });
    const wrapper = mountDialog({ pinIds });
    pinIds.push(99);
    await settle();

    expect(API.Export.preview).toHaveBeenCalledTimes(1);
    expect(API.Export.preview).toHaveBeenCalledWith({ scope: 'pins', pin_ids: [9, 4] });
    expect(Object.isFrozen(wrapper.vm.requestPayload)).toBe(true);
    expect(Object.isFrozen(wrapper.vm.requestPayload.pin_ids)).toBe(true);
    expect(wrapper.find('[data-test="export-as-of"]').text()).toContain(UTC);
    expect(wrapper.find('[data-test="export-eligible-total"]').text()).toContain('2');
    expect(wrapper.find('[data-test="export-excluded-total"]').text()).toContain('1');
    expect(wrapper.find('[data-test="export-owned-private-total"]').text()).toContain('1');
    expect(wrapper.find('[data-test="export-original-files"]').text()).toContain('2');
    expect(wrapper.find('[data-test="export-original-bytes"]').text()).toContain('1234');
    expect(wrapper.find('[data-test="export-zip-bytes"]').text()).toContain('987');
    expect(wrapper.find('[data-test="export-confirm"]').attributes('disabled')).toBeUndefined();
  });

  it('uses an immutable board payload and registers one named route before the wildcard', async () => {
    API.Export.preview.mockResolvedValue({ data: preview({ scope: 'board' }) });
    const wrapper = mountDialog({ boardId: 17 });
    await settle();

    expect(API.Export.preview).toHaveBeenCalledWith({ scope: 'board', board_id: 17 });
    expect(Object.isFrozen(wrapper.vm.requestPayload)).toBe(true);
    const { routes } = router.options;
    expect(routes.filter(route => route.name === 'exports')).toHaveLength(1);
    expect(routes.findIndex(route => route.name === 'exports'))
      .toBeLessThan(routes.findIndex(route => route.path === '*'));
    expect(router.resolve({ name: 'exports' }).route.path).toBe('/exports');
  });

  it.each([
    ['neither target', {}],
    ['both targets', { boardId: 7, pinIds: [9] }],
    ['non-positive board', { boardId: 0 }],
    ['unsafe board', { boardId: Number.MAX_SAFE_INTEGER + 1 }],
    ['empty Pins', { pinIds: [] }],
    ['invalid Pin', { pinIds: [9, 0] }],
  ])('fails closed without an API call for %s', async (_name, propsData) => {
    const wrapper = mountDialog(propsData);
    await settle();

    expect(API.Export.preview).not.toHaveBeenCalled();
    expect(API.Export.create).not.toHaveBeenCalled();
    expect(wrapper.find('[data-test="export-error"]').text()).toBe('exportErrorGeneric');
    expect(wrapper.find('[data-test="export-confirm"]').attributes('disabled'))
      .toBe('disabled');
  });

  it('submits once, validates the create response, then navigates and closes immediately', async () => {
    const createRequest = deferred();
    API.Export.preview.mockResolvedValue({ data: preview() });
    API.Export.create.mockReturnValue(createRequest.promise);
    const wrapper = mountDialog({ pinIds: [9, 4] });
    await settle();

    await wrapper.find('[data-test="export-confirm"]').trigger('click');
    await wrapper.find('[data-test="export-confirm"]').trigger('click');
    expect(API.Export.create).toHaveBeenCalledTimes(1);
    expect(API.Export.create).toHaveBeenCalledWith({ scope: 'pins', pin_ids: [9, 4] });

    createRequest.resolve({ data: created({ target_total: 1, excluded_total: 2 }) });
    await settle();

    expect(wrapper.push).toHaveBeenCalledWith({ name: 'exports' });
    expect(wrapper.close).toHaveBeenCalledTimes(1);
    expect(wrapper.push.mock.calls[0][0]).toEqual({ name: 'exports' });
    expect(wrapper.find('[data-test="export-final-counts"]').exists()).toBe(false);
  });

  it('does not navigate when the create exact contract is invalid', async () => {
    API.Export.preview.mockResolvedValue({ data: preview() });
    API.Export.create.mockResolvedValue({ data: { ...created(), debug_path: '/secret' } });
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="export-confirm"]').trigger('click');
    await settle();

    expect(wrapper.find('[data-test="export-error"]').text()).toBe('exportErrorGeneric');
    expect(wrapper.html()).not.toContain('/secret');
    expect(wrapper.push).not.toHaveBeenCalled();
    expect(wrapper.close).not.toHaveBeenCalled();
  });

  it('does not display or confirm an invalid preview contract', async () => {
    API.Export.preview.mockResolvedValue({
      data: { ...preview(), debug_path: '/data/exports/private' },
    });
    const wrapper = mountDialog();
    await settle();

    expect(wrapper.find('[data-test="export-preview"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="export-error"]').text()).toBe('exportErrorGeneric');
    expect(wrapper.html()).not.toContain('/data/exports/private');
    expect(wrapper.find('[data-test="export-confirm"]').attributes('disabled'))
      .toBe('disabled');
    expect(API.Export.create).not.toHaveBeenCalled();
  });

  it('shows the fixed login message when preview is unauthorized', async () => {
    API.Export.preview.mockRejectedValue({
      response: { status: 401, data: { detail: '/private/session' } },
    });
    const wrapper = mountDialog();
    await settle();

    expect(wrapper.find('[data-test="export-error"]').text()).toBe('exportLoginRequired');
    expect(wrapper.html()).not.toContain('/private/session');
    expect(wrapper.find('[data-test="export-confirm"]').attributes('disabled'))
      .toBe('disabled');
  });

  it('recovers only an exact active-export conflict through the validated latest envelope', async () => {
    API.Export.preview.mockResolvedValue({ data: preview() });
    API.Export.create.mockRejectedValue({
      response: { status: 409, data: { code: 'active_export_exists' } },
    });
    API.Export.fetchLatest.mockResolvedValue(latest());
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="export-confirm"]').trigger('click');
    await settle();

    expect(API.Export.fetchLatest).toHaveBeenCalledTimes(1);
    expect(wrapper.toast.open).toHaveBeenCalledWith({
      message: 'exportActiveExists',
      type: 'is-info',
    });
    expect(wrapper.push).toHaveBeenCalledWith({ name: 'exports' });
    expect(wrapper.close).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['another 409', { response: { status: 409, data: { code: 'different_conflict' } } }],
    ['a 401', { response: { status: 401, data: { code: 'authentication_required' } } }],
    ['a transport detail', new Error('/data/exports/private')],
  ])('shows a fixed safe error for %s', async (name, error) => {
    API.Export.preview.mockResolvedValue({ data: preview() });
    API.Export.create.mockRejectedValue(error);
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="export-confirm"]').trigger('click');
    await settle();

    const expected = name === 'a 401' ? 'exportLoginRequired' : 'exportErrorGeneric';
    expect(wrapper.find('[data-test="export-error"]').text()).toBe(expected);
    expect(wrapper.html()).not.toContain('/data/exports/private');
    expect(API.Export.fetchLatest).not.toHaveBeenCalled();
    expect(wrapper.push).not.toHaveBeenCalled();
  });

  it('keeps an exact conflict in the dialog when latest validation fails', async () => {
    API.Export.preview.mockResolvedValue({ data: preview() });
    API.Export.create.mockRejectedValue({
      response: { status: 409, data: { code: 'active_export_exists' } },
    });
    API.Export.fetchLatest.mockRejectedValue(new Error('invalid_export_contract'));
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="export-confirm"]').trigger('click');
    await settle();

    expect(wrapper.find('[data-test="export-error"]').text()).toBe('exportErrorGeneric');
    expect(wrapper.push).not.toHaveBeenCalled();
    expect(wrapper.close).not.toHaveBeenCalled();
  });

  it('does not recover a conflict when the validated latest envelope has no active job', async () => {
    API.Export.preview.mockResolvedValue({ data: preview() });
    API.Export.create.mockRejectedValue({
      response: { status: 409, data: { code: 'active_export_exists' } },
    });
    API.Export.fetchLatest.mockResolvedValue({
      schema_version: 1,
      latest_attempt: null,
      downloadable_job: null,
    });
    const wrapper = mountDialog();
    await settle();

    await wrapper.find('[data-test="export-confirm"]').trigger('click');
    await settle();

    expect(wrapper.find('[data-test="export-error"]').text()).toBe('exportErrorGeneric');
    expect(wrapper.toast.open).not.toHaveBeenCalled();
    expect(wrapper.push).not.toHaveBeenCalled();
    expect(wrapper.close).not.toHaveBeenCalled();
  });

  it.each([
    ['preview', 'resolve'], ['preview', 'reject'],
    ['create', 'resolve'], ['create', 'reject'],
    ['latest', 'resolve'], ['latest', 'reject'],
  ])('ignores a late %s %s after destruction', async (phase, outcome) => {
    const pending = deferred();
    if (phase === 'preview') {
      API.Export.preview.mockReturnValue(pending.promise);
    } else {
      API.Export.preview.mockResolvedValue({ data: preview() });
      if (phase === 'create') API.Export.create.mockReturnValue(pending.promise);
      if (phase === 'latest') {
        API.Export.create.mockRejectedValue({
          response: { status: 409, data: { code: 'active_export_exists' } },
        });
        API.Export.fetchLatest.mockReturnValue(pending.promise);
      }
    }
    const wrapper = mountDialog();
    await settle();
    if (phase !== 'preview') {
      await wrapper.find('[data-test="export-confirm"]').trigger('click');
      await flushPromises();
    }
    const stateBeforeDestroy = {
      preview: wrapper.vm.preview,
      errorKey: wrapper.vm.errorKey,
      previewLoading: wrapper.vm.previewLoading,
      submitting: wrapper.vm.submitting,
    };
    wrapper.destroy();

    if (outcome === 'resolve') {
      let value = latest();
      if (phase === 'preview') value = { data: preview() };
      if (phase === 'create') value = { data: created() };
      pending.resolve(value);
    } else {
      pending.reject(new Error('/private/late-error'));
    }
    await settle();

    expect(wrapper.vm).toMatchObject(stateBeforeDestroy);
    expect(wrapper.push).not.toHaveBeenCalled();
    expect(wrapper.close).not.toHaveBeenCalled();
    expect(wrapper.toast.open).not.toHaveBeenCalled();
  });
});
