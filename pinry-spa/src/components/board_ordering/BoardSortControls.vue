<template>
  <div class="board-sort" aria-labelledby="board-sort-label">
    <span id="board-sort-label" class="board-sort__label">{{ $t('boardSortLabel') }}</span>
    <div class="board-sort__buttons buttons has-addons">
      <button
        v-for="option in options"
        :key="option.mode"
        type="button"
        class="button"
        :class="{ 'is-primary': mode === option.mode }"
        :data-test="`board-sort-${option.mode}`"
        :aria-pressed="String(mode === option.mode)"
        :disabled="busy"
        @click="$emit('select', option.mode)"
      >
        {{ $t(option.label) }}
      </button>
    </div>
  </div>
</template>

<script>
export default {
  name: 'BoardSortControls',
  props: {
    mode: { type: String, required: true },
    busy: { type: Boolean, default: false },
  },
  data() {
    return {
      options: [
        { mode: 'custom', label: 'boardSortCustom' },
        { mode: 'latest', label: 'boardSortLatest' },
        { mode: 'oldest', label: 'boardSortOldest' },
        { mode: 'random', label: 'boardSortRandom' },
      ],
    };
  },
};
</script>

<style scoped lang="scss">
.board-sort { display: flex; align-items: center; flex-wrap: wrap; gap: .5rem; }
.board-sort__buttons { margin-bottom: 0; }
.board-sort__buttons .button { margin-bottom: 0; }
</style>
