<template>
  <div id="app">
    <router-view/>
    <AppOverlays />
  </div>
</template>

<script>
import AppOverlays from './components/ui/AppOverlays.vue';

export default {
  name: 'app',
  components: { AppOverlays },
};
</script>

<style lang="scss">
  @import "bulma/sass/utilities/_all";
  @import "bulma/bulma.sass";
</style>

<style src="./assets/pinry-theme.css"></style>
