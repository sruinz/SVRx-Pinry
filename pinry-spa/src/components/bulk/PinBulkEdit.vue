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
            :disabled="operationInFlight"
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
            :disabled="operationInFlight"
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
          :disabled="operationInFlight"
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
        <button class="button" type="button" :disabled="operationInFlight" @click="$parent.close()">
          {{ $t('closeButton') }}
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
  },
  props: {
    selectedIds: {
      type: Array,
      required: true,
    },
  },
  data() {
    return {
      privacyMode: null,
      tagMode: null,
      tagValues: [],
      operationInFlight: false,
      progress: null,
      result: null,
    };
  },
  computed: {
    trimmedTagValues() {
      return this.tagValues.map(value => value.trim()).filter(Boolean);
    },
    canSubmit() {
      if (this.operationInFlight || this.selectedIds.length === 0) return false;
      if (this.privacyMode === 'public' || this.privacyMode === 'private') return true;
      if (this.tagMode === 'replace') return true;
      return (this.tagMode === 'add' || this.tagMode === 'remove')
        && this.trimmedTagValues.length > 0;
    },
  },
  beforeDestroy() {
    this.disposed = true;
    this.operationToken += 1;
  },
  methods: {
    submit() {
      if (!this.canSubmit) return null;
      const token = this.operationToken + 1;
      this.operationToken = token;
      this.operationInFlight = true;
      this.progress = { completed: 0, total: this.selectedIds.length };
      this.result = null;
      return executeBulk({
        ids: [...this.selectedIds],
        operation: 'update',
        fields: {
          changes: buildChanges({
            privacyMode: this.privacyMode,
            tagMode: this.tagMode,
            tagValues: this.tagValues,
          }),
        },
        request: payload => API.Pin.bulk(payload),
        onProgress: (progress) => {
          if (!this.disposed && this.operationToken === token) this.progress = progress;
        },
      }).then((result) => {
        if (this.disposed || this.operationToken !== token) return result;
        this.operationInFlight = false;
        this.result = result;
        this.$emit('completed', result);
        return result;
      });
    },
  },
};
</script>
