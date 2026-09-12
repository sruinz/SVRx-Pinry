/* eslint-env jest */
import { mount } from '@vue/test-utils';
import flushPromises from 'flush-promises';
import api from '@/components/api';
import SignUpForm from '@/components/SignUpForm.vue';
import TagInput from '@/components/ui/TagInput.vue';
import FileUpload from '@/components/pin_edit/FileUpload.vue';
import FilterSelect from '@/components/pin_edit/FilterSelect.vue';

describe('실제 입력 경계', () => {
  const wrappers = [];
  function render(component, props = {}) {
    const wrapper = mount(component, { props, global: { mocks: { $t: key => key } } });
    wrappers.push(wrapper);
    return wrapper;
  }
  afterEach(() => {
    wrappers.splice(0).forEach(wrapper => wrapper.unmount());
    jest.restoreAllMocks();
  });

  it('가입 이메일은 이메일 검증을 유지하고 비밀번호 표시 전환과 분리된다', async () => {
    jest.spyOn(api.SSO, 'policy').mockResolvedValue({ password_login_enabled: true });
    const wrapper = render(SignUpForm);
    await flushPromises();
    const email = wrapper.find('input[type="email"]');
    expect(email.exists()).toBe(true);
    await email.setValue('invalid-email');
    expect(email.element.validity.typeMismatch).toBe(true);
    await email.setValue('pin@example.com');
    expect(email.element.checkValidity()).toBe(true);
    expect(wrapper.vm.form.email.value).toBe('pin@example.com');
    const password = wrapper.find('input[type="password"]');
    await password.setValue('a-password');
    await wrapper.find('.password-input-toggle').trigger('click');
    expect(password.attributes('type')).toBe('text');
    expect(wrapper.vm.form.password.value).toBe('a-password');
    expect(email.attributes('type')).toBe('email');
  });

  it('태그 자동완성은 typing을 전달하고 키보드로 기존 태그만 선택한다', async () => {
    const wrapper = render(TagInput, {
      modelValue: [], data: ['alpha', 'beta'], allowNew: false, autocomplete: true,
    });
    expect(wrapper.find('input').exists()).toBe(true);
    await wrapper.find('input').setValue('unknown');
    await wrapper.find('input').trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('update:modelValue')).toBeUndefined();
    expect(wrapper.emitted('typing').at(-1)).toEqual(['unknown']);
    await wrapper.find('input').setValue('al');
    await wrapper.setProps({ data: ['alpha'] });
    await wrapper.find('input').trigger('keydown', { key: 'ArrowDown' });
    await wrapper.find('input').trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('update:modelValue').at(-1)).toEqual([['alpha']]);
  });

  it('새 태그 입력·삭제를 지원하고 disabled 중 변경을 막는다', async () => {
    const wrapper = render(TagInput, { modelValue: [], allowNew: true });
    expect(wrapper.find('input').exists()).toBe(true);
    await wrapper.find('input').setValue('new tag');
    await wrapper.find('input').trigger('keydown', { key: 'Enter' });
    expect(wrapper.emitted('update:modelValue')).toEqual([[['new tag']]]);
    await wrapper.setProps({ modelValue: ['new tag'] });
    await wrapper.find('button').trigger('click');
    expect(wrapper.emitted('update:modelValue').at(-1)).toEqual([[]]);
    await wrapper.setProps({ disabled: true });
    expect(wrapper.find('input').attributes('disabled')).toBeDefined();
    expect(wrapper.find('button').attributes('disabled')).toBeDefined();
  });

  it('다중 보드 선택은 DOM에서 숫자 ID 배열을 유지한다', async () => {
    const wrapper = render(FilterSelect);
    await wrapper.setProps({ allOptions: [{ value: 7, name: 'A' }, { value: 9, name: 'B', disabled: true }] });
    await wrapper.find('select').setValue(['7']);
    expect(wrapper.emitted('selected').at(-1)).toEqual([[7]]);
    expect(wrapper.find('option[value="9"]').attributes('disabled')).toBeDefined();
  });

  it('파일 선택과 드롭은 File 한 개를 전달하고 취소는 null을 전달한다', async () => {
    URL.createObjectURL = jest.fn(() => 'blob:preview');
    URL.revokeObjectURL = jest.fn();
    const wrapper = render(FileUpload);
    expect(wrapper.find('input[type="file"]').exists()).toBe(true);
    const file = new File(['image'], 'pin.png', { type: 'image/png' });
    Object.defineProperty(wrapper.find('input').element, 'files', { configurable: true, value: [file] });
    await wrapper.find('input').trigger('change');
    expect(wrapper.emitted('imageSelected').at(-1)).toEqual([file]);
    Object.defineProperty(wrapper.find('input').element, 'files', { configurable: true, value: [] });
    await wrapper.find('input').trigger('change');
    expect(wrapper.emitted('imageSelected').at(-1)).toEqual([null]);
    await wrapper.find('[data-test="file-drop"]').trigger('drop', { dataTransfer: { files: [file] } });
    expect(wrapper.emitted('imageSelected').at(-1)).toEqual([file]);
  });
});
