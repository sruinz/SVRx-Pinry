<template>
  <div class="export-modal">
    <div class="modal-card export-modal__card">
      <header class="modal-card-head">
        <p class="modal-card-title">{{ $t('exportTitle') }}</p>
      </header>
      <section class="modal-card-body">
        <p v-if="previewLoading" data-test="export-loading">
          {{ $t('exportPreviewLoading') }}
        </p>
        <div v-if="preview" data-test="export-preview">
          <p data-test="export-as-of">
            {{ $t('exportAsOf', { value: preview.as_of }) }}
          </p>
          <p data-test="export-eligible-total">
            {{ $t('exportEligibleTotal', { count: preview.eligible_total }) }}
          </p>
          <p data-test="export-excluded-total">
            {{ $t('exportExcludedTotal', { count: preview.excluded_total }) }}
          </p>
          <p data-test="export-owned-private-total">
            {{ $t('exportOwnedPrivateTotal', { count: preview.owned_private_total }) }}
          </p>
          <p data-test="export-original-files">
            {{ $t('exportOriginalFiles', { count: preview.estimated_original_files }) }}
          </p>
          <p data-test="export-original-bytes">
            {{ $t('exportOriginalBytes', { count: preview.estimated_original_bytes }) }}
          </p>
          <p data-test="export-zip-bytes">
            {{ $t('exportZipBytes', { count: preview.estimated_zip_bytes }) }}
          </p>
        </div>
        <p
          v-if="errorKey"
          class="has-text-danger"
          data-test="export-error"
          role="alert"
        >
          {{ $t(errorKey) }}
        </p>
      </section>
      <footer class="modal-card-foot export-modal__actions">
        <button
          type="button"
          class="button"
          data-test="export-cancel"
          @click="closeDialog"
        >
          {{ $t('closeButton') }}
        </button>
        <button
          type="button"
          class="button is-primary"
          data-test="export-confirm"
          :disabled="!canConfirm"
          @click="confirmExport"
        >
          {{ $t(submitting ? 'exportSubmitting' : 'exportConfirm') }}
        </button>
      </footer>
    </div>
  </div>
</template>

<script>
import API from '../api';
import {
  validateExportCreate,
  validateExportPreview,
} from './exportContract';

const ACTIVE_STATES = ['queued', 'snapshotting', 'archiving', 'verifying'];

function isPositiveSafeInteger(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function makeRequestPayload(boardId, pinIds) {
  const hasBoard = boardId !== null;
  const hasPins = pinIds !== null;
  if (hasBoard === hasPins) return null;
  if (hasBoard) {
    if (!isPositiveSafeInteger(boardId)) return null;
    return Object.freeze({ scope: 'board', board_id: boardId });
  }
  if (
    !Array.isArray(pinIds)
    || pinIds.length === 0
    || !pinIds.every(isPositiveSafeInteger)
  ) return null;
  return Object.freeze({
    scope: 'pins',
    pin_ids: Object.freeze(pinIds.slice()),
  });
}

function isActiveExportConflict(error) {
  return Boolean(
    error
    && error.response
    && error.response.status === 409
    && error.response.data
    && error.response.data.code === 'active_export_exists',
  );
}

export default {
  name: 'ExportDialog',
  props: {
    boardId: { type: Number, default: null },
    pinIds: { type: Array, default: null },
  },
  data() {
    const requestPayload = makeRequestPayload(this.boardId, this.pinIds);
    return {
      requestPayload,
      preview: null,
      previewLoading: requestPayload !== null,
      submitting: false,
      errorKey: requestPayload === null ? 'exportErrorGeneric' : null,
      isDestroyed: false,
      requestSequence: 0,
    };
  },
  computed: {
    canConfirm() {
      return this.preview !== null && !this.previewLoading && !this.submitting;
    },
  },
  mounted() {
    if (this.requestPayload !== null) this.loadPreview();
  },
  beforeDestroy() {
    this.isDestroyed = true;
    this.requestSequence += 1;
  },
  methods: {
    isCurrent(sequence) {
      return !this.isDestroyed && sequence === this.requestSequence;
    },
    showSafeError(error) {
      const status = error && error.response ? error.response.status : null;
      this.errorKey = status === 401 ? 'exportLoginRequired' : 'exportErrorGeneric';
    },
    closeDialog() {
      if (this.$parent && typeof this.$parent.close === 'function') this.$parent.close();
    },
    navigateAndClose() {
      this.$router.push({ name: 'exports' });
      this.closeDialog();
    },
    async loadPreview() {
      const sequence = this.requestSequence + 1;
      this.requestSequence = sequence;
      this.previewLoading = true;
      this.errorKey = null;
      try {
        const response = await API.Export.preview(this.requestPayload);
        if (!this.isCurrent(sequence)) return;
        this.preview = validateExportPreview(response.data);
      } catch (error) {
        if (!this.isCurrent(sequence)) return;
        this.preview = null;
        this.showSafeError(error);
      } finally {
        if (this.isCurrent(sequence)) this.previewLoading = false;
      }
    },
    async confirmExport() {
      if (!this.canConfirm) return;
      const sequence = this.requestSequence + 1;
      this.requestSequence = sequence;
      this.submitting = true;
      this.errorKey = null;
      try {
        const response = await API.Export.create(this.requestPayload);
        if (!this.isCurrent(sequence)) return;
        validateExportCreate(response.data);
        this.navigateAndClose();
      } catch (error) {
        if (!this.isCurrent(sequence)) return;
        if (isActiveExportConflict(error)) {
          try {
            const latest = await API.Export.fetchLatest();
            if (!this.isCurrent(sequence)) return;
            if (
              !latest.latest_attempt
              || !ACTIVE_STATES.includes(latest.latest_attempt.state)
            ) throw new Error('invalid_export_contract');
            this.$buefy.toast.open({
              message: this.$t('exportActiveExists'),
              type: 'is-info',
            });
            this.navigateAndClose();
          } catch (latestError) {
            if (!this.isCurrent(sequence)) return;
            this.showSafeError(latestError);
          }
        } else {
          this.showSafeError(error);
        }
      } finally {
        if (this.isCurrent(sequence)) this.submitting = false;
      }
    },
  },
};
</script>

<style lang="scss" scoped>
.export-modal__card {
  width: min(36rem, calc(100vw - 2rem));
}

.export-modal__actions {
  display: flex;
  flex-wrap: wrap;
}

@media screen and (max-width: 768px) {
  .export-modal__actions {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
