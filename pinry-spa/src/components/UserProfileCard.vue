<template>
    <div class="user-profile-card" :class="{ 'user-profile-card--settings': inProfile }">
      <div id="user-home-container">
        <div class="card">
          <div class="card-content">
            <div class="media">
              <div class="media-left">
                <figure class="image is-48x48">
                  <span v-if="avatarLoading" class="app-skeleton" />
                  <img
                    @load="onAvatarLoaded"
                    v-show="!avatarLoading"
                    :src="user.avatar"
                    alt="avatar"
                  >
                </figure>
              </div>
              <div class="media-content" v-show="!avatarLoading">
                <p class="title is-4">{{ user.username }}</p>
                <p class="subtitle is-6">@{{ location }}</p>
              </div>
            </div>
            <div class="content">
              {{ $t("userProfileCardContent") }}
              <br>
            </div>

            <div class="tabs is-toggle">
              <ul>
                <li :class="trueFalse2Class(inPins)">
                  <router-link :to="{ name: 'user', params: { user: username } }">
                    <i aria-hidden="true" class="mdi mdi-image"></i>
                    <span>{{ $t("pinsUserProfileCardLink") }}</span>
                  </router-link>
                </li>
                <li :class="trueFalse2Class(inBoard)">
                  <router-link :to="{ name: 'boards4user', params: { username } }">
                    <i aria-hidden="true" class="mdi mdi-folder-multiple-image"></i>
                    <span>{{ $t("boardsUserProfileCardLink") }}</span>
                  </router-link>
                </li>
                <li :class="trueFalse2Class(inProfile)">
                  <router-link :to="{ name: 'profile4user', params: { username } }">
                    <i aria-hidden="true" class="mdi mdi-account"></i>
                    <span>{{ $t("profileUserProfileCardLink") }}</span>
                  </router-link>
                </li>
              </ul>
            </div>

          </div>
        </div>
      </div>
    </div>
</template>

<script>
import api from './api';

export default {
  name: 'UserProfileCard.vue',
  props: {
    username: String,
    inBoard: {
      type: Boolean,
      default: false,
    },
    inPins: {
      type: Boolean,
      default: false,
    },
    inProfile: {
      type: Boolean,
      default: false,
    },
  },
  data() {
    return {
      location: window.location.host,
      avatarLoading: true,
      userRequestSequence: 0,
      user: {
        avatar: '',
        username: '',
      },
    };
  },
  beforeMount() {
    this.initializeUser(this.username);
  },
  beforeUnmount() {
    this.userRequestSequence += 1;
  },
  watch: {
    username(username) {
      this.initializeUser(username);
    },
  },
  methods: {
    trueFalse2Class(boolValue) {
      if (boolValue) {
        return 'is-active';
      }
      return '';
    },
    onAvatarLoaded() {
      this.avatarLoading = false;
    },
    initializeUser(username) {
      const requestSequence = this.userRequestSequence + 1;
      this.userRequestSequence = requestSequence;
      this.avatarLoading = true;
      this.user = {
        avatar: '',
        username: '',
      };
      const self = this;
      api.User.fetchUserInfoByName(username).then(
        (user) => {
          if (requestSequence !== self.userRequestSequence) return;
          if (user === null) {
            self.$router.push(
              { name: 'PageNotFound' },
            );
          } else {
            self.user.avatar = `//gravatar.com/avatar/${user.gravatar}`;
            self.user.username = user.username;
            self.user.meta = user;
          }
        },
      );
    },
  },
};
</script>

<style lang="scss" scoped>
@use 'utils/grid-layout' as *;
#user-home-container {
  margin-top: 2rem;
  margin-left: auto;
  margin-right: auto;
  box-shadow: 5px 5px 2px 1px rgba(0, 0, 255, .1);
}
@include screen-grid-layout("#user-home-container");
.user-profile-card--settings #user-home-container {
  width: calc(100% - 96px);
  max-width: 1280px;
  box-shadow: none;
}
.user-profile-card--settings .card {
  border: 1px solid var(--pinry-border);
  border-radius: 16px;
  box-shadow: none;
}
.user-profile-card--settings .card-content { padding: 32px; }
.user-profile-card--settings .media { margin-bottom: 16px; }
.user-profile-card--settings .content { color: var(--pinry-muted); margin-bottom: 20px; }
.user-profile-card--settings .tabs { margin-bottom: 0; }
.user-profile-card--settings .tabs a { min-height: 44px; gap: 8px; }
@media (max-width: 700px) {
  .user-profile-card--settings #user-home-container { width: calc(100% - 32px); }
  .user-profile-card--settings .card-content { padding: 20px; }
  .user-profile-card--settings .tabs a { padding: 8px 12px; }
}
</style>
