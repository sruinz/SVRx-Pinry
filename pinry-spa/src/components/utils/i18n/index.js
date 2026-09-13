import { createI18n } from 'vue-i18n';
import en from './locales/en.json';
import zh from './locales/zh.json';
import fr from './locales/fr.json';
import ko from './locales/ko.json';

export const DEFAULT_LOCALE = 'ko';
export const SUPPORTED_LOCALES = ['ko', 'en', 'zh', 'fr'];

export function resolveLocale(storedLocale) {
  return SUPPORTED_LOCALES.includes(storedLocale)
    ? storedLocale
    : DEFAULT_LOCALE;
}

export function loadStoredLocale(storage) {
  try {
    return resolveLocale(storage.getItem('localeCode'));
  } catch (_error) {
    return DEFAULT_LOCALE;
  }
}

export function syncDocumentLocale(documentRef, locale) {
  const resolved = resolveLocale(locale);
  const rootElement = documentRef.documentElement;
  rootElement.lang = resolved;
  return resolved;
}

export function loadAndSyncStoredLocale(storage, documentRef) {
  return syncDocumentLocale(documentRef, loadStoredLocale(storage));
}

export function persistLocale(storage, locale) {
  const resolved = resolveLocale(locale);
  try {
    storage.setItem('localeCode', resolved);
  } catch (_error) {
    // 저장소 접근이 차단되어도 현재 화면의 언어 선택은 유지한다.
  }
  return resolved;
}

const messages = {
  ko,
  en,
  zh,
  fr,
};

export function createAppI18n(storage, documentRef) {
  return createI18n({
    legacy: false,
    globalInjection: true,
    locale: loadAndSyncStoredLocale(storage, documentRef),
    fallbackLocale: DEFAULT_LOCALE,
    messages,
  });
}

const langCode2Name = {
  ko: '한국어',
  en: 'English',
  zh: '简体中文',
  fr: 'Français',
};

export default {
  DEFAULT_LOCALE,
  SUPPORTED_LOCALES,
  loadAndSyncStoredLocale,
  loadStoredLocale,
  persistLocale,
  resolveLocale,
  syncDocumentLocale,
  messages,
  langCode2Name,
};
