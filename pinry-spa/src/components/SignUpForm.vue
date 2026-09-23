<template>
  <div class="signup-modal">
    <div>
      <div class="modal-card" style="width: auto">
        <header class="modal-card-head">
          <p class="modal-card-title">{{ $t("signUpTitle") }}</p>
        </header>
        <p v-if="policyError" role="alert">{{ $t('ssoSettingsFailed') }}</p>
        <p v-else-if="!registrationAllowed">{{ $t('ssoRegistrationDisabled') }}</p>
        <p v-else-if="!passwordAllowed">{{ $t('ssoPasswordDisabled') }}</p>
        <section v-if="canRegister" class="modal-card-body">
          <FormField v-bind:label="$t('usernameLabel')"
                   :type="form.username.type"
                   :message="form.username.error">
            <input class="input" :aria-label="$t('usernameLabel')"
              type="text"
              v-model="form.username.value"
              v-bind:placeholder="$t('usernamePlaceholder')"
              maxlength="30"
              required>
          </FormField>

          <FormField v-bind:label="$t('emailLabel')"
                   :type="form.email.type"
                   :message="form.email.error">
            <input class="input" :aria-label="$t('emailLabel')"
              type="email"
              v-model="form.email.value"
              v-bind:placeholder="$t('emailPlaceholder')"
              required>
          </FormField>
          <FormField v-bind:label="$t('passwordLabel')"
                   :type="form.password.type"
                   :message="form.password.error">
            <PasswordInput
              type="password"
              v-model="form.password.value"
              v-bind:placeholder="$t('passwordSignUpPlaceholder')"
              required />
          </FormField>
          <FormField v-bind:label="$t('repeatPasswordLabel')"
                   :type="form.password_repeat.type"
                   :message="form.password_repeat.error">
            <PasswordInput
              type="password"
              v-model="form.password_repeat.value"
              v-bind:placeholder="$t('repeatPasswordInputPlaceholder')"
              required />
          </FormField>
        </section>
        <footer class="modal-card-foot">
          <button class="button" type="button" @click="$emit('close')">{{ $t("closeButton") }}</button>
          <button
            v-if="canRegister"
            @click="doRegister"
            class="button is-primary">{{ $t("registerButton") }}</button>
        </footer>
      </div>
    </div>
  </div>
</template>

<script>
import FormField from './ui/FormField.vue';
import PasswordInput from './ui/PasswordInput.vue';
import overlays from './utils/overlays';
import api from './api';
import ModelForm from './utils/ModelForm';

const fields = [
  'username',
  'email',
  'password',
  'password_repeat',
];

export default {
  components: { FormField, PasswordInput },
  name: 'SignUpForm',
  data() {
    const model = ModelForm.fromFields(fields);
    return {
      form: model.form,
      helper: model,
      passwordAllowed: false,
      registrationAllowed: false,
      policyError: false,
    };
  },
  created() {
    api.SSO.policy().then((policy) => {
      this.passwordAllowed = policy.password_login_enabled;
      this.registrationAllowed = policy.allow_new_registrations;
    })
      .catch(() => { this.policyError = true; });
  },
  computed: {
    canRegister() {
      return this.passwordAllowed && this.registrationAllowed;
    },
  },
  methods: {
    doRegister() {
      if (!this.canRegister) return;
      this.helper.resetAllFields();
      const self = this;
      const promise = api.User.signUp(
        self.form.username.value,
        self.form.email.value,
        self.form.password.value,
        self.form.password_repeat.value,
      );
      promise.then(
        (user) => {
          self.$emit('signup.succeed', user);
          self.$emit('close');
        },
        (resp) => {
          if (resp.status === 401) {
            overlays.toast(
              { type: 'is-danger', message: 'sign up of this site closed by owner' },
            );
          } else {
            self.helper.markFieldsAsDanger(resp.data);
          }
        },
      );
    },
  },
};
</script>
