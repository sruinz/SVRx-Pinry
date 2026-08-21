<template>
  <div class="board-delete-modal">
    <div class="modal-card" style="width: auto">
      <header class="modal-card-head">
        <p class="modal-card-title">{{ $t('boardDeleteTitle') }}</p>
      </header>
      <section class="modal-card-body">
        <p v-if="phase === 'loading-preview'">{{ $t('boardDeleteLoadingPreview') }}</p>
        <p v-if="phase === 'failed-preview'" data-test="board-delete-preview-error">
          {{ $t('boardDeletePreviewError') }}
        </p>
        <div v-if="preview" data-test="board-delete-preview">
          <p data-test="board-delete-preview-exclusive">
            {{ $t('boardDeletePreviewExclusive', {
              count: preview.exclusive_owned_count,
            }) }}
          </p>
          <p data-test="board-delete-preview-shared">
            {{ $t('boardDeletePreviewShared', {
              count: preview.shared_owned_count,
            }) }}
          </p>
          <p data-test="board-delete-preview-non-owned">
            {{ $t('boardDeletePreviewNonOwned', {
              count: preview.non_owned_count,
            }) }}
          </p>
        </div>
        <p
          v-if="selectionError"
          class="has-text-danger"
          data-test="board-delete-selection-error"
        >
          {{ $t(selectionError) }}
        </p>
        <p v-if="progress" data-test="board-delete-progress" aria-live="polite">
          {{ $t('bulkPinProgress', progress) }}
        </p>
        <p
          v-if="actualExclusiveCount !== null"
          data-test="board-delete-actual-count"
        >
          {{ $t('boardDeleteActualExclusiveCount', { count: actualExclusiveCount }) }}
        </p>
        <p
          v-if="actualExclusiveCount !== null"
          data-test="board-delete-result"
          aria-live="polite"
        >
          {{ $t('boardDeleteResult', { deleted, preserved }) }}
        </p>
        <p
          v-if="phase === 'failed-board-delete'"
          class="has-text-danger"
          data-test="board-delete-error"
        >
          {{ $t('boardDeleteError') }}
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
          {{ $t('boardDeleteCancel') }}
        </button>
        <button
          v-if="phase === 'ready'"
          type="button"
          class="button"
          data-test="board-delete-only"
          @click="deleteBoardOnly"
        >
          {{ $t('boardDeleteOnly') }}
        </button>
        <button
          v-if="phase === 'ready'"
          type="button"
          class="button is-danger"
          data-test="board-delete-with-pins"
          :disabled="!canDeleteExclusivePins"
          @click="deleteExclusivePins"
        >
          {{ $t('boardDeleteWithExclusivePins', {
            count: preview.exclusive_owned_count,
          }) }}
        </button>
        <button
          v-if="phase === 'failed-retry-refresh'"
          type="button"
          class="button is-danger"
          data-test="board-delete-retry-refresh"
          @click="retryRefresh"
        >
          {{ $t('boardDeleteRetryRefresh') }}
        </button>
        <button
          v-if="showRetry"
          type="button"
          class="button is-danger"
          data-test="board-delete-retry"
          :disabled="phase === 'retrying-pins' && !retryReady"
          @click="retry"
        >
          {{ $t('bulkPinRetry') }}
        </button>
        <button
          v-if="canClose"
          type="button"
          class="button"
          data-test="board-delete-close"
          @click="close"
        >
          {{ $t('closeButton') }}
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
    this.closeConsumed = false;
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
    canClose() {
      return [
        'completed',
        'failed-board-delete',
        'failed-retry-refresh',
      ].includes(this.phase);
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
          this.selectionError = this.selectionBlocked
            ? 'bulkPinSelectionTooLarge'
            : null;
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
      if (this.closeConsumed) return;
      this.closeConsumed = true;
      this.$emit('closed');
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
              ? 'bulkPinSelectionTooLarge'
              : 'boardDeleteSelectionInvalid';
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
          this.selectionError = code === 'selection_too_large'
            ? 'bulkPinSelectionTooLarge'
            : 'boardDeleteSelectionLoadError';
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
          this.phase = 'refreshing-retry';
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
            this.selectionError = 'boardDeleteSelectionInvalid';
            this.retryReady = false;
            this.phase = 'failed-retry-refresh';
            return;
          }
          this.retryIds = intersectRemaining(candidates, rows);
          this.retryReady = true;
          this.phase = 'retrying-pins';
        },
        (error) => {
          if (!this.isCurrent(token)) return;
          const code = error && error.response && error.response.data
            ? error.response.data.code
            : null;
          this.selectionError = code === 'selection_too_large'
            ? 'bulkPinSelectionTooLarge'
            : 'boardDeleteSelectionRefreshError';
          this.retryReady = false;
          this.phase = 'failed-retry-refresh';
        },
      );
    },
    retryRefresh() {
      if (this.phase !== 'failed-retry-refresh') return null;
      const token = this.requestToken + 1;
      this.requestToken = token;
      this.phase = 'refreshing-retry';
      this.selectionError = null;
      return this.refreshRetryIds([...this.failedIds], token);
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
          this.completeBoardDeletion(token);
        },
        () => {
          if (!this.isCurrent(token)) return null;
          let lookup;
          try {
            lookup = API.Board.get(this.boardId);
          } catch (error) {
            lookup = Promise.reject(error);
          }
          return Promise.resolve(lookup).then(
            () => {
              this.failBoardDeletion(token);
            },
            (error) => {
              if (error && error.response && error.response.status === 404) {
                this.completeBoardDeletion(token);
                return;
              }
              this.failBoardDeletion(token);
            },
          );
        },
      );
    },
    completeBoardDeletion(token) {
      if (!this.isCurrent(token)) return;
      this.boardDeletePending = false;
      this.phase = 'completed';
      this.$emit('completed', this.boardId);
    },
    failBoardDeletion(token) {
      if (!this.isCurrent(token)) return;
      this.boardDeletePending = false;
      this.phase = 'failed-board-delete';
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
