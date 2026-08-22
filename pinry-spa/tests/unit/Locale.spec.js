/* eslint-env jest */
import VueI18n from 'vue-i18n';
import { createLocalVue, shallowMount } from '@vue/test-utils';

import PHeader from '@/components/PHeader.vue';
import localeUtils, {
  DEFAULT_LOCALE,
  SUPPORTED_LOCALES,
  loadAndSyncStoredLocale,
  loadStoredLocale,
  persistLocale,
  resolveLocale,
  syncDocumentLocale,
} from '@/components/utils/i18n';
import en from '@/components/utils/i18n/locales/en.json';
import fr from '@/components/utils/i18n/locales/fr.json';
import ko from '@/components/utils/i18n/locales/ko.json';
import zh from '@/components/utils/i18n/locales/zh.json';


const EXPECTED_LOCALE_KEYS = [
  'Add2BoardModalCardButton',
  'Add2BoardModalCardTitle',
  'BoardCreateTitle',
  'BoardEditTitle',
  'EditPinTitle',
  'FileUploadDescription',
  'NewPinTitle',
  'SearchPanelBoardOption',
  'SearchPanelTagOption',
  'boardDeleteActualExclusiveCount',
  'boardDeleteCancel',
  'boardDeleteError',
  'boardDeleteLoadingPreview',
  'boardDeleteOnly',
  'boardDeletePreviewError',
  'boardDeletePreviewExclusive',
  'boardDeletePreviewNonOwned',
  'boardDeletePreviewShared',
  'boardDeleteResult',
  'boardDeleteRetryRefresh',
  'boardDeleteSelectionInvalid',
  'boardDeleteSelectionLoadError',
  'boardDeleteSelectionRefreshError',
  'boardDeleteTitle',
  'boardDeleteWithExclusivePins',
  'boardLink',
  'boardNamePlaceholder',
  'boardsLink',
  'boardsUserProfileCardLink',
  'bookmarkletLink',
  'browserExtensionsLink',
  'buildBrandLabel',
  'buildInfoTitle',
  'buildVersionLabel',
  'bulkPinAddToBoard',
  'bulkPinAddToBoardTitle',
  'bulkPinAllSelected',
  'bulkPinApply',
  'bulkPinChooseBoard',
  'bulkPinClear',
  'bulkPinDelete',
  'bulkPinDeleteConfirm',
  'bulkPinEdit',
  'bulkPinEditTitle',
  'bulkPinMakePrivate',
  'bulkPinMakePublic',
  'bulkPinMove',
  'bulkPinMoveTitle',
  'bulkPinNoPrivacyChange',
  'bulkPinNoTagChange',
  'bulkPinProgress',
  'bulkPinResultFailed',
  'bulkPinResultPreserved',
  'bulkPinResultSucceeded',
  'bulkPinRetry',
  'bulkPinSelectAll',
  'bulkPinSelectExit',
  'bulkPinSelectLoaded',
  'bulkPinSelectOne',
  'bulkPinSelectStart',
  'bulkPinSelectedCount',
  'bulkPinSelectionTooLarge',
  'bulkPinTagAdd',
  'bulkPinTagRemove',
  'bulkPinTagReplace',
  'bulkPinTargetBoard',
  'chooseFilterPlaceholder',
  'chromeLink',
  'closeButton',
  'createBoardButton',
  'createLink',
  'customExtensionPendingLink',
  'descriptionLabel',
  'drfApiDocumentationLink',
  'emailLabel',
  'emailPlaceholder',
  'end',
  'error404',
  'filterSelectCreateNewBoardButton',
  'filterSelectSelectBoardPlaceholder',
  'firefoxLink',
  'forMoreDetailsParagraph',
  'imageSourceLabel',
  'imageUrlLabel',
  'isPrivateCheckbox',
  'logInLink',
  'logOutLink',
  'loginButton',
  'loginTitle',
  'myLink',
  'nameLabel',
  'noResultsFound',
  'originalImageButton',
  'pageNotFound',
  'passwordLabel',
  'passwordLoginPlaceholder',
  'passwordSignUpPlaceholder',
  'permalinkButton',
  'pinCreateError',
  'pinCreateModalCreatePinButton',
  'pinCreateModalEmptySlot',
  'pinCreateModalImageDescriptionPlaceholder',
  'pinCreateModalImageSourcePlaceholder',
  'pinCreateModalImageTagsPlaceholder',
  'pinCreateModalImageURLPlaceholder',
  'pinCreateModalSaveChangesButton',
  'pinDeleteConfirm',
  'pinDeleteError',
  'pinDeleted',
  'pinLink',
  'pinnedByInfo',
  'pinnedByTitle',
  'pinsInBoard',
  'pinsLink',
  'pinsUserProfileCardLink',
  'pleaseReadTokenUserProfileCardContent',
  'privacyOptionLabel',
  'profileLink',
  'profileUserProfileCardLink',
  'registerButton',
  'repeatPasswordInputPlaceholder',
  'repeatPasswordLabel',
  'saveChangesButton',
  'searchBoardPlaceholder',
  'searchButton',
  'selectBoardLabel',
  'selectFilterPlaceholder',
  'signUpLink',
  'signUpTitle',
  'sourceButton',
  'sourceLink',
  'tagsLabel',
  'tokenUserProfileCardContent',
  'tokenUserProfileCardTitle',
  'userProfileCardContent',
  'usernameLabel',
  'usernamePlaceholder',
];

const REQUIRED_KOREAN_TEXT = {
  NewPinTitle: '새 Pin',
  closeButton: '닫기',
  pinCreateModalCreatePinButton: 'Pin 만들기',
  pinDeleteConfirm: '이 Pin을 삭제하시겠습니까?',
  pinDeleted: 'Pin을 삭제했습니다',
  pinDeleteError: 'Pin을 삭제하지 못했습니다',
  pinCreateError: 'Pin을 만들지 못했습니다',
  browserExtensionsLink: '브라우저 확장 프로그램',
  chromeLink: 'Chrome — 기존 단건용',
  firefoxLink: 'Firefox — 기존 단건용',
  customExtensionPendingLink: 'SVRx Pinry 전체선택 확장 — 준비 중',
  buildBrandLabel: '제품',
  buildInfoTitle: '빌드 정보',
  buildVersionLabel: '실행 버전',
  bulkPinDeleteConfirm: '선택한 Pin {count}개를 삭제하시겠습니까?',
  boardDeleteOnly: '보드만 삭제',
  boardDeleteWithExclusivePins: '보드와 전용 Pin {count}개 삭제',
};


describe('Korean-first locale contract', () => {
  it.each([
    ['en', en], ['ko', ko], ['zh', zh], ['fr', fr],
  ])('keeps the complete nonempty key set for %s', (_name, locale) => {
    expect(Object.keys(locale).sort()).toEqual(EXPECTED_LOCALE_KEYS);
    Object.values(locale).forEach((value) => {
      expect(typeof value).toBe('string');
      expect(value.trim()).not.toBe('');
    });
  });

  it('uses the approved Korean product copy', () => {
    expect(ko).toMatchObject(REQUIRED_KOREAN_TEXT);
  });

  it('resolves only persisted supported locales and otherwise uses Korean', () => {
    expect(DEFAULT_LOCALE).toBe('ko');
    expect(SUPPORTED_LOCALES).toEqual(['ko', 'en', 'zh', 'fr']);
    SUPPORTED_LOCALES.forEach(locale => expect(resolveLocale(locale)).toBe(locale));
    [
      undefined, null, '', ' ', 'de', 'EN', 'en-US', 1, {},
    ].forEach(
      locale => expect(resolveLocale(locale)).toBe('ko'),
    );
    expect(localeUtils.langCode2Name).toEqual({
      ko: '한국어',
      en: 'English',
      zh: '简体中文',
      fr: 'Français',
    });
  });

  it('uses Korean when stored locale access is unavailable', () => {
    const storage = {
      getItem() {
        throw new DOMException('blocked', 'SecurityError');
      },
    };

    expect(loadStoredLocale(storage)).toBe('ko');
    expect(loadStoredLocale({ getItem: () => null })).toBe('ko');
  });

  it('returns the selected locale even when persistence is unavailable', () => {
    const storage = {
      setItem() {
        throw new DOMException('blocked', 'SecurityError');
      },
    };

    expect(() => persistLocale(storage, 'en')).not.toThrow();
    expect(persistLocale(storage, 'en')).toBe('en');
  });

  it('synchronizes the resolved locale to the document language', () => {
    document.documentElement.lang = 'en';

    expect(syncDocumentLocale(document, 'zh')).toBe('zh');
    expect(document.documentElement.lang).toBe('zh');
    expect(syncDocumentLocale(document, 'unsupported')).toBe('ko');
    expect(document.documentElement.lang).toBe('ko');
  });

  it('loads and synchronizes the stored locale for application startup', () => {
    const storage = { getItem: () => 'fr' };
    document.documentElement.lang = 'ko';

    expect(loadAndSyncStoredLocale(storage, document)).toBe('fr');
    expect(document.documentElement.lang).toBe('fr');
  });
});


describe('Header locale and extension menus', () => {
  let initializeUser;

  beforeEach(() => {
    localStorage.clear();
    document.documentElement.lang = 'ko';
    initializeUser = jest.spyOn(PHeader.methods, 'initializeUser')
      .mockImplementation(() => {});
  });

  afterEach(() => {
    initializeUser.mockRestore();
  });

  function mountHeader() {
    const localVue = createLocalVue();
    localVue.use(VueI18n);
    const i18n = new VueI18n({
      locale: 'ko',
      fallbackLocale: 'ko',
      messages: localeUtils.messages,
    });
    const wrapper = shallowMount(PHeader, {
      localVue,
      i18n,
      stubs: ['b-icon', 'router-link'],
    });
    return { i18n, wrapper };
  }

  it('renders and persists locale choices in the explicit product order', async () => {
    const { i18n, wrapper } = mountHeader();
    const options = wrapper.findAll('[data-test="locale-option"]');

    expect(options.wrappers.map(option => option.text()))
      .toEqual(['한국어', 'English', '简体中文', 'Français']);
    await options.at(1).trigger('click');
    expect(i18n.locale).toBe('en');
    expect(localStorage.getItem('localeCode')).toBe('en');
    expect(document.documentElement.lang).toBe('en');
  });

  it('still applies a locale when local storage rejects the write', async () => {
    const setItem = jest.spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new DOMException('blocked', 'SecurityError');
      });
    const { i18n, wrapper } = mountHeader();

    await wrapper.findAll('[data-test="locale-option"]').at(1).trigger('click');

    expect(i18n.locale).toBe('en');
    setItem.mockRestore();
  });

  it('separates legacy single-image stores from the disabled custom item', () => {
    const { wrapper } = mountHeader();
    const chrome = wrapper.find('[data-test="chrome-extension-link"]');
    const firefox = wrapper.find('[data-test="firefox-extension-link"]');
    const custom = wrapper.find('[data-test="custom-extension-pending"]');

    expect(chrome.text()).toBe('Chrome — 기존 단건용');
    expect(chrome.attributes()).toMatchObject({
      href: 'https://chrome.google.com/webstore/detail/jmhdcnmfkglikfjafdmdikoonedgijpa/',
      target: '_blank',
      rel: 'noopener noreferrer',
    });
    expect(firefox.text()).toBe('Firefox — 기존 단건용');
    expect(firefox.attributes()).toMatchObject({
      href: 'https://addons.mozilla.org/en-US/firefox/addon/add-to-pinry/',
      target: '_blank',
      rel: 'noopener noreferrer',
    });
    expect(custom.text()).toBe('SVRx Pinry 전체선택 확장 — 준비 중');
    expect(custom.attributes('href')).toBeUndefined();
    expect(custom.attributes('aria-disabled')).toBe('true');
    expect(custom.classes()).toContain('is-disabled');
  });
});
