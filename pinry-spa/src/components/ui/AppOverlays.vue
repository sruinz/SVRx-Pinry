<template>
  <div class="app-overlays">
    <Dialog
      v-for="entry in state.modals"
      :key="entry.id"
      :visible="true"
      modal
      :block-scroll="false"
      :closable="false"
      :close-on-escape="false"
      :dismissable-mask="canCancel(entry, 'outside')"
      :draggable="false"
      :class="['app-modal', entry.customClass]"
      :pt="{ mask: { class: 'app-modal-mask' }, content: { class: 'modal-content' } }"
      @update:visible="entry.close()">
      <template #container>
        <div class="modal-content" tabindex="-1" @vue:mounted="focusModal(entry, $event.el)">
          <ModalContent :entry="entry" />
        </div>
      </template>
    </Dialog>
    <div class="app-toasts" aria-live="polite">
      <div v-for="item in state.toasts" :key="item.id" class="notification" :class="item.type" role="status">
        {{ item.message }}
      </div>
    </div>
    <div v-if="state.loading" class="app-loading" role="status" aria-live="polite" :aria-label="$t('loading')">
      <span class="app-loading-spinner" />
    </div>
  </div>
</template>

<script>
import Dialog from 'primevue/dialog';
import ModalContent from './ModalContent';
import { overlayState } from '../utils/overlays';

export default {
  name: 'AppOverlays',
  components: { Dialog, ModalContent },
  data: () => ({ state: overlayState }),
  mounted() {
    document.addEventListener('keydown', this.onKeydown, true);
  },
  beforeUnmount() {
    document.removeEventListener('keydown', this.onKeydown, true);
    [...this.state.modals].reverse().forEach(entry => entry.close());
  },
  methods: {
    focusModal(entry, content) {
      if (entry !== this.state.modals[this.state.modals.length - 1]
          || content.contains(document.activeElement)) return;
      const target = content.querySelector('[autofocus]:not([disabled])')
        || content.querySelector('button:not([disabled]), a[href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]')
        || content;
      target.focus({ preventScroll: true });
    },
    canCancel(entry, action) {
      if (entry !== this.state.modals[this.state.modals.length - 1]) return false;
      return entry.canCancel === true
        || (Array.isArray(entry.canCancel) && entry.canCancel.includes(action));
    },
    onKeydown(event) {
      const top = this.state.modals[this.state.modals.length - 1];
      if (!top || event.key !== 'Escape') return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (this.canCancel(top, 'escape')) top.close();
    },
  },
};
</script>

<style>
.app-modal-mask { background: rgba(0, 0, 0, .7); padding: 24px; }
.app-modal { max-width: calc(100vw - 48px); max-height: calc(100dvh - 48px); }
.app-modal .modal-content { display: block; max-height: calc(100dvh - 48px); overflow: auto; margin: 0; }
.app-modal .modal-card { max-width: 100%; margin: 0; }
.app-modal.pin-preview-at-home { width: 900px; }
.app-modal.pin-preview-at-home .modal-content { width: 100%; padding-inline: 72px; }
@media screen and (max-width: 768px) {
  .app-modal-mask { padding: 12px; }
  .app-modal { max-width: calc(100vw - 24px); max-height: calc(100dvh - 24px); }
  .app-modal .modal-content { max-width: 100%; max-height: calc(100dvh - 24px); }
  .app-modal.pin-preview-at-home .modal-content { padding-inline: 0; }
}
.app-toasts { position: fixed; top: 24px; right: 24px; z-index: 1300; max-width: min(400px, calc(100vw - 48px)); }
.app-loading { position: fixed; inset: 0; z-index: 1400; display: grid; place-items: center; background: rgba(0, 0, 0, .35); }
.app-loading-spinner { width: 40px; height: 40px; border: 4px solid #fff; border-top-color: transparent; border-radius: 50%; animation: app-spin .8s linear infinite; }
@keyframes app-spin { to { transform: rotate(360deg); } }
</style>
