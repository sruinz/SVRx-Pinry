/* eslint-env jest */
import { mount } from '@vue/test-utils';
import { createRouter, createMemoryHistory } from 'vue-router';
import { createI18n } from 'vue-i18n';
import flushPromises from 'flush-promises';
import { ESLint } from 'eslint';
import API from '@/components/api';
import PHeader from '@/components/PHeader.vue';
import UserProfileCard from '@/components/UserProfileCard.vue';
import PinEditorUI from '@/components/editors/PinEditorUI.vue';
import BoardEditUI from '@/components/editors/BoardEditUI.vue';
import FormField from '@/components/ui/FormField.vue';
import TagInput from '@/components/ui/TagInput.vue';
import FileUpload from '@/components/pin_edit/FileUpload.vue';
import ko from '@/components/utils/i18n/locales/ko.json';

describe('키보드와 입력 접근성', () => {
  const wrappers = [];
  function render(component, options = {}) {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } },
        ...['user', 'boards4user', 'profile4user', 'exports', 'search'].map(name => ({
          name, path: `/${name}/:${name === 'user' ? 'user' : 'username'}?`, component: { template: '<div />' },
        }))],
    });
    const wrapper = mount(component, {
      attachTo: document.body,
      ...options,
      global: { plugins: [router, createI18n({ legacy: false, locale: 'ko', messages: { ko } })] },
    });
    wrappers.push(wrapper);
    return wrapper;
  }
  beforeEach(() => {
    jest.spyOn(API.User, 'fetchUserInfo').mockResolvedValue({ username: 'owner' });
    jest.spyOn(API.User, 'fetchUserInfoByName').mockResolvedValue({ username: 'owner', gravatar: '' });
    jest.spyOn(API.SSO, 'policy').mockResolvedValue({ password_login_enabled: true });
  });
  afterEach(() => {
    wrappers.splice(0).forEach(wrapper => wrapper.unmount());
    jest.restoreAllMocks();
  });

  it('메뉴는 버튼 실행으로 열리고 내부 이동 중 유지되며 Escape로 닫힌다', async () => {
    const wrapper = render(PHeader);
    await flushPromises();
    const menu = wrapper.find('.has-dropdown');
    const trigger = menu.find('.navbar-link');
    expect(trigger.element.tagName).toBe('BUTTON');
    trigger.element.focus();
    await wrapper.vm.$nextTick();
    expect(trigger.attributes('aria-expanded')).toBe('false');
    await trigger.trigger('click');
    expect(trigger.attributes('aria-expanded')).toBe('true');
    const action = menu.find('.navbar-dropdown .navbar-item');
    expect(action.element.tagName).toBe('BUTTON');
    action.element.focus();
    await menu.trigger('mouseleave');
    expect(trigger.attributes('aria-expanded')).toBe('true');
    const dialog = document.createElement('div');
    dialog.setAttribute('role', 'dialog');
    const dialogInput = document.createElement('input');
    dialog.appendChild(dialogInput);
    document.body.appendChild(dialog);
    try {
      dialogInput.focus();
      await menu.trigger('pointerleave');
      expect(trigger.attributes('aria-expanded')).toBe('true');
      action.element.focus();
    } finally {
      dialog.remove();
    }
    await action.trigger('keydown', { key: 'Escape' });
    expect(document.activeElement).toBe(trigger.element);
    expect(trigger.attributes('aria-expanded')).toBe('false');
    await trigger.trigger('click');
    expect(trigger.attributes('aria-expanded')).toBe('true');
    await trigger.trigger('click');
    expect(trigger.attributes('aria-expanded')).toBe('false');
    await trigger.trigger('click');
    wrapper.find('[data-test="theme-toggle"]').element.focus();
    await wrapper.vm.$nextTick();
    expect(trigger.attributes('aria-expanded')).toBe('false');
  });

  it('터치 포인터는 호버로 열지 않고 다시 누르거나 모바일 메뉴를 닫으면 접힌다', async () => {
    const wrapper = render(PHeader);
    await flushPromises();
    const menu = wrapper.find('.has-dropdown');
    const trigger = menu.find('.navbar-link');
    await menu.trigger('pointerenter', { pointerType: 'touch' });
    expect(trigger.attributes('aria-expanded')).toBe('false');
    await trigger.trigger('click');
    expect(trigger.attributes('aria-expanded')).toBe('true');
    await trigger.trigger('click');
    expect(trigger.attributes('aria-expanded')).toBe('false');
    await wrapper.find('.navbar-burger').trigger('click');
    await trigger.trigger('click');
    await wrapper.find('.navbar-burger').trigger('click');
    expect(trigger.attributes('aria-expanded')).toBe('false');
  });

  it('모바일 메뉴와 비로그인 동작이 기본 버튼으로 제공된다', async () => {
    API.User.fetchUserInfo.mockResolvedValue(null);
    const wrapper = render(PHeader);
    await flushPromises();
    const burger = wrapper.find('.navbar-burger');
    expect(burger.element.tagName).toBe('BUTTON');
    await burger.trigger('click');
    expect(wrapper.find('#PinryNav').classes()).toContain('is-active');
    expect(wrapper.findAll('.buttons .button').every(button => button.element.tagName === 'BUTTON')).toBe(true);
  });

  it.each([
    [PinEditorUI, {
      pin: { id: 1, author: 'owner' },
      currentUsername: 'owner',
      currentBoard: { id: 1, submitter: { username: 'owner' } },
    }, 4],
    [BoardEditUI, { board: { id: 1, name: 'board' } }, 2],
  ])('편집 아이콘은 이름이 있는 탭 가능한 버튼이다', (component, props, count) => {
    const wrapper = render(component, { props });
    const controls = wrapper.findAll('.icon-container');
    expect(controls).toHaveLength(count);
    controls.forEach((control) => {
      expect(control.element.tagName).toBe('BUTTON');
      expect(control.element.tabIndex).toBe(0);
      expect(control.attributes('aria-label')).toBeTruthy();
    });
  });

  it('프로필 탐색은 실제 목적지가 있는 링크이다', async () => {
    const wrapper = render(UserProfileCard, { props: { username: 'owner' } });
    await flushPromises();
    const links = wrapper.findAll('.tabs a');
    expect(links).toHaveLength(3);
    expect(links.map(link => link.attributes('href'))).toEqual([
      '/user/owner', '/boards4user/owner', '/profile4user/owner',
    ]);
  });

  it('동적 입력과 명시적 입력 모두 라벨이 실제 컨트롤에 연결된다', () => {
    const first = render(FormField, { props: { label: '계정' }, slots: { default: '<input>' } });
    const second = render(FormField, { props: { label: '이메일' }, slots: { default: '<input id="email">' } });
    [first, second].forEach((wrapper) => {
      expect(wrapper.find('label').element.control).toBe(wrapper.find('input').element);
    });
    expect(first.find('input').attributes('id')).not.toBe(second.find('input').attributes('id'));
  });

  it('자동완성 옵션은 입력 포커스를 유지하며 화살표로 선택된다', async () => {
    const wrapper = render(TagInput, {
      props: {
        modelValue: [], data: ['alpha', 'beta'], autocomplete: true, allowNew: false,
      },
    });
    const input = wrapper.find('input');
    input.element.focus();
    await input.setValue('a');
    await input.trigger('keydown', { key: 'ArrowDown' });
    const option = wrapper.find('[role="option"]');
    expect(option.attributes('tabindex')).toBe('-1');
    expect(input.attributes('aria-activedescendant')).toBe(option.attributes('id'));
    expect(document.activeElement).toBe(input.element);
    await input.trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('update:modelValue').at(-1)).toEqual([['alpha']]);
  });

  it('업로드 미리보기는 대체 텍스트가 있다', () => {
    expect(render(FileUpload).find('img').attributes('alt')).toBeTruthy();
  });
});

it('라벨 검사는 중첩과 for 연결을 인정하고 연결 없는 라벨을 검출한다', async () => {
  const eslint = new ESLint();
  const [result] = await eslint.lintText(
    '<template><div><label for="name">계정</label><input id="name"><label>비번<input></label><label>고립</label></div></template>',
    { filePath: 'src/LabelFixture.vue' },
  );
  expect(result.messages.filter(message => message.ruleId === 'vuejs-accessibility/label-has-for')).toHaveLength(1);
});
