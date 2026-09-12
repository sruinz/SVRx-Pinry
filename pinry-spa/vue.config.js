const path = require('path');

module.exports = {
  devServer: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000/',
        changeOrigin: true,
        ws: true,
      },
      '/media': {
        target: 'http://127.0.0.1:8000/',
        changeOrigin: true,
      },
      '/static/js/': {
        target: 'http://127.0.0.1:8000/',
        changeOrigin: true,
      },
      '/static/auth/': {
        target: 'http://127.0.0.1:8000/',
        changeOrigin: true,
      },
    },
  },
  pwa: {
    name: 'SVRx Pinry',
    appleMobileWebAppCapable: 'yes',
    appleMobileWebAppStatusBarStyle: 'black',
    iconPaths: {
      favicon32: 'favicon.png',
      favicon16: 'favicon.png',
      appleTouchIcon: 'img/icons/android-chrome-192x192.png',
      maskIcon: null,
      msTileImage: 'img/icons/android-chrome-192x192.png',
    },
    manifestOptions: {
      icons: [
        {
          src: './img/icons/android-chrome-192x192.png',
          sizes: '192x192',
          type: 'image/png',
        },
        {
          src: './img/icons/android-chrome-512x512.png',
          sizes: '512x512',
          type: 'image/png',
        },
      ],
    },
  },
  chainWebpack: (config) => {
    config.plugins.delete('workbox');
    config.plugin('copy').tap((args) => {
      args[0].push({
        from: path.resolve(__dirname, 'src/service-worker.js'),
        to: path.resolve(__dirname, 'dist/service-worker.js'),
        toType: 'file',
      });
      return args;
    });
  },
};
