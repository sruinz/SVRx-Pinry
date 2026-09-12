<template>
  <div class="p-header">
    <nav class="navbar" role="navigation" aria-label="main navigation">
      <div class="container">
        <div class="navbar-brand">
          <a class="navbar-item brand-lockup" data-test="brand-lockup" href="/">
            <img src="../assets/svrx-pinry-light-ui.png" alt="" height="32">
            <span class="brand-name" data-test="brand-name">SVRx Pinry</span>
          </a>
          <a role="button" class="navbar-burger burger"
             aria-label="menu" aria-expanded="false"
             v-on:click="toggleMenu"
             data-target="PinryNav">
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
          </a>
        </div>
        <div id="PinryNav" class="navbar-menu" :class="{ 'is-active': active}">
          <div class="navbar-start">
            <a class="navbar-item" :href="bookmarklet">
              {{ $t("bookmarkletLink") }}
            </a>
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
              class="navbar-item">
              <i aria-hidden="true" class="mdi mdi-magnify"
                ></i>
            </router-link>
            <div
              class="navbar-item has-dropdown is-hoverable">
              <a class="navbar-link">
                <i aria-hidden="true" class="mdi mdi-translate"
                  ></i>
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
            <div class="navbar-item">
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
                <a
                  v-show="user.loggedIn"
                  v-on:click="logOut"
                  class="button is-light">
                  {{ $t("logOutLink") }}
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

<style scoped>
.brand-lockup {
  gap: .5rem;
}

.brand-name {
  white-space: nowrap;
}
</style>
