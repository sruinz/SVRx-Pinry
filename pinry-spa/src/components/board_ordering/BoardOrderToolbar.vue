<template>
  <div
    v-if="canEnter || editing || loading || error"
    class="board-order-toolbar"
    data-test="board-order-toolbar"
  >
    <button
      v-if="!editing"
      type="button"
      class="button is-light board-order-toolbar__enter"
      :class="{ 'is-loading': loading }"
      :disabled="!canEnter || loading"
      data-test="board-order-enter"
      @click="$emit('enter')"
    >
      {{ $t('boardOrderEnter') }}
    </button>
    <div v-else class="board-order-toolbar__editing">
      <div class="board-order-toolbar__actions buttons">
        <button
          type="button"
          class="button is-primary"
          :class="{ 'is-loading': loading }"
          :disabled="loading || !changed"
          data-test="board-order-save"
          @click="$emit('save')"
        >
          {{ $t('boardOrderSave') }}
        </button>
        <button
          type="button"
          class="button is-light"
          :disabled="loading"
          data-test="board-order-cancel"
          @click="$emit('cancel')"
        >
          {{ $t('boardOrderCancel') }}
        </button>
      </div>
      <span v-if="changed" class="board-order-toolbar__changed">
        {{ $t('boardOrderChanged') }}
      </span>
    </div>
    <p
      v-if="error"
      class="board-order-toolbar__error"
      role="alert"
      data-test="board-order-error"
    >
      {{ error }}
    </p>
    <p
      class="is-sr-only"
      aria-live="polite"
      aria-atomic="true"
      data-test="board-order-announcement"
    >
      {{ announcement }}
    </p>
  </div>
</template>

<script>
export default {
  name: 'BoardOrderToolbar',
  props: {
    canEnter: { type: Boolean, default: false },
    editing: { type: Boolean, default: false },
    loading: { type: Boolean, default: false },
    changed: { type: Boolean, default: false },
    error: { type: String, default: '' },
    announcement: { type: String, default: '' },
  },
};
</script>

<style scoped lang="scss">
.board-order-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: .55rem .75rem;
  min-height: 2.25rem;
}

.board-order-toolbar__editing {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: .5rem;
}

.board-order-toolbar__actions { margin-bottom: 0; }
.board-order-toolbar__actions .button { margin-bottom: 0; }
.board-order-toolbar__changed { color: #5f6570; font-size: .875rem; }
.board-order-toolbar__error { flex-basis: 100%; color: #b23a3a; font-size: .875rem; }

@media screen and (max-width: 480px) {
  .board-order-toolbar,
  .board-order-toolbar__editing { align-items: stretch; width: 100%; }
  .board-order-toolbar__actions { display: flex; width: 100%; }
  .board-order-toolbar__actions .button { flex: 1; }
}
</style>
