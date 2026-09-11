<template>
  <div class="profile-container">
    <div class="card">
      <header class="card-header">
        <p class="card-header-title">
          {{ $t("tokenUserProfileCardTitle") }}
        </p>
      </header>
      <div class="card-content">
        <div class="content">
          <p>{{ $t("tokenUserProfileCardContent") }}</p>
          <pre v-if="policy && policy.api_tokens_enabled">{{ token }}</pre>
          <p v-else>{{ $t('ssoTokensDisabled') }}</p>
          {{ $t("pleaseReadTokenUserProfileCardContent") }}<a target="_blank" href="https://www.django-rest-framework.org/api-guide/authentication/#tokenauthentication">{{ $t("drfApiDocumentationLink") }}</a>{{ $t("forMoreDetailsParagraph") }}
          <br>
        </div>
      </div>
    </div>
    <div class="card sso-card">
      <header class="card-header"><p class="card-header-title">{{ $t('ssoAccounts') }}</p></header>
      <div class="card-content">
        <p v-if="policyError" role="alert">{{ $t('ssoSettingsFailed') }}</p>
        <p v-if="identityError" role="alert">{{ $t('ssoIdentitiesFailed') }}</p>
        <p v-if="actionError" role="alert">{{ $t('ssoActionFailed') }}</p>
        <p v-if="reauthenticated" role="status">{{ $t('ssoReauthenticated') }}</p>
        <p>{{ $t('ssoRecentAuthHelp') }}</p>
        <form v-if="policy && policy.password_login_enabled" @submit.prevent="passwordReauth">
          <label>{{ $t('passwordLabel') }}
            <input v-model="password" type="password" autocomplete="current-password" required>
          </label>
          <button class="button" type="submit">{{ $t('ssoReauth') }}</button>
        </form>
        <p>{{ $t('ssoReauthHelp') }}</p>
        <div v-for="identity in identities" :key="identity.id">
          <span>{{ identity.provider_name }}</span>
          <span v-if="!identity.enabled">{{ $t('ssoUnavailable') }}</span>
          <form v-if="identity.enabled" method="post"
                :action="`/api/v2/sso/${identity.provider_id}/reauth/`">
            <input type="hidden" name="csrfmiddlewaretoken" :value="csrfToken">
            <input type="hidden" name="next" :value="returnPath">
            <button class="button" type="submit">{{ $t('ssoReauth') }}</button>
          </form>
          <button class="button" type="button" @click="unlink(identity.id)">{{ $t('ssoUnlink') }}</button>
        </div>
        <div v-if="policy && !identityError">
          <form v-for="provider in policy.providers" :key="provider.id" method="post"
                :action="`/api/v2/sso/${provider.id}/link/`">
            <input type="hidden" name="csrfmiddlewaretoken" :value="csrfToken">
            <input type="hidden" name="next" :value="returnPath">
            <button class="button" type="submit">{{ provider.name }} — {{ $t('ssoLink') }}</button>
          </form>
        </div>
      </div>
    </div>
    <a
      v-if="canAccessAdmin"
      class="card admin-settings-card"
      data-test="admin-settings-link"
      href="/admin/">
      <div class="card-content">
        <div class="content">
          {{ $t("adminSettingsLink") }}
        </div>
      </div>
    </a>
    <div class="card build-info-card">
      <header class="card-header">
        <p class="card-header-title">
          {{ $t("buildInfoTitle") }}
        </p>
      </header>
      <div class="card-content">
        <div class="content">
          <div><span>{{ $t("buildBrandLabel") }}:</span> <strong data-test="build-brand">SVRx Pinry</strong></div>
          <div>
            <span>{{ $t("buildVersionLabel") }}:</span>
            <code
              v-if="displayVersion"
              data-test="build-version">{{ displayVersion }}</code>
            <span
              v-else
              data-test="build-version-unavailable">—</span>
          </div>
        </div>
      </div>
    </div>
    <div
      class="card dependency-info-card"
      data-test="dependency-versions">
      <header class="card-header">
        <p class="card-header-title">
          {{ $t("dependencyVersionsTitle") }}
        </p>
      </header>
      <div class="card-content">
        <div class="content">
          <div
            v-for="dependency in dependencies"
            :key="dependency.key">
            <span>{{ dependency.label }}:</span>
            <code v-if="dependency.version">{{ dependency.version }}</code>
            <span v-else>—</span>
          </div>
        </div>
      </div>
    </div>
    <div
      class="card open-source-card"
      data-test="open-source-license">
      <header class="card-header">
        <p class="card-header-title">
          {{ $t("openSourceLicenseTitle") }}
        </p>
      </header>
      <div class="card-content">
        <div class="content">
          <p>{{ $t("openSourceLicenseDescription") }}</p>
          <p>{{ $t("openSourceLicenseCopyright") }}</p>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import API from '../api';

export default {
  name: 'profile',
  props: {
    token: String,
    canAccessAdmin: {
      type: Boolean,
      default: false,
    },
  },
  data() {
    return {
      componentAlive: true,
      policy: null,
      policyError: false,
      identities: [],
      identityError: false,
      actionError: false,
      reauthenticated: false,
      password: '',
      csrfToken: API.SSO.csrfToken(),
      returnPath: window.location.pathname,
      displayVersion: null,
      dependencies: [
        { key: 'python', label: 'Python', version: null },
        { key: 'django', label: 'Django', version: null },
        { key: 'drf', label: 'Django REST Framework', version: null },
        { key: 'pillow', label: 'Pillow', version: null },
      ],
      versionRequestSequence: 0,
    };
  },
  created() {
    this.fetchBuildVersion();
    API.SSO.policy().then((policy) => { if (this.componentAlive) this.policy = policy; })
      .catch(() => { if (this.componentAlive) this.policyError = true; });
    this.fetchIdentities();
  },
  beforeDestroy() {
    this.componentAlive = false;
    this.versionRequestSequence += 1;
  },
  methods: {
    fetchIdentities() {
      return API.SSO.identities().then((identities) => {
        if (this.componentAlive) { this.identities = identities; this.identityError = false; }
      }).catch(() => { if (this.componentAlive) this.identityError = true; });
    },
    passwordReauth() {
      this.actionError = false;
      this.reauthenticated = false;
      API.SSO.passwordReauth(this.password).then(() => { this.reauthenticated = true; })
        .catch(() => { this.actionError = true; });
      this.password = '';
    },
    unlink(id) {
      this.actionError = false;
      API.SSO.unlink(id).then(() => this.fetchIdentities())
        .catch(() => { this.actionError = true; });
    },
    fetchBuildVersion() {
      const requestSequence = this.versionRequestSequence + 1;
      this.versionRequestSequence = requestSequence;
      API.Version.fetch().then((response) => {
        if (!this.componentAlive || requestSequence !== this.versionRequestSequence) {
          return;
        }
        const value = response && response.data && response.data.display_version;
        if (typeof value === 'string' && value.trim() !== '') {
          this.displayVersion = value;
        }
        const versions = response && response.data && response.data.dependencies;
        this.dependencies = this.dependencies.map((dependency) => {
          const version = versions && versions[dependency.key];
          return {
            ...dependency,
            version: typeof version === 'string' && version.trim() !== ''
              ? version.trim() : null,
          };
        });
      }).catch(() => {});
    },
  },
};
</script>

<style scoped lang="scss">
.profile-container {
  margin-top: 2rem;
  margin-left: auto;
  margin-right: auto;
  box-shadow: 5px 5px 2px 1px rgba(0, 0, 255, .1);
}

.admin-settings-card,
.build-info-card,
.dependency-info-card,
.open-source-card {
  margin-top: 1rem;
}

.admin-settings-card {
  color: inherit;
}

[data-test="build-version"],
.dependency-info-card code {
  margin-left: .5rem;
  user-select: text;
}

@import '../utils/grid-layout';
@include screen-grid-layout(".profile-container");
</style>
