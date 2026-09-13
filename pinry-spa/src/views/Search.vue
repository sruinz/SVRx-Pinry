<template>
  <div class="pins-for-tag">
    <PHeader></PHeader>
    <SearchPanel :query="routeQuery" v-on:selected="doSearch"></SearchPanel>
    <p v-if="queryInvalid" class="search-url-error" role="alert">{{ $t('searchInvalidUrl') }}</p>
    <Pins v-if="pinFilters" :pin-filters="pinFilters" :search-mode="true"></Pins>
    <Boards v-if="boardFilters" :filters="boardFilters"></Boards>
  </div>
</template>

<script>
import PHeader from '../components/PHeader.vue';
import Pins from '../components/Pins.vue';
import Boards from '../components/Boards.vue';
import SearchPanel from '../components/search/SearchPanel.vue';
import { readSearchQuery, writeSearchQuery } from '../components/search/searchQuery';

export default {
  name: 'Search',
  data() {
    return {
      pinFilters: null,
      boardFilters: null,
      queryInvalid: false,
    };
  },
  components: {
    PHeader,
    Pins,
    Boards,
    SearchPanel,
  },
  computed: {
    routeQuery() { return this.$route ? this.$route.query : {}; },
  },
  watch: {
    routeQuery: {
      immediate: true,
      handler(query) { this.applySearch(readSearchQuery(query)); },
    },
  },
  methods: {
    doSearch(args) {
      if (this.$router) this.$router.push({ query: writeSearchQuery(args) });
      else this.applySearch(args);
    },
    applySearch(args) {
      this.queryInvalid = Object.keys(args.errors || {}).length > 0;
      if (this.queryInvalid) return;
      this.pinFilters = null;
      this.boardFilters = null;
      if (args.filterType === 'Tag') {
        const filters = args.filters || {};
        if (Array.isArray(args.selected)
          && (args.selected.length > 0 || Object.keys(filters).length > 0)) {
          this.pinFilters = { tagFilter: args.selected.slice() };
          if (Object.keys(filters).length) this.pinFilters.searchFilters = { ...filters };
        }
      } else if (args.filterType === 'Board') {
        this.boardFilters = { boardNameContains: args.selected };
      }
    },
  },
};
</script>

<!-- Add "scoped" attribute to limit CSS to this component only -->
<style scoped lang="scss">
.search-url-error { margin: 1rem 2rem; color: var(--pinry-text); }
</style>
