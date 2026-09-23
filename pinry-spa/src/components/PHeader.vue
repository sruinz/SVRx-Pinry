<template>
  <div class="p-header">
    <nav class="navbar" role="navigation" aria-label="main navigation">
      <div class="container">
        <div class="navbar-brand">
          <a class="navbar-item brand-lockup" data-test="brand-lockup" href="/">
            <img src="../assets/svrx-pinry-dark-ui.png" alt="" height="32">
            <span class="brand-name" data-test="brand-name">SVRx Pinry</span>
          </a>
          <button type="button" class="navbar-burger burger"
             aria-label="menu" :aria-expanded="String(active)"
             v-on:click="toggleMenu"
             data-target="PinryNav">
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
          </button>
        </div>
        <div id="PinryNav" class="navbar-menu" :class="{ 'is-active': active}">
          <div class="navbar-start">
            <div
              v-if="user.loggedIn"
              class="navbar-item has-dropdown" :class="{ 'is-active': openMenu === 'create' }"
              @pointerenter="showPointerMenu($event, 'create')" @pointerleave="leaveMenu($event)"
              @focusout="leaveMenu($event)"
              @keydown.esc.stop.prevent="dismissMenu($event)">
              <button type="button" class="navbar-link" :aria-expanded="openMenu === 'create'"
                @click="toggleDropdown('create')">
                {{ $t("createLink") }}
              </button>
              <div class="navbar-dropdown">
                <button type="button"
                  @click="createPin"
                  class="navbar-item">
                  {{ $t("pinLink") }}
                </button>
                <button type="button"
                  @click="createBoard"
                  class="navbar-item">
                  {{ $t("boardLink") }}
                </button>
              </div>
            </div>
            <div
              v-if="user.loggedIn"
              class="navbar-item has-dropdown" :class="{ 'is-active': openMenu === 'my' }"
              @pointerenter="showPointerMenu($event, 'my')" @pointerleave="leaveMenu($event)"
              @focusout="leaveMenu($event)"
              @keydown.esc.stop.prevent="dismissMenu($event)">
              <button type="button" class="navbar-link" :aria-expanded="openMenu === 'my'"
                @click="toggleDropdown('my')">
                {{ $t("myLink") }}
              </button>
              <div class="navbar-dropdown" data-test="my-menu">
                <router-link
                  :to="{ name: 'user', params: {user: user.meta.username} }"
                  class="navbar-item"
                  data-test="my-pins-link">
                  {{ $t("pinsLink") }}
                </router-link>
                <router-link
                  :to="{ name: 'boards4user', params: {username: user.meta.username} }"
                  class="navbar-item"
                  data-test="my-boards-link">
                  {{ $t("boardsLink") }}
                </router-link>
                <router-link
                  :to="{ name: 'exports' }"
                  class="navbar-item"
                  data-test="my-exports-link">
                  {{ $t("exportsLink") }}
                </router-link>
                <router-link
                  :to="{ name: 'profile4user', params: {username: user.meta.username} }"
                  class="navbar-item"
                  data-test="my-profile-link">
                  {{ $t("profileLink") }}
                </router-link>
              </div>
            </div>
            <div class="navbar-item has-dropdown" :class="{ 'is-active': openMenu === 'extensions' }"
              @pointerenter="showPointerMenu($event, 'extensions')" @pointerleave="leaveMenu($event)"
              @focusout="leaveMenu($event)"
              @keydown.esc.stop.prevent="dismissMenu($event)">
              <button type="button" class="navbar-link" :aria-expanded="openMenu === 'extensions'"
                @click="toggleDropdown('extensions')">
                {{ $t("browserExtensionsLink") }}
              </button>
              <div
                class="navbar-dropdown"
                data-test="browser-extension-menu">
                <a class="navbar-item" :href="bookmarklet">
                  {{ $t("bookmarkletLink") }}
                </a>
                <a
                  class="navbar-item"
                  data-test="custom-extension-github"
                  href="https://github.com/sruinz/SVRx-Pinry-Extention"
                  target="_blank"
                  rel="noopener noreferrer">
                  {{ $t("customExtensionGitHubLink") }}
                </a>
                <a
                  class="navbar-item"
                  data-test="custom-extension-chrome"
                  href="https://chromewebstore.google.com/detail/svrx-pinry/kgncmldoobdakadnojepmalpbmacoonh?authuser=0&amp;hl=ko"
                  target="_blank"
                  rel="noopener noreferrer">
                  {{ $t("customExtensionChromeLink") }}
                </a>
                <a
                  class="navbar-item"
                  data-test="custom-extension-edge"
                  href="https://microsoftedge.microsoft.com/addons/detail/svrx-pinry/gmbgeiddpdblpjdbceoofclpeiikobjj"
                  target="_blank"
                  rel="noopener noreferrer">
                  {{ $t("customExtensionEdgeLink") }}
                </a>
                <a
                  class="navbar-item"
                  data-test="legacy-chrome-link"
                  href="https://chrome.google.com/webstore/detail/jmhdcnmfkglikfjafdmdikoonedgijpa/"
                  target="_blank"
                  rel="noopener noreferrer">
                  {{ $t("legacyChromeLink") }}
                </a>
                <a
                  class="navbar-item"
                  data-test="legacy-firefox-link"
                  href="https://addons.mozilla.org/en-US/firefox/addon/add-to-pinry/"
                  target="_blank"
                  rel="noopener noreferrer">
                  {{ $t("legacyFirefoxLink") }}
                </a>
              </div>
            </div>
          </div>
          <div class="navbar-end">
            <router-link
              :to="{ name: 'search' }"
              class="navbar-item" :aria-label="$t('searchButton')">
              <i aria-hidden="true" class="mdi mdi-magnify"
                ></i>
            </router-link>
            <div
              class="navbar-item has-dropdown" :class="{ 'is-active': openMenu === 'locale' }"
              @pointerenter="showPointerMenu($event, 'locale')" @pointerleave="leaveMenu($event)"
              @focusout="leaveMenu($event)"
              @keydown.esc.stop.prevent="dismissMenu($event)">
              <button type="button" class="navbar-link" :aria-expanded="openMenu === 'locale'"
                @click="toggleDropdown('locale')">
                <i aria-hidden="true" class="mdi mdi-web"
                  ></i>
                <span class="current-language">{{ langs[$i18n.locale] }}</span>
              </button>
              <div class="navbar-dropdown">
                <button type="button"
                  v-for="locale in locales"
                  :key="`locale-${locale}`"
                  @click="setLocale(locale)"
                  data-test="locale-option"
                  class="navbar-item">
                  {{ langs[locale] }}
                </button>
              </div>
            </div>
            <div v-if="user.loggedIn" class="navbar-item has-dropdown user-menu"
              :class="{ 'is-active': openMenu === 'user' }"
              @pointerenter="showPointerMenu($event, 'user')" @pointerleave="leaveMenu($event)"
              @focusout="leaveMenu($event)"
              @keydown.esc.stop.prevent="dismissMenu($event)">
              <button type="button" class="navbar-link is-arrowless" :aria-label="user.meta.username"
                :aria-expanded="openMenu === 'user'" @click="toggleDropdown('user')">
                <img
                  v-if="headerAvatarUrl"
                  class="user-avatar"
                  data-test="header-avatar"
                  :src="headerAvatarUrl"
                  alt=""
                  @error="userAvatarFailed = true"
                >
                <i
                  v-else
                  aria-hidden="true"
                  class="mdi mdi-account-circle"
                  data-test="header-account-icon"
                ></i>
              </button>
              <div class="navbar-dropdown is-right">
                <router-link
                  :to="{ name: 'profile4user', params: {username: user.meta.username} }"
                  class="navbar-item">{{ $t('profileLink') }}</router-link>
                <button type="button" class="navbar-item" @click="logOut">{{ $t('logOutLink') }}</button>
              </div>
            </div>
            <div v-else class="navbar-item">
              <div class="buttons">
                <button type="button"
                  @click="signUp"
                  v-if="registrationAllowed"
                  class="button is-primary">
                  <strong>{{ $t("signUpLink") }}</strong>
                </button>
                <button type="button"
                  v-show="!user.loggedIn"
                  v-on:click="logIn"
                  class="button is-light">
                  {{ $t("logInLink") }}
                </button>
              </div>
            </div>
          </div>
        </div>
        <button type="button" class="theme-toggle" data-test="theme-toggle"
          :aria-label="$t(theme === 'dark' ? 'switchToLightTheme' : 'switchToDarkTheme')"
          :title="$t(theme === 'dark' ? 'switchToLightTheme' : 'switchToDarkTheme')"
          @click="toggleTheme">
          <i aria-hidden="true" class="mdi"
            :class="theme === 'dark' ? 'mdi-white-balance-sunny' : 'mdi-weather-night'"></i>
        </button>
      </div>
    </nav>
  </div>
</template>

<script>
import localeUtils, {
  SUPPORTED_LOCALES,
  persistLocale,
  syncDocumentLocale,
} from '@/components/utils/i18n';
import api from './api';
import modals from './modals';
import { loadTheme, applyTheme } from './utils/theme';

export default {
  name: 'p-header',
  data() {
    return {
      active: false,
      openMenu: null,
      theme: loadTheme(),
      registrationAllowed: false,
      user: {
        loggedIn: false,
        meta: {},
      },
      userAvatarFailed: false,
      langs: localeUtils.langCode2Name,
      locales: SUPPORTED_LOCALES,
    };
  },
  computed: {
    bookmarklet() {
      const url = new URL(window.location);
      const host = url.origin;
      return `javascript:void((function(d){var s=d.createElement('script');s.id='pinry-bookmarklet';s.src='${host}/static/js/bookmarklet.js?'+Math.random()*10000000000000000;d.body.appendChild(s)})(document));`;
    },
    headerAvatarUrl() {
      const { gravatar } = this.user.meta;
      if (!this.user.loggedIn || !gravatar || this.userAvatarFailed) return '';
      return `https://www.gravatar.com/avatar/${gravatar}?d=404&s=64`;
    },
  },
  methods: {
    showPointerMenu(event, menu) {
      if (event.pointerType === 'mouse') this.openMenu = menu;
    },
    toggleDropdown(menu) {
      this.openMenu = this.openMenu === menu ? null : menu;
    },
    leaveMenu(event) {
      const target = event.type === 'focusout' ? event.relatedTarget : document.activeElement;
      if (target && target.closest('[role="dialog"]')) return;
      if (!event.currentTarget.contains(target)) this.openMenu = null;
    },
    dismissMenu(event) {
      event.currentTarget.querySelector('.navbar-link').focus();
      this.openMenu = null;
    },
    toggleTheme() {
      this.theme = this.theme === 'dark' ? 'light' : 'dark';
      applyTheme(this.theme);
    },
    setLocale(locale) {
      const persisted = persistLocale(localStorage, locale);
      this.$i18n.locale = syncDocumentLocale(document, persisted);
    },
    toggleMenu() {
      this.active = !this.active;
      if (!this.active) this.openMenu = null;
    },
    onLoginSucceed() {
      this.initializeUser(true);
    },
    onSignUpSucceed() {
      this.initializeUser(true);
    },
    logOut() {
      api.User.logOut().then(
        () => {
          window.location.reload();
        },
      );
    },
    logIn() {
      modals.openLogin(this, this.onLoginSucceed);
    },
    createPin() {
      modals.openPinEdit(
        this,
        { username: this.user.meta.username },
      );
    },
    createBoard() {
      modals.openBoardCreate(this);
    },
    signUp() {
      modals.openSignUp(this, this.onSignUpSucceed);
    },
    initializeUser(force = false) {
      const self = this;
      api.User.fetchUserInfo(force).then(
        (user) => {
          if (user === null) {
            self.user.loggedIn = false;
            self.user.meta = {};
            self.userAvatarFailed = false;
          } else {
            self.user.meta = user;
            self.user.loggedIn = true;
            self.userAvatarFailed = false;
          }
        },
      );
    },
  },
  beforeMount() {
    this.initializeUser();
    api.SSO.policy().then((policy) => {
      this.registrationAllowed = policy.password_login_enabled && policy.allow_new_registrations;
    })
      .catch(() => {});
  },
};
</script>

<style scoped lang="scss">
.navbar { background: var(--pinry-surface); border-bottom: 1px solid var(--pinry-border); min-height: 56px; }
.navbar > .container { width: calc(100% - 96px); max-width: none; flex-grow: 0; }
.navbar > .container .navbar-brand { margin-left: 0; }
.navbar > .container .navbar-menu { margin-right: 0; }
.navbar-item, .navbar-link { color: var(--pinry-muted); font-size: 14px; }
.navbar-link:not(.is-arrowless)::after { border-color: var(--pinry-muted); width: .45em; height: .45em; }
.navbar-item img { max-height: 32px; }
.theme-toggle { align-self: center; flex-shrink: 0; width: 44px; height: 44px; margin-left: 8px; border: 0; border-radius: 6px; background: transparent; color: var(--pinry-text); cursor: pointer; font-size: 22px; }
.theme-toggle:hover { background: var(--pinry-hover); }
.navbar-item:hover, .navbar-link:hover,
.navbar-item.has-dropdown:hover .navbar-link { background: var(--pinry-hover); color: var(--pinry-text); }
.navbar-dropdown { background: var(--pinry-surface); border: 1px solid var(--pinry-border); }
.navbar-dropdown .navbar-item:hover { background: var(--pinry-hover); color: var(--pinry-text); }
.navbar-start { margin-left: 24px; gap: 8px; }
.navbar-end { align-items: center; gap: 12px; }
.navbar-end .mdi { font-size: 22px; }
.navbar-end .user-menu .mdi { font-size: 34px; line-height: 1; }
.navbar-end .user-menu .user-avatar {
  display: block;
  width: 34px;
  height: 34px;
  max-height: 34px;
  border-radius: 50%;
  object-fit: cover;
}
button.navbar-link, button.navbar-item, .navbar-burger {
  border: 0; background: transparent; cursor: pointer; font-family: inherit;
}
.navbar-dropdown button { width: 100%; text-align: left; }
.navbar .has-dropdown:not(.is-active) .navbar-dropdown { display: none; }
.navbar .has-dropdown.is-active .navbar-dropdown { display: block; }
.current-language { margin-left: 8px; }
.brand-lockup {
  gap: .5rem;
  padding-left: 0;
}

.brand-name {
  white-space: nowrap;
  font-size: 18px;
  color: var(--pinry-text);
}
@media screen and (max-width: 1023px) {
  .navbar > .container { width: 100%; }
  .theme-toggle { position: absolute; top: 4px; right: 56px; margin-left: 0; }
  .navbar-brand { padding-right: 56px; }
  .navbar-burger { position: absolute; right: 0; top: 0; }
  .brand-lockup { padding-left: 16px; }
  .navbar-burger { color: var(--pinry-text); }
  .navbar-menu { background: var(--pinry-surface); }
  button.navbar-link { width: 100%; text-align: left; }
  .navbar-start { margin-left: 0; }
  .navbar-dropdown { border: 0; }
  .navbar-end { display: block; }
}
</style>
