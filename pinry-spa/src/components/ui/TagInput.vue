<template>
  <div class="tag-input">
    <div class="tag-input-control" :class="{ 'is-disabled': disabled }">
      <span v-for="tag in modelValue" :key="tag" class="tag">
        {{ tag }}
        <button type="button" :disabled="disabled" :aria-label="`${$t('closeButton')}: ${tag}`"
          @click="remove(tag)">×</button>
      </span>
      <input :id="inputId" :value="text" :disabled="disabled" :placeholder="placeholder"
        :aria-label="placeholder || $t('tagsLabel')" :role="autocomplete ? 'combobox' : undefined"
        :aria-expanded="autocomplete ? showOptions : undefined" :aria-controls="autocomplete ? listId : undefined"
        :aria-activedescendant="active >= 0 ? `${listId}-${active}` : undefined"
        autocomplete="off" @input="onInput" @keydown="onKeydown" @focus="focused = true"
        @blur="focused = false">
    </div>
    <ul v-if="showOptions" :id="listId" class="tag-input-options" role="listbox">
      <li v-for="(option, index) in options" :id="`${listId}-${index}`" :key="option"
        role="option" :aria-selected="index === active" @mousedown.prevent="add(option)">
        <slot :option="option">{{ option }}</slot>
      </li>
      <li v-if="!options.length" class="tag-input-empty"><slot name="empty" /></li>
    </ul>
  </div>
</template>
<script>
export default {
  name: 'TagInput',
  props: {
    modelValue: { type: Array, default: () => [] },
    data: { type: Array, default: () => [] },
    allowNew: { type: Boolean, default: true },
    autocomplete: { type: Boolean, default: false },
    openOnFocus: { type: Boolean, default: false },
    disabled: { type: Boolean, default: false },
    placeholder: { type: String, default: '' },
  },
  emits: ['update:modelValue', 'typing'],
  data() {
    return {
      text: '', active: -1, focused: false, inputId: `tags-${this.$.uid}`,
    };
  },
  computed: {
    listId() { return `${this.inputId}-options`; },
    options() { return this.data.filter(option => !this.modelValue.includes(option)); },
    showOptions() { return this.autocomplete && this.focused && (this.openOnFocus || this.text.length > 0); },
  },
  methods: {
    onInput(event) {
      this.text = event.target.value;
      this.active = -1;
      this.$emit('typing', this.text);
    },
    add(value) {
      if (this.disabled) return;
      const tag = String(value || '').trim();
      if (!tag || this.modelValue.includes(tag) || (!this.allowNew && !this.options.includes(tag))) return;
      this.$emit('update:modelValue', [...this.modelValue, tag]);
      this.text = '';
      this.active = -1;
      this.$emit('typing', '');
    },
    remove(tag) {
      if (!this.disabled) this.$emit('update:modelValue', this.modelValue.filter(value => value !== tag));
    },
    onKeydown(event) {
      if (this.disabled || event.isComposing || event.keyCode === 229) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        this.focused = true;
        this.active = Math.max(0, Math.min(this.options.length - 1,
          this.active + (event.key === 'ArrowDown' ? 1 : -1)));
      } else if (event.key === 'Enter' || event.key === ',') {
        event.preventDefault();
        this.add(this.active >= 0 ? this.options[this.active] : this.text);
      } else if (event.key === 'Backspace' && !this.text && this.modelValue.length) {
        this.remove(this.modelValue[this.modelValue.length - 1]);
      } else if (event.key === 'Escape') {
        this.focused = false;
        this.active = -1;
      }
    },
  },
};
</script>

<style scoped>
.tag-input { position: relative; width: 100%; }
.tag-input-control { min-height: 36px; display: flex; flex-wrap: wrap; align-items: center; gap: 4px; border: 1px solid #b5b5b5; border-radius: 4px; padding: 4px; background: #fff; }
.tag-input-control input { flex: 1; min-width: 100px; border: 0; background: transparent; color: inherit; font: inherit; outline: none; }
.tag-input-control:focus-within { outline: 2px solid #00c4a7; }
.tag button { border: 0; background: transparent; cursor: pointer; margin-left: 4px; }
.tag-input-options { position: absolute; z-index: 3; width: 100%; max-height: 200px; overflow: auto; margin: 0; background: #fff; border: 1px solid #b5b5b5; border-radius: 4px; list-style: none; }
.tag-input-options li { padding: 8px; cursor: pointer; }
.tag-input-options li[aria-selected="true"], .tag-input-options li:hover { background: #e4f8f3; }
.is-disabled { opacity: .6; }
</style>
