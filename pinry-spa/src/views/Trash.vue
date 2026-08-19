<template>
  <div class="trash">
    <PHeader></PHeader>
    <main class="section">
      <div class="container">
        <h1 class="title">{{ $t("trashTitle") }}</h1>
        <TrashPins></TrashPins>
      </div>
    </main>
  </div>
</template>

<script>
import PHeader from '../components/PHeader.vue';
import TrashPins from '../components/TrashPins.vue';

export default {
  name: 'Trash',
  components: {
    PHeader,
    TrashPins,
  },
};
</script>
