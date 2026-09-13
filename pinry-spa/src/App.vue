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

<style src="bulma/css/bulma.css"></style>

<style src="./assets/pinry-theme.css"></style>
