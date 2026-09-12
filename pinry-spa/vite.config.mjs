import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import buildDependencies from './scripts/build-dependencies.cjs';

export default defineConfig({
  define: { __PINRY_BUILD_DEPENDENCIES__: JSON.stringify(buildDependencies()) },
  plugins: [vue({ template: { transformAssetUrls: { includeAbsolute: false } } }), {
    name: 'pinry-network-only-worker',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'service-worker.js',
        source: readFileSync(new URL('./src/service-worker.js', import.meta.url), 'utf8'),
      });
    },
  }],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: {
    outDir: 'dist',
    // 최근 두 버전 browserslist보다 넓은 문법 지원 범위를 유지한다.
    target: ['chrome87', 'edge88', 'firefox78', 'safari14'],
  },
  server: {
    proxy: Object.fromEntries(['/api', '/media', '/static/js/', '/static/auth/'].map(prefix => [
      prefix, { target: 'http://127.0.0.1:8000/', changeOrigin: true },
    ])),
  },
});
