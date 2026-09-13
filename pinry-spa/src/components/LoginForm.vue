<template>
  <div class="login-modal">
    <div class="modal-card login-card">
      <header class="login-card__header">
        <div class="login-card__brand" data-test="login-brand">
          <img :src="brandLogo" alt="">
          <span>SVRx Pinry</span>
        </div>
        <button
          class="login-card__close"
          type="button"
          :aria-label="$t('closeButton')"
          @click="$emit('close')">
          <img src="/static/auth/icons/x.svg" alt="">
        </button>
      </header>

      <section class="login-card__body">
        <h1>{{ $t('loginTitle') }}</h1>
        <p class="login-card__subtitle">{{ $t('loginSubtitle') }}</p>

        <p v-if="policyError" class="login-card__status" role="alert">
          {{ $t('ssoSettingsFailed') }}
        </p>
        <p v-else-if="!policy" class="login-card__status">
          {{ $t('ssoLoading') }}
        </p>

        <div v-if="providers.length" class="login-providers">
          <a
            v-for="provider in providers"
            :key="provider.id"
            class="login-provider"
            :class="{ 'login-provider--authentik': providerKind(provider.kind) === 'authentik' }"
            :href="provider.login_url">
            <img
              data-test="provider-icon"
              :src="providerIcon(provider.kind)"
              alt="">
            <span>{{ $t('ssoLoginWithProvider', { provider: provider.name }) }}</span>
          </a>
        </div>

        <button
          v-if="showPasswordDisclosure"
          data-test="password-disclosure"
          class="password-disclosure"
          type="button"
          :aria-expanded="passwordExpanded ? 'true' : 'false'"
          @click="passwordExpanded = !passwordExpanded">
          <span class="password-disclosure__label">
            <img src="/static/auth/icons/lock-keyhole.svg" alt="">
            {{ $t('passwordLoginTitle') }}
          </span>
          <img
            class="password-disclosure__chevron"
            :class="{ 'is-expanded': passwordExpanded }"
            src="/static/auth/icons/chevron-down.svg"
            alt="">
        </button>

        <form
          v-if="showPasswordForm"
          data-test="password-form"
          class="password-form"
          @submit.prevent="doLogin">
          <FormField
            v-bind:label="$t('usernameLabel')"
            label-for="login-username"
            :type="form.username.type"
            :message="form.username.error">
            <input class="input" :aria-label="$t('usernameLabel')"
              id="login-username"
              name="username"
              type="text"
              v-model="form.username.value"
              v-bind:placeholder="$t('usernamePlaceholder')"
              autocomplete="username"
              maxlength="30"
              required>
          </FormField>

          <FormField
            v-bind:label="$t('passwordLabel')"
            label-for="login-password"
            :type="form.password.type"
            :message="form.password.error">
            <PasswordInput
              id="login-password"
              name="password"
              type="password"
              v-model="form.password.value"
              autocomplete="current-password"
              v-bind:placeholder="$t('passwordLoginPlaceholder')"
              required />
          </FormField>

          <button
            class="button login-submit"
            :class="{ 'is-loading': loginPending }"
            type="submit"
            :disabled="loginPending">
            {{ $t('loginButton') }}
          </button>
        </form>

        <a
          v-if="policy && policy.recovery_login_url"
          class="recovery-login"
          :href="policy.recovery_login_url">
          관리자 복구 로그인
        </a>
      </section>
    </div>
  </div>
</template>

<script>
import FormField from './ui/FormField.vue';
import PasswordInput from './ui/PasswordInput.vue';
import brandLogo from '@/assets/svrx-pinry-dark-ui.png';
import api from './api';
import ModelForm from './utils/ModelForm';

const fields = ['username', 'password'];
const providerKinds = ['authentik', 'google', 'microsoft', 'github', 'synology', 'oidc'];

export default {
  components: { FormField, PasswordInput },
  name: 'LoginForm',
  data() {
    const model = ModelForm.fromFields(fields);
    return {
      brandLogo,
      form: model.form,
      helper: model,
      policy: null,
      policyError: false,
      passwordExpanded: false,
      loginPending: false,
    };
  },
  computed: {
    passwordAllowed() { return this.policy && this.policy.password_login_enabled; },
    providers() { return this.policy ? this.policy.providers : []; },
    showPasswordDisclosure() { return this.passwordAllowed && this.providers.length > 0; },
    showPasswordForm() {
      return this.passwordAllowed && (!this.providers.length || this.passwordExpanded);
    },
  },
  created() {
    api.SSO.policy().then((policy) => { this.policy = policy; })
      .catch(() => { this.policyError = true; });
  },
  methods: {
    providerKind(kind) {
      return providerKinds.includes(kind) ? kind : 'oidc';
    },
    providerIcon(kind) {
      return `/static/auth/providers/${this.providerKind(kind)}.svg`;
    },
    doLogin() {
      if (!this.showPasswordForm || this.loginPending) return;
      this.helper.resetAllFields();
      this.loginPending = true;
      const promise = api.User.logIn(
        this.form.username.value,
        this.form.password.value,
      );
      promise.then(
        (user) => {
          this.$emit('login.succeed', user);
          this.$emit('close');
          window.location.reload();
        },
        (resp) => {
          this.loginPending = false;
          this.helper.markFieldsAsDanger(resp.data);
        },
      );
    },
  },
};
</script>

<style scoped>
.login-modal {
  width: calc(100vw - 32px);
  max-width: 520px;
  margin: 0 auto;
}

.login-card {
  width: 100%;
  max-height: calc(100vh - 32px);
  overflow-y: auto;
  overflow-x: hidden;
  -webkit-overflow-scrolling: touch;
  color: #f4f5f5;
  background: #1b1f21;
  border: 1px solid #3b4246;
  border-radius: 12px;
  box-shadow: 0 24px 70px rgba(0, 0, 0, 0.42);
}

.login-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 32px 34px 0;
}

.login-card__brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  color: #f4f5f5;
  font-size: 19px;
  font-weight: 500;
  letter-spacing: 0.01em;
}

.login-card__brand img {
  width: 32px;
  height: 32px;
  object-fit: contain;
}

.login-card__close {
  display: inline-flex;
  width: 36px;
  height: 36px;
  padding: 8px;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 0;
  border-radius: 6px;
  cursor: pointer;
}

.login-card__close:hover,
.login-card__close:focus-visible {
  background: #292e31;
  outline: none;
}

.login-card__close img {
  width: 20px;
  height: 20px;
}

.login-card__body {
  flex: 0 0 auto;
  padding: 44px 34px 34px;
}

.login-card__body h1 {
  margin: 0;
  color: #f7f8f8;
  font-size: 34px;
  font-weight: 700;
  line-height: 1.2;
}

.login-card__subtitle {
  margin: 10px 0 28px;
  color: #aeb4b7;
  font-size: 17px;
}

.login-card__status {
  margin: 0;
  padding: 14px 16px;
  color: #c9ced0;
  background: #141719;
  border-radius: 8px;
}

.login-providers {
  display: grid;
  gap: 12px;
}

.login-provider {
  display: flex;
  min-height: 58px;
  padding: 12px 18px;
  align-items: center;
  justify-content: center;
  gap: 14px;
  color: #f5f6f6;
  background: #171a1c;
  border: 1px solid #697176;
  border-radius: 9px;
  font-size: 17px;
  font-weight: 600;
  text-decoration: none;
  transition: border-color 120ms ease, background-color 120ms ease;
}

.login-provider:hover,
.login-provider:focus-visible {
  color: #fff;
  background: #22272a;
  border-color: #9aa1a5;
  outline: none;
}

.login-provider--authentik {
  background: #ed4b24;
  border-color: #ed4b24;
}

.login-provider--authentik:hover,
.login-provider--authentik:focus-visible {
  background: #f15a35;
  border-color: #f15a35;
}

.login-provider img {
  width: 28px;
  height: 28px;
  object-fit: contain;
}

.password-disclosure {
  display: flex;
  width: 100%;
  margin-top: 28px;
  padding: 24px 6px 0;
  align-items: center;
  justify-content: space-between;
  color: #eef0f1;
  background: transparent;
  border: 0;
  border-top: 1px solid #3b4246;
  font-size: 16px;
  cursor: pointer;
}

.password-disclosure__label {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}

.password-disclosure__label img,
.password-disclosure__chevron {
  width: 20px;
  height: 20px;
}

.password-disclosure__chevron {
  transition: transform 160ms ease;
}

.password-disclosure__chevron.is-expanded {
  transform: rotate(180deg);
}

.password-form {
  margin-top: 24px;
}

.password-disclosure + .password-form {
  margin-top: 18px;
}

.password-form :deep(.label) {
  color: #d9dcde;
}

.password-form :deep(.input) {
  min-height: 48px;
  color: #f5f6f6;
  background: #121516;
  border-color: #4b5357;
}

.password-form :deep(.input::placeholder) {
  color: #aeb4b7;
  opacity: 1;
}

.password-form :deep(.input:focus) {
  border-color: #e86036;
  box-shadow: 0 0 0 1px #e86036;
}

.login-submit {
  width: 100%;
  min-height: 48px;
  margin-top: 8px;
  color: #10211e;
  background: #00d1b2;
  border-color: #00d1b2;
  font-weight: 600;
}

.login-submit:hover,
.login-submit:focus-visible {
  color: #10211e;
  background: #00d1b2;
  border-color: #00d1b2;
}

.recovery-login {
  display: block;
  margin-top: 20px;
  color: #bbc0c3;
  font-size: 14px;
  text-align: center;
}

@media (max-width: 560px) {
  .login-card__header {
    padding: 24px 24px 0;
  }

  .login-card__body {
    padding: 34px 24px 26px;
  }

  .login-card__body h1 {
    font-size: 30px;
  }
}
</style>
