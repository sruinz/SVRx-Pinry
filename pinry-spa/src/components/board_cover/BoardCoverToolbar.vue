<template>
  <div class="board-cover-toolbar">
    <button
      v-if="!active"
      ref="enterButton"
      type="button"
      class="button"
      data-test="board-cover-enter"
      :disabled="disabled || busy"
      @click="$emit('enter')"
    >
      {{ $t('boardCoverEnter') }}
    </button>
    <div v-else class="board-cover-toolbar__buttons">
      <button
        type="button"
        class="button is-primary"
        data-test="board-cover-apply"
        :disabled="busy || selectedId === null"
        @click="$emit('apply')"
      >
        {{ $t('boardCoverApply') }}
      </button>
      <button
        type="button"
        class="button"
        data-test="board-cover-cancel"
        :disabled="busy"
        @click="$emit('cancel')"
      >
        {{ $t('boardCoverCancel') }}
      </button>
      <button
        type="button"
        class="button"
        data-test="board-cover-reset"
        :disabled="busy || !canReset"
        @click="$emit('reset')"
      >
        {{ $t('boardCoverReset') }}
      </button>
    </div>
  </div>
</template>

<script>
export default {
  name: 'BoardCoverToolbar',
  props: {
    active: { type: Boolean, required: true },
    selectedId: { type: Number, default: null },
    currentCoverId: { type: Number, default: null },
    busy: { type: Boolean, required: true },
    canReset: { type: Boolean, required: true },
    disabled: { type: Boolean, default: false },
  },
  methods: {
    focusEnter() {
      if (this.$refs.enterButton) this.$refs.enterButton.focus();
    },
  },
};
</script>

<style lang="scss" scoped>
.board-cover-toolbar {
  margin-bottom: 1rem;
}

.board-cover-toolbar__buttons {
  display: flex;
  flex-wrap: wrap;
  gap: .5rem;
}
</style>
