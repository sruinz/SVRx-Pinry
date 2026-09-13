module.exports = {
  root: true,
  env: {
    node: true,
  },
  extends: [
    'plugin:vue/vue3-essential',
    'airbnb-base',
    'plugin:vuejs-accessibility/recommended',
  ],
  settings: {
    'import/resolver': {
      alias: { map: [['@', './src']], extensions: ['.js', '.vue', '.json', '.mjs'] },
    },
  },
  rules: {
    'import/extensions': ['error', 'always', { js: 'never', mjs: 'never' }],
    'no-param-reassign': ['error', {
      props: true,
      ignorePropertyModificationsFor: ['state', 'acc', 'e'],
    }],
    'vuejs-accessibility/no-onchange': 'off',
    // 'no-console': process.env.NODE_ENV === 'production' ? 'error' : 'off',
    'no-debugger': process.env.NODE_ENV === 'production' ? 'error' : 'off',
    'max-len': 'off',
    'no-console': 'off',
    // 도구 전환에서는 기존 Airbnb 4 서식을 유지한다.
    'arrow-parens': ['error', 'as-needed', { requireForBlockBody: true }],
    'function-paren-newline': ['error', 'consistent'],
    'no-multiple-empty-lines': ['error', { max: 2, maxEOF: 0 }],
    // 새로 도입된 서식·접근성 검사는 경고로 남겨 화면 변경과 분리한다.
    'function-call-argument-newline': 'warn',
    'vue/multi-word-component-names': 'warn',
    'vuejs-accessibility/alt-text': 'warn',
    'vuejs-accessibility/anchor-has-content': 'warn',
    'vuejs-accessibility/click-events-have-key-events': 'warn',
    'vuejs-accessibility/interactive-supports-focus': 'warn',
    'vuejs-accessibility/label-has-for': ['warn', { required: { some: ['nesting', 'id'] } }],
    'vuejs-accessibility/mouse-events-have-key-events': 'warn',
    'vuejs-accessibility/no-autofocus': 'warn',
  },
  parserOptions: {
    parser: 'babel-eslint',
  },
};
