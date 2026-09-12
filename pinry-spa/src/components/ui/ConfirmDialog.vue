<template>
  <section class="modal-card">
    <div class="modal-card-body">{{ message }}</div>
    <footer class="modal-card-foot">
      <button class="button" type="button" @click="$emit('close')">{{ $t('closeButton') }}</button>
      <button class="button is-primary" type="button" @click="$emit('confirm'); $emit('close')">
        {{ $t('confirmButton') }}
      </button>
    </footer>
  </section>
</template>

<script>
export default {
  name: 'ConfirmDialog',
  props: { message: { type: String, required: true } },
  emits: ['confirm', 'close'],
};
</script>
