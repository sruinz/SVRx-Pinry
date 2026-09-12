<template>
  <div class="password-input">
    <input v-bind="$attrs" class="input" :type="revealed ? 'text' : 'password'" :aria-label="$t('passwordLabel')"
      :value="modelValue" @input="$emit('update:modelValue', $event.target.value)">
    <button class="password-input-toggle" type="button" :disabled="$attrs.disabled"
      :aria-label="$attrs.placeholder || $t('passwordLabel')" :aria-pressed="revealed"
      @click="revealed = !revealed">
      <i class="mdi" :class="revealed ? 'mdi-eye-off' : 'mdi-eye'" aria-hidden="true" />
    </button>
  </div>
</template>

<script>
export default {
  name: 'PasswordInput',
  inheritAttrs: false,
  props: { modelValue: { type: String, default: '' } },
  emits: ['update:modelValue'],
  data: () => ({ revealed: false }),
};
</script>

<style scoped>
.password-input { position: relative; }
.password-input .input { padding-right: 42px; }
.password-input-toggle { position: absolute; right: 0; top: 0; height: 100%; width: 42px; border: 0; background: transparent; color: inherit; cursor: pointer; }
</style>
