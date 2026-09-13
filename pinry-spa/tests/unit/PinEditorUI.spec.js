/* eslint-env jest */
import axios from 'axios';
import flushPromises from 'flush-promises';
import { createI18n } from 'vue-i18n';
import { shallowMount } from '@vue/test-utils';

import BoardDeleteDialog from '@/components/bulk/BoardDeleteDialog.vue';
import BoardEditUI from '@/components/editors/BoardEditUI.vue';
import PinEditorUI from '@/components/editors/PinEditorUI.vue';
import { openBoardDelete } from '@/components/modals';
import en from '@/components/utils/i18n/locales/en.json';
import overlays from '@/components/utils/overlays';

jest.mock('@/components/utils/overlays', () => ({
  __esModule: true,
  default: {
    openModal: jest.fn(), confirm: jest.fn(), toast: jest.fn(), openLoading: jest.fn(),
  },
}));
beforeEach(() => {
  overlays.openModal.mockReset();
  overlays.confirm.mockReset();
  overlays.toast.mockReset();
  overlays.openLoading.mockReset().mockReturnValue({ close: jest.fn() });
});

jest.mock('axios');

function deferred() {
  const request = {};
  request.promise = new Promise((resolve, reject) => {
    request.resolve = resolve;
    request.reject = reject;
  });
  request.promise.catch(() => {});
  return request;
}

async function resolveRequest(request) {
  request.resolve({ status: 204 });
  await request.promise;
  await flushPromises();
  await flushPromises();
}

async function rejectRequest(request) {
  request.reject(new Error('network'));
  await request.promise.catch(() => {});
  await flushPromises();
  await flushPromises();
}

describe('PinEditorUI delete behavior', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.spyOn(console, 'log').mockImplementation(() => {});
    axios.delete.mockResolvedValue({ status: 204 });
  });

  afterEach(() => {
    console.log.mockRestore();
  });

  function mountEditor() {
    const dialog = { confirm: overlays.confirm };
    const toast = { open: overlays.toast };
    return {
      dialog,
      toast,
      wrapper: shallowMount(PinEditorUI, {
        global: {
          directives: { masonry: {}, 'masonry-tile': {} },
          mocks: {},
          stubs: [],
          plugins: [createI18n({ legacy: false, locale: 'en', messages: { en } })],
        },

        props: {
          currentUsername: 'owner',
          pin: { id: 41, author: 'owner' },
        },
      }),
    };
  }

  it('asks for confirmation before permanently deleting a pin', async () => {
    const { dialog, toast, wrapper } = mountEditor();
    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    expect(dialog.confirm).toHaveBeenCalledTimes(1);
    expect(dialog.confirm.mock.calls[0][1].message).toBe('Delete this Pin?');
    expect(axios.delete).not.toHaveBeenCalled();

    dialog.confirm.mock.calls[0][1].onConfirm();
    dialog.confirm.mock.calls[0][1].onConfirm();
    await flushPromises();

    expect(axios.delete).toHaveBeenCalledTimes(1);
    expect(axios.delete).toHaveBeenCalledWith('/api/v2/pins/41/');
    expect(toast.open).toHaveBeenCalledWith('Pin deleted');
    expect(wrapper.emitted('pin-delete-succeed')[0]).toEqual([41]);
  });

  it('keeps the pin and reports an error when direct deletion fails', async () => {
    axios.delete.mockRejectedValueOnce(new Error('network'));
    const { dialog, toast, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    dialog.confirm.mock.calls[0][1].onConfirm();
    await flushPromises();

    expect(toast.open).toHaveBeenCalledWith({
      type: 'is-danger', message: 'Failed to delete Pin',
    });
    expect(wrapper.emitted('pin-delete-succeed')).toBeUndefined();
  });

  it('opens one confirmation while a delete confirmation is already open', async () => {
    const request = deferred();
    axios.delete.mockImplementation(() => request.promise);
    const { dialog, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    await wrapper.find('[data-test="delete-pin"]').trigger('click');

    expect(dialog.confirm).toHaveBeenCalledTimes(1);
    expect(axios.delete).not.toHaveBeenCalled();
  });

  it('opens a new confirmation after cancellation without deleting', async () => {
    const { dialog, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    const firstDialog = dialog.confirm.mock.calls[0][1];
    firstDialog.onCancel();
    firstDialog.onCancel();

    expect(axios.delete).not.toHaveBeenCalled();
    expect(wrapper.vm.deleteDialogOpen).toBe(false);
    await wrapper.find('[data-test="delete-pin"]').trigger('click');

    expect(dialog.confirm).toHaveBeenCalledTimes(2);
    expect(wrapper.vm.deleteDialogOpen).toBe(true);
  });

  it('never accepts a canceled confirmation callback', async () => {
    const request = deferred();
    axios.delete.mockImplementation(() => request.promise);
    const { dialog, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    const firstDialog = dialog.confirm.mock.calls[0][1];
    firstDialog.onCancel();
    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    const currentDialog = dialog.confirm.mock.calls[1][1];

    firstDialog.onConfirm();
    expect(axios.delete).not.toHaveBeenCalled();
    currentDialog.onConfirm();
    currentDialog.onConfirm();
    firstDialog.onConfirm();

    expect(axios.delete).toHaveBeenCalledTimes(1);
    await resolveRequest(request);
    firstDialog.onConfirm();
    currentDialog.onConfirm();
    await flushPromises();

    expect(axios.delete).toHaveBeenCalledTimes(1);
  });

  it('uses a confirmation callback only once before and after deletion settles', async () => {
    const request = deferred();
    axios.delete.mockImplementation(() => request.promise);
    const { dialog, toast, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    const { onConfirm } = dialog.confirm.mock.calls[0][1];
    onConfirm();
    onConfirm();

    expect(axios.delete).toHaveBeenCalledTimes(1);
    await resolveRequest(request);
    onConfirm();
    await flushPromises();

    expect(axios.delete).toHaveBeenCalledTimes(1);
    expect(wrapper.vm.deleteInFlight).toBe(false);
    expect(toast.open).toHaveBeenCalledTimes(1);
    expect(wrapper.emitted('pin-delete-succeed')).toHaveLength(1);
  });

  it('allows a failed deletion to retry only through a new confirmation', async () => {
    const firstRequest = deferred();
    const secondRequest = deferred();
    axios.delete
      .mockReturnValueOnce(firstRequest.promise)
      .mockReturnValueOnce(secondRequest.promise);
    const { dialog, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    const firstConfirm = dialog.confirm.mock.calls[0][1].onConfirm;
    firstConfirm();
    await rejectRequest(firstRequest);
    firstConfirm();

    expect(axios.delete).toHaveBeenCalledTimes(1);
    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    expect(dialog.confirm).toHaveBeenCalledTimes(2);
    dialog.confirm.mock.calls[1][1].onConfirm();

    expect(axios.delete).toHaveBeenCalledTimes(2);
    await rejectRequest(secondRequest);
  });

  it('does not update state or notify after destruction when deletion succeeds', async () => {
    const request = deferred();
    axios.delete.mockImplementation(() => request.promise);
    const { dialog, toast, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    dialog.confirm.mock.calls[0][1].onConfirm();
    expect(wrapper.vm.deleteInFlight).toBe(true);
    wrapper.unmount();
    await resolveRequest(request);

    expect(wrapper.vm.deleteInFlight).toBe(true);
    expect(toast.open).not.toHaveBeenCalled();
    expect(wrapper.emitted('pin-delete-succeed')).toBeUndefined();
  });

  it('does not update state or notify after destruction when deletion fails', async () => {
    const request = deferred();
    axios.delete.mockImplementation(() => request.promise);
    const { dialog, toast, wrapper } = mountEditor();

    await wrapper.find('[data-test="delete-pin"]').trigger('click');
    dialog.confirm.mock.calls[0][1].onConfirm();
    expect(wrapper.vm.deleteInFlight).toBe(true);
    wrapper.unmount();
    await rejectRequest(request);

    expect(wrapper.vm.deleteInFlight).toBe(true);
    expect(toast.open).not.toHaveBeenCalled();
    expect(wrapper.emitted('pin-delete-succeed')).toBeUndefined();
  });
});

describe('board delete modal helper', () => {
  it('opens the board delete component and forwards completed and closed events', () => {
    const vm = { };
    const board = { id: 7, name: 'Reference' };
    const completed = jest.fn();
    const closed = jest.fn();

    openBoardDelete(vm, { board }, completed, closed);
    board.name = 'Changed later';

    const config = overlays.openModal.mock.calls[0][1];
    expect(config).toMatchObject({
      component: BoardDeleteDialog,
      props: { board: { id: 7, name: 'Reference' } },
      canCancel: false,
      events: { completed, closed },
    });
  });
});

describe('BoardEditUI delete behavior', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  function mountBoardEditor() {
    const modal = { open: overlays.openModal };
    const dialog = { confirm: overlays.confirm };
    return {
      dialog,
      modal,
      wrapper: shallowMount(BoardEditUI, {
        global: { mocks: { $t: key => key }, stubs: [] },
        props: { board: { id: 7, name: 'Reference' } },
      }),
    };
  }

  it('opens the deletion modal and emits success only from its completed event', async () => {
    const { dialog, modal, wrapper } = mountBoardEditor();

    await wrapper.find('[data-test="delete-board"]').trigger('click');

    expect(dialog.confirm).not.toHaveBeenCalled();
    expect(axios.delete).not.toHaveBeenCalled();
    expect(wrapper.emitted('board-delete-succeed')).toBeUndefined();
    const config = modal.open.mock.calls[0][1];
    expect(config.component).toBe(BoardDeleteDialog);
    expect(config.props.board).toEqual({ id: 7, name: 'Reference' });

    config.events.completed(7);
    expect(wrapper.emitted('board-delete-succeed')).toEqual([[7]]);
    expect(wrapper.vm.deleteDialogOpen).toBe(false);
  });

  it('opens only one modal until its closed lifecycle releases the latch', async () => {
    const { modal, wrapper } = mountBoardEditor();

    await wrapper.find('[data-test="delete-board"]').trigger('click');
    await wrapper.find('[data-test="delete-board"]').trigger('click');

    expect(modal.open).toHaveBeenCalledTimes(1);
    expect(wrapper.vm.deleteDialogOpen).toBe(true);
    modal.open.mock.calls[0][1].events.closed();
    expect(wrapper.vm.deleteDialogOpen).toBe(false);

    await wrapper.find('[data-test="delete-board"]').trigger('click');
    expect(modal.open).toHaveBeenCalledTimes(2);
    expect(wrapper.vm.deleteDialogOpen).toBe(true);
  });

  it('ignores a late completed event after closed consumes the modal generation', async () => {
    const { modal, wrapper } = mountBoardEditor();

    await wrapper.find('[data-test="delete-board"]').trigger('click');
    const { closed, completed } = modal.open.mock.calls[0][1].events;
    closed();
    completed(7);

    expect(wrapper.vm.deleteDialogOpen).toBe(false);
    expect(wrapper.emitted('board-delete-succeed')).toBeUndefined();
  });

  it('emits board deletion success once when completed is delivered twice', async () => {
    const { modal, wrapper } = mountBoardEditor();

    await wrapper.find('[data-test="delete-board"]').trigger('click');
    const { completed } = modal.open.mock.calls[0][1].events;
    completed(7);
    completed(7);

    expect(wrapper.vm.deleteDialogOpen).toBe(false);
    expect(wrapper.emitted('board-delete-succeed')).toEqual([[7]]);
  });

  it('releases the modal latch when opening the modal throws', async () => {
    const { modal, wrapper } = mountBoardEditor();
    modal.open.mockImplementationOnce(() => { throw new Error('open failed'); });

    await wrapper.find('[data-test="delete-board"]').trigger('click');
    expect(wrapper.vm.deleteDialogOpen).toBe(false);

    modal.open.mockReturnValueOnce({});
    await wrapper.find('[data-test="delete-board"]').trigger('click');
    expect(modal.open).toHaveBeenCalledTimes(2);
    expect(wrapper.vm.deleteDialogOpen).toBe(true);
  });

  it('ignores callbacks captured before modal opening throws', async () => {
    const { modal, wrapper } = mountBoardEditor();
    let capturedEvents;
    modal.open.mockImplementationOnce((_vm, config) => {
      capturedEvents = config.events;
      throw new Error('open failed');
    });

    await wrapper.find('[data-test="delete-board"]').trigger('click');
    capturedEvents.completed(7);
    capturedEvents.closed();

    expect(wrapper.vm.deleteDialogOpen).toBe(false);
    expect(wrapper.emitted('board-delete-succeed')).toBeUndefined();
  });

  it('ignores a late completed event after the board editor is destroyed', async () => {
    const { modal, wrapper } = mountBoardEditor();
    await wrapper.find('[data-test="delete-board"]').trigger('click');
    const { completed } = modal.open.mock.calls[0][1].events;

    wrapper.unmount();
    completed(7);

    expect(wrapper.emitted('board-delete-succeed')).toBeUndefined();
    expect(axios.delete).not.toHaveBeenCalled();
  });
});
