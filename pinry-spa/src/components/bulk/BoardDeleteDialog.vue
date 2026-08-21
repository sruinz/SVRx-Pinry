<template>
  <div class="board-delete-modal">
    <div class="modal-card" style="width: auto">
      <header class="modal-card-head">
        <p class="modal-card-title">Delete board</p>
      </header>
      <section class="modal-card-body">
        <p v-if="phase === 'loading-preview'">Loading deletion preview…</p>
        <p v-if="phase === 'failed-preview'" data-test="board-delete-preview-error">
          Failed to load the deletion preview.
        </p>
        <div v-if="preview" data-test="board-delete-preview">
          <p data-test="board-delete-preview-exclusive">
            Exclusive owned pins: {{ preview.exclusive_owned_count }}
          </p>
          <p data-test="board-delete-preview-shared">
            Shared owned pins preserved: {{ preview.shared_owned_count }}
          </p>
          <p data-test="board-delete-preview-non-owned">
            Non-owned pins preserved: {{ preview.non_owned_count }}
          </p>
        </div>
        <p
          v-if="selectionError"
          class="has-text-danger"
          data-test="board-delete-selection-error"
        >
          {{ selectionError }}
        </p>
        <p v-if="progress" data-test="board-delete-progress" aria-live="polite">
          {{ progress.completed }}/{{ progress.total }}
        </p>
        <p
          v-if="actualExclusiveCount !== null"
          data-test="board-delete-actual-count"
        >
          Exclusive pins at confirmation: {{ actualExclusiveCount }}
        </p>
        <p
          v-if="actualExclusiveCount !== null"
          data-test="board-delete-result"
          aria-live="polite"
        >
          Deleted: {{ deleted }}; preserved: {{ preserved }}
        </p>
        <p
          v-if="phase === 'failed-board-delete'"
          class="has-text-danger"
          data-test="board-delete-error"
        >
          Pin processing finished, but the board could not be deleted.
        </p>
      </section>
      <footer class="modal-card-foot">
        <button
          v-if="canCancel"
          type="button"
          class="button"
          data-test="board-delete-cancel"
          @click="cancel"
        >
          Cancel
        </button>
        <button
          v-if="phase === 'ready'"
          type="button"
          class="button"
          data-test="board-delete-only"
          @click="deleteBoardOnly"
        >
          Delete board only
        </button>
        <button
          v-if="phase === 'ready'"
          type="button"
          class="button is-danger"
          data-test="board-delete-with-pins"
          :disabled="!canDeleteExclusivePins"
          @click="deleteExclusivePins"
        >
          Delete board and exclusive pins
        </button>
        <button
          v-if="showRetry"
          type="button"
          class="button is-danger"
          data-test="board-delete-retry"
          :disabled="phase === 'retrying-pins' && !retryReady"
          @click="retry"
        >
          Retry
        </button>
        <button
          v-if="phase === 'completed'"
          type="button"
          class="button"
          data-test="board-delete-close"
          @click="close"
        >
          Close
        </button>
      </footer>
    </div>
  </div>
</template>

<script>
import API from '../api';
import { executeBulk, intersectRemaining } from './bulkExecutor';

const MAX_SELECTION_IDS = 50000;
const PREVIEW_FIELDS = [
  'exclusive_owned_count',
  'non_owned_count',
  'shared_owned_count',
];
const SELECTION_FIELDS = ['count', 'results'];
const SELECTION_ROW_FIELDS = ['id', 'owned'];

function hasExactFields(value, fields) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  return keys.length === fields.length && keys.every((key, index) => key === fields[index]);
}

function validatedPreview(response) {
  if (!response || !hasExactFields(response.data, PREVIEW_FIELDS)) return null;
  const preview = response.data;
  const valid = PREVIEW_FIELDS.every(
    field => Number.isInteger(preview[field]) && preview[field] >= 0,
  );
  return valid ? preview : null;
}

function validatedExclusiveSelection(response) {
  if (!response || !hasExactFields(response.data, SELECTION_FIELDS)) return null;
  const { count, results } = response.data;
  if (
    !Number.isInteger(count)
    || count < 0
    || count > MAX_SELECTION_IDS
    || !Array.isArray(results)
    || count !== results.length
  ) return null;

  const ids = new Set();
  const valid = results.every((row) => {
    if (
      !hasExactFields(row, SELECTION_ROW_FIELDS)
      || !Number.isInteger(row.id)
      || row.id <= 0
      || row.owned !== true
      || ids.has(row.id)
    ) return false;
    ids.add(row.id);
    return true;
  });
  return valid ? results : null;
}

function failedCandidates(ids, result) {
  const candidates = new Set([
    ...(result.failedIds || []),
    ...(result.remainingIds || []),
  ]);
  return ids.filter(id => candidates.has(id));
}

export default {
  name: 'BoardDeleteDialog',
  beforeCreate() {
    this.disposed = false;
    this.requestToken = 0;
  },
  props: {
    board: {
      type: Object,
      required: true,
    },
  },
  data() {
    return {
      phase: 'loading-preview',
      preview: null,
      previewError: false,
      selectionError: null,
      selectionBlocked: false,
      selectedIds: [],
      actualExclusiveCount: null,
      processed: 0,
      deleted: 0,
      preserved: 0,
      failedIds: [],
      retryIds: [],
      retryReady: false,
      progress: null,
      boardDeletePending: false,
    };
  },
  computed: {
    boardId() {
      return this.board.id;
    },
    canCancel() {
      return this.phase === 'ready' || this.phase === 'failed-preview';
    },
    canDeleteExclusivePins() {
      return this.phase === 'ready'
        && !this.selectionBlocked
        && this.preview !== null
        && this.preview.exclusive_owned_count <= MAX_SELECTION_IDS;
    },
    showRetry() {
      return this.phase === 'failed-board-delete'
        || (this.phase === 'retrying-pins' && this.retryReady);
    },
  },
  created() {
    this.loadPreview();
  },
  beforeDestroy() {
    this.disposed = true;
    this.requestToken += 1;
  },
  methods: {
    isCurrent(token) {
      return !this.disposed && this.requestToken === token;
    },
    loadPreview() {
      if (!Number.isInteger(this.boardId) || this.boardId <= 0) {
        this.phase = 'failed-preview';
        this.previewError = true;
        return null;
      }
      const token = this.requestToken + 1;
      this.requestToken = token;
      return API.Board.deletePreview(this.boardId).then(
        (response) => {
          if (!this.isCurrent(token)) return;
          const preview = validatedPreview(response);
          if (preview === null) {
            this.phase = 'failed-preview';
            this.previewError = true;
            return;
          }
          this.preview = preview;
          this.selectionBlocked = preview.exclusive_owned_count > MAX_SELECTION_IDS;
          this.phase = 'ready';
        },
        () => {
          if (!this.isCurrent(token)) return;
          this.phase = 'failed-preview';
          this.previewError = true;
        },
      );
    },
    cancel() {
      if (!this.canCancel) return;
      this.close();
    },
    close() {
      if (this.$parent && typeof this.$parent.close === 'function') this.$parent.close();
    },
    deleteBoardOnly() {
      if (this.phase !== 'ready') return null;
      return this.startBoardDeletion();
    },
    deleteExclusivePins() {
      if (!this.canDeleteExclusivePins) return null;
      const token = this.requestToken + 1;
      this.requestToken = token;
      this.phase = 'deleting-pins';
      this.selectionError = null;
      this.retryReady = false;
      return API.Pin.fetchSelectionIds({
        boardId: this.boardId,
        exclusiveOwned: true,
      }).then(
        (response) => {
          if (!this.isCurrent(token)) return null;
          const rows = validatedExclusiveSelection(response);
          if (rows === null) {
            const responseCount = response && response.data ? response.data.count : null;
            const oversized = Number.isInteger(responseCount)
              && responseCount > MAX_SELECTION_IDS;
            this.selectionBlocked = oversized;
            this.phase = 'ready';
            this.selectionError = oversized
              ? 'selection_too_large'
              : 'Invalid exclusive pin selection.';
            return null;
          }
          this.selectedIds = rows.map(row => row.id);
          this.actualExclusiveCount = rows.length;
          if (rows.length === 0) return this.startBoardDeletion();
          return this.runPinDeletion(this.selectedIds);
        },
        (error) => {
          if (!this.isCurrent(token)) return null;
          const code = error && error.response && error.response.data
            ? error.response.data.code
            : null;
          this.selectionBlocked = code === 'selection_too_large';
          this.selectionError = code || 'Failed to load exclusive pins.';
          this.phase = 'ready';
          return null;
        },
      );
    },
    runPinDeletion(ids) {
      const token = this.requestToken + 1;
      const baseDeleted = this.deleted;
      const basePreserved = this.preserved;
      this.requestToken = token;
      this.phase = 'deleting-pins';
      this.retryReady = false;
      this.progress = { completed: 0, total: ids.length };
      return executeBulk({
        ids: [...ids],
        operation: 'delete_if_exclusive_to_board',
        fields: { source_board_id: this.boardId },
        request: payload => API.Pin.bulk(payload),
        onProgress: (progress) => {
          if (this.isCurrent(token)) this.progress = progress;
        },
      }).then((result) => {
        if (!this.isCurrent(token)) return result;
        this.deleted = baseDeleted + result.succeeded;
        this.preserved = basePreserved + result.preserved;
        this.processed = Math.min(
          this.actualExclusiveCount,
          this.deleted + this.preserved + result.failed,
        );
        this.progress = null;
        const candidates = failedCandidates(ids, result);
        if (result.failed > 0 || result.error || candidates.length > 0) {
          this.failedIds = candidates;
          this.retryIds = [];
          this.phase = 'retrying-pins';
          return this.refreshRetryIds(candidates, token).then(() => result);
        }
        this.failedIds = [];
        this.retryIds = [];
        return this.startBoardDeletion().then(() => result);
      });
    },
    refreshRetryIds(candidates, token) {
      return API.Pin.fetchSelectionIds({
        boardId: this.boardId,
        exclusiveOwned: true,
      }).then(
        (response) => {
          if (!this.isCurrent(token)) return;
          const rows = validatedExclusiveSelection(response);
          if (rows === null) {
            this.selectionError = 'Invalid exclusive pin selection.';
            this.retryReady = false;
            return;
          }
          this.retryIds = intersectRemaining(candidates, rows);
          this.retryReady = true;
        },
        (error) => {
          if (!this.isCurrent(token)) return;
          const code = error && error.response && error.response.data
            ? error.response.data.code
            : null;
          this.selectionError = code || 'Failed to refresh exclusive pins.';
          this.retryReady = false;
        },
      );
    },
    retryPins() {
      if (this.phase !== 'retrying-pins' || !this.retryReady) return null;
      const ids = [...this.retryIds];
      this.selectionError = null;
      if (ids.length === 0) return this.startBoardDeletion();
      return this.runPinDeletion(ids);
    },
    startBoardDeletion() {
      const token = this.requestToken + 1;
      this.requestToken = token;
      this.phase = 'deleting-board';
      this.boardDeletePending = true;
      let pending;
      try {
        pending = API.Board.delete(this.boardId);
      } catch (error) {
        pending = Promise.reject(error);
      }
      return Promise.resolve(pending).then(
        () => {
          if (!this.isCurrent(token)) return;
          this.boardDeletePending = false;
          this.phase = 'completed';
          this.$emit('completed', this.boardId);
        },
        () => {
          if (!this.isCurrent(token)) return;
          this.boardDeletePending = false;
          this.phase = 'failed-board-delete';
        },
      );
    },
    retryBoardDeletion() {
      if (this.phase !== 'failed-board-delete') return null;
      return this.startBoardDeletion();
    },
    retry() {
      if (this.phase === 'failed-board-delete') return this.retryBoardDeletion();
      if (this.phase === 'retrying-pins') return this.retryPins();
      return null;
    },
  },
};
</script>
