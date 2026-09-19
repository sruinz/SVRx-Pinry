import { markRaw, nextTick, shallowReactive } from 'vue';
import ConfirmDialog from '../ui/ConfirmDialog.vue';

export const overlayState = shallowReactive({ modals: [], toasts: [], loading: 0 });
let sequence = 0;
let scrollState = null;

function lockScroll() {
  if (scrollState) return;
  scrollState = {
    body: document.body.style.overflow,
    html: document.documentElement.style.overflow,
    x: window.scrollX,
    y: window.scrollY,
  };
  document.body.style.overflow = 'hidden';
  document.documentElement.style.overflow = 'hidden';
}

function unlockScroll() {
  if (!scrollState || overlayState.modals.length || overlayState.loading) return;
  document.body.style.overflow = scrollState.body;
  document.documentElement.style.overflow = scrollState.html;
  if (window.scrollX !== scrollState.x || window.scrollY !== scrollState.y) {
    window.scrollTo(scrollState.x, scrollState.y);
  }
  scrollState = null;
}

function openModal(vm, {
  component, props = {}, events = {}, canCancel = true, onClose, customClass = '',
  width = '480px',
}) {
  const focused = document.activeElement;
  const entry = {
    id: sequence += 1,
    component: markRaw(component),
    props,
    events,
    canCancel,
    customClass,
    width,
    provides: vm && vm.$ ? vm.$.provides : null,
    closed: false,
    close() {
      if (entry.closed) return;
      entry.closed = true;
      overlayState.modals = overlayState.modals.filter(modal => modal !== entry);
      unlockScroll();
      if (onClose) onClose();
      nextTick(() => {
        if (focused && focused.isConnected) focused.focus({ preventScroll: true });
      });
    },
  };
  lockScroll();
  overlayState.modals = [...overlayState.modals, entry];
  return { close: entry.close };
}

function toast(options) {
  const config = typeof options === 'string' ? { message: options } : options;
  const entry = { id: sequence += 1, ...config };
  const close = () => {
    overlayState.toasts = overlayState.toasts.filter(item => item !== entry);
  };
  overlayState.toasts = [...overlayState.toasts, entry];
  setTimeout(close, config.duration || 3500);
  return { close };
}

function confirm(vm, { message, onConfirm, onCancel }) {
  let confirmed = false;
  return openModal(vm, {
    component: ConfirmDialog,
    props: { message },
    events: { confirm: () => { confirmed = true; if (onConfirm) onConfirm(); } },
    onClose: () => { if (!confirmed && onCancel) onCancel(); },
  });
}

function openLoading() {
  lockScroll();
  overlayState.loading += 1;
  let closed = false;
  return {
    close() {
      if (closed) return;
      closed = true;
      overlayState.loading -= 1;
      unlockScroll();
    },
  };
}

export default {
  openModal, toast, confirm, openLoading,
};
