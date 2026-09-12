<template>
  <div class="p-header">
    <nav class="navbar" role="navigation" aria-label="main navigation">
      <div class="container">
        <div class="navbar-brand">
          <a class="navbar-item brand-lockup" data-test="brand-lockup" href="/">
            <img src="../assets/svrx-pinry-dark-ui.png" alt="" height="32">
            <span class="brand-name" data-test="brand-name">SVRx Pinry</span>
          </a>
          <a role="button" class="navbar-burger burger"
             aria-label="menu" :aria-expanded="String(active)"
             v-on:click="toggleMenu"
             data-target="PinryNav">
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
          </a>
        </div>
        <div id="PinryNav" class="navbar-menu" :class="{ 'is-active': active}">
          <div class="navbar-start">
            <div
              v-if="user.loggedIn"
              class="navbar-item has-dropdown is-hoverable">
              <a class="navbar-link">
                {{ $t("createLink") }}
              </a>
              <div class="navbar-dropdown">
                <a
                  @click="createPin"
                  class="navbar-item">
                  {{ $t("pinLink") }}
                </a>
                <a
                  @click="createBoard"
                  class="navbar-item">
                  {{ $t("boardLink") }}
                </a>
              </div>
            </div>
            <div
              v-if="user.loggedIn"
              class="navbar-item has-dropdown is-hoverable">
              <a class="navbar-link">
                {{ $t("myLink") }}
              </a>
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
            <div class="navbar-item has-dropdown is-hoverable">
              <a class="navbar-link">
                {{ $t("browserExtensionsLink") }}
              </a>
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
              class="navbar-item has-dropdown is-hoverable">
              <a class="navbar-link">
                <i aria-hidden="true" class="mdi mdi-web"
                  ></i>
                <span class="current-language">{{ langs[$i18n.locale] }}</span>
              </a>
              <div class="navbar-dropdown">
                <a
                  v-for="locale in locales"
                  :key="`locale-${locale}`"
                  @click="setLocale(locale)"
                  data-test="locale-option"
                  class="navbar-item">
                  {{ langs[locale] }}
                </a>
              </div>
            </div>
            <div v-if="user.loggedIn" class="navbar-item has-dropdown is-hoverable user-menu">
              <button type="button" class="navbar-link is-arrowless" :aria-label="user.meta.username">
                <i aria-hidden="true" class="mdi mdi-account-circle"></i>
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
                <a
                  @click="signUp"
                  v-show="!user.loggedIn && passwordAllowed"
                  class="button is-primary">
                  <strong>{{ $t("signUpLink") }}</strong>
                </a>
                <a
                  v-show="!user.loggedIn"
                  v-on:click="logIn"
                  class="button is-light">
                  {{ $t("logInLink") }}
                </a>
              </div>
            </div>
          </div>
        </div>
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

export default {
  name: 'p-header',
  data() {
    return {
      active: false,
      passwordAllowed: false,
      user: {
        loggedIn: false,
        meta: {},
      },
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
  },
  methods: {
    setLocale(locale) {
      const persisted = persistLocale(localStorage, locale);
      this.$i18n.locale = syncDocumentLocale(document, persisted);
    },
    toggleMenu() {
      this.active = !this.active;
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
          } else {
            self.user.meta = user;
            self.user.loggedIn = true;
          }
        },
      );
    },
  },
  beforeMount() {
    this.initializeUser();
    api.SSO.policy().then((policy) => { this.passwordAllowed = policy.password_login_enabled; })
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
.navbar-item:hover, .navbar-link:hover,
.navbar-item.has-dropdown:hover .navbar-link { background: #253135; color: var(--pinry-text); }
.navbar-dropdown { background: var(--pinry-surface); border: 1px solid var(--pinry-border); }
.navbar-dropdown .navbar-item:hover { background: #253135; color: var(--pinry-text); }
.navbar-start { margin-left: 24px; gap: 8px; }
.navbar-end { align-items: center; gap: 12px; }
.navbar-end .mdi { font-size: 22px; }
.navbar-end .user-menu .mdi { font-size: 34px; line-height: 1; }
.user-menu button { border: 0; background: transparent; cursor: pointer; font: inherit; }
.user-menu .navbar-dropdown button { width: 100%; text-align: left; }
.user-menu:focus-within .navbar-dropdown { display: block; }
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
  .brand-lockup { padding-left: 16px; }
  .navbar-burger { color: var(--pinry-text); }
  .navbar-menu { background: var(--pinry-surface); }
  .navbar-start { margin-left: 0; }
  .navbar-dropdown { border: 0; }
  .navbar-end { display: block; }
}
</style>
