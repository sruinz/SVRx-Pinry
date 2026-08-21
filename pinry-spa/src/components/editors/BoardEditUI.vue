<template>
  <div class="editor">
    <div class="editor-buttons">
      <span class="icon-container" data-test="delete-board" @click="deleteBoard">
         <b-icon
           type="is-light"
           icon="delete"
           custom-size="mdi-24px">
         </b-icon>
      </span>
      <span class="icon-container" @click="editBoard">
       <b-icon
         type="is-light"
         icon="pencil"
         custom-size="mdi-24px">
       </b-icon>
      </span>
    </div>
  </div>
</template>

<script>
import modals, { openBoardDelete } from '../modals';


export default {
  name: 'BoardEditor',
  props: {
    board: {
      default() {
        return {};
      },
      type: Object,
    },
  },
  data() {
    return {
      disposed: false,
      deleteDialogOpen: false,
      deleteDialogToken: 0,
    };
  },
  beforeDestroy() {
    this.disposed = true;
    this.deleteDialogToken += 1;
  },
  methods: {
    onBoardSaved() {
      this.$emit('board-save-succeed');
    },
    editBoard() {
      modals.openBoardEdit(
        this,
        this.board,
        this.onBoardSaved,
      );
    },
    releaseDeleteDialog(token) {
      if (this.disposed || this.deleteDialogToken !== token) return false;
      this.deleteDialogOpen = false;
      return true;
    },
    onBoardDeleted(boardId, token) {
      if (!this.releaseDeleteDialog(token)) return;
      this.$emit('board-delete-succeed', boardId);
    },
    deleteBoard() {
      if (this.disposed || this.deleteDialogOpen) return;
      const token = this.deleteDialogToken + 1;
      this.deleteDialogToken = token;
      this.deleteDialogOpen = true;
      try {
        openBoardDelete(
          this,
          { board: this.board },
          boardId => this.onBoardDeleted(boardId, token),
          () => this.releaseDeleteDialog(token),
        );
      } catch (_error) {
        this.releaseDeleteDialog(token);
      }
    },
  },
};
</script>

<style lang="scss" scoped>
@import './editor';
</style>
