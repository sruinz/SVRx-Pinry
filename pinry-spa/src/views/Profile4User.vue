<template>
  <div class="profile-for-user">
    <PHeader></PHeader>
    <UserProfileCard :in-profile="true" :username="filters.userFilter"></UserProfileCard>
    <Profile
      v-if="profile"
      :token="profile.token"
      :can-access-admin="profile.can_access_admin"></Profile>
  </div>
</template>

<script>
import PHeader from '../components/PHeader.vue';
import UserProfileCard from '../components/UserProfileCard.vue';
import Profile from '../components/user/profile.vue';
import api from '../components/api';

export default {
  name: 'Profile4User',
  data() {
    return {
      filters: { userFilter: null },
      profile: null,
      userRequestSequence: 0,
    };
  },
  components: {
    PHeader,
    UserProfileCard,
    Profile,
  },
  created() {
    this.initializeBoard();
    this.initializeUser(this.filters.userFilter);
  },
  beforeRouteUpdate(to, from, next) {
    this.filters = { userFilter: to.params.username };
    this.initializeUser(to.params.username);
    next();
  },
  beforeDestroy() {
    this.userRequestSequence += 1;
  },
  methods: {
    initializeBoard() {
      this.filters = { userFilter: this.$route.params.username };
    },
    async initializeUser(username) {
      const requestSequence = this.userRequestSequence + 1;
      this.userRequestSequence = requestSequence;
      this.profile = null;

      const publicUser = await api.User.fetchUserInfoByName(username);
      if (requestSequence !== this.userRequestSequence) return;
      if (publicUser === null) {
        this.$router.push({ name: 'PageNotFound' });
        return;
      }

      const currentUser = await api.User.fetchUserInfo(true);
      if (requestSequence !== this.userRequestSequence) return;
      if (currentUser !== null && currentUser.username === username) {
        this.profile = currentUser;
      }
    },
  },
};
</script>

<!-- Add "scoped" attribute to limit CSS to this component only -->
<style scoped lang="scss">
</style>
