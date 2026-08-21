<template>
  <div class="pin-bulk-toolbar">
    <button
      v-if="!active"
      type="button"
      class="button is-primary"
      data-test="pin-selection-enter"
      @click="$emit('enter')"
    >
      {{ $t('bulkPinSelectStart') }}
    </button>
    <template v-else>
      <div class="pin-bulk-toolbar__summary" data-test="pin-selection-summary">
        <span v-if="scope === 'all'">{{ $t('bulkPinAllSelected', { count: allCount }) }}</span>
        <span v-else>{{ $t('bulkPinSelectedCount', { count: selectedCount }) }}</span>
      </div>
      <div class="pin-bulk-toolbar__buttons">
        <button
          type="button"
          class="button"
          data-test="pin-selection-select-loaded"
          :disabled="operationInFlight || loadedCount === 0"
          @click="$emit('select-loaded')"
        >
          {{ $t('bulkPinSelectLoaded') }}
        </button>
        <button
          type="button"
          class="button"
          data-test="pin-selection-clear"
          :disabled="operationInFlight || selectedCount === 0"
          @click="$emit('clear')"
        >
          {{ $t('bulkPinClear') }}
        </button>
        <button
          type="button"
          class="button"
          data-test="pin-selection-select-all"
          :disabled="operationInFlight"
          @click="$emit('select-all')"
        >
          {{ $t('bulkPinSelectAll') }}
        </button>
        <button
          type="button"
          class="button"
          data-test="pin-selection-add-to-board"
          :disabled="operationInFlight || selectedCount === 0"
          @click="$emit('add-to-board')"
        >
          {{ $t('bulkPinAddToBoard') }}
        </button>
        <button
          type="button"
          class="button"
          data-test="pin-selection-move"
          :disabled="operationInFlight || selectedCount === 0 || !canModify"
          @click="$emit('move')"
        >
          {{ $t('bulkPinMove') }}
        </button>
        <button
          type="button"
          class="button"
          data-test="pin-selection-edit"
          :disabled="operationInFlight || selectedCount === 0 || !canModify"
          @click="$emit('edit')"
        >
          {{ $t('bulkPinEdit') }}
        </button>
        <button
          type="button"
          class="button is-danger"
          data-test="pin-selection-delete"
          :disabled="operationInFlight || selectedCount === 0 || !canModify"
          @click="$emit('delete')"
        >
          {{ $t('bulkPinDelete') }}
        </button>
        <button
          type="button"
          class="button"
          data-test="pin-selection-exit"
          :disabled="operationInFlight"
          @click="$emit('exit')"
        >
          {{ $t('bulkPinSelectExit') }}
        </button>
      </div>
      <p
        class="is-sr-only"
        data-test="pin-selection-live"
        aria-live="polite"
        aria-atomic="true"
      >
        {{ announcement }}
      </p>
    </template>
  </div>
</template>

<script>
export default {
  name: 'PinBulkToolbar',
  props: {
    active: { type: Boolean, required: true },
    selectedCount: { type: Number, required: true },
    loadedCount: { type: Number, required: true },
    scope: { type: String, required: true },
    allCount: { type: Number, required: true },
    canModify: { type: Boolean, required: true },
    operationInFlight: { type: Boolean, required: true },
    announcement: { type: String, default: '' },
  },
};
</script>

<style lang="scss" scoped>
.pin-bulk-toolbar {
  margin-bottom: 1rem;
}

.pin-bulk-toolbar__summary {
  margin-bottom: .5rem;
}

.pin-bulk-toolbar__buttons {
  display: flex;
  flex-wrap: wrap;
  gap: .5rem;
}
</style>
