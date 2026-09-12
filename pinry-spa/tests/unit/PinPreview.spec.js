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
  afterEach(() => { if (wrapper) wrapper.unmount(); });

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
