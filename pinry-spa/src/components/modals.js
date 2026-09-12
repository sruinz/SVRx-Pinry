import overlays from './utils/overlays';
import PinCreateModal from './pin_edit/PinCreateModal.vue';
import LoginForm from './LoginForm.vue';
import SignUpForm from './SignUpForm.vue';
import BoardEdit from './BoardEdit.vue';
import Add2Board from './pin_edit/Add2Board.vue';
import PinBulkBoardDialog from './bulk/PinBulkBoardDialog.vue';
import PinBulkEdit from './bulk/PinBulkEdit.vue';
import BoardDeleteDialog from './bulk/BoardDeleteDialog.vue';
import ExportDialog from './export/ExportDialog.vue';

function openPinEdit(vm, props = null, onCreated = null) {
  return overlays.openModal(vm, {
    component: PinCreateModal,
    props,
    canCancel: false,
    events: {
      pinCreated() {
        if (onCreated !== null) {
          onCreated();
        }
      },
    },
  });
}

function openAdd2Board(vm, pin, username) {
  return overlays.openModal(vm, {
    component: Add2Board,
    props: { pin, username },
  });
}

function openBoardCreate(vm) {
  return overlays.openModal(vm, {
    component: BoardEdit,
  });
}

function openBoardEdit(vm, board, onSaved) {
  return overlays.openModal(vm, {
    component: BoardEdit,
    props: {
      board,
      isEdit: true,
    },
    events: {
      boardSaved: onSaved,
    },
  });
}

function openLogin(vm, onSucceed) {
  return overlays.openModal(vm, {
    component: LoginForm,
    canCancel: ['escape', 'outside'],
    events: {
      'login.succeed': onSucceed,
    },
  });
}

function openSignUp(vm, onSignUpSucceed) {
  return overlays.openModal(vm, {
    component: SignUpForm,
    events: {
      'signup.succeed': onSignUpSucceed,
    },
  });
}

function bulkModalEvents(onCompleted, lifecycle) {
  const events = {};
  if (lifecycle && typeof lifecycle.started === 'function') {
    events.started = lifecycle.started;
  }
  if (onCompleted !== null) events.completed = onCompleted;
  if (lifecycle && typeof lifecycle.settled === 'function') {
    events.settled = lifecycle.settled;
  }
  if (lifecycle && typeof lifecycle.closed === 'function') {
    events.closed = lifecycle.closed;
  }
  return events;
}

export function openPinBulkBoard(vm, props, onCompleted = null, lifecycle = null) {
  const config = {
    component: PinBulkBoardDialog,
    props: {
      ...props,
      selectedIds: [...props.selectedIds],
    },
    canCancel: false,
  };
  const events = bulkModalEvents(onCompleted, lifecycle);
  if (Object.keys(events).length > 0) config.events = events;
  return overlays.openModal(vm, config);
}

export function openPinBulkEdit(vm, props, onCompleted = null, lifecycle = null) {
  const config = {
    component: PinBulkEdit,
    props: {
      ...props,
      selectedIds: [...props.selectedIds],
    },
    canCancel: false,
  };
  const events = bulkModalEvents(onCompleted, lifecycle);
  if (Object.keys(events).length > 0) config.events = events;
  return overlays.openModal(vm, config);
}

export function openBoardDelete(vm, props, onCompleted = null, onClosed = null) {
  const config = {
    component: BoardDeleteDialog,
    props: {
      ...props,
      board: { ...props.board },
    },
    canCancel: false,
  };
  const events = {};
  if (onCompleted !== null) events.completed = onCompleted;
  if (onClosed !== null) events.closed = onClosed;
  if (Object.keys(events).length > 0) config.events = events;
  return overlays.openModal(vm, config);
}

function isPositiveSafeInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

export function openExport(vm, props) {
  const hasBoard = props && props.boardId !== undefined;
  const hasPins = props && props.pinIds !== undefined;
  const validBoard = hasBoard && isPositiveSafeInteger(props.boardId);
  const validPins = hasPins
    && Array.isArray(props.pinIds)
    && props.pinIds.length > 0
    && props.pinIds.every(isPositiveSafeInteger);
  if (hasBoard === hasPins || (hasBoard && !validBoard) || (hasPins && !validPins)) {
    throw new Error('invalid_export_target');
  }
  return overlays.openModal(vm, {
    component: ExportDialog,
    props: hasBoard ? { boardId: props.boardId } : { pinIds: props.pinIds.slice() },
    canCancel: true,
  });
}

export default {
  openBoardCreate,
  openBoardEdit,
  openAdd2Board,
  openPinEdit,
  openLogin,
  openSignUp,
  openPinBulkBoard,
  openPinBulkEdit,
  openBoardDelete,
  openExport,
};
