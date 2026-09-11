<template>
  <div class="login-modal">
    <div @keydown="triggerDoLogin">
      <div class="modal-card" style="width: auto">
        <header class="modal-card-head">
          <p class="modal-card-title">{{ $t("loginTitle") }}</p>
        </header>
        <section class="modal-card-body">
          <p v-if="policyError" role="alert">{{ $t('ssoSettingsFailed') }}</p>
          <p v-else-if="!policy">{{ $t('ssoLoading') }}</p>
          <a v-for="provider in providers" :key="provider.id"
             class="button is-link" :href="provider.login_url">{{ provider.name }}</a>
          <p v-if="policy && !policy.password_login_enabled">{{ $t('ssoPasswordDisabled') }}</p>
          <div v-if="passwordAllowed" data-test="password-form">
          <b-field v-bind:label="$t('usernameLabel')"
                   :type="form.username.type"
                   :message="form.username.error">
            <b-input
              name="username"
              type="text"
              v-model="form.username.value"
              v-bind:placeholder="$t('usernamePlaceholder')"
              maxlength="30"
              required>
            </b-input>
          </b-field>

          <b-field v-bind:label="$t('passwordLabel')"
                   :type="form.password.type"
                   :message="form.password.error">
            <b-input
              type="password"
              v-model="form.password.value"
              password-reveal
              v-bind:placeholder="$t('passwordLoginPlaceholder')"
              required>
            </b-input>
          </b-field>
          </div>
        </section>
        <footer class="modal-card-foot">
          <button class="button" type="button" @click="$parent.close()">{{ $t("closeButton") }}</button>
          <button
            v-if="passwordAllowed"
            @click="doLogin"
            class="button is-primary">{{ $t("loginButton") }}</button>
        </footer>
      </div>
    </div>
  </div>
</template>

<script>
import api from './api';
import ModelForm from './utils/ModelForm';

const fields = ['username', 'password'];

export default {
  name: 'LoginForm',
  data() {
    const model = ModelForm.fromFields(fields);
    return {
      form: model.form,
      helper: model,
      policy: null,
      policyError: false,
    };
  },
  computed: {
    passwordAllowed() { return this.policy && this.policy.password_login_enabled; },
    providers() { return this.policy ? this.policy.providers : []; },
  },
  created() {
    api.SSO.policy().then((policy) => { this.policy = policy; })
      .catch(() => { this.policyError = true; });
  },
  methods: {
    triggerDoLogin(e) {
      if (e.keyCode === 13) {
        this.doLogin();
        return false;
      }
      return true;
    },
    doLogin() {
      if (!this.passwordAllowed) return;
      this.helper.resetAllFields();
      const self = this;
      const promise = api.User.logIn(
        self.form.username.value,
        self.form.password.value,
      );
      promise.then(
        (user) => {
          self.$emit('login.succeed', user);
          self.$parent.close();
          window.location.reload();
        },
        (resp) => {
          self.helper.markFieldsAsDanger(resp.data);
        },
      );
    },
  },
};
</script>
