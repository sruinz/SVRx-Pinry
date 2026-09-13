<template>
  <div class="search-panel">
    <div class="filter-selector">
      <div class="card-content">
        <FormField>
          <select class="input search-mode" :aria-label="$t('chooseFilterPlaceholder')"
            v-model="filterType" @change="changeMode">
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
        <template v-if="filterType === 'Tag'">
          <details class="image-search-details" :open="detailsOpen">
            <summary>{{ $t('imageSearch_title') }}</summary>
            <div class="image-search-grid">
              <div v-for="key in ['animation', 'aspect']" :key="key" class="image-search-field">
                <label :for="`search-${key}`"><span>{{ $t(`imageSearch_${key}`) }}</span>
                <select class="input" :id="`search-${key}`" :name="key" v-model="draft[key]"
                  :aria-invalid="Boolean(errors[key])" :aria-describedby="errors[key] ? `search-error-${key}` : undefined">
                  <option value="">{{ $t('imageSearch_all') }}</option>
                  <option v-for="option in choices[key]" :key="option" :value="option">{{ $t(`imageSearch_${option}`) }}</option>
                </select>
                </label>
                <p v-if="errors[key]" :id="`search-error-${key}`" role="alert">{{ $t('imageSearch_invalid') }}</p>
              </div>
              <div v-for="key in ['min_width', 'min_height', 'date_from', 'date_to']" :key="key" class="image-search-field">
                <label :for="`search-${key}`"><span>{{ $t(`imageSearch_${key}`) }}</span>
                <input class="input" :id="`search-${key}`" :name="key" v-model="draft[key]"
                  :type="key.startsWith('date_') ? 'date' : 'number'"
                  :min="key.startsWith('date_') ? '0002-01-01' : '1'"
                  :max="key.startsWith('date_') ? '9998-12-31' : '2147483647'"
                  :placeholder="key.startsWith('date_') ? 'YYYY-MM-DD' : '800'"
                  :aria-invalid="Boolean(errors[key])" :aria-describedby="errors[key] ? `search-error-${key}` : undefined">
                </label>
                <p v-if="errors[key]" :id="`search-error-${key}`" role="alert">{{ $t(errors[key] === 'before_start' ? 'imageSearch_dateOrder' : 'imageSearch_invalid') }}</p>
              </div>
            </div>
            <p class="image-search-hint">{{ $t('imageSearch_dateHint') }}</p>
            <p v-if="draft.animation" class="image-search-hint">{{ $t('imageSearch_animationHint') }}</p>
          </details>
          <div class="image-search-actions">
            <button type="button" class="button is-primary" data-search-apply @click="applyFilters">{{ $t('searchButton') }}</button>
            <button type="button" class="button" data-search-reset @click="resetFilters">{{ $t('imageSearch_reset') }}</button>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script>
import FormField from '../ui/FormField.vue';
import TagInput from '../ui/TagInput.vue';
import api from '../api';
import { readSearchQuery, SEARCH_FIELDS, validateSearchFilters } from './searchQuery';

export default {
  components: { FormField, TagInput },
  name: 'FilterSelector',
  props: { query: { type: Object, default: () => ({}) } },
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
      draft: Object.fromEntries(SEARCH_FIELDS.map(key => [key, ''])),
      errors: {},
      detailsOpen: false,
      restoringQuery: false,
      choices: { animation: ['static', 'animated'], aspect: ['landscape', 'portrait', 'square'] },
    };
  },
  methods: {
    changeMode() {
      this.selectOption(this.filterType);
      this.errors = {};
    },
    applyFilters() {
      const { filters, errors } = validateSearchFilters(this.draft);
      this.errors = errors;
      if (Object.keys(errors).length) {
        this.detailsOpen = true;
        return;
      }
      const payload = { filterType: 'Tag', selected: this.selectedTags.slice() };
      if (Object.keys(filters).length) payload.filters = filters;
      this.$emit('selected', payload);
    },
    resetFilters() {
      this.restoringQuery = true;
      this.selectedTags = [];
      this.draft = Object.fromEntries(SEARCH_FIELDS.map(key => [key, '']));
      this.errors = {};
      this.applyFilters();
      this.$nextTick(() => { this.restoringQuery = false; });
    },
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
    query: {
      immediate: true,
      handler(query) {
        const state = readSearchQuery(query);
        this.restoringQuery = true;
        this.filterType = state.filterType;
        this.selectedTags = state.filterType === 'Tag' ? state.selected : [];
        this.boardText = state.filterType === 'Board' ? state.selected : '';
        this.draft = Object.fromEntries(SEARCH_FIELDS.map(key => [key, typeof query[key] === 'string' ? query[key] : '']));
        this.errors = state.errors;
        this.detailsOpen = Object.keys(state.filters).length > 0 || Object.keys(state.errors).length > 0;
        this.$nextTick(() => { this.restoringQuery = false; });
      },
    },
    selectedTags() {
      if (this.filterType !== 'Tag' || this.restoringQuery) return;
      this.applyFilters();
    },
  },
  computed: {
    filteredDataArray() {
      return this.options.Tag.filter(
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
    background-color: var(--pinry-surface);
    border: 1px solid var(--pinry-border);
    border-radius: 12px;
    .search-input {
      width: 100%;
    }
  }
  .search-mode { max-width: 220px; }
  .image-search-details { margin-top: 1rem; }
  summary { cursor: pointer; padding: .5rem 0; font-weight: 600; }
  .image-search-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1rem; margin-top: .75rem; }
  .image-search-field { min-width: 0; }
  .image-search-field label { display: block; margin-bottom: .4rem; }
  .image-search-field label span { display: block; margin-bottom: .4rem; }
  .image-search-field input, .image-search-field select { width: 100%; min-width: 0; box-sizing: border-box; }
  .image-search-field [role="alert"] { margin-top: .4rem; color: var(--pinry-text); }
  [aria-invalid="true"] { border-color: #d46969; }
  .image-search-hint { color: var(--pinry-muted); font-size: .875rem; margin-top: .75rem; }
  .image-search-actions { display: flex; gap: .75rem; margin-top: 1rem; flex-wrap: wrap; }
  @media (max-width: 700px) {
    .search-panel { padding: 1rem; }
    .card-content { padding: 1rem; }
    .image-search-grid { grid-template-columns: minmax(0, 1fr); }
    .search-mode { max-width: none; }
  }
</style>
