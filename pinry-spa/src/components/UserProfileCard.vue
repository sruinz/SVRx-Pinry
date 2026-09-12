<template>
    <div class="user-profile-card">
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
                  <a @click="go2UserPins">
                    <i aria-hidden="true" class="mdi mdi-image"></i>
                    <span>{{ $t("pinsUserProfileCardLink") }}</span>
                  </a>
                </li>
                <li :class="trueFalse2Class(inBoard)">
                  <a @click="go2UserBoard">
                    <i aria-hidden="true" class="mdi mdi-folder-multiple-image"></i>
                    <span>{{ $t("boardsUserProfileCardLink") }}</span>
                  </a>
                </li>
                <li :class="trueFalse2Class(inProfile)">
                  <a @click="go2UserProfile">
                    <i aria-hidden="true" class="mdi mdi-account"></i>
                    <span>{{ $t("profileUserProfileCardLink") }}</span>
                  </a>
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
    go2UserBoard() {
      this.$router.push(
        { name: 'boards4user', params: { username: this.username } },
      );
    },
    go2UserProfile() {
      this.$router.push(
        { name: 'profile4user', params: { username: this.username } },
      );
    },
    go2UserPins() {
      this.$router.push(
        { name: 'user', params: { user: this.username } },
      );
    },
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
#user-home-container {
  margin-top: 2rem;
  margin-left: auto;
  margin-right: auto;
  box-shadow: 5px 5px 2px 1px rgba(0, 0, 255, .1);
}
@import '../components/utils/grid-layout';
@include screen-grid-layout("#user-home-container");
</style>
