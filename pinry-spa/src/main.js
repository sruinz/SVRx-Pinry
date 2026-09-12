import { createApp } from 'vue';
import PrimeVue from 'primevue/config';
import { VueMasonryPlugin } from 'vue-masonry';
import { createI18n } from 'vue-i18n';
import localeUtils, {
  DEFAULT_LOCALE,
  loadAndSyncStoredLocale,
} from './components/utils/i18n';
import App from './App.vue';
import router from './router';
import setUpAxiosCsrfConfig from './components/utils/csrf';
import './registerServiceWorker';


setUpAxiosCsrfConfig();

const i18n = createI18n({
  legacy: true,
  locale: loadAndSyncStoredLocale(localStorage, document),
  fallbackLocale: DEFAULT_LOCALE,
  messages: localeUtils.messages,
});

createApp(App)
  .use(router)
  .use(i18n)
  .use(PrimeVue, { unstyled: true })
  .use(VueMasonryPlugin)
  .mount('#app');
