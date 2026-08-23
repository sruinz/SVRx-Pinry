<template>
  <div class="pin-sort" aria-labelledby="pin-sort-label">
    <span id="pin-sort-label" class="pin-sort__label">{{ $t('pinSortLabel') }}</span>
    <div class="pin-sort__buttons buttons has-addons">
      <button
        v-for="option in options"
        :key="option.mode"
        type="button"
        class="button"
        :class="{ 'is-primary': mode === option.mode }"
        :data-test="`pin-sort-${option.mode}`"
        :aria-pressed="String(mode === option.mode)"
        :disabled="disabled || busy"
        @click="$emit('select', option.mode)"
      >
        {{ $t(option.label) }}
      </button>
    </div>
    <span v-if="disabled" class="is-sr-only">{{ $t('pinSortDisabled') }}</span>
    <span data-test="pin-sort-announcement" class="is-sr-only" aria-live="polite">
      {{ announcement }}
    </span>
  </div>
</template>

<script>
export default {
  name: 'PinSortControls',
  props: {
    mode: { type: String, required: true },
    disabled: { type: Boolean, default: false },
    busy: { type: Boolean, default: false },
    announcement: { type: String, default: '' },
  },
  data() {
    return {
      options: [
        { mode: 'latest', label: 'pinSortLatest' },
        { mode: 'oldest', label: 'pinSortOldest' },
        { mode: 'random', label: 'pinSortRandom' },
      ],
    };
  },
};
</script>

<style scoped lang="scss">
.pin-sort { display: flex; align-items: center; flex-wrap: wrap; gap: .5rem; }
.pin-sort__buttons { margin-bottom: 0; }
.pin-sort__buttons .button { margin-bottom: 0; }
</style>
