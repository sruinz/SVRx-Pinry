/** @jest-environment node */
/* eslint-env jest */
import fs from 'fs';
import path from 'path';
import { parse, compileStyle } from '@vue/compiler-sfc';
import * as sass from 'sass';

describe('컴포넌트 스타일 컴파일', () => {
  it.each([
    'Boards.vue',
    'Pins.vue',
    'UserProfileCard.vue',
    'editors/BoardEditUI.vue',
    'editors/PinEditorUI.vue',
    'pin_edit/FileUpload.vue',
  ])('%s는 폐기된 Sass import 없이 실제 스타일을 생성한다', (component) => {
    const filename = path.resolve(__dirname, '../../src/components', component);
    const { descriptor } = parse(fs.readFileSync(filename, 'utf8'));
    const style = descriptor.styles.find(block => block.lang === 'scss');
    const result = sass.compileString(style.content, {
      loadPaths: [path.dirname(filename)],
      fatalDeprecations: ['import'],
    });
    expect(result.css.length).toBeGreaterThan(0);
    const scoped = compileStyle({
      source: result.css, filename, id: 'data-v-style-test', scoped: true,
    });
    expect(scoped.errors).toEqual([]);
    expect(scoped.code).toContain('[data-v-style-test]');
  });
});
