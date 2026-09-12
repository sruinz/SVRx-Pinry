<template>
  <div class="pin-create-modal">
    <div>
      <div class="modal-card" style="width: auto">
        <header class="modal-card-head">
          <p class="modal-card-title">{{ $t(editorMeta.title) }}</p>
        </header>
        <section class="modal-card-body">
          <div class="columns">
            <div class="column">
              <FileUpload
                :previewImageURL="pinModel.form.url.value"
                v-on:imageSelected="onImageSelected"
              ></FileUpload>
              <div class="description" v-show="pinModel.form.description.value" v-html="niceLinks(pinModel.form.description.value)"></div>
            </div>
            <div class="column">
              <FormField v-bind:label="$t('imageUrlLabel')"
                       v-show="!disableUrlField && !isEdit"
                       :type="pinModel.form.url.type"
                       :message="pinModel.form.url.error">
                <input class="input" :aria-label="$t('imageUrlLabel')"
                  type="text"
                  v-model="pinModel.form.url.value"
                  v-bind:placeholder="$t('pinCreateModalImageURLPlaceholder')"
                  maxlength="2048"
                >
              </FormField>
              <FormField v-bind:label="$t('privacyOptionLabel')"
                       :type="pinModel.form.private.type"
                       :message="pinModel.form.private.error">
                <label class="checkbox"><input type="checkbox" v-model="pinModel.form.private.value">
                    {{ $t("isPrivateCheckbox") }}
                </label>
              </FormField>
              <FormField v-bind:label="$t('imageSourceLabel')"
                       :type="pinModel.form.referer.type"
                       :message="pinModel.form.referer.error">
                <input class="input" :aria-label="$t('imageSourceLabel')"
                  type="text"
                  v-model="pinModel.form.referer.value"
                  v-bind:placeholder="$t('pinCreateModalImageSourcePlaceholder')"
                  maxlength="2048"
                >
              </FormField>
              <FormField v-bind:label="$t('tagsLabel')">
                <TagInput
                    v-model="pinModel.form.tags.value"
                    :data="editorMeta.filteredTagOptions"
                    autocomplete
                    ellipsis
                    icon="label"
                    :allow-new="true"
                    v-bind:placeholder="$t('pinCreateModalImageTagsPlaceholder')"
                    @typing="getFilteredTags">
                  <template #default="props">
                    <strong>{{ props.option }}</strong>
                  </template>
                  <template #empty>
                    {{ $t("pinCreateModalEmptySlot") }}
                  </template>
                </TagInput>
              </FormField>
              <FormField v-bind:label="$t('descriptionLabel')"
                       :type="pinModel.form.description.type"
                       :message="pinModel.form.description.error">
                <textarea class="textarea" :aria-label="$t('descriptionLabel')"
                  v-model="pinModel.form.description.value"
                  v-bind:placeholder="$t('pinCreateModalImageDescriptionPlaceholder')"
                  maxlength="1024"
                ></textarea>
              </FormField>
            </div>
            <div class="column" v-if="!isEdit">
              <FilterSelect
                :allOptions="boardOptions"
                v-on:selected="onSelectBoard"
              ></FilterSelect>
            </div>
          </div>
        </section>
        <footer class="modal-card-foot">
          <p
            v-if="createError"
            class="help is-danger"
            data-test="create-error">{{ createError }}</p>
          <button
            class="button"
            type="button"
            :disabled="createInFlight"
            @click="closeModal">{{ $t("closeButton") }}</button>
          <button
            v-if="!isEdit"
            @click="createPin"
            :disabled="createInFlight"
            class="button is-primary">{{ $t("pinCreateModalCreatePinButton") }}
          </button>
          <button
            v-if="isEdit"
            @click="savePin"
            class="button is-primary">{{ $t("pinCreateModalSaveChangesButton") }}
          </button>
        </footer>
      </div>
    </div>
  </div>
</template>

<script>
import axios from 'axios';

import FormField from '../ui/FormField.vue';
import TagInput from '../ui/TagInput.vue';
import API from '../api';
import FileUpload from './FileUpload.vue';
import FilterSelect from './FilterSelect.vue';
import bus from '../utils/bus';
import ModelForm from '../utils/ModelForm';
import Loading from '../utils/Loading';
import AutoComplete from '../utils/AutoComplete';
import niceLinks from '../utils/niceLinks';


function isURLBlank(url) {
  return url !== null && url === '';
}


function textOrBlank(value) {
  return value === null || typeof value === 'undefined' ? '' : value;
}


function createErrorMessage(error, fallbackMessage) {
  const responseData = error && error.response && error.response.data;
  if (typeof responseData === 'string') {
    return responseData;
  }
  if (responseData !== null && typeof responseData === 'object') {
    const values = Object.values(responseData);
    const firstValue = values.length > 0 ? values[0] : null;
    const message = Array.isArray(firstValue) ? firstValue[0] : firstValue;
    if (typeof message === 'string') {
      return message;
    }
  }
  return fallbackMessage;
}


const fields = ['url', 'referer', 'description', 'tags', 'private'];

export default {
  name: 'PinCreateModal',
  props: {
    fromUrl: {
      type: Object,
      default: null,
    },
    username: {
      type: String,
      default: null,
    },
    isEdit: {
      type: Boolean,
      default: false,
    },
    existedPin: {
      type: Object,
      default: null,
    },
  },
  components: {
    FormField,
    TagInput,
    FileUpload,
    FilterSelect,
  },
  data() {
    const pinModel = ModelForm.fromFields(fields);
    pinModel.form.tags.value = [];
    return {
      disableUrlField: false,
      pinModel,
      selectedFile: null,
      createInFlight: false,
      createError: null,
      componentAlive: true,
      activeLoading: null,
      boardIds: null,
      boardOptions: [],
      tagOptions: [],
      editorMeta: {
        title: 'NewPinTitle',
        filteredTagOptions: [],
      },
    };
  },
  created() {
    this.fetchBoardList();
    this.fetchTagList();
    if (this.isEdit) {
      this.editorMeta.title = 'EditPinTitle';
      this.pinModel.form.url.value = this.existedPin.url;
      this.pinModel.form.referer.value = this.existedPin.referer;
      this.pinModel.form.description.value = this.existedPin.description;
      this.pinModel.form.tags.value = this.existedPin.tags;
      this.pinModel.form.private.value = this.existedPin.private;
    } else {
      this.pinModel.form.private.value = false;
    }
    if (this.fromUrl) {
      this.pinModel.form.url.value = this.fromUrl.url;
      this.pinModel.form.referer.value = this.fromUrl.referer;
      this.pinModel.form.description.value = this.fromUrl.description;
    }
  },
  beforeUnmount() {
    this.componentAlive = false;
    this.closeLoading();
  },
  methods: {
    closeLoading() {
      if (this.activeLoading !== null) {
        const loading = this.activeLoading;
        this.activeLoading = null;
        loading.close();
      }
    },
    closeModal() {
      if (!this.createInFlight) {
        this.$emit('close');
      }
    },
    fetchTagList() {
      API.Tag.fetchList().then(
        (resp) => {
          this.tagOptions = resp.data;
        },
      );
    },
    getFilteredTags(text) {
      const filteredTagOptions = [];
      AutoComplete.getFilteredOptions(
        this.tagOptions,
        text,
      ).forEach(
        (option) => {
          filteredTagOptions.push(option.name);
        },
      );
      this.editorMeta.filteredTagOptions = filteredTagOptions;
    },
    fetchBoardList() {
      API.Board.fetchFullList(this.username).then(
        (resp) => {
          const boardOptions = [];
          resp.data.forEach(
            (board) => {
              const boardOption = { name: board.name, value: board.id };
              boardOptions.push(boardOption);
            },
          );
          this.boardOptions = boardOptions;
        },
        () => {
          console.log('Error occurs while fetch board full list');
        },
      );
    },
    onSelectBoard(boardIds) {
      this.boardIds = boardIds;
    },
    onImageSelected(file) {
      this.selectedFile = file;
      this.disableUrlField = file !== null;
    },
    savePin() {
      const self = this;
      const data = this.pinModel.asDataByFields(
        ['referer', 'description', 'tags', 'private'],
      );
      const promise = API.Pin.updateById(this.existedPin.id, data);
      promise.then(
        (resp) => {
          bus.bus.emit(bus.events.refreshPin);
          self.$emit('pinUpdated', resp);
          self.$emit('close');
        },
      );
    },
    createPin() {
      if (this.createInFlight) {
        return;
      }
      const self = this;
      let promise;
      if (isURLBlank(this.pinModel.form.url.value) && this.selectedFile === null) {
        return;
      }
      this.createInFlight = true;
      this.createError = null;
      this.activeLoading = Loading.open(this);
      if (this.selectedFile === null) {
        const data = this.pinModel.asDataByFields(fields);
        promise = API.Pin.createFromURL(data);
      } else {
        const formData = new FormData();
        const referer = this.pinModel.form.referer.value;
        const description = this.pinModel.form.description.value;
        formData.append('image_file', this.selectedFile);
        formData.append('referer', textOrBlank(referer));
        formData.append('description', textOrBlank(description));
        formData.append('private', String(this.pinModel.form.private.value));
        this.pinModel.form.tags.value.forEach(
          tag => formData.append('tags', tag),
        );
        if (this.boardIds) {
          this.boardIds.forEach(
            boardId => formData.append('board_ids', boardId),
          );
        }
        promise = API.Pin.createFromUpload(formData);
      }
      promise.then(
        (resp) => {
          if (!self.componentAlive) {
            return;
          }
          const promises = [];
          function done() {
            if (!self.componentAlive) {
              return;
            }
            self.createInFlight = false;
            self.$emit('pinCreated', resp);
            self.$emit('close');
            self.closeLoading();
          }
          bus.bus.emit(bus.events.refreshPin);
          if (self.selectedFile === null && self.boardIds) {
            // FIXME(winkidney): Should handle error for add-to board
            self.boardIds.forEach(
              (boardId) => {
                promises.push(API.Board.addToBoard(boardId, [resp.data.id]));
              },
            );
          }
          if (promises.length > 0) {
            axios.all(promises).then(done);
          } else {
            done();
          }
        },
      ).catch((error) => {
        if (!self.componentAlive) {
          return;
        }
        console.log('Cannot create pin:', error);
        self.createInFlight = false;
        self.createError = createErrorMessage(error, self.$t('pinCreateError'));
        self.closeLoading();
      });
    },
    niceLinks,
  },
};
</script>

<style scoped>
.columns > .column { min-width: 0; }
</style>
