/* eslint-env jest */
import { mount } from '@vue/test-utils';
import { nextTick } from 'vue';
import { createI18n } from 'vue-i18n';
import { createMemoryHistory, createRouter } from 'vue-router';
import PrimeVue from 'primevue/config';
import AppOverlays from '@/components/ui/AppOverlays.vue';
import PinPreview from '@/components/PinPreview.vue';
import ko from '@/components/utils/i18n/locales/ko.json';
import overlays from '@/components/utils/overlays';

const Content = {
  props: ['navigation'],
  emits: ['close', 'completed'],
  template: `<section><p>{{ $t('context') }} {{ $route.path }} {{ navigation().label }}</p>
    <button data-test="complete" @click="$emit('completed', { id: 17 })">완료</button>
    <button data-test="close" @click="$emit('close'); $emit('close')">닫기</button></section>`,
};

describe('실제 앱 모달 경계', () => {
  let wrapper;
  let trigger;
  const handles = [];
  beforeEach(async () => {
    trigger = document.createElement('button');
    document.body.appendChild(trigger);
    trigger.focus();
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/pins', component: { template: '<div />' } }],
    });
    await router.push('/pins');
    wrapper = mount(AppOverlays, {
      attachTo: document.body,
      global: {
        plugins: [
          [PrimeVue, { unstyled: true }],
          router,
          createI18n({ legacy: false, locale: 'ko', messages: { ko: { ...ko, context: '전달된 문맥' } } }),
        ],
      },
    });
  });
  afterEach(async () => {
    handles.splice(0).forEach(handle => handle.close());
    await nextTick();
    wrapper.unmount();
    trigger.remove();
  });
  function open(options = {}) {
    const handle = overlays.openModal(wrapper.vm, {
      component: Content,
      props: { navigation: () => ({ label: '목록' }) },
      ...options,
    });
    handles.push(handle);
    return handle;
  }

  it('앱의 라우터·번역·함수 참조와 실제 자식 완료 이벤트를 전달한다', async () => {
    const completed = jest.fn();
    open({ events: { completed } });
    await nextTick();
    expect(document.body.textContent).toContain('전달된 문맥 /pins 목록');
    document.querySelector('[data-test="complete"]').click();
    expect(completed).toHaveBeenCalledWith({ id: 17 });
  });

  it('전체 화면의 Escape는 브라우저 종료에 맡기고 모달을 유지한다', async () => {
    open();
    await nextTick();
    Object.defineProperty(document, 'fullscreenElement', {
      configurable: true, value: document.querySelector('[data-test="close"]'),
    });
    try {
      const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
      document.dispatchEvent(event);
      await nextTick();
      expect(document.querySelector('[data-test="close"]')).not.toBeNull();
      expect(event.defaultPrevented).toBe(false);
    } finally {
      delete document.fullscreenElement;
    }
  });

  it('취소 금지 모달은 Escape와 바깥 클릭으로 닫히지 않는다', async () => {
    const onClose = jest.fn();
    open({ canCancel: false, onClose });
    await nextTick();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    document.querySelector('.app-modal-mask').dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    document.querySelector('.app-modal-mask').dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    await nextTick();
    expect(document.querySelector('[data-test="close"]')).not.toBeNull();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('확인 모달은 취소 버튼에 초점을 두고 닫으면 원래 실행 버튼으로 복원한다', async () => {
    handles.push(overlays.confirm(wrapper.vm, { message: '삭제하시겠습니까?' }));
    await nextTick();
    expect(document.querySelector('[role="dialog"]').contains(document.activeElement)).toBe(true);
    expect(document.activeElement.textContent).toBe('닫기');
    document.activeElement.click();
    await nextTick();
    expect(document.activeElement).toBe(trigger);
  });

  it('명시적 닫기는 한 번만 통지하고 내용·스크롤 잠금 해제와 초점을 복원한다', async () => {
    const onClose = jest.fn();
    open({ onClose });
    await nextTick();
    const close = document.querySelector('[data-test="close"]');
    expect(close).not.toBeNull();
    expect(document.querySelector('[role="dialog"]').contains(document.activeElement)).toBe(true);
    close.click();
    await nextTick();
    await nextTick();
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(document.querySelector('[data-test="close"]')).toBeNull();
    expect(document.activeElement).toBe(trigger);
    expect(document.body.style.overflow).not.toBe('hidden');
  });

  it('중첩 모달에서 Escape는 맨 위만 닫고 아래 모달과 스크롤 잠금을 유지한다', async () => {
    const lowerClosed = jest.fn();
    const upperClosed = jest.fn();
    open({ onClose: lowerClosed });
    await nextTick();
    const lowerFocus = document.activeElement;
    expect(document.querySelector('[role="dialog"]').contains(lowerFocus)).toBe(true);
    open({ onClose: upperClosed });
    await nextTick();
    const dialogs = document.querySelectorAll('[role="dialog"]');
    expect(dialogs[1].contains(document.activeElement)).toBe(true);
    expect(dialogs[0].contains(document.activeElement)).toBe(false);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await nextTick();
    expect(upperClosed).toHaveBeenCalledTimes(1);
    expect(lowerClosed).not.toHaveBeenCalled();
    expect(document.querySelectorAll('[data-test="close"]')).toHaveLength(1);
    expect(document.body.style.overflow).toBe('hidden');
    expect(document.activeElement).toBe(lowerFocus);
  });

  it('실제 상세보기의 다음 클릭은 모달을 유지하고 외부 닫기 직후 추가 요청을 막는다', async () => {
    const items = [1, 2].map(id => ({
      id,
      description: '',
      author: 'owner',
      avatar: '',
      tags: [],
      referer: null,
      large_image_url: `/image-${id}.jpg`,
      original_image_url: `/image-${id}.jpg`,
    }));
    const loadNext = jest.fn();
    const onClose = jest.fn();
    const handle = open({
      component: PinPreview,
      props: { pinItem: items[0], navigation: () => ({ items, hasNext: true }), loadNext },
      onClose,
    });
    await nextTick();
    expect(document.activeElement).toBe(document.querySelector('[data-test="preview-close"]'));
    const next = document.querySelector('[data-test="preview-next"]');
    next.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
    next.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
    next.click();
    await nextTick();
    expect(document.querySelector('[data-test="preview-image"]').getAttribute('src')).toBe('/image-2.jpg');
    expect(onClose).not.toHaveBeenCalled();
    handle.close();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    expect(loadNext).not.toHaveBeenCalled();
    await nextTick();
    expect(document.querySelector('[data-test="preview-image"]')).toBeNull();
  });
});
