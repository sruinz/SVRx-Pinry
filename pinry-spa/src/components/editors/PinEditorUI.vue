<template>
  <div class="editor">
    <div class="editor-buttons">
      <span class="icon-container" v-if="inOwnedBoard" @click="removeFromBoard">
          <i aria-hidden="true" class="mdi mdi-minus-box"></i>
      </span>
      <span class="icon-container" @click="addToBoard">
          <i aria-hidden="true" class="mdi mdi-plus-box"></i>
      </span>
      <span
        class="icon-container"
        data-test="delete-pin"
        @click="deletePin"
        v-if="isOwner">
         <i aria-hidden="true" class="mdi mdi-delete"></i>
      </span>
      <span class="icon-container" v-if="isOwner" @click="editPin">
       <i aria-hidden="true" class="mdi mdi-pencil"></i>
      </span>
    </div>
  </div>
</template>

<script>
import overlays from '../utils/overlays';
import API from '../api';
import modals from '../modals';

export default {
  name: 'Editor',
  props: {
    currentBoard: {
      type: Object,
      default() {
        return {};
      },
    },
    currentUsername: {
      default: null,
      type: String,
    },
    pin: {
      default() {
        return {};
      },
      type: Object,
    },
  },
  computed: {
    isOwner() {
      return this.pin.author === this.currentUsername;
    },
    inOwnedBoard() {
      console.log(this.currentBoard, this.currentUsername);
      return (
        Object.values(this.currentBoard).length !== 0
        && this.currentBoard.submitter.username === this.currentUsername
      );
    },
  },
  data() {
    return {
      deleteDialogOpen: false,
      deleteInFlight: false,
      deleteConsumed: false,
      disposed: false,
    };
  },
  beforeUnmount() {
    this.disposed = true;
  },
  methods: {
    addToBoard() {
      modals.openAdd2Board(this, this.pin, this.currentUsername);
    },
    removeFromBoard() {
      overlays.confirm(this, {
        message: 'Remove Pin from Board?',
        onConfirm: () => {
          API.Board.removeFromBoard(this.currentBoard.id, [this.pin.id]).then(
            () => {
              overlays.toast('Pin removed');
              this.$emit('pin-remove-from-board-succeed', this.pin.id);
            },
            () => {
              overlays.toast(
                { type: 'is-danger', message: 'Failed to Remove Pin' },
              );
            },
          );
        },
      });
    },
    editPin() {
      const props = {
        username: this.currentUsername,
        existedPin: this.pin,
        isEdit: true,
      };
      modals.openPinEdit(
        this,
        props,
      );
    },
    deletePin() {
      if (
        this.disposed
        || this.deleteDialogOpen
        || this.deleteInFlight
        || this.deleteConsumed
      ) return;
      this.deleteDialogOpen = true;
      let active = true;
      overlays.confirm(this, {
        message: this.$t('pinDeleteConfirm'),
        onConfirm: () => {
          if (!active || this.disposed || this.deleteConsumed) return;
          active = false;
          this.deleteDialogOpen = false;
          this.deleteInFlight = true;
          API.Pin.deleteById(this.pin.id).then(
            () => {
              if (this.disposed) return;
              this.deleteInFlight = false;
              this.deleteConsumed = true;
              overlays.toast(this.$t('pinDeleted'));
              this.$emit('pin-delete-succeed', this.pin.id);
            },
            () => {
              if (this.disposed) return;
              this.deleteInFlight = false;
              overlays.toast(
                { type: 'is-danger', message: this.$t('pinDeleteError') },
              );
            },
          );
        },
        onCancel: () => {
          if (!active || this.disposed) return;
          active = false;
          this.deleteDialogOpen = false;
        },
      });
    },
  },
};
</script>

<style lang="scss" scoped>
@import './editor';
</style>
