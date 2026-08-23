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
    <p
      v-if="error"
      class="notification is-danger board-cover-toolbar__error"
      data-test="board-cover-error"
      role="alert"
    >
      {{ error }}
    </p>
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
    error: { type: String, default: null },
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
  width: 100%;
}

.board-cover-toolbar__buttons {
  display: flex;
  flex-wrap: wrap;
  gap: .5rem;
}

.board-cover-toolbar__error {
  margin-top: .75rem;
}

@media screen and (min-width: 769px) {
  .board-cover-toolbar {
    width: auto;
  }
}
</style>
