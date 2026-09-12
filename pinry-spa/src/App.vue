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
  html {
    background-color: #F5F5EB;
  }
  .body {
    font-family: 'Open Sans', sans-serif;
  }
</style>
