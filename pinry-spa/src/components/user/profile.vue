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
