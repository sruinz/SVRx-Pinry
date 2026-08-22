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
          <pre>{{ token }}</pre>
          {{ $t("pleaseReadTokenUserProfileCardContent") }}<a target="_blank" href="https://www.django-rest-framework.org/api-guide/authentication/#tokenauthentication">{{ $t("drfApiDocumentationLink") }}</a>{{ $t("forMoreDetailsParagraph") }}
          <br>
        </div>
      </div>
    </div>
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
  </div>
</template>

<script>
import API from '../api';

export default {
  name: 'profile',
  props: ['token'],
  data() {
    return {
      componentAlive: true,
      displayVersion: null,
      versionRequestSequence: 0,
    };
  },
  created() {
    this.fetchBuildVersion();
  },
  beforeDestroy() {
    this.componentAlive = false;
    this.versionRequestSequence += 1;
  },
  methods: {
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

.build-info-card {
  margin-top: 1rem;
}

[data-test="build-version"] {
  margin-left: .5rem;
  user-select: text;
}

@import '../utils/grid-layout';
@include screen-grid-layout(".profile-container");
</style>
