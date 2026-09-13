/* eslint-env jest */
import { mount } from '@vue/test-utils';
import { nextTick, reactive } from 'vue';
import flushPromises from 'flush-promises';
import PinPreview from '@/components/PinPreview.vue';

function pin(id) {
  return {
    id,
    description: `Pin ${id}`,
    tags: [`tag-${id}`],
    author: 'owner',
    avatar: '',
    referer: null,
    original_image_url: `/original-${id}.gif`,
    large_image_url: `/image-${id}.gif`,
  };
}

describe('핀 상세 연속 감상', () => {
  // jsdom에는 브라우저의 innerText 구현이 없어 niceLinks의 텍스트 변환을 보완한다.
  const innerTextDescriptor = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'innerText');
  beforeAll(() => {
    Object.defineProperty(HTMLElement.prototype, 'innerText', {
      configurable: true,
      get() { return this.textContent; },
      set(value) { this.textContent = value; },
    });
  });
  afterAll(() => {
    if (innerTextDescriptor) Object.defineProperty(HTMLElement.prototype, 'innerText', innerTextDescriptor);
    else delete HTMLElement.prototype.innerText;
  });
  let wrapper;
  function open(props = {}) {
    wrapper = mount(PinPreview, {
      global: { mocks: { $t: key => key, $router: { push: jest.fn() } }, stubs: {} },
      props: { pinItem: pin(3), ...props },
    });
    return wrapper;
  }
  beforeEach(() => { window.localStorage.clear(); });
  afterEach(() => {
    if (wrapper) wrapper.unmount();
    jest.restoreAllMocks();
    jest.useRealTimers();
  });

  it('슬라이드쇼는 로딩 완료 후 5초씩 이동하고 마지막에서 멈춘다', async () => {
    jest.useFakeTimers();
    window.localStorage.setItem('pinry-preview-size', 'original');
    open({ navigation: () => ({ items: [pin(3), pin(8), pin(1)], hasNext: false }) });
    const play = wrapper.find('[data-test="preview-play"]');
    expect(play.exists()).toBe(true);
    expect(play.attributes('disabled')).toBeDefined();
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    await play.trigger('click');
    jest.advanceTimersByTime(4999);
    await nextTick();
    expect(wrapper.vm.currentPin.id).toBe(3);
    jest.advanceTimersByTime(1);
    await nextTick();
    expect(wrapper.vm.currentPin.id).toBe(8);
    jest.advanceTimersByTime(20000);
    await nextTick();
    expect(wrapper.vm.currentPin.id).toBe(8);
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    jest.advanceTimersByTime(5000);
    await nextTick();
    expect(wrapper.vm.currentPin.id).toBe(1);
    expect(play.attributes('aria-pressed')).toBe('false');
    expect(wrapper.find('[data-test="preview-zoom"]').attributes('aria-pressed')).toBe('true');
    expect(jest.getTimerCount()).toBe(0);
  });

  it.each(['pause', 'manual', 'error', 'hidden', 'close'])('%s 동작은 예약된 자동 이동을 취소한다', async (action) => {
    jest.useFakeTimers();
    open({ navigation: () => ({ items: [pin(3), pin(8), pin(1)], hasNext: false }) });
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    const play = wrapper.find('[data-test="preview-play"]');
    expect(play.exists()).toBe(true);
    await play.trigger('click');
    if (action === 'pause') await play.trigger('click');
    if (action === 'manual') await wrapper.find('[data-test="preview-next"]').trigger('click');
    if (action === 'error') await wrapper.find('[data-test="preview-image"]').trigger('error');
    if (action === 'hidden') {
      jest.spyOn(document, 'hidden', 'get').mockReturnValue(true);
      document.dispatchEvent(new Event('visibilitychange'));
    }
    if (action === 'close') await wrapper.find('[data-test="preview-close"]').trigger('click');
    await nextTick();
    jest.advanceTimersByTime(20000);
    await nextTick();
    expect(wrapper.vm.currentPin.id).toBe(action === 'manual' ? 8 : 3);
    expect(play.attributes('aria-pressed')).toBe('false');
    expect(jest.getTimerCount()).toBe(0);
  });

  it('다음 페이지 실패 시 자동 재시도를 반복하지 않는다', async () => {
    jest.useFakeTimers();
    open({ navigation: () => ({ items: [pin(3)], hasNext: true }), loadNext: () => Promise.reject(new Error('offline')) });
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    expect(wrapper.find('[data-test="preview-play"]').exists()).toBe(true);
    await wrapper.find('[data-test="preview-play"]').trigger('click');
    jest.advanceTimersByTime(5000);
    await Promise.resolve();
    await nextTick();
    expect(wrapper.text()).toContain('previewPageError');
    expect(wrapper.find('[data-test="preview-play"]').attributes('aria-pressed')).toBe('false');
    expect(jest.getTimerCount()).toBe(0);
  });

  it('자동 페이지 로딩 도중 일시정지하면 늦은 응답으로 이동하지 않는다', async () => {
    jest.useFakeTimers();
    const state = reactive({ items: [pin(3)], hasNext: true });
    let finish;
    open({ navigation: () => state, loadNext: () => new Promise((resolve) => { finish = resolve; }) });
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    await wrapper.find('[data-test="preview-play"]').trigger('click');
    jest.advanceTimersByTime(5000);
    await nextTick();
    await wrapper.find('[data-test="preview-play"]').trigger('click');
    state.items.push(pin(8));
    state.hasNext = false;
    finish();
    await Promise.resolve();
    await nextTick();
    expect(wrapper.vm.currentPin.id).toBe(3);
    expect(jest.getTimerCount()).toBe(0);
  });

  it('상세보기 제거 시 타이머를 남기지 않는다', async () => {
    jest.useFakeTimers();
    open({ navigation: () => ({ items: [pin(3), pin(8)], hasNext: false }) });
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    await wrapper.find('[data-test="preview-play"]').trigger('click');
    wrapper.unmount();
    wrapper = null;
    expect(jest.getTimerCount()).toBe(0);
  });

  it('전체 화면에서 이동하면 설명까지 내린 스크롤을 이미지 상단으로 돌린다', async () => {
    open({ navigation: () => ({ items: [pin(3), pin(8)], hasNext: false }) });
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, value: wrapper.element });
    wrapper.element.scrollTop = 500;
    try {
      await wrapper.find('[data-test="preview-next"]').trigger('click');
      expect(wrapper.element.scrollTop).toBe(0);
    } finally {
      delete document.fullscreenElement;
    }
  });

  it('전체 화면 미지원 환경은 버튼을 숨기고 상세보기는 유지한다', () => {
    open();
    expect(wrapper.find('[data-test="preview-fullscreen"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="preview-image"]').exists()).toBe(true);
  });

  it('전체 화면 진입·종료 상태를 실제 이벤트로 반영하고 닫을 때 정리한다', async () => {
    let fullscreen = null;
    Object.defineProperty(document, 'fullscreenElement', { configurable: true, get: () => fullscreen });
    Element.prototype.requestFullscreen = async function enter() {
      fullscreen = this;
      document.dispatchEvent(new Event('fullscreenchange'));
    };
    document.exitFullscreen = async () => {
      fullscreen = null;
      document.dispatchEvent(new Event('fullscreenchange'));
    };
    try {
      open();
      await nextTick();
      const button = wrapper.find('[data-test="preview-fullscreen"]');
      expect(button.exists()).toBe(true);
      await button.trigger('click');
      expect(fullscreen).toBe(wrapper.element);
      expect(button.text()).toContain('previewExitFullscreen');
      await button.trigger('click');
      expect(fullscreen).toBe(null);
      expect(button.text()).toContain('previewFullscreen');
      await button.trigger('click');
      await wrapper.find('[data-test="preview-close"]').trigger('click');
      expect(fullscreen).toBe(null);
    } finally {
      delete Element.prototype.requestFullscreen;
      delete document.exitFullscreen;
      delete document.fullscreenElement;
    }
  });

  it('전체 화면 요청 거부를 안내하고 모달을 닫지 않는다', async () => {
    Element.prototype.requestFullscreen = () => Promise.reject(new Error('denied'));
    try {
      open();
      await nextTick();
      expect(wrapper.find('[data-test="preview-fullscreen"]').exists()).toBe(true);
      await wrapper.find('[data-test="preview-fullscreen"]').trigger('click');
      await flushPromises();
      expect(wrapper.text()).toContain('previewFullscreenError');
      expect(wrapper.emitted('close')).toBeUndefined();
    } finally {
      delete Element.prototype.requestFullscreen;
    }
  });

  it('목록의 순서로 이미지·설명·링크를 바꾸고 끝에서는 순환하지 않는다', async () => {
    open({ navigation: () => ({ items: [pin(3), pin(8), pin(1)], hasNext: false }) });
    expect(wrapper.find('[data-test="preview-previous"]').attributes('disabled')).toBeDefined();
    await wrapper.find('[data-test="preview-next"]').trigger('click');
    expect(wrapper.find('[data-test="preview-image"]').attributes('src')).toBe('/image-8.gif');
    expect(wrapper.text()).toContain('Pin 8');
    expect(wrapper.find('a[href="/original-8.gif"]').exists()).toBe(true);
    await wrapper.find('[data-test="preview-next"]').trigger('click');
    expect(wrapper.find('[data-test="preview-next"]').attributes('disabled')).toBeDefined();
    await wrapper.find('[data-test="preview-previous"]').trigger('click');
    expect(wrapper.text()).toContain('Pin 8');
  });

  it('단독 핀에는 이동 UI가 없다', () => {
    open();
    expect(wrapper.find('[data-test="preview-navigation"]').exists()).toBe(false);
  });

  it('로드 후 원본 크기를 켜고 끄며 저장된 원본을 새 탭에서 연다', async () => {
    open();
    const toggle = wrapper.find('.preview-links [data-test="preview-zoom"]');
    expect(toggle.exists()).toBe(true);
    expect(toggle.attributes('disabled')).toBeDefined();
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    await toggle.trigger('click');
    expect(toggle.attributes('aria-pressed')).toBe('true');
    expect(wrapper.find('.preview-image-viewport').classes()).toContain('is-original-size');
    await wrapper.find('[data-test="preview-image-toggle"]').trigger('click');
    expect(toggle.attributes('aria-pressed')).toBe('false');
    const original = wrapper.find('[data-test="preview-stored-original"]');
    expect(original.attributes('href')).toBe('/image-3.gif');
    expect(original.attributes('target')).toBe('_blank');
  });

  it('다음·이전 이동에서 확대 선택은 유지하고 이미지 스크롤만 초기화한다', async () => {
    open({ navigation: () => ({ items: [pin(3), pin(8)], hasNext: false }) });
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    expect(wrapper.find('[data-test="preview-zoom"]').exists()).toBe(true);
    await wrapper.find('[data-test="preview-zoom"]').trigger('click');
    const viewport = wrapper.find('.preview-image-viewport').element;
    viewport.scrollTop = 120;
    viewport.scrollLeft = 80;
    await wrapper.find('[data-test="preview-next"]').trigger('click');
    expect(wrapper.find('[data-test="preview-zoom"]').attributes('aria-pressed')).toBe('true');
    expect([viewport.scrollTop, viewport.scrollLeft]).toEqual([0, 0]);
    expect(wrapper.find('[data-test="preview-stored-original"]').attributes('href')).toBe('/image-8.gif');
    await wrapper.find('[data-test="preview-previous"]').trigger('click');
    expect(wrapper.find('[data-test="preview-zoom"]').attributes('aria-pressed')).toBe('true');
    expect(wrapper.emitted('close')).toBeUndefined();
  });

  it('다시 열린 상세보기에서 원본 선택과 이후 화면 맞춤 선택을 각각 복원한다', async () => {
    open();
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    await wrapper.find('[data-test="preview-zoom"]').trigger('click');
    wrapper.unmount();
    open();
    expect(wrapper.find('[data-test="preview-zoom"]').attributes('aria-pressed')).toBe('true');
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    await wrapper.find('[data-test="preview-image-toggle"]').trigger('click');
    wrapper.unmount();
    open();
    expect(wrapper.find('[data-test="preview-zoom"]').attributes('aria-pressed')).toBe('false');
  });

  it('잘못된 저장 값은 화면 맞춤으로 시작한다', () => {
    window.localStorage.setItem('pinry-preview-size', 'invalid');
    open();
    expect(wrapper.find('[data-test="preview-zoom"]').attributes('aria-pressed')).toBe('false');
  });

  it('저장소가 차단되어도 확대 전환과 다음 이미지의 선택 유지는 동작한다', async () => {
    jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    open({ navigation: () => ({ items: [pin(3), pin(8)], hasNext: false }) });
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    await wrapper.find('[data-test="preview-zoom"]').trigger('click');
    await wrapper.find('[data-test="preview-next"]').trigger('click');
    expect(wrapper.find('[data-test="preview-zoom"]').attributes('aria-pressed')).toBe('true');
  });

  it('이전다음 클릭은 바깥 닫기에 전파되지 않고 순번만 이동한다', async () => {
    open({ navigation: () => ({ items: [pin(3), pin(8)], hasNext: false }) });
    const outsideClick = jest.fn();
    wrapper.element.addEventListener('click', outsideClick);
    await wrapper.find('[data-test="preview-next"]').trigger('click');
    expect(wrapper.find('.preview-position').text()).toBe('2 / 2');
    expect(wrapper.emitted('close')).toBeUndefined();
    expect(outsideClick).not.toHaveBeenCalled();
    await wrapper.find('[data-test="preview-previous"]').trigger('click');
    expect(wrapper.find('.preview-position').text()).toBe('1 / 2');
    expect(wrapper.emitted('close')).toBeUndefined();
    expect(outsideClick).not.toHaveBeenCalled();
    await wrapper.find('[data-test="preview-close"]').trigger('click');
    expect(wrapper.emitted('close')).toHaveLength(1);
  });

  it('닫기 애니메이션 중에는 방향키로 추가 요청을 하지 않는다', async () => {
    const loadNext = jest.fn();
    open({ navigation: () => ({ items: [pin(3)], hasNext: true }), loadNext });
    await wrapper.find('[data-test="preview-close"]').trigger('click');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    await nextTick();
    expect(loadNext).not.toHaveBeenCalled();
  });

  it('추가 로딩 중 연속 요청을 막고 다음 페이지의 첫 핀으로 이동한다', async () => {
    const state = reactive({ items: [pin(3)], hasNext: true });
    let complete;
    const loadNext = jest.fn(() => new Promise((resolve) => { complete = resolve; }));
    open({ navigation: () => state, loadNext });
    await wrapper.find('[data-test="preview-next"]').trigger('click');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    expect(loadNext).toHaveBeenCalledTimes(1);
    expect(wrapper.find('[data-test="preview-next"]').attributes('disabled')).toBeDefined();
    state.items.push(pin(8));
    state.hasNext = false;
    complete();
    await flushPromises();
    expect(wrapper.text()).toContain('Pin 8');
    expect(wrapper.find('[data-test="preview-next"]').attributes('disabled')).toBeDefined();
  });

  it('페이지 요청 실패는 현재 핀을 유지하고 재시도할 수 있다', async () => {
    const state = reactive({ items: [pin(3)], hasNext: true });
    const loadNext = jest.fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockImplementationOnce(() => { state.items.push(pin(8)); state.hasNext = false; });
    open({ navigation: () => state, loadNext });
    await wrapper.find('[data-test="preview-next"]').trigger('click');
    await flushPromises();
    expect(wrapper.text()).toContain('previewPageError');
    expect(wrapper.text()).toContain('Pin 3');
    await wrapper.find('[data-test="preview-next"]').trigger('click');
    await flushPromises();
    expect(wrapper.text()).toContain('Pin 8');
    expect(wrapper.text()).not.toContain('previewPageError');
  });

  it('방향키는 이동하지만 입력 중·수정키 조합에는 개입하지 않는다', async () => {
    open({ navigation: () => ({ items: [pin(3), pin(8)], hasNext: false }) });
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', ctrlKey: true }));
    const input = document.createElement('input');
    document.body.appendChild(input);
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    input.remove();
    await nextTick();
    expect(wrapper.text()).toContain('Pin 3');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
    await nextTick();
    expect(wrapper.text()).toContain('Pin 8');
  });

  it('이전 이미지의 늦은 load가 새 이미지의 로딩 상태를 해제하지 않는다', async () => {
    open({ navigation: () => ({ items: [pin(3), pin(8)], hasNext: false }) });
    const oldImage = wrapper.find('[data-test="preview-image"]').element;
    await wrapper.find('[data-test="preview-next"]').trigger('click');
    oldImage.dispatchEvent(new Event('load'));
    await nextTick();
    expect(wrapper.find('[data-test="preview-image"]').isVisible()).toBe(false);
    await wrapper.find('[data-test="preview-image"]').trigger('load');
    expect(wrapper.find('[data-test="preview-image"]').isVisible()).toBe(true);
  });

  it('이미지 실패를 알리고 다른 핀으로 이동하면 오류를 해제한다', async () => {
    open({ navigation: () => ({ items: [pin(3), pin(8)], hasNext: false }) });
    await wrapper.find('[data-test="preview-image"]').trigger('error');
    expect(wrapper.text()).toContain('previewImageError');
    await wrapper.find('[data-test="preview-next"]').trigger('click');
    expect(wrapper.text()).not.toContain('previewImageError');
  });
});
