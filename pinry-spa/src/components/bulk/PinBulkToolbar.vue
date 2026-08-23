<template>
  <div class="pin-bulk-toolbar" :class="{ 'is-active': active }">
    <button
      v-if="!active"
      type="button"
      class="button is-primary"
      data-test="pin-selection-enter"
      :disabled="enterDisabled"
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
          v-if="showAddToBoard"
          type="button"
          class="button"
          data-test="pin-selection-add-to-board"
          :disabled="operationInFlight || selectedCount === 0 || !canAddToBoard"
          @click="$emit('add-to-board')"
        >
          {{ $t('bulkPinAddToBoard') }}
        </button>
        <button
          v-if="showMove"
          type="button"
          class="button"
          data-test="pin-selection-move"
          :disabled="operationInFlight || selectedCount === 0 || !canMove"
          @click="$emit('move')"
        >
          {{ $t('bulkPinMove') }}
        </button>
        <button
          v-if="showEdit"
          type="button"
          class="button"
          data-test="pin-selection-edit"
          :disabled="operationInFlight || selectedCount === 0 || !canEdit"
          @click="$emit('edit')"
        >
          {{ $t('bulkPinEdit') }}
        </button>
        <button
          v-if="showDelete"
          type="button"
          class="button is-danger"
          data-test="pin-selection-delete"
          :disabled="operationInFlight || selectedCount === 0 || !canDelete"
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
    showAddToBoard: { type: Boolean, required: true },
    canAddToBoard: { type: Boolean, required: true },
    showMove: { type: Boolean, required: true },
    canMove: { type: Boolean, required: true },
    showEdit: { type: Boolean, required: true },
    canEdit: { type: Boolean, required: true },
    showDelete: { type: Boolean, required: true },
    canDelete: { type: Boolean, required: true },
    operationInFlight: { type: Boolean, required: true },
    enterDisabled: { type: Boolean, default: false },
    announcement: { type: String, default: '' },
  },
};
</script>

<style lang="scss" scoped>
.pin-bulk-toolbar {
  width: 100%;
}

.pin-bulk-toolbar__summary {
  margin-bottom: .5rem;
}

.pin-bulk-toolbar__buttons {
  display: flex;
  flex-wrap: wrap;
  gap: .5rem;
}

@media screen and (min-width: 769px) {
  .pin-bulk-toolbar {
    width: auto;
  }

  .pin-bulk-toolbar.is-active {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: .5rem;
    width: 100%;
  }

  .pin-bulk-toolbar__summary {
    margin-bottom: 0;
  }
}
</style>
