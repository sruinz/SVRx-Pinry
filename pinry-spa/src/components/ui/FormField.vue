<template>
  <div class="field" :class="type">
    <label v-if="label" class="label" :for="labelFor || fieldId">{{ label }}</label>
    <div class="control"><slot /></div>
    <p v-if="message" class="help" :class="type" :id="`${fieldId}-error`" role="alert">
      {{ Array.isArray(message) ? message.join(' ') : message }}
    </p>
  </div>
</template>

<script>
export default {
  name: 'FormField',
  props: {
    label: { type: String, default: '' },
    labelFor: { type: String, default: '' },
    type: { type: String, default: null },
    message: { type: [String, Array], default: null },
  },
  data() { return { fieldId: `field-${this.$.uid}` }; },
  mounted() { this.connectLabel(); },
  updated() { this.connectLabel(); },
  methods: {
    connectLabel() {
      const input = this.$el.querySelector('input, textarea, select');
      if (!input) return;
      if (!input.id) input.id = this.labelFor || this.fieldId;
      const label = this.$el.querySelector('label');
      if (label) label.htmlFor = input.id;
      if (this.message) {
        input.setAttribute('aria-describedby', `${this.fieldId}-error`);
        input.setAttribute('aria-invalid', 'true');
      } else {
        input.removeAttribute('aria-describedby');
        input.removeAttribute('aria-invalid');
      }
    },
  },
};
</script>
