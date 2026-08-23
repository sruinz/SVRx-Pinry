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


const CHROME_URL = 'https://chrome.google.com/webstore/detail/jmhdcnmfkglikfjafdmdikoonedgijpa/';
const FIREFOX_URL = 'https://addons.mozilla.org/en-US/firefox/addon/add-to-pinry/';

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
  'boardCoverApply',
  'boardCoverCancel',
  'boardCoverEnter',
  'boardCoverPrivateUnavailable',
  'boardCoverPublishWarning',
  'boardCoverRefreshRequired',
  'boardCoverReset',
  'boardCoverResetConfirm',
  'boardCoverSaveFailed',
  'boardCoverSaved',
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
  'bulkPinBoardCreateError',
  'bulkPinBoardCreated',
  'bulkPinBoardNameRequired',
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
  'closeButton',
  'createBoardButton',
  'createLink',
  'customExtensionChromePendingLink',
  'customExtensionEdgePendingLink',
  'customExtensionGitHubLink',
  'descriptionLabel',
  'drfApiDocumentationLink',
  'emailLabel',
  'emailPlaceholder',
  'end',
  'error404',
  'filterSelectCreateNewBoardButton',
  'filterSelectSelectBoardPlaceholder',
  'forMoreDetailsParagraph',
  'imageSourceLabel',
  'imageUrlLabel',
  'isPrivateCheckbox',
  'legacyChromeLink',
  'legacyFirefoxLink',
  'logInLink',
  'logOutLink',
  'loginButton',
  'loginTitle',
  'myLink',
  'nameLabel',
  'noResultsFound',
  'openSourceLicenseCopyright',
  'openSourceLicenseDescription',
  'openSourceLicenseTitle',
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
  'pinSortDisabled',
  'pinSortLabel',
  'pinSortLatest',
  'pinSortOldest',
  'pinSortRandom',
  'pinSortReshuffled',
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
  pinSortLabel: '정렬',
  pinSortLatest: '최신순',
  pinSortOldest: '오래된순',
  pinSortRandom: '랜덤',
  pinSortReshuffled: '랜덤 순서를 새로 섞었습니다.',
  pinSortDisabled: 'Pin 선택 또는 일괄 작업 중에는 정렬을 변경할 수 없습니다.',
  browserExtensionsLink: '브라우저 확장 프로그램',
  customExtensionGitHubLink: 'SVRx Pinry - GitHub',
  customExtensionChromePendingLink: 'SVRx Pinry - Chrome 웹 스토어 (준비 중)',
  customExtensionEdgePendingLink: 'SVRx Pinry - Microsoft Edge Add-ons (준비 중)',
  legacyChromeLink: 'Pinry 레거시 - Chrome',
  legacyFirefoxLink: 'Pinry 레거시 - Firefox',
  buildBrandLabel: '제품',
  buildInfoTitle: '빌드 정보',
  buildVersionLabel: '실행 버전',
  openSourceLicenseCopyright: "Copyright (c) 2019, Pinry's Contributors",
  openSourceLicenseDescription: '이 제품에는 BSD 2-Clause 라이선스로 제공되는 Pinry 구성 요소가 포함되어 있습니다.',
  openSourceLicenseTitle: '오픈소스 라이선스',
  bulkPinDeleteConfirm: '선택한 Pin {count}개를 삭제하시겠습니까?',
  bulkPinBoardCreateError: '보드를 만들지 못했습니다. 입력 내용을 확인하고 다시 시도하세요.',
  bulkPinBoardCreated: '보드를 만들고 대상으로 선택했습니다.',
  bulkPinBoardNameRequired: '새 보드 이름을 입력하세요.',
  boardCoverApply: '적용',
  boardCoverCancel: '취소',
  boardCoverEnter: '대표 이미지 설정',
  boardCoverPrivateUnavailable: '공개 보드에서는 비공개 Pin을 대표 이미지로 선택할 수 없습니다.',
  boardCoverPublishWarning: '현재 대표 이미지가 비공개입니다. 보드를 공개하면 자동 대표 이미지로 변경됩니다.',
  boardCoverRefreshRequired: 'Pin 상태가 바뀌었습니다. 목록을 새로 고친 뒤 다시 선택하세요.',
  boardCoverReset: '자동 선택으로 돌리기',
  boardCoverResetConfirm: '자동 대표 이미지로 돌리시겠습니까?',
  boardCoverSaveFailed: '대표 이미지를 저장하지 못했습니다.',
  boardCoverSaved: '대표 이미지를 저장했습니다.',
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
      stubs: {
        'b-icon': true,
        'router-link': {
          name: 'RouterLinkStub',
          props: ['to'],
          template: '<a><slot /></a>',
        },
      },
    });
    return { i18n, wrapper };
  }

  it('renders My menu as Pin, boards, and profile with exact route params', async () => {
    const { wrapper } = mountHeader();
    await wrapper.setData({
      user: { loggedIn: true, meta: { username: 'owner' } },
    });

    const menu = wrapper.find('[data-test="my-menu"]');
    expect([...menu.element.children].map(item => item.dataset.test)).toEqual([
      'my-pins-link',
      'my-boards-link',
      'my-profile-link',
    ]);
    expect(wrapper.find('[data-test="my-pins-link"]').props('to')).toEqual({
      name: 'user', params: { user: 'owner' },
    });
    expect(wrapper.find('[data-test="my-boards-link"]').props('to')).toEqual({
      name: 'boards4user', params: { username: 'owner' },
    });
    expect(wrapper.find('[data-test="my-profile-link"]').props('to')).toEqual({
      name: 'profile4user', params: { username: 'owner' },
    });
  });

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

  it('renders the custom and legacy extension items in the release order', () => {
    const { wrapper } = mountHeader();
    const menu = wrapper.find('[data-test="browser-extension-menu"]');

    expect(menu.exists()).toBe(true);
    expect([...menu.element.children].map(item => item.dataset.test)).toEqual([
      'custom-extension-github',
      'custom-extension-chrome-pending',
      'custom-extension-edge-pending',
      'legacy-chrome-link',
      'legacy-firefox-link',
    ]);

    const items = wrapper.findAll(
      '[data-test="browser-extension-menu"] > [data-test]',
    );
    expect(items.at(0).attributes('href'))
      .toBe('https://github.com/sruinz/SVRx-Pinry-Extention');
    expect(items.at(1).attributes('aria-disabled')).toBe('true');
    expect(items.at(2).attributes('aria-disabled')).toBe('true');
    expect(items.at(3).attributes('href')).toBe(CHROME_URL);
    expect(items.at(4).attributes('href')).toBe(FIREFOX_URL);

    [0, 3, 4].forEach((index) => {
      expect(items.at(index).attributes()).toMatchObject({
        target: '_blank',
        rel: 'noopener noreferrer',
      });
    });
    [1, 2].forEach((index) => {
      expect(items.at(index).element.tagName).toBe('SPAN');
      expect(items.at(index).attributes('href')).toBeUndefined();
      expect(items.at(index).classes()).toContain('is-disabled');
    });
  });
});
