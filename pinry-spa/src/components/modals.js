import PinCreateModal from './pin_edit/PinCreateModal.vue';
import LoginForm from './LoginForm.vue';
import SignUpForm from './SignUpForm.vue';
import BoardEdit from './BoardEdit.vue';
import Add2Board from './pin_edit/Add2Board.vue';
import PinBulkBoardDialog from './bulk/PinBulkBoardDialog.vue';
import PinBulkEdit from './bulk/PinBulkEdit.vue';
import BoardDeleteDialog from './bulk/BoardDeleteDialog.vue';


function openPinEdit(vm, props = null, onCreated = null) {
  vm.$buefy.modal.open(
    {
      parent: vm,
      component: PinCreateModal,
      props,
      hasModalCard: true,
      canCancel: false,
      events: {
        pinCreated() {
          if (onCreated !== null) {
            onCreated();
          }
        },
      },
    },
  );
}

function openAdd2Board(vm, pin, username) {
  vm.$buefy.modal.open(
    {
      parent: vm,
      component: Add2Board,
      props: { pin, username },
      hasModalCard: true,
    },
  );
}

function openBoardCreate(vm) {
  vm.$buefy.modal.open(
    {
      parent: vm,
      component: BoardEdit,
      hasModalCard: true,
    },
  );
}

function openBoardEdit(vm, board, onSaved) {
  vm.$buefy.modal.open(
    {
      parent: vm,
      component: BoardEdit,
      props: {
        board,
        isEdit: true,
      },
      events: {
        boardSaved: onSaved,
      },
      hasModalCard: true,
    },
  );
}

function openLogin(vm, onSucceed) {
  vm.$buefy.modal.open({
    parent: vm,
    component: LoginForm,
    hasModalCard: true,
    events: {
      'login.succeed': onSucceed,
    },
  });
}

function openSignUp(vm, onSignUpSucceed) {
  vm.$buefy.modal.open({
    parent: vm,
    component: SignUpForm,
    hasModalCard: true,
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
    parent: vm,
    component: PinBulkBoardDialog,
    props: {
      ...props,
      selectedIds: [...props.selectedIds],
    },
    hasModalCard: true,
    canCancel: false,
  };
  const events = bulkModalEvents(onCompleted, lifecycle);
  if (Object.keys(events).length > 0) config.events = events;
  return vm.$buefy.modal.open(config);
}

export function openPinBulkEdit(vm, props, onCompleted = null, lifecycle = null) {
  const config = {
    parent: vm,
    component: PinBulkEdit,
    props: {
      ...props,
      selectedIds: [...props.selectedIds],
    },
    hasModalCard: true,
    canCancel: false,
  };
  const events = bulkModalEvents(onCompleted, lifecycle);
  if (Object.keys(events).length > 0) config.events = events;
  return vm.$buefy.modal.open(config);
}

export function openBoardDelete(vm, props, onCompleted = null, onClosed = null) {
  const config = {
    parent: vm,
    component: BoardDeleteDialog,
    props: {
      ...props,
      board: { ...props.board },
    },
    hasModalCard: true,
    canCancel: false,
  };
  const events = {};
  if (onCompleted !== null) events.completed = onCompleted;
  if (onClosed !== null) events.closed = onClosed;
  if (Object.keys(events).length > 0) config.events = events;
  return vm.$buefy.modal.open(config);
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
};
