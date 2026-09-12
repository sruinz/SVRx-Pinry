const { readFileSync } = require('node:fs');
const { dirname, join } = require('node:path');

// 선언 범위가 아닌 실제 설치 버전을 빌드 결과에 기록한다.
module.exports = function buildDependencies() {
  const packages = [
    ['vue', 'Vue'], ['vue-router', 'Vue Router'],
    ['primevue', 'PrimeVue'], ['vue-i18n', 'vue-i18n'],
  ];
  return {
    frontend: packages.map(([key, label]) => ({
      key,
      label,
      // PrimeVue는 package.json을 exports에 공개하지 않는다.
      version: key === 'primevue'
        ? JSON.parse(readFileSync(join(dirname(require.resolve('primevue')), 'package.json'), 'utf8')).version
        : require(`${key}/package.json`).version,
    })),
    buildTools: [
      { key: 'node', label: 'Node.js', version: process.versions.node },
      { key: 'vite', label: 'Vite', version: require('vite/package.json').version },
    ],
  };
};
