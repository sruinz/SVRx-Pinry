/* eslint-env jest */
import fs from 'fs';
import path from 'path';
import { shallowMount } from '@vue/test-utils';
import { createI18n } from 'vue-i18n';
import { parse, compileStyle } from '@vue/compiler-sfc';
import PHeader from '@/components/PHeader.vue';
import api from '@/components/api';
import ko from '@/components/utils/i18n/locales/ko.json';

describe('화면 테마 선택', () => {
  let wrapper;
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
    jest.spyOn(api.User, 'fetchUserInfo').mockResolvedValue(null);
    jest.spyOn(api.SSO, 'policy').mockResolvedValue({ password_login_enabled: true });
  });
  afterEach(() => {
    if (wrapper) wrapper.unmount();
    jest.restoreAllMocks();
  });
  function mountHeader() {
    wrapper = shallowMount(PHeader, {
      global: {
        stubs: ['router-link'],
        plugins: [createI18n({ legacy: false, locale: 'ko', messages: { ko } })],
      },
    });
    return wrapper.find('[data-test="theme-toggle"]');
  }

  it('비로그인 사용자도 다크에서 라이트로 전환하고 선택을 유지한다', async () => {
    const button = mountHeader();
    expect(button.exists()).toBe(true);
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(button.attributes('aria-label')).toBe('라이트 모드로 전환');
    await button.trigger('click');
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(localStorage.getItem('pinry-theme')).toBe('light');
    expect(button.attributes('aria-label')).toBe('다크 모드로 전환');
    wrapper.unmount();
    mountHeader();
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('저장된 라이트 모드에서 다크 모드로 복귀한다', async () => {
    localStorage.setItem('pinry-theme', 'light');
    const button = mountHeader();
    expect(document.documentElement.dataset.theme).toBe('light');
    await button.trigger('click');
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(localStorage.getItem('pinry-theme')).toBe('dark');
  });

  it('테마 버튼을 접히는 메뉴 밖의 우측 도구로 배치한다', () => {
    const button = mountHeader();
    expect(button.element.parentElement.classList.contains('container')).toBe(true);
    expect(button.element.previousElementSibling.id).toBe('PinryNav');
    expect(wrapper.findAll('[data-test="theme-toggle"]')).toHaveLength(1);
  });

  it('잘못된 저장값은 기존 다크 화면으로 처리한다', () => {
    localStorage.setItem('pinry-theme', 'invalid');
    mountHeader();
    expect(document.documentElement.dataset.theme).toBe('dark');
  });

  it('브라우저 저장소가 차단되어도 현재 화면은 전환한다', async () => {
    jest.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked'); });
    jest.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked'); });
    const button = mountHeader();
    expect(button.exists()).toBe(true);
    await button.trigger('click');
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('컴파일된 헤더 스타일이 라이트 화면 전체를 어둡게 하지 않는다', () => {
    const filename = path.resolve(__dirname, '../../src/components/PHeader.vue');
    const { descriptor } = parse(fs.readFileSync(filename, 'utf8'));
    const style = document.createElement('style');
    style.textContent = compileStyle({
      source: descriptor.styles[0].content, filename, id: 'data-v-theme', scoped: true,
    }).code;
    document.head.appendChild(style);
    document.documentElement.dataset.theme = 'light';
    try {
      expect(['', 'none']).toContain(getComputedStyle(document.documentElement).filter);
    } finally {
      style.remove();
    }
  });
});
