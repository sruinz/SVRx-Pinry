<template>
  <div class="filter-select">
    <FormField v-bind:label="$t('selectBoardLabel')"
             :type="form.name.type"
             :message="form.name.error">
      <input class="input" :aria-label="$t('nameLabel')"
        type="text"
        v-model="form.name.value"
        v-bind:placeholder="$t('filterSelectSelectBoardPlaceholder')"
        maxlength="128"
      >
    </FormField>
    <FormField>
      <button
        @click="createNewBoard"
        class="button is-primary">
        {{ $t("filterSelectCreateNewBoardButton") }}
      </button>
    </FormField>
    <FormField>
      <select
        :aria-label="$t('selectBoardLabel')"
        class="select-list"
        multiple
        size="8"
        v-model="selectedOptions">
        <template v-for="option in availableOptions" :key="option.value">
          <option
            :disabled="option.disabled === true"
            :value="option.value">{{ option.displayName || option.name }}</option>
        </template>
      </select>
    </FormField>
  </div>
</template>

<script>
import FormField from '../ui/FormField.vue';
import API from '../api';
import ModelForm from '../utils/ModelForm';
import AutoComplete from '../utils/AutoComplete';

const fields = ['name'];

function getBoardFromResp(boardObject) {
  return { name: boardObject.name, value: boardObject.id };
}

function getAvailableOptions(vm, filter) {
  const knownValues = new Set(
    vm.allOptions.map(option => String(option.value)),
  );
  const options = vm.createdOptions
    .filter(option => !knownValues.has(String(option.value)))
    .concat(vm.allOptions);
  let availableOptions;
  if (filter === '' || filter === null) {
    availableOptions = options;
  } else {
    availableOptions = AutoComplete.getFilteredOptions(
      options, vm.form.name.value,
    );
  }
  return availableOptions;
}


export default {
  components: { FormField },
  name: 'FilterSelect',
  props: {
    allOptions: {
      type: Array,
      default() {
        return [];
      },
    },
  },
  data() {
    const model = ModelForm.fromFields(fields);
    return {
      form: model.form,
      selectedOptions: [],
      helper: model,
      availableOptions: [],
      createdOptions: [],
    };
  },
  methods: {
    select(board) {
      this.selectedOptions = [board.value];
    },
    createNewBoard() {
      const self = this;
      const promise = API.Board.create(this.form.name.value);
      promise.then(
        (data) => {
          self.$emit('boardCreated', data);
          const board = getBoardFromResp(data);
          self.createdOptions.unshift(board);
          this.availableOptions = getAvailableOptions(
            this, this.form.name.value,
          );
          self.select(board);
          self.form.name.value = null;
        },
        (resp) => {
          self.helper.markFieldsAsDanger(resp.data);
        },
      );
    },
  },
  watch: {
    // eslint-disable-next-line func-names
    'form.name.value': function (newVal) {
      this.availableOptions = getAvailableOptions(this, newVal);
    },
    allOptions() {
      this.availableOptions = getAvailableOptions(
        this, this.form.name.value,
      );
    },
    selectedOptions() {
      this.helper.resetAllFields();
      this.$emit('selected', this.selectedOptions);
    },
  },
};
</script>

<style scoped>
.select-list {
  display: block;
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  padding: 6px;
  font: inherit;
  color: var(--pinry-text);
  background: var(--pinry-background);
  border: 1px solid var(--pinry-border);
  border-radius: 6px;
}
.select-list option { padding: 8px 10px; border-radius: 4px; }
.select-list option:checked { background: var(--pinry-selection); color: var(--pinry-text); }
.select-list:focus { border-color: var(--pinry-accent); }
</style>
