<template>
  <div class="search-panel">
    <div class="filter-selector">
      <div class="card-content">
        <FormField>
          <select :aria-label="$t('chooseFilterPlaceholder')" v-model="filterType">
            <option :value="null" disabled>{{ $t('chooseFilterPlaceholder') }}</option>
            <option value="Tag">{{ $t("SearchPanelTagOption") }}</option>
            <option value="Board">{{ $t("SearchPanelBoardOption") }}</option>
          </select>
          <TagInput
            v-show="filterType === 'Tag'"
            class="search-input"
            v-model="selectedTags"
            :data="filteredDataArray"
            autocomplete
            ellipsis
            :allow-new="false"
            :open-on-focus="true"
            v-bind:placeholder="$t('selectFilterPlaceholder')"
            icon="magnify"
            @typing="updateTagSearch">
            <template #empty>{{ $t("noResultsFound") }}</template>
          </TagInput>
          <template v-if="filterType === 'Board'">
            <input class="input search-input" :aria-label="$t('searchBoardPlaceholder')"
              type="search"
              v-model="boardText"
              v-bind:placeholder="$t('searchBoardPlaceholder')"
            >
            <p class="control">
              <button type="button" @click="searchBoard" class="button is-primary">{{ $t("searchButton") }}</button>
            </p>
          </template>
        </FormField>
      </div>
    </div>
  </div>
</template>

<script>
import FormField from '../ui/FormField.vue';
import TagInput from '../ui/TagInput.vue';
import api from '../api';

export default {
  components: { FormField, TagInput },
  name: 'FilterSelector',
  data() {
    return {
      filterType: null,
      selectedOption: [],
      options: {
        Tag: [],
      },
      name: '',
      boardText: '',
      selectedTags: [],
    };
  },
  methods: {
    selectOption(filterName) {
      this.name = '';
      this.boardText = '';
      this.selectedTags = [];
      if (filterName === 'Tag') {
        this.selectedOption = this.options.Tag;
      }
    },
    updateTagSearch(value) {
      this.name = value;
    },
    searchBoard() {
      if (this.boardText === '') {
        return;
      }
      this.$emit(
        'selected',
        { filterType: this.filterType, selected: this.boardText },
      );
    },
  },
  watch: {
    filterType(newVal) {
      this.selectOption(newVal);
    },
    selectedTags(newVal) {
      if (this.filterType !== 'Tag') return;
      this.$emit(
        'selected',
        { filterType: this.filterType, selected: newVal.slice() },
      );
    },
  },
  computed: {
    filteredDataArray() {
      return this.selectedOption.filter(
        (option) => {
          const ret = !this.selectedTags.includes(option) && option
            .toString()
            .toLowerCase()
            .indexOf(this.name.toLowerCase()) >= 0;
          return ret;
        },
      );
    },
  },
  created() {
    api.Tag.fetchList().then(
      (resp) => {
        const options = [];
        resp.data.forEach(
          (tag) => {
            options.push(tag.name);
          },
        );
        this.options.Tag = options;
        if (this.filterType === 'Tag') {
          this.selectedOption = options;
        }
      },
    );
  },
};
</script>

<style scoped="scoped" lang="scss">
  .search-panel {
    padding-top: 3rem;
    padding-left: 2rem;
    padding-right: 2rem;
  }
  .filter-selector {
    background-color: white;
    border-radius: 3px;
    .search-input {
      width: 100%;
    }
  }
</style>
