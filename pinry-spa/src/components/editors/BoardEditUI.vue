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
    return { disposed: false };
  },
  beforeDestroy() {
    this.disposed = true;
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
    onBoardDeleted(boardId) {
      if (this.disposed) return;
      this.$emit('board-delete-succeed', boardId);
    },
    deleteBoard() {
      if (this.disposed) return;
      openBoardDelete(
        this,
        { board: this.board },
        this.onBoardDeleted,
      );
    },
  },
};
</script>

<style lang="scss" scoped>
@import './editor';
</style>
