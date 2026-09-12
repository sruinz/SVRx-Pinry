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
        <p v-if="policyError" class="sso-message is-error" role="alert">
          {{ $t('ssoSettingsFailed') }}
        </p>
        <p v-if="identityError" class="sso-message is-error" role="alert">
          {{ $t('ssoIdentitiesFailed') }}
        </p>
        <p v-if="actionError" class="sso-message is-error" role="alert">
          {{ $t('ssoActionFailed') }}
        </p>
        <p v-if="reauthenticated" class="sso-message is-success" role="status">
          {{ $t('ssoReauthenticated') }}
        </p>
        <p class="sso-auto-link-help">{{ $t('ssoAutoLinkHelp') }}</p>
        <details class="sso-manual-controls">
          <summary>{{ $t('ssoManualControls') }}</summary>
          <div class="sso-manual-controls__body">
            <p v-if="policy && policy.password_login_enabled" class="sso-manual-help">
              {{ $t('ssoRecentAuthHelp') }}
            </p>
            <p v-else class="sso-manual-help">
              {{ $t('ssoManualSsoOnlyHelp') }}
            </p>
            <form
              v-if="policy && policy.password_login_enabled"
              data-test="password-reauth-form"
              class="sso-password-reauth"
              @submit.prevent="passwordReauth">
              <label for="sso-password">{{ $t('passwordLabel') }}</label>
              <div class="sso-password-reauth__row">
                <input
                  id="sso-password"
                  v-model="password"
                  type="password"
                  autocomplete="current-password"
                  required>
                <button class="button" type="submit">{{ $t('ssoReauth') }}</button>
              </div>
            </form>
            <p class="sso-provider-help">{{ $t('ssoReauthHelp') }}</p>
            <div class="sso-provider-list">
              <div
                v-for="identity in identities"
                :key="identity.id"
                data-test="identity-row"
                class="sso-provider-row">
                <div class="sso-provider-row__identity">
                  <img
                    data-test="provider-icon"
                    :src="providerIcon(identity.provider_id, identity.provider_kind)"
                    alt="">
                  <span>{{ identity.provider_name }}</span>
                  <span v-if="!identity.enabled" class="sso-provider-status">
                    {{ $t('ssoUnavailable') }}
                  </span>
                </div>
                <div class="sso-provider-row__actions">
                  <form
                    v-if="identity.enabled"
                    method="post"
                    :action="`/api/v2/sso/${identity.provider_id}/reauth/`">
                    <input type="hidden" name="csrfmiddlewaretoken" :value="csrfToken">
                    <input type="hidden" name="next" :value="returnPath">
                    <button class="button sso-secondary-action" type="submit">
                      {{ $t('ssoReauth') }}
                    </button>
                  </form>
                  <button
                    data-test="unlink-button"
                    class="button is-danger is-outlined"
                    type="button"
                    @click="unlink(identity.id)">
                    {{ $t('ssoUnlink') }}
                  </button>
                </div>
              </div>
            </div>
            <div v-if="policy && !identityError" class="sso-provider-list sso-link-list">
              <form
                v-for="provider in policy.providers"
                :key="provider.id"
                data-test="provider-link-row"
                class="sso-provider-row"
                method="post"
                :action="`/api/v2/sso/${provider.id}/link/`">
                <div class="sso-provider-row__identity">
                  <img
                    data-test="provider-icon"
                    :src="providerIcon(provider.id, provider.kind)"
                    alt="">
                  <span>{{ provider.name }}</span>
                </div>
                <input type="hidden" name="csrfmiddlewaretoken" :value="csrfToken">
                <input type="hidden" name="next" :value="returnPath">
                <button class="button sso-link-action" type="submit">{{ $t('ssoLink') }}</button>
              </form>
            </div>
          </div>
        </details>
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

const providerKinds = ['authentik', 'google', 'microsoft', 'github', 'synology', 'oidc'];

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
    providerKind(providerId, kind = null) {
      if (providerKinds.includes(kind)) return kind;
      const providers = this.policy && this.policy.providers;
      const provider = providers && providers.find(candidate => candidate.id === providerId);
      return provider && providerKinds.includes(provider.kind) ? provider.kind : 'oidc';
    },
    providerIcon(providerId, kind = null) {
      return `/static/auth/providers/${this.providerKind(providerId, kind)}.svg`;
    },
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

.sso-card {
  color: #eef0f1;
  background: #1b1f21;
  border: 1px solid #3b4246;
}

.sso-card .card-header {
  background: #1b1f21;
  border-bottom: 1px solid #3b4246;
  box-shadow: none;
}

.sso-card .card-header-title {
  color: #f7f8f8;
}

.sso-auto-link-help,
.sso-manual-help,
.sso-provider-help {
  color: #b8bec1;
  overflow-wrap: anywhere;
  word-break: keep-all;
}

.sso-message {
  padding: .75rem 1rem;
  border-radius: 6px;
}

.sso-message.is-error {
  color: #ffb3b3;
  background: #3a2023;
}

.sso-message.is-success {
  color: #8ce8d8;
  background: #15332e;
}

.sso-manual-controls {
  margin-top: 1.25rem;
  border-top: 1px solid #3b4246;
}

.sso-manual-controls summary {
  padding: 1rem 0;
  color: #f4f5f5;
  font-weight: 600;
  cursor: pointer;
}

.sso-manual-controls__body {
  padding-bottom: .25rem;
}

.sso-password-reauth {
  margin: 1rem 0;
}

.sso-password-reauth label {
  display: block;
  margin-bottom: .5rem;
  color: #d9dcde;
  font-weight: 600;
}

.sso-password-reauth__row {
  display: flex;
  align-items: stretch;
  gap: .75rem;
}

.sso-password-reauth input {
  min-width: 0;
  min-height: 44px;
  flex: 1;
  padding: .625rem .75rem;
  color: #f5f6f6;
  background: #121516;
  border: 1px solid #4b5357;
  border-radius: 4px;
}

.sso-password-reauth input:focus {
  border-color: #00d1b2;
  box-shadow: 0 0 0 1px #00d1b2;
  outline: none;
}

.sso-password-reauth input::placeholder {
  color: #aeb4b7;
  opacity: 1;
}

.sso-password-reauth .button,
.sso-link-action {
  min-height: 44px;
  color: #10211e;
  background: #00d1b2;
  border-color: #00d1b2;
  font-weight: 600;
}

.sso-provider-list {
  display: grid;
  gap: .75rem;
  margin-top: 1rem;
}

.sso-link-list {
  padding-top: 1rem;
  border-top: 1px solid #3b4246;
}

.sso-provider-row {
  display: flex;
  min-height: 58px;
  padding: .75rem;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  background: #171a1c;
  border: 1px solid #3b4246;
  border-radius: 8px;
}

.sso-provider-row__identity,
.sso-provider-row__actions {
  display: flex;
  align-items: center;
  gap: .75rem;
}

.sso-provider-row__identity {
  min-width: 0;
  font-weight: 600;
}

.sso-provider-row__identity span {
  min-width: 0;
  overflow-wrap: anywhere;
}

.sso-provider-row__identity img {
  width: 28px;
  height: 28px;
  flex: 0 0 auto;
  object-fit: contain;
}

.sso-provider-status {
  color: #aeb4b7;
  font-size: .875rem;
  font-weight: 400;
}

.sso-provider-row__actions form {
  margin: 0;
}

.sso-secondary-action {
  color: #eef0f1;
  background: transparent;
  border-color: #697176;
}

.sso-card .button.is-danger.is-outlined {
  color: #ff7d87;
  background: transparent;
  border-color: #ff5c68;
}

@media (max-width: 600px) {
  .sso-password-reauth__row,
  .sso-provider-row {
    align-items: stretch;
    flex-direction: column;
  }

  .sso-password-reauth .button,
  .sso-provider-row__actions,
  .sso-provider-row__actions .button,
  .sso-link-action {
    width: 100%;
  }

  .sso-provider-row__actions {
    align-items: stretch;
    flex-direction: column;
  }

  .sso-provider-row__actions form {
    width: 100%;
  }
}

[data-test="build-version"],
.dependency-info-card code {
  margin-left: .5rem;
  user-select: text;
}

@import '../utils/grid-layout';
@include screen-grid-layout(".profile-container");
</style>
