<template>
  <div class="pin-bulk-edit-modal">
    <div class="modal-card" style="width: auto">
      <header class="modal-card-head">
        <p class="modal-card-title">{{ $t('bulkPinEditTitle') }}</p>
      </header>
      <section class="modal-card-body">
        <label class="label" for="pin-bulk-privacy">{{ $t('privacyOptionLabel') }}</label>
        <div class="select is-fullwidth">
          <select
            id="pin-bulk-privacy"
            v-model="privacyMode"
            data-test="bulk-edit-privacy"
            :disabled="operationInFlight || result !== null"
          >
            <option :value="null">{{ $t('bulkPinNoPrivacyChange') }}</option>
            <option value="public">{{ $t('bulkPinMakePublic') }}</option>
            <option value="private">{{ $t('bulkPinMakePrivate') }}</option>
          </select>
        </div>
        <label class="label" for="pin-bulk-tag-mode">{{ $t('tagsLabel') }}</label>
        <div class="select is-fullwidth">
          <select
            id="pin-bulk-tag-mode"
            v-model="tagMode"
            data-test="bulk-edit-tag-mode"
            :disabled="operationInFlight || result !== null"
          >
            <option :value="null">{{ $t('bulkPinNoTagChange') }}</option>
            <option value="add">{{ $t('bulkPinTagAdd') }}</option>
            <option value="remove">{{ $t('bulkPinTagRemove') }}</option>
            <option value="replace">{{ $t('bulkPinTagReplace') }}</option>
          </select>
        </div>
        <b-taginput
          v-if="tagMode !== null"
          v-model="tagValues"
          data-test="bulk-edit-tags"
          :disabled="operationInFlight || result !== null"
        />
        <p v-if="progress" data-test="bulk-edit-progress" aria-live="polite">
          {{ $t('bulkPinProgress', progress) }}
        </p>
        <p v-if="result" data-test="bulk-edit-result">
          {{ $t('bulkPinResultSucceeded', { count: result.succeeded }) }}
          {{ $t('bulkPinResultPreserved', { count: result.preserved }) }}
          {{ $t('bulkPinResultFailed', { count: result.failed }) }}
        </p>
      </section>
      <footer class="modal-card-foot">
        <button class="button" type="button" :disabled="operationInFlight" @click="close">
          {{ $t('closeButton') }}
        </button>
        <button
          v-if="canRetry"
          class="button"
          type="button"
          data-test="bulk-edit-retry"
          @click="retry"
        >
          {{ $t('bulkPinRetry') }}
        </button>
        <button
          class="button is-primary"
          type="button"
          data-test="bulk-edit-submit"
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
import { executeBulk } from './bulkExecutor';

export function buildChanges({ privacyMode, tagMode, tagValues }) {
  const changes = {};
  if (privacyMode === 'public') changes.private = false;
  if (privacyMode === 'private') changes.private = true;
  if (tagMode !== null) {
    changes.tags = {
      mode: tagMode,
      values: tagValues.map(value => value.trim()).filter(Boolean),
    };
  }
  return changes;
}

export default {
  name: 'PinBulkEdit',
  beforeCreate() {
    this.disposed = false;
    this.operationToken = 0;
    this.operationChangesSnapshot = null;
    this.closeConsumed = false;
  },
  props: {
    selectedIds: {
      type: Array,
      required: true,
    },
    canStartOperation: {
      type: Function,
      default: () => true,
    },
  },
  data() {
    return {
      privacyMode: null,
      tagMode: null,
      tagValues: [],
      operationInFlight: false,
      operationCompleted: false,
      progress: null,
      result: null,
    };
  },
  computed: {
    trimmedTagValues() {
      return this.tagValues.map(value => value.trim()).filter(Boolean);
    },
    canSubmit() {
      if (
        this.operationInFlight
        || this.operationCompleted
        || this.result !== null
        || this.selectedIds.length === 0
      ) return false;
      if (this.tagMode === 'add' || this.tagMode === 'remove') {
        return this.trimmedTagValues.length > 0;
      }
      if (this.tagMode === 'replace') return true;
      if (this.tagMode !== null) return false;
      return this.privacyMode === 'public' || this.privacyMode === 'private';
    },
    canRetry() {
      return !this.operationInFlight
        && !this.operationCompleted
        && this.result !== null
        && this.result.retryable === true;
    },
  },
  beforeDestroy() {
    this.disposed = true;
    this.operationToken += 1;
  },
  methods: {
    close() {
      if (this.operationInFlight || this.closeConsumed) return;
      this.closeConsumed = true;
      this.$emit('closed');
      if (this.$parent && typeof this.$parent.close === 'function') this.$parent.close();
    },
    submit() {
      if (!this.canSubmit || this.canStartOperation() !== true) return null;
      const changes = buildChanges({
        privacyMode: this.privacyMode,
        tagMode: this.tagMode,
        tagValues: this.tagValues,
      });
      this.operationChangesSnapshot = {
        ...changes,
        ...(changes.tags ? {
          tags: { ...changes.tags, values: [...changes.tags.values] },
        } : {}),
      };
      return this.runOperation(this.operationChangesSnapshot);
    },
    retry() {
      if (
        !this.canRetry
        || this.operationChangesSnapshot === null
        || this.canStartOperation() !== true
      ) return null;
      return this.runOperation(this.operationChangesSnapshot);
    },
    runOperation(changes) {
      const token = this.operationToken + 1;
      this.operationToken = token;
      this.operationInFlight = true;
      this.operationCompleted = false;
      this.$emit('started');
      this.progress = { completed: 0, total: this.selectedIds.length };
      this.result = null;
      return executeBulk({
        ids: [...this.selectedIds],
        operation: 'update',
        fields: {
          changes,
        },
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
