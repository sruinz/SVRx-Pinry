<template>
  <div class="pin-bulk-board-modal">
    <div class="modal-card" style="width: auto">
      <header class="modal-card-head">
        <p class="modal-card-title">
          {{ $t(mode === 'move' ? 'bulkPinMoveTitle' : 'bulkPinAddToBoardTitle') }}
        </p>
      </header>
      <section class="modal-card-body">
        <label class="label" for="pin-bulk-board-target">
          {{ $t('bulkPinTargetBoard') }}
        </label>
        <div class="select is-fullwidth">
          <select
            id="pin-bulk-board-target"
            ref="targetBoard"
            v-model.number="targetBoardId"
            data-test="bulk-board-target"
            :disabled="loadingBoards || boardCreateInFlight || operationInFlight || result !== null"
          >
            <option :value="null">{{ $t('bulkPinChooseBoard') }}</option>
            <option v-for="board in boardOptions" :key="board.id" :value="board.id">
              {{ board.name }}
            </option>
          </select>
        </div>
        <form
          v-if="mode === 'add'"
          class="pin-bulk-board-create"
          data-test="bulk-board-create-form"
          novalidate
          @submit.prevent="createBoard"
        >
          <h3 class="title is-6">{{ $t('BoardCreateTitle') }}</h3>
          <label class="label" for="pin-bulk-new-board-name">{{ $t('nameLabel') }}</label>
          <input
            id="pin-bulk-new-board-name"
            v-model="newBoardName"
            class="input"
            type="text"
            maxlength="128"
            data-test="bulk-board-new-name"
            :disabled="!canCreateBoard"
            :aria-invalid="boardCreateError ? 'true' : null"
            :aria-describedby="boardCreateError ? 'pin-bulk-new-board-error' : null"
            @input="boardCreateError = null"
          >
          <p
            v-if="boardCreateError"
            id="pin-bulk-new-board-error"
            class="help is-danger"
            data-test="bulk-board-create-error"
            role="alert"
          >
            {{ $t(boardCreateError) }}
          </p>
          <label class="checkbox" for="pin-bulk-new-board-private">
            <input
              id="pin-bulk-new-board-private"
              v-model="newBoardPrivate"
              type="checkbox"
              data-test="bulk-board-new-private"
              :disabled="!canCreateBoard"
            >
            {{ $t('isPrivateCheckbox') }}
          </label>
          <button
            class="button"
            type="submit"
            data-test="bulk-board-create"
            :disabled="!canCreateBoard"
          >
            {{ $t('createBoardButton') }}
          </button>
          <p
            class="is-sr-only"
            data-test="bulk-board-create-status"
            aria-live="polite"
            aria-atomic="true"
          >
            {{ boardCreateAnnouncement ? $t(boardCreateAnnouncement) : '' }}
          </p>
        </form>
        <p v-if="progress" data-test="bulk-board-progress" aria-live="polite">
          {{ $t('bulkPinProgress', progress) }}
        </p>
        <p v-if="result" data-test="bulk-board-result">
          {{ $t('bulkPinResultSucceeded', { count: result.succeeded }) }}
          {{ $t('bulkPinResultPreserved', { count: result.preserved }) }}
          {{ $t('bulkPinResultFailed', { count: result.failed }) }}
        </p>
      </section>
      <footer class="modal-card-foot">
        <button
          class="button"
          type="button"
          data-test="bulk-board-close"
          :disabled="boardCreateInFlight || operationInFlight"
          @click="close"
        >
          {{ $t('closeButton') }}
        </button>
        <button
          v-if="canRetry"
          class="button"
          type="button"
          data-test="bulk-board-retry"
          @click="retry"
        >
          {{ $t('bulkPinRetry') }}
        </button>
        <button
          class="button is-primary"
          type="button"
          data-test="bulk-board-submit"
          :disabled="!canSubmit"
          @click="submit"
        >
          {{ $t('bulkPinApply') }}
        </button>
      </footer>
    </div>
  </div>
</template>

<script>
import API from '../api';
import bus from '../utils/bus';
import { executeBulk } from './bulkExecutor';

export default {
  name: 'PinBulkBoardDialog',
  beforeCreate() {
    this.disposed = false;
    this.boardRequestToken = 0;
    this.boardCreateToken = 0;
    this.operationToken = 0;
    this.operationSnapshot = null;
    this.operationFieldsSnapshot = null;
    this.closeConsumed = false;
  },
  props: {
    mode: {
      type: String,
      required: true,
      validator: value => ['add', 'move'].includes(value),
    },
    sourceBoardId: {
      type: [Number, String],
      default: null,
    },
    selectedIds: {
      type: Array,
      required: true,
    },
    username: {
      type: String,
      required: true,
    },
    canStartOperation: {
      type: Function,
      default: () => true,
    },
  },
  data() {
    return {
      boardOptions: [],
      targetBoardId: null,
      loadingBoards: false,
      newBoardName: '',
      newBoardPrivate: false,
      boardCreateInFlight: false,
      boardCreateError: null,
      boardCreateAnnouncement: null,
      operationInFlight: false,
      operationCompleted: false,
      progress: null,
      result: null,
      loadError: false,
    };
  },
  computed: {
    canCreateBoard() {
      return this.mode === 'add'
        && !this.loadingBoards
        && !this.boardCreateInFlight
        && !this.operationInFlight
        && !this.operationCompleted
        && this.result === null;
    },
    canSubmit() {
      return !this.loadingBoards
        && !this.boardCreateInFlight
        && !this.operationInFlight
        && !this.operationCompleted
        && this.result === null
        && Number.isInteger(Number(this.targetBoardId))
        && Number(this.targetBoardId) > 0
        && this.selectedIds.length > 0;
    },
    canRetry() {
      return !this.operationInFlight
        && !this.operationCompleted
        && this.result !== null
        && this.result.retryable === true;
    },
  },
  created() {
    this.loadBoards();
  },
  beforeDestroy() {
    this.disposed = true;
    this.boardRequestToken += 1;
    this.boardCreateToken += 1;
    this.operationToken += 1;
  },
  methods: {
    close() {
      if (this.boardCreateInFlight || this.operationInFlight || this.closeConsumed) return;
      this.closeConsumed = true;
      this.$emit('closed');
      if (this.$parent && typeof this.$parent.close === 'function') this.$parent.close();
    },
    loadBoards() {
      const token = this.boardRequestToken + 1;
      this.boardRequestToken = token;
      this.loadingBoards = true;
      this.loadError = false;
      return API.Board.fetchFullList(this.username).then(
        (response) => {
          if (this.disposed || this.boardRequestToken !== token) return;
          const boards = Array.isArray(response.data) ? response.data : [];
          const sourceId = this.sourceBoardId === null ? null : Number(this.sourceBoardId);
          this.boardOptions = boards.filter(
            board => this.mode !== 'move' || Number(board.id) !== sourceId,
          );
        },
        () => {
          if (this.disposed || this.boardRequestToken !== token) return;
          this.loadError = true;
        },
      ).then(() => {
        if (!this.disposed && this.boardRequestToken === token) {
          this.loadingBoards = false;
        }
      });
    },
    async createBoard() {
      if (!this.canCreateBoard || this.canStartOperation() !== true) return null;
      const name = this.newBoardName.trim();
      if (name === '') {
        this.boardCreateError = 'bulkPinBoardNameRequired';
        this.boardCreateAnnouncement = null;
        return null;
      }

      const privateBoard = Boolean(this.newBoardPrivate);
      const token = this.boardCreateToken + 1;
      this.boardCreateToken = token;
      this.boardCreateInFlight = true;
      this.boardCreateError = null;
      this.boardCreateAnnouncement = null;
      try {
        const created = await API.Board.create(name, privateBoard);
        if (this.disposed || this.boardCreateToken !== token) return created;
        const boardId = Number(created && created.id);
        if (!Number.isSafeInteger(boardId) || boardId <= 0
          || !created || typeof created.name !== 'string' || created.name === '') {
          throw new Error('invalid_board_response');
        }
        if (!this.boardOptions.some(board => Number(board.id) === boardId)) {
          this.boardOptions = [{ id: boardId, name: created.name }, ...this.boardOptions];
        }
        this.targetBoardId = boardId;
        this.newBoardName = '';
        this.newBoardPrivate = false;
        this.boardCreateAnnouncement = 'bulkPinBoardCreated';
        bus.bus.$emit(bus.events.refreshBoards);
        this.$nextTick(() => {
          if (!this.disposed && this.boardCreateToken === token
            && this.$refs.targetBoard && typeof this.$refs.targetBoard.focus === 'function') {
            this.$refs.targetBoard.focus();
          }
        });
        return created;
      } catch (_error) {
        if (!this.disposed && this.boardCreateToken === token) {
          this.boardCreateError = 'bulkPinBoardCreateError';
          this.boardCreateAnnouncement = null;
        }
        return null;
      } finally {
        if (!this.disposed && this.boardCreateToken === token) {
          this.boardCreateInFlight = false;
        }
      }
    },
    operationFields() {
      const targetBoardId = Number(this.targetBoardId);
      if (this.mode === 'move') {
        return {
          source_board_id: Number(this.sourceBoardId),
          target_board_id: targetBoardId,
        };
      }
      return { board_id: targetBoardId };
    },
    submit() {
      if (!this.canSubmit || this.canStartOperation() !== true) return null;
      this.operationSnapshot = this.mode === 'move'
        ? 'move_between_boards'
        : 'add_to_board';
      this.operationFieldsSnapshot = { ...this.operationFields() };
      return this.runOperation(this.operationSnapshot, this.operationFieldsSnapshot);
    },
    retry() {
      if (
        !this.canRetry
        || this.operationSnapshot === null
        || this.operationFieldsSnapshot === null
        || this.canStartOperation() !== true
      ) return null;
      return this.runOperation(this.operationSnapshot, this.operationFieldsSnapshot);
    },
    runOperation(operation, fields) {
      const token = this.operationToken + 1;
      this.operationToken = token;
      this.operationInFlight = true;
      this.operationCompleted = false;
      this.$emit('started');
      this.progress = { completed: 0, total: this.selectedIds.length };
      this.result = null;
      return executeBulk({
        ids: [...this.selectedIds],
        operation,
        fields: { ...fields },
        request: payload => API.Pin.bulk(payload),
        onProgress: (progress) => {
          if (!this.disposed && this.operationToken === token) this.progress = progress;
        },
      }).then((result) => {
        if (this.disposed || this.operationToken !== token) return result;
        const remaining = Array.isArray(result.remainingIds)
          ? result.remainingIds.length
          : 0;
        const retryable = Boolean(result.error)
          || remaining > 0
          || result.failed > 0
          || result.completed !== result.total;
        const summary = {
          ...result,
          failed: result.failed + remaining,
          retryable,
        };
        this.operationInFlight = false;
        this.result = summary;
        if (retryable) {
          this.$emit('settled', summary);
          return summary;
        }
        this.operationCompleted = true;
        this.$emit('completed', summary);
        return summary;
      });
    },
  },
};
</script>
