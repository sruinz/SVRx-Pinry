<template>
  <div class="profile-container">
    <div class="card token-card">
      <header class="card-header">
        <p class="card-header-title">
          {{ $t("profileTokenTitle") }}
        </p>
      </header>
      <div class="card-content">
        <div class="content">
          <p>{{ $t("profileTokenDescription") }}</p>
          <div v-if="policy && policy.api_tokens_enabled && token" class="token-controls">
            <div class="token-field">
              <code data-test="token-value">{{ tokenVisible ? token : '•••• •••• •••• ••••' }}</code>
              <button class="button token-toggle" type="button" data-test="token-toggle"
                :aria-pressed="String(tokenVisible)" @click="tokenVisible = !tokenVisible">
                <i class="mdi" :class="tokenVisible ? 'mdi-eye-off-outline' : 'mdi-eye-outline'" aria-hidden="true"></i>
                {{ $t(tokenVisible ? 'profileTokenHide' : 'profileTokenShow') }}
              </button>
            </div>
            <button class="button token-copy" type="button" data-test="token-copy" @click="copyToken">
              <i class="mdi mdi-content-copy" aria-hidden="true"></i>{{ $t('profileTokenCopy') }}
            </button>
          </div>
          <p v-else>{{ $t('ssoTokensDisabled') }}</p>
          <p v-if="tokenStatus" role="status" data-test="token-status">{{ $t(tokenStatus) }}</p>
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
        <p
          v-if="actionError"
          data-test="sso-action-error"
          class="sso-message is-error"
          role="alert">
          {{ $t(actionError) }}
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
              <label for="sso-password">{{ $t('profilePinryPassword') }}</label>
              <div class="sso-password-reauth__row">
                <input
                  id="sso-password"
                  v-model="password"
                  type="password"
                  :placeholder="$t('profileCurrentPassword')"
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
                  <span v-if="identity.enabled" class="sso-connected-status">{{ $t('profileConnected') }}</span>
                  <span v-if="!identity.enabled" class="sso-provider-status">
                    {{ $t('ssoUnavailable') }}
                  </span>
                </div>
                <div class="sso-provider-row__controls">
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
                      :disabled="identity.unlink_allowed === false"
                      :aria-describedby="identity.unlink_allowed === false
                        ? `sso-unlink-reason-${identity.id}` : null"
                      @click="unlink(identity)">
                      {{ $t('ssoUnlink') }}
                    </button>
                  </div>
                  <p
                    v-if="identity.unlink_allowed === false"
                    :id="`sso-unlink-reason-${identity.id}`"
                    data-test="unlink-reason"
                    class="sso-unlink-reason">
                    {{ $t(unlinkReasonKey(identity.unlink_reason)) }}
                  </p>
                </div>
              </div>
            </div>
            <div v-if="policy && !identityError" class="sso-provider-list sso-link-list">
              <form
                v-for="provider in availableProviders"
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
      <div class="card-content admin-settings-content">
        <i class="mdi mdi-shield-account-outline" aria-hidden="true"></i>
        <div class="admin-settings-description">
          <strong>{{ $t("adminSettingsLink") }}</strong>
          <p>{{ $t('profileAdminHelp') }}</p>
        </div>
        <span class="button admin-settings-action">{{ $t('profileAdminOpen') }}
          <i class="mdi mdi-arrow-top-right" aria-hidden="true"></i>
        </span>
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
          <section v-for="group in dependencyGroups" :key="group.key"
            class="dependency-group" :class="`dependency-group--${group.key}`"
            :data-test="`dependencies-${group.key}`">
            <h3>{{ $t(group.title) }}</h3>
            <ul class="dependency-bubbles">
              <li v-for="dependency in group.items" :key="dependency.key" class="dependency-bubble">
                <span>{{ dependency.label }}</span>{{ ' ' }}<code v-if="dependency.version">{{ dependency.version }}</code>
                <span v-else>—</span>
              </li>
            </ul>
          </section>
          <p class="dependency-build-help">{{ $t('profileBuildToolsHelp') }}</p>
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
import buildDependencies from '../utils/build-dependencies';

const providerKinds = ['authentik', 'google', 'microsoft', 'github', 'synology', 'oidc'];
const unlinkErrorKeys = {
  last_login_method: 'ssoUnlinkBlockedLastLoginMethod',
  recent_auth_required: 'ssoUnlinkRecentAuthRequired',
  identity_not_found: 'ssoUnlinkIdentityNotFound',
};

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
      tokenVisible: false,
      tokenStatus: '',
      buildDependencies: buildDependencies(),
      policy: null,
      policyError: false,
      identities: [],
      identityError: false,
      actionError: null,
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
  computed: {
    availableProviders() {
      const connected = new Set(this.identities.map(identity => identity.provider_id));
      return ((this.policy && this.policy.providers) || [])
        .filter(provider => !connected.has(provider.id));
    },
    dependencyGroups() {
      return [
        { key: 'backend', title: 'profileBackend', items: this.dependencies },
        { key: 'frontend', title: 'profileFrontend', items: this.buildDependencies.frontend },
        { key: 'buildTools', title: 'profileBuildTools', items: this.buildDependencies.buildTools },
      ];
    },
  },
  watch: {
    token() { this.tokenVisible = false; this.tokenStatus = ''; },
  },
  created() {
    this.fetchBuildVersion();
    API.SSO.policy().then((policy) => { if (this.componentAlive) this.policy = policy; })
      .catch(() => { if (this.componentAlive) this.policyError = true; });
    this.fetchIdentities();
  },
  beforeUnmount() {
    this.componentAlive = false;
    this.versionRequestSequence += 1;
  },
  methods: {
    async copyToken() {
      if (!this.policy || !this.policy.api_tokens_enabled || !this.token) return;
      this.tokenStatus = '';
      try {
        if (navigator.clipboard) {
          await navigator.clipboard.writeText(this.token);
        } else {
          // 내부망 HTTP에서는 Clipboard API를 사용할 수 없다.
          const previousFocus = document.activeElement;
          const selection = document.createElement('textarea');
          selection.value = this.token;
          selection.readOnly = true;
          selection.style.cssText = 'position:fixed;opacity:0;pointer-events:none';
          document.body.appendChild(selection);
          try {
            selection.focus();
            selection.select();
            if (!document.execCommand('copy')) throw new Error('copy failed');
          } finally {
            selection.remove();
            if (previousFocus) previousFocus.focus();
          }
        }
        if (this.componentAlive) this.tokenStatus = 'profileTokenCopied';
      } catch (_) {
        if (this.componentAlive) this.tokenStatus = 'profileTokenCopyFailed';
      }
    },
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
      this.actionError = null;
      this.reauthenticated = false;
      API.SSO.passwordReauth(this.password).then(() => { this.reauthenticated = true; })
        .catch(() => { this.actionError = 'ssoActionFailed'; });
      this.password = '';
    },
    unlinkReasonKey(reason) {
      return unlinkErrorKeys[reason] || 'ssoActionFailed';
    },
    unlink(identity) {
      if (identity.unlink_allowed === false) return;
      this.actionError = null;
      API.SSO.unlink(identity.id).then(() => this.fetchIdentities())
        .catch((error) => {
          const data = error && error.response && error.response.data;
          this.actionError = this.unlinkReasonKey(data && data.code);
        });
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
  display: grid;
  gap: 24px;
  width: calc(100% - 96px);
  max-width: 1280px;
  margin: 24px auto 40px;
}

.card {
  margin: 0;
  border: 1px solid var(--pinry-border);
  border-radius: 16px;
  box-shadow: none;
  overflow: hidden;
}
.card-header { box-shadow: none; padding: 28px 32px 0; }
.card-header-title { padding: 0; font-size: 22px; }
.card-content { padding: 20px 32px 28px; }
.content { color: var(--pinry-muted); }
.button { min-height: 44px; border-radius: 10px; gap: 8px; }
.token-controls { display: flex; gap: 12px; margin: 16px 0; }
.token-field {
  display: flex;
  align-items: center;
  min-width: 0;
  flex: 1;
  border: 1px solid var(--pinry-border);
  border-radius: 10px;
  background: var(--pinry-background);
}
.token-field code { min-width: 0; flex: 1; padding: 12px 16px; overflow-wrap: anywhere; background: none; color: var(--pinry-text); }
.token-toggle { flex-shrink: 0; border: 0; background: transparent; }
.token-copy, .admin-settings-action { color: var(--pinry-link); border-color: var(--pinry-accent); background: transparent; }
.admin-settings-content { display: flex; align-items: center; gap: 20px; padding: 24px 32px; }
.admin-settings-content > .mdi { color: var(--pinry-accent); font-size: 32px; }
.admin-settings-description { flex: 1; }
.admin-settings-description strong { color: var(--pinry-text); font-size: 18px; }
.admin-settings-description p { color: var(--pinry-muted); margin-top: 4px; }
.build-info-card .content { display: flex; flex-wrap: wrap; gap: 12px 28px; }
.build-info-card code { background: none; color: var(--pinry-text); }
.dependency-group { display: flex; gap: 16px; align-items: flex-start; margin-top: 16px; }
.dependency-group h3 { display: flex; align-items: center; gap: 12px; flex: 0 0 130px; margin: 0; min-height: 44px; font-size: 14px; }
.dependency-group h3::before { content: ''; width: 28px; height: 6px; border-radius: 4px; background: var(--dependency-marker); }
.dependency-group--backend { --dependency-marker: #347bd1; --dependency-bubble: var(--pinry-dependency-backend); }
.dependency-group--frontend { --dependency-marker: #00c4a7; --dependency-bubble: var(--pinry-dependency-frontend); }
.dependency-group--buildTools { --dependency-marker: #8fa1b8; --dependency-bubble: var(--pinry-dependency-tools); }
.content ul.dependency-bubbles { display: flex; flex: 1; flex-wrap: wrap; gap: 12px; margin: 0; list-style: none; }
.content li.dependency-bubble { display: flex; align-items: center; gap: 18px; max-width: 100%; min-height: 44px; padding: 10px 20px; margin: 0; border-radius: 999px; background: var(--dependency-bubble); color: var(--pinry-text); }
.dependency-bubble span { overflow-wrap: anywhere; }
.dependency-bubble code { color: inherit; font: inherit; white-space: nowrap; padding: 0; background: none; }
.dependency-build-help { margin-top: 20px; font-size: 14px; }
.sso-connected-status { padding: 4px 12px; border-radius: 999px; background: var(--pinry-selection); color: var(--pinry-link); font-size: 12px; }
.sso-card .card-header {
  background: var(--pinry-surface);
}

.admin-settings-card {
  color: inherit;
}

.sso-card {
  color: var(--pinry-text);
}

.sso-card .card-header-title {
  color: var(--pinry-text);
}

.sso-auto-link-help,
.sso-manual-help,
.sso-provider-help {
  color: var(--pinry-muted);
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
  border-top: 1px solid var(--pinry-border);
}

.sso-manual-controls summary {
  padding: 1rem 0;
  color: var(--pinry-text);
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
  color: var(--pinry-text);
  font-weight: 600;
}

.sso-card .button,
.sso-password-reauth input {
  box-sizing: border-box;
  height: 44px;
  min-height: 44px;
  font: inherit;
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
  color: var(--pinry-text);
  background: var(--pinry-background);
  border: 1px solid var(--pinry-border);
  border-radius: 10px;
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
  color: var(--pinry-link);
  background: transparent;
  border-color: var(--pinry-accent);
  font-weight: 600;
}

.sso-provider-list {
  display: grid;
  gap: .75rem;
  margin-top: 1rem;
}

.sso-link-list {
  padding-top: 1rem;
  border-top: 1px solid var(--pinry-border);
}

.sso-provider-row {
  display: flex;
  min-height: 58px;
  padding: .75rem 0;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  background: transparent;
}

.sso-provider-row__identity,
.sso-provider-row__actions {
  display: flex;
  align-items: center;
  gap: .75rem;
}

.sso-provider-row__controls {
  display: grid;
  justify-items: end;
  gap: .5rem;
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

.sso-unlink-reason {
  max-width: 24rem;
  margin: 0;
  color: #ffb3b3;
  font-size: .875rem;
  line-height: 1.4;
  text-align: right;
}

.sso-provider-row__actions form {
  margin: 0;
}

.sso-secondary-action {
  color: var(--pinry-text);
  background: transparent;
  border-color: #697176;
}

.sso-card .button.is-danger.is-outlined {
  color: #ff7d87;
  background: transparent;
  border-color: #ff5c68;
}

.sso-card .button.is-danger.is-outlined[disabled] {
  color: #9ca2a5;
  background: #222729;
  border-color: #555d61;
  cursor: not-allowed;
  opacity: 1;
}

@media (max-width: 600px) {
  .sso-password-reauth__row,
  .sso-provider-row {
    align-items: stretch;
    flex-direction: column;
  }

  .sso-password-reauth .button,
  .sso-provider-row__controls,
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

  .sso-provider-row__controls {
    justify-items: stretch;
  }

  .sso-unlink-reason {
    max-width: none;
    text-align: left;
  }
}

[data-test="build-version"],
.build-info-card code {
  margin-left: .5rem;
  user-select: text;
}

@media (max-width: 700px) {
  .profile-container { width: calc(100% - 32px); gap: 16px; margin-top: 16px; }
  .card-header { padding: 20px 20px 0; }
  .card-content { padding: 16px 20px 20px; }
  .card-header-title { font-size: 20px; }
  .token-controls { flex-wrap: wrap; }
  .token-field { flex-basis: 100%; }
  .token-field code { font-size: 12px; }
  .admin-settings-content { flex-wrap: wrap; }
  .admin-settings-action { margin-left: auto; }
  .dependency-group { flex-direction: column; gap: 8px; }
  .dependency-group h3 { flex-basis: auto; min-height: 24px; }
  .content ul.dependency-bubbles { gap: 8px; }
  .content li.dependency-bubble { padding: 8px 14px; gap: 12px; font-size: 14px; }
}
</style>
