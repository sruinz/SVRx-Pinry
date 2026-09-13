import { createApp } from 'vue';
import PrimeVue from 'primevue/config';
import { VueMasonryPlugin } from 'vue-masonry';
import { createAppI18n } from './components/utils/i18n';
import App from './App.vue';
import router from './router';
import setUpAxiosCsrfConfig from './components/utils/csrf';
import './registerServiceWorker';
import { loadTheme } from './components/utils/theme';


setUpAxiosCsrfConfig();
loadTheme();

const i18n = createAppI18n(localStorage, document);

createApp(App)
  .use(router)
  .use(i18n)
  .use(PrimeVue, { unstyled: true })
  .use(VueMasonryPlugin)
  .mount('#app');
